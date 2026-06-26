"""
market_data.py — Reliable price & chart data for the StockPulz miniapp.

Priority order:
  Prices  : Alpaca IEX (real-time, free tier)  →  yfinance fallback
  OHLCV   : Polygon.io daily bars              →  yfinance fallback

Required env vars:
  ALPACA_API_KEY       — Alpaca key ID
  ALPACA_SECRET_KEY    — Alpaca secret key
  POLYGON_API_KEY      — Polygon.io API key
"""
from __future__ import annotations

import os
import time
import requests
from datetime import date, timedelta, datetime, timezone

# ── Credentials ──────────────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get("ALPACA_KEY_ID", "")      # matches Render env var name
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
POLYGON_KEY   = os.environ.get("POLYGON_API_KEY", "")

_ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

# Canonical crypto set — single source of truth (was a local hardcoded list).
from price_checker import CRYPTO_SYMBOLS as _CRYPTO_SYMBOLS

_TIMEOUT = 6  # seconds for all external HTTP calls

# ── Process-level price dedup cache ──────────────────────────────────────────
# 15-second TTL only — prevents duplicate in-flight requests hitting Alpaca/yfinance
# at the same moment (e.g. two rapid tab switches). NOT a staleness compromise.
_PRICE_CACHE: dict = {}   # ticker → {"price": float | None, "ts": float}
_PRICE_CACHE_TTL = 15     # seconds


# ── Helpers ───────────────────────────────────────────────────────────────────
def _ms_to_date(ms: int) -> str:
    """Convert Polygon millisecond timestamp → YYYY-MM-DD."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _yf_price(ticker: str) -> float | None:
    """Last-resort price via yfinance."""
    try:
        import yfinance as _yf
        is_crypto = ticker.upper() in _CRYPTO_SYMBOLS
        yf_sym    = f"{ticker}-USD" if is_crypto else ticker
        fi        = _yf.Ticker(yf_sym).fast_info
        p         = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
        return float(p) if p else None
    except Exception:
        return None


def _yf_ohlcv(ticker: str, days: int = 92) -> list | None:
    """Last-resort OHLCV bars via yfinance."""
    try:
        import yfinance as _yf
        is_crypto = ticker.upper() in _CRYPTO_SYMBOLS
        yf_sym    = f"{ticker}-USD" if is_crypto else ticker
        hist      = _yf.Ticker(yf_sym).history(period="3mo", interval="1d")
        if hist.empty:
            return None
        return [
            {
                "time":   ts.strftime("%Y-%m-%d"),
                "open":   round(float(row["Open"]),  4),
                "high":   round(float(row["High"]),  4),
                "low":    round(float(row["Low"]),   4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            }
            for ts, row in hist.iterrows()
        ]
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────
def validate_ticker(ticker: str) -> bool:
    """Return True if ticker resolves to a real tradeable asset (stock or crypto).
    Uses _yf_price as the check — fast_info lookup, no .info call."""
    return _yf_price(ticker.upper()) is not None


def get_live_price(ticker: str) -> float | None:
    """Fetch the latest trade price for one ticker (stock or crypto).

    Tries Alpaca first; falls back to yfinance.
    Results are dedup-cached for 15 s to avoid hammering the API on rapid tab switches.
    """
    ticker = ticker.upper()
    now    = time.time()
    hit    = _PRICE_CACHE.get(ticker)
    if hit and now - hit["ts"] < _PRICE_CACHE_TTL:
        return hit["price"]

    is_crypto = ticker in _CRYPTO_SYMBOLS
    price: float | None = None

    if ALPACA_KEY:
        try:
            if is_crypto:
                r = requests.get(
                    "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades",
                    params={"symbols": f"{ticker}/USD"},
                    headers=_ALPACA_HEADERS,
                    timeout=_TIMEOUT,
                )
                if r.ok:
                    trades = r.json().get("trades", {})
                    t = trades.get(f"{ticker}/USD", {})
                    if t.get("p"):
                        price = round(float(t["p"]), 6)
            else:
                r = requests.get(
                    "https://data.alpaca.markets/v2/stocks/trades/latest",
                    params={"symbols": ticker, "feed": "iex"},
                    headers=_ALPACA_HEADERS,
                    timeout=_TIMEOUT,
                )
                if r.ok:
                    trades = r.json().get("trades", {})
                    t = trades.get(ticker, {})
                    if t.get("p"):
                        price = round(float(t["p"]), 4)
        except Exception:
            pass

    if price is None:
        price = _yf_price(ticker)

    _PRICE_CACHE[ticker] = {"price": price, "ts": now}
    return price


def get_live_prices(tickers: list) -> dict:
    """Fetch live prices for multiple tickers in parallel.

    Returns {ticker: price} dict — missing tickers are omitted.
    Tries Alpaca batch first; falls back per-ticker via yfinance for any misses.
    Results are dedup-cached for 15 s; already-cached tickers skip the network call.
    """
    if not tickers:
        return {}

    tickers   = [t.upper() for t in tickers]
    now       = time.time()

    # Serve from dedup cache; only fetch tickers whose cache has expired
    prices: dict = {}
    to_fetch: list = []
    for t in tickers:
        hit = _PRICE_CACHE.get(t)
        if hit and now - hit["ts"] < _PRICE_CACHE_TTL and hit["price"] is not None:
            prices[t] = hit["price"]
        else:
            to_fetch.append(t)

    if not to_fetch:
        return prices

    stocks    = [t for t in to_fetch if t not in _CRYPTO_SYMBOLS]
    cryptos   = [t for t in to_fetch if t in _CRYPTO_SYMBOLS]
    new_prices: dict = {}

    # ── Alpaca batch: stocks ──
    if stocks and ALPACA_KEY:
        try:
            r = requests.get(
                "https://data.alpaca.markets/v2/stocks/snapshots",
                params={"symbols": ",".join(stocks), "feed": "iex"},
                headers=_ALPACA_HEADERS,
                timeout=_TIMEOUT,
            )
            if r.ok:
                for tk, snap in r.json().items():
                    p = (snap.get("latestTrade") or {}).get("p")
                    if p:
                        new_prices[tk] = round(float(p), 4)
        except Exception:
            pass

    # ── Alpaca batch: crypto ──
    if cryptos and ALPACA_KEY:
        try:
            symbols = ",".join(f"{t}/USD" for t in cryptos)
            r = requests.get(
                "https://data.alpaca.markets/v1beta3/crypto/us/snapshots",
                params={"symbols": symbols},
                headers=_ALPACA_HEADERS,
                timeout=_TIMEOUT,
            )
            if r.ok:
                for sym, snap in r.json().get("snapshots", {}).items():
                    tk = sym.split("/")[0]
                    p  = (snap.get("latestTrade") or {}).get("p")
                    if p:
                        new_prices[tk] = round(float(p), 6)
        except Exception:
            pass

    # ── yfinance fallback for any misses ──
    missing = [t for t in to_fetch if t not in new_prices]
    if missing:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(missing), 8)) as ex:
            results = ex.map(_yf_price, missing)
        for tk, p in zip(missing, results):
            if p is not None:
                new_prices[tk] = p

    # Populate dedup cache for everything we just fetched
    for tk in to_fetch:
        _PRICE_CACHE[tk] = {"price": new_prices.get(tk), "ts": now}

    prices.update(new_prices)
    return prices


# ── Company name lookup ───────────────────────────────────────────────────────
_NAME_CACHE: dict[str, str] = {}   # process-level cache; survives across requests

_CRYPTO_NAMES: dict[str, str] = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "Binance Coin",
    "XRP": "XRP", "ADA": "Cardano", "DOGE": "Dogecoin", "AVAX": "Avalanche",
    "DOT": "Polkadot", "MATIC": "Polygon", "LINK": "Chainlink", "UNI": "Uniswap",
    "ATOM": "Cosmos", "LTC": "Litecoin", "BCH": "Bitcoin Cash", "ALGO": "Algorand",
    "XLM": "Stellar", "TRX": "TRON", "NEAR": "NEAR Protocol", "OP": "Optimism",
    "ARB": "Arbitrum", "SUI": "Sui", "APT": "Aptos", "INJ": "Injective",
    "FIL": "Filecoin", "VET": "VeChain", "ICP": "Internet Computer", "HYPE": "Hyperliquid",
}

def _short_name(name: str, max_len: int = 22) -> str:
    """Trim long company names at a word boundary."""
    if not name:
        return ""
    for suffix in (", Inc.", " Inc.", " Corp.", " Corporation", " & Co.", " Co.", " Ltd.", " plc", " LLC"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if len(name) <= max_len else name[:max_len].rsplit(" ", 1)[0] + "…"


def _fetch_one_name(ticker: str) -> tuple[str, str]:
    """yfinance shortName lookup for a single ticker. Returns (ticker, name)."""
    try:
        import yfinance as _yf
        is_crypto = ticker in _CRYPTO_SYMBOLS
        yf_sym    = f"{ticker}-USD" if is_crypto else ticker
        info      = _yf.Ticker(yf_sym).info
        name      = info.get("shortName") or info.get("longName") or ""
        return ticker, _short_name(name)
    except Exception:
        return ticker, ""


def get_company_names(tickers: list[str]) -> dict[str, str]:
    """Return {ticker: short_company_name} for all tickers, using a process-level cache.

    Results are cached indefinitely within the process lifetime (company names
    change rarely). Missing or erroring tickers map to "".
    """
    if not tickers:
        return {}

    result: dict[str, str] = {}
    to_fetch: list[str]    = []

    for t in tickers:
        t = t.upper()
        if t in _NAME_CACHE:
            result[t] = _NAME_CACHE[t]
        elif t in _CRYPTO_NAMES:
            result[t] = _NAME_CACHE.setdefault(t, _CRYPTO_NAMES[t])
        else:
            to_fetch.append(t)

    if to_fetch:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(to_fetch), 6)) as ex:
            pairs = list(ex.map(_fetch_one_name, to_fetch))
        for tk, name in pairs:
            _NAME_CACHE[tk] = name
            result[tk]      = name

    return result


def get_ohlcv(ticker: str, days: int = 92) -> list | None:
    """Fetch daily OHLCV bars for the past `days` days.

    Returns list of {time, open, high, low, close, volume} sorted ascending,
    or None if all sources fail.

    Tries Polygon.io first; falls back to yfinance.
    """
    ticker    = ticker.upper()
    is_crypto = ticker in _CRYPTO_SYMBOLS
    end_date  = date.today().isoformat()
    start_date = (date.today() - timedelta(days=days)).isoformat()

    if POLYGON_KEY:
        try:
            poly_sym = f"X:{ticker}USD" if is_crypto else ticker
            r = requests.get(
                f"https://api.polygon.io/v2/aggs/ticker/{poly_sym}/range/1/day/{start_date}/{end_date}",
                params={
                    "adjusted": "true",
                    "sort":     "asc",
                    "limit":    300,
                    "apiKey":   POLYGON_KEY,
                },
                timeout=_TIMEOUT,
            )
            if r.ok:
                results = r.json().get("results", [])
                if results:
                    return [
                        {
                            "time":   _ms_to_date(bar["t"]),
                            "open":   round(bar["o"], 4),
                            "high":   round(bar["h"], 4),
                            "low":    round(bar["l"], 4),
                            "close":  round(bar["c"], 4),
                            "volume": int(bar["v"]),
                        }
                        for bar in results
                    ]
        except Exception:
            pass

    return _yf_ohlcv(ticker, days)
