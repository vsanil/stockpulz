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

import os
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

_CRYPTO_SYMBOLS = {
    "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","MATIC",
    "LINK","UNI","ATOM","LTC","BCH","ALGO","XLM","VET","ICP","FIL",
    "TRX","NEAR","OP","ARB","SUI","APT","INJ","SEI","TIA","HYPE",
}

_TIMEOUT = 6  # seconds for all external HTTP calls


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
def get_live_price(ticker: str) -> float | None:
    """Fetch the latest trade price for one ticker (stock or crypto).

    Tries Alpaca first; falls back to yfinance.
    """
    ticker    = ticker.upper()
    is_crypto = ticker in _CRYPTO_SYMBOLS

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
                        return round(float(t["p"]), 6)
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
                        return round(float(t["p"]), 4)
        except Exception:
            pass

    return _yf_price(ticker)


def get_live_prices(tickers: list) -> dict:
    """Fetch live prices for multiple tickers in parallel.

    Returns {ticker: price} dict — missing tickers are omitted.
    Tries Alpaca batch first; falls back per-ticker via yfinance for any misses.
    """
    if not tickers:
        return {}

    tickers   = [t.upper() for t in tickers]
    stocks    = [t for t in tickers if t not in _CRYPTO_SYMBOLS]
    cryptos   = [t for t in tickers if t in _CRYPTO_SYMBOLS]
    prices: dict = {}

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
                        prices[tk] = round(float(p), 4)
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
                        prices[tk] = round(float(p), 6)
        except Exception:
            pass

    # ── yfinance fallback for any misses ──
    missing = [t for t in tickers if t not in prices]
    if missing:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(missing), 8)) as ex:
            results = ex.map(_yf_price, missing)
        for tk, p in zip(missing, results):
            if p is not None:
                prices[tk] = p

    return prices


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
