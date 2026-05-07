"""
crypto_screener.py — Crypto screener using CoinGecko free API (no key needed).

Two-phase approach for reliable RSI + MA scoring:
  Phase 1: Bulk /coins/markets call (sparkline=False) → filter + rank → top CANDIDATE_N
  Phase 2: Individual /coins/{id}/market_chart calls → full RSI/MA scoring

Sparkline is explicitly disabled — it's a premium CoinGecko feature that causes
rate-limit (429) errors on the free tier. Phase 2 individual calls (with 1.5s delay)
are slow but reliable within free-tier limits (~10-30 req/min).
"""

import time
import statistics
import requests

COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
MAX_COINS       = 50     # Reduced from 100 — smaller bulk call = less rate-limit risk
CANDIDATE_N     = 10     # Fetch individual price history for this many candidates
TOP_N           = 5      # Final picks returned per category
HISTORY_DELAY   = 1.5    # Seconds between individual market_chart calls (was 0.5 — too fast)

# CoinGecko free tier: ~10-30 calls/min. Use a real User-Agent to avoid bot detection.
_HEADERS = {
    "User-Agent": "StockPulz/1.0 (personal trading assistant; contact vasanth.sanil@gmail.com)",
    "Accept": "application/json",
}

# Retry config for rate-limited bulk calls
_BULK_RETRY_DELAYS = [5, 15, 30, 60]  # seconds — 4 retries after first attempt

# Min market cap filter — exclude micro-caps
MIN_MARKET_CAP = 200_000_000   # $200M

# Stablecoins, wrapped tokens, liquid staking tokens to exclude
EXCLUDE_IDS = {
    "tether", "usd-coin", "binance-usd", "dai", "frax", "true-usd",
    "usdd", "gemini-dollar", "paxos-standard", "wrapped-bitcoin",
    "wrapped-ethereum", "staked-ether", "rocket-pool-eth",
    "coinbase-wrapped-staked-eth", "mantle-staked-ether",
    "bridged-usdc-polygon-pos-bridge", "first-digital-usd",
    "paypal-usd", "eurc", "lido-staked-ether",
}


# ── CoinGecko API calls ───────────────────────────────────────────────────────

def _get_top_coins(limit: int = MAX_COINS) -> list[dict]:
    """
    Bulk fetch top coins by market cap with retry on rate-limit (429).

    Sparkline is NOT requested — it's a premium CoinGecko feature that causes
    429s on the free tier. We always use Phase 2 (individual market_chart calls)
    for price history, which is more reliable.
    """
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency":            "usd",
        "order":                  "market_cap_desc",
        "per_page":               limit,
        "page":                   1,
        "sparkline":              False,   # disabled — free tier rate-limit trigger
        "price_change_percentage": "24h,7d,30d",
    }

    last_exc = None
    for attempt, delay in enumerate([0] + _BULK_RETRY_DELAYS, start=1):
        if delay:
            print(f"[crypto_screener] Bulk call retry {attempt}/5 — waiting {delay}s...")
            time.sleep(delay)
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=25)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", delay or 30))
                print(f"[crypto_screener] Rate limited (429) — waiting {retry_after}s...")
                time.sleep(retry_after)
                last_exc = Exception(f"CoinGecko rate limited (429)")
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data:
                print(f"[crypto_screener] Bulk call returned empty list (attempt {attempt}/5).")
                last_exc = Exception("Empty response from CoinGecko bulk call")
                continue
            print(f"[crypto_screener] Bulk call OK — {len(data)} coins returned.")
            return data
        except Exception as exc:
            print(f"[crypto_screener] Bulk call attempt {attempt}/5 failed: {exc}")
            last_exc = exc

    raise last_exc or Exception("CoinGecko bulk call failed after all retries")


def _get_price_history(coin_id: str, days: int = 7) -> list[float]:
    """
    Fetch hourly price history for a single coin via /coins/{id}/market_chart.
    Returns a flat list of prices, or [] on failure.
    Retries once on 429 after the Retry-After header (or 30s default).
    """
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    for attempt in range(2):
        try:
            resp = requests.get(
                url,
                params={"vs_currency": "usd", "days": days, "interval": "hourly"},
                headers=_HEADERS,
                timeout=20,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 30))
                print(f"[crypto_screener] Rate limited on {coin_id} — waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            raw = resp.json().get("prices", [])
            return [p[1] for p in raw]   # [[timestamp, price], ...] → [price, ...]
        except Exception as exc:
            print(f"[crypto_screener] Price history fetch failed for {coin_id}: {exc}")
            return []
    return []


# ── Technical indicators ──────────────────────────────────────────────────────

def _simple_rsi(prices: list[float], period: int = 14) -> float | None:
    if not prices or len(prices) < period + 1:
        return None
    deltas   = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains    = [d for d in deltas[-period:] if d > 0]
    losses   = [-d for d in deltas[-period:] if d < 0]
    avg_gain = statistics.mean(gains)  if gains  else 0
    avg_loss = statistics.mean(losses) if losses else 1e-9
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _simple_ma(prices: list[float], period: int) -> float | None:
    if not prices or len(prices) < max(period, 1):
        return None
    return round(statistics.mean(prices[-period:]), 8)


# ── Scoring ───────────────────────────────────────────────────────────────────

def _short_term_score(coin: dict, prices: list[float]) -> tuple[int, dict]:
    score = 0
    metrics = {}
    current_price = coin.get("current_price", 0)

    # RSI — needs price history (25 pts)
    rsi = _simple_rsi(prices[-30:], period=14) if prices else None
    metrics["rsi"] = rsi
    if rsi and 40 <= rsi <= 65:
        score += 25

    # 24h price change > 2% (20 pts)
    chg_24h = coin.get("price_change_percentage_24h_in_currency", 0) or 0
    metrics["price_change_24h_pct"] = round(chg_24h, 2)
    if chg_24h > 2:
        score += 20

    # Price above 48h MA — needs price history (20 pts)
    ma48 = _simple_ma(prices, 48) if prices else None
    metrics["ma48"] = ma48
    if ma48 and current_price > ma48:
        score += 20

    # 7d change positive but < 30% (20 pts)
    chg_7d = coin.get("price_change_percentage_7d_in_currency", 0) or 0
    metrics["price_change_7d_pct"] = round(chg_7d, 2)
    if 0 < chg_7d < 30:
        score += 20

    # 24h volume above $50M (15 pts)
    vol_24h = coin.get("total_volume", 0) or 0
    metrics["volume_24h_usd"] = vol_24h
    if vol_24h > 50_000_000:
        score += 15

    return score, metrics


def _long_term_score(coin: dict, prices: list[float]) -> tuple[int, dict]:
    score = 0
    metrics = {}
    current_price = coin.get("current_price", 0)
    market_cap    = coin.get("market_cap", 0)
    ath           = coin.get("ath", 0)

    # Market cap > $10B (30 pts)
    metrics["market_cap_usd"] = market_cap
    if market_cap and market_cap > 10_000_000_000:
        score += 30

    # Price above 7-day MA — needs price history (25 pts)
    ma_7d = _simple_ma(prices, len(prices)) if prices else None
    metrics["ma7d"] = ma_7d
    if ma_7d and current_price > ma_7d:
        score += 25

    # 30d price change positive (20 pts)
    chg_30d = coin.get("price_change_percentage_30d_in_currency", 0) or 0
    metrics["price_change_30d_pct"] = round(chg_30d, 2)
    if chg_30d > 0:
        score += 20

    # Within 60% of ATH (15 pts)
    metrics["ath_usd"] = ath
    if ath and current_price > 0:
        pct_from_ath = ((ath - current_price) / ath) * 100
        metrics["pct_below_ath"] = round(pct_from_ath, 1)
        if pct_from_ath < 60:
            score += 15

    # Room to grow — ATH > 30% above current (10 pts)
    if ath and current_price > 0 and ath > current_price * 1.3:
        score += 10

    return score, metrics


# ── Main screener ─────────────────────────────────────────────────────────────

def run_crypto_screener() -> dict:
    """
    Two-phase crypto screening:
      Phase 1 — Bulk call: filter + basic score → pick top CANDIDATE_N
      Phase 2 — Individual price history: full RSI/MA scoring for candidates
                 (skipped if sparkline already present in bulk response)
    """
    print(f"[crypto_screener] Fetching top {MAX_COINS} coins from CoinGecko...")
    try:
        coins = _get_top_coins(MAX_COINS)
    except Exception as exc:
        print(f"[crypto_screener] ERROR fetching coin list: {exc}")
        return {"short_term": [], "long_term": []}

    # Filter stablecoins, wrapped tokens, micro-caps
    coins = [
        c for c in coins
        if c.get("id") not in EXCLUDE_IDS
        and (c.get("market_cap") or 0) >= MIN_MARKET_CAP
    ]

    print(f"[crypto_screener] {len(coins)} coins after exclusions.")

    # Phase 1: basic score on all coins using available market data (no price history yet)
    basic_scores = []
    for coin in coins:
        st_score, _ = _short_term_score(coin, [])
        lt_score, _ = _long_term_score(coin, [])
        basic_scores.append((coin, st_score, lt_score))

    # Pick top CANDIDATE_N by combined score for individual price history fetch
    combined   = sorted(basic_scores, key=lambda x: x[1] + x[2], reverse=True)
    candidates = [c[0] for c in combined[:CANDIDATE_N]]

    # Phase 2: fetch individual price history for each candidate
    # (Sparkline is disabled in bulk call — free tier rate limits; individual calls are reliable)
    print(f"[crypto_screener] Fetching price history for {len(candidates)} candidates...")
    for coin in candidates:
        prices = _get_price_history(coin["id"], days=7)
        coin["_fetched_prices"] = prices
        if not prices:
            print(f"[crypto_screener] No price history for {coin['id']} — will score without it.")
        time.sleep(HISTORY_DELAY)

    # Final scoring with full price data
    short_results = []
    long_results  = []

    for coin in candidates:
        prices = coin.get("_fetched_prices") or []

        symbol        = coin.get("symbol", "").upper()
        name          = coin.get("name", coin["id"])
        current_price = coin.get("current_price", 0)

        st_score, st_metrics = _short_term_score(coin, prices)
        short_results.append({
            "id": coin["id"], "symbol": symbol, "name": name,
            "current_price": current_price,
            "market_cap": coin.get("market_cap"),
            "score": st_score, **st_metrics,
        })

        lt_score, lt_metrics = _long_term_score(coin, prices)
        long_results.append({
            "id": coin["id"], "symbol": symbol, "name": name,
            "current_price": current_price,
            "market_cap": coin.get("market_cap"),
            "score": lt_score, **lt_metrics,
        })

    short_top = sorted(short_results, key=lambda x: x["score"], reverse=True)[:TOP_N]
    long_top  = sorted(long_results,  key=lambda x: x["score"], reverse=True)[:TOP_N]

    print(f"[crypto_screener] Top short-term: {[c['symbol'] for c in short_top]}")
    print(f"[crypto_screener] Top long-term:  {[c['symbol'] for c in long_top]}")

    return {"short_term": short_top, "long_term": long_top}


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    results = run_crypto_screener()
    print(json.dumps(results, indent=2, default=str))
