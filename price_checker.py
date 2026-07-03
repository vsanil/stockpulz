"""
price_checker.py — Fetch current prices for the 10:30 AM confirmation run.
Uses yfinance for stocks and CoinGecko for crypto (single bulk call each),
with a yfinance fallback for crypto in case CoinGecko fails or ids are missing.
"""
from __future__ import annotations

import concurrent.futures
import time
import threading
import requests
import yfinance as yf

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"

# ── Shared CoinGecko price fetch: 60s cache + 429 backoff ─────────────────────
# CoinGecko's free tier is rate-limited (~30 calls/min, shared across ALL our
# call sites: alerts, positions, watchlist, paper, screener). Uncoordinated
# per-coin calls were 429-ing. Route crypto price fetches through this so each
# coin is fetched at most once per _CG_TTL, and a 429 backs off instead of failing.
_CG_CACHE: dict[str, tuple[float, float]] = {}   # cg_id -> (usd_price, ts)
_CG_TTL = 60                                       # seconds
_CG_LOCK = threading.Lock()


def cg_prices(cg_ids: list[str]) -> dict[str, float]:
    """
    Batched CoinGecko /simple/price for coin ids, with a 60s cache and one 429
    backoff. Returns {cg_id: usd_price} for whatever resolved. Safe/shared so the
    app never hammers CoinGecko's free-tier limit from many places at once.
    """
    now = time.time()
    out: dict[str, float] = {}
    missing: list[str] = []
    with _CG_LOCK:
        for cid in cg_ids:
            hit = _CG_CACHE.get(cid)
            if hit and (now - hit[1]) < _CG_TTL:
                out[cid] = hit[0]
            else:
                missing.append(cid)
    if not missing:
        return out
    ids = ",".join(sorted(set(missing)))
    for attempt in (1, 2):
        try:
            resp = requests.get(COINGECKO_SIMPLE,
                                params={"ids": ids, "vs_currencies": "usd"}, timeout=10)
            if resp.status_code == 429 and attempt == 1:
                try:
                    wait = min(int(resp.headers.get("Retry-After", "2") or 2), 10)
                except (ValueError, TypeError):
                    wait = 2
                print(f"[price_checker] CoinGecko 429 — backing off {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            stamp = time.time()
            with _CG_LOCK:
                for cid in set(missing):
                    p = data.get(cid, {}).get("usd")
                    if p and p > 0:
                        _CG_CACHE[cid] = (float(p), stamp)
                        out[cid] = float(p)
            break
        except Exception as exc:
            print(f"[price_checker] CoinGecko batch failed ({exc}).")
            break
    return out

# Fallback map: crypto symbol → CoinGecko coin id
# Used when the picks dict doesn't carry an `id` field (e.g. Claude omitted it)
_SYMBOL_TO_CG_ID = {
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "SOL":   "solana",
    "BNB":   "binancecoin",
    "XRP":   "ripple",
    "ADA":   "cardano",
    "DOGE":  "dogecoin",
    "AVAX":  "avalanche-2",
    "DOT":   "polkadot",
    "MATIC": "matic-network",
    "LINK":  "chainlink",
    "UNI":   "uniswap",
    "ATOM":  "cosmos",
    "LTC":   "litecoin",
    "BCH":   "bitcoin-cash",
    "ALGO":  "algorand",
    "XLM":   "stellar",
    "VET":   "vechain",
    "ICP":   "internet-computer",
    "FIL":   "filecoin",
    "HYPE":  "hyperliquid",
    "SUI":   "sui",
    "APT":   "aptos",
    "ARB":   "arbitrum",
    "OP":    "optimism",
    "INJ":   "injective-protocol",
    "TIA":   "celestia",
    "SEI":   "sei-network",
    "TON":   "the-open-network",
    "TRX":   "tron",
    "NEAR":  "near",
    "SHIB":  "shiba-inu",
    "PEPE":  "pepe",
    "BONK":  "bonk",
    "WLD":   "worldcoin-wld",
    "FLOKI": "floki",
}

# Canonical crypto-symbol set — the single source of truth for "is this ticker a
# crypto?" and for the yfinance -USD suffix decision. Every module that used to
# keep its own hardcoded _CRYPTO_SYMBOLS list now imports this, so a coin the
# screener picks can't work in one feature and break in another.
CRYPTO_SYMBOLS = frozenset(_SYMBOL_TO_CG_ID)


def get_current_prices(picks: dict) -> dict:
    """
    Given the morning picks dict, fetch current prices for all tickers/symbols.
    Returns { "AAPL": 185.20, "MSFT": 418.50, "BTC": 66200, ... }
    """
    prices = {}

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})

    # ── Stock / ETF / Commodity prices via yfinance ───────────────────────────
    stock_tickers = set()
    for section in ("short_term", "long_term"):
        for s in stocks.get(section, []):
            if s.get("ticker"):
                stock_tickers.add(s["ticker"])
    # Also include ETFs and commodities — they use the same yfinance path
    for asset_key in ("etfs", "commodities"):
        bucket = picks.get(asset_key, {})
        for section in ("short_term", "long_term"):
            for e in bucket.get(section, []):
                if e.get("ticker"):
                    stock_tickers.add(e["ticker"])

    def _fetch_stock_price(ticker: str) -> tuple[str, float | None]:
        """Fetch current price for one stock. Returns (ticker, price|None)."""
        try:
            info  = yf.Ticker(ticker).fast_info
            price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
            if price and price > 0:   # reject 0 / nan / negative (nan is truthy)
                return ticker, round(float(price), 2)
        except Exception:
            pass
        # Fallback: 1-min history with explicit timeout
        try:
            hist = yf.Ticker(ticker).history(period="1d", interval="1m", timeout=8)
            if not hist.empty:
                return ticker, round(float(hist["Close"].iloc[-1]), 2)
        except Exception:
            pass
        print(f"[price_checker] Could not fetch price for {ticker}")
        return ticker, None

    if stock_tickers:
        # Parallel fetch — 8 threads, 20s hard cap per ticker
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_stock_price, t): t for t in stock_tickers}
            for future in concurrent.futures.as_completed(futs):
                try:
                    ticker, price = future.result(timeout=20)
                    if price is not None:
                        prices[ticker] = price
                except concurrent.futures.TimeoutError:
                    print(f"[price_checker] Timeout fetching price for {futs[future]} — skipping.")
                except Exception as exc:
                    print(f"[price_checker] Error fetching price for {futs[future]}: {exc}")

    # ── Crypto prices via CoinGecko (one bulk call) ───────────────────────────
    # Build symbol→id map from picks, filling any gaps with the fallback table
    crypto_symbols = set()
    for _tf in ("short_term", "long_term"):
        for c in crypto.get(_tf, []):
            sym = c.get("symbol", "").upper()
            if sym:
                crypto_symbols.add(sym)

    # Collect all crypto picks across both timeframes for id lookup
    _all_crypto_picks = crypto.get("short_term", []) + crypto.get("long_term", [])

    symbol_to_id: dict[str, str] = {}
    for sym in crypto_symbols:
        # Prefer id from picks (Claude may supply it), else use fallback table
        cid = ""
        for c in _all_crypto_picks:
            if c.get("symbol", "").upper() == sym and c.get("id"):
                cid = c["id"]
                break
        if not cid:
            cid = _SYMBOL_TO_CG_ID.get(sym, "")
        if cid:
            symbol_to_id[sym] = cid
        else:
            print(f"[price_checker] No CoinGecko id for {sym} — will try yfinance fallback.")

    if symbol_to_id:
        # Cached + 429-backoff batch fetch (shared across the app's call sites).
        cg_out = cg_prices(list(symbol_to_id.values()))
        for sym, cid in symbol_to_id.items():
            price = cg_out.get(cid)
            if price and price > 0:   # reject 0 / nan / negative
                prices[sym] = float(price)
                crypto_symbols.discard(sym)   # mark as resolved
        print(f"[price_checker] CoinGecko resolved: {sorted(set(symbol_to_id) - crypto_symbols)}")

    # ── yfinance fallback for any crypto still missing a price ────────────────
    still_missing = [s for s in crypto_symbols if s not in prices]
    for sym in still_missing:
        try:
            yf_ticker = f"{sym}-USD"
            data  = yf.Ticker(yf_ticker).fast_info
            price = getattr(data, "last_price", None) or getattr(data, "regular_market_price", None)
            if price and price > 0:   # reject 0 / nan / negative (nan is truthy)
                prices[sym] = round(float(price), 2)
                print(f"[price_checker] yfinance fallback: {sym} = ${price:.2f}")
            else:
                print(f"[price_checker] yfinance also returned no price for {sym}.")
        except Exception as exc:
            print(f"[price_checker] yfinance fallback failed for {sym}: {exc}")

    print(f"[price_checker] Current prices: {prices}")
    return prices
