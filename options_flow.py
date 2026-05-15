"""
options_flow.py — Detect unusual options activity.

Primary: Polygon.io free tier (15-min delayed, reliable exchange data).
Fallback: yfinance options data (scrape-based, limited but no key needed).

Set POLYGON_API_KEY env var to enable Polygon.
Without it, automatically falls back to yfinance — nothing breaks.

Polygon improvements over yfinance:
  - Official API with uptime SLA (yfinance scrapes Yahoo, can break anytime)
  - All expiry dates, not just nearest
  - Accurate volume and OI from exchange feeds
  - Consistent coverage across all optionable tickers
  - Sweep detection via single-contract volume spike analysis

Signals returned:
  - unusual:         bool  — abnormally high volume vs open interest
  - bullish_flow:    bool  — put/call ratio < 0.7 (calls dominating)
  - bearish_flow:    bool  — put/call ratio > 1.5 (puts dominating)
  - sweep_detected:  bool  — large single-contract block
  - signal_score:    int   — -5 to +5 composite score
"""

import os
from datetime import date, timedelta
import requests
import yfinance as yf

_POLYGON_BASE = "https://api.polygon.io"


def _polygon_key() -> str | None:
    return os.environ.get("POLYGON_API_KEY") or None


# ── Polygon options fetch ─────────────────────────────────────────────────────

def _get_polygon_options(ticker: str) -> dict | None:
    """
    Fetch options snapshot from Polygon for the nearest 2 expiry windows.
    Uses the /v3/snapshot/options/{ticker} endpoint — returns all contracts
    with current volume and OI in one paginated call.
    """
    key = _polygon_key()
    if not key:
        return None

    # Limit to contracts expiring within the next 45 days for relevance
    today      = date.today()
    exp_min    = today.isoformat()
    exp_max    = (today + timedelta(days=45)).isoformat()

    total_call_vol = 0
    total_put_vol  = 0
    total_call_oi  = 0
    total_put_oi   = 0
    max_single_vol = 0

    url    = f"{_POLYGON_BASE}/v3/snapshot/options/{ticker.upper()}"
    params = {
        "expiration_date.gte": exp_min,
        "expiration_date.lte": exp_max,
        "limit":               250,
        "apiKey":              key,
    }

    pages_fetched = 0
    while url and pages_fetched < 3:   # cap at 3 pages (750 contracts) — enough signal
        try:
            resp = requests.get(url, params=params, timeout=8)
            if resp.status_code == 403:
                # Free tier doesn't include options snapshot — fall through to yfinance
                return None
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"[options_flow] Polygon fetch failed for {ticker}: {exc}")
            return None

        results = data.get("results", [])
        for contract in results:
            day     = contract.get("day", {})
            details = contract.get("details", {})
            vol     = int(day.get("volume") or 0)
            oi      = int(contract.get("open_interest") or 0)
            ctype   = (details.get("contract_type") or "").lower()

            if ctype == "call":
                total_call_vol += vol
                total_call_oi  += oi
            elif ctype == "put":
                total_put_vol += vol
                total_put_oi  += oi

            if vol > max_single_vol:
                max_single_vol = vol

        # Pagination — Polygon returns next_url when there are more pages
        next_url = data.get("next_url")
        if next_url:
            url    = next_url
            params = {"apiKey": key}   # apiKey only needed on continuation
        else:
            break
        pages_fetched += 1

    if total_call_vol + total_put_vol == 0:
        return None

    return {
        "call_volume":    total_call_vol,
        "put_volume":     total_put_vol,
        "call_oi":        total_call_oi,
        "put_oi":         total_put_oi,
        "max_single_vol": max_single_vol,
        "source":         "polygon",
    }


# ── yfinance fallback ─────────────────────────────────────────────────────────

def _get_yfinance_options(ticker: str) -> dict | None:
    """Fetch options data via yfinance (fallback when Polygon key not set)."""
    try:
        tk   = yf.Ticker(ticker)
        exps = tk.options
        if not exps:
            return None
        chain = tk.option_chain(exps[0])
        calls = chain.calls
        puts  = chain.puts
        if calls.empty and puts.empty:
            return None
        return {
            "call_volume":    int(calls["volume"].fillna(0).sum()),
            "put_volume":     int(puts["volume"].fillna(0).sum()),
            "call_oi":        int(calls["openInterest"].fillna(0).sum()),
            "put_oi":         int(puts["openInterest"].fillna(0).sum()),
            "max_single_vol": 0,
            "source":         "yfinance",
        }
    except Exception:
        return None


# ── Signal computation ────────────────────────────────────────────────────────

def _compute_signal(raw: dict) -> dict:
    """Compute options flow signals from aggregated volume/OI data."""
    call_vol = raw["call_volume"]
    put_vol  = raw["put_volume"]
    call_oi  = raw["call_oi"]
    put_oi   = raw["put_oi"]
    max_vol  = raw.get("max_single_vol", 0)
    source   = raw.get("source", "unknown")

    total_vol = call_vol + put_vol
    total_oi  = call_oi  + put_oi

    put_call_ratio = round(put_vol / call_vol, 2) if call_vol > 0 else None
    vol_oi_ratio   = round(total_vol / total_oi, 2) if total_oi > 0 else None

    # Unusual: total volume > 2x open interest (someone is betting big)
    unusual = bool(vol_oi_ratio and vol_oi_ratio > 2.0)

    # Flow direction
    bullish_flow = put_call_ratio is not None and put_call_ratio < 0.7
    bearish_flow = put_call_ratio is not None and put_call_ratio > 1.5

    # Sweep proxy: single contract > 20% of total volume and > 500 contracts
    sweep_detected = (
        total_vol > 0
        and max_vol > total_vol * 0.20
        and max_vol > 500
    )

    score = 0
    notes = []
    if unusual:
        score += 2
        notes.append(f"unusual vol/OI={vol_oi_ratio}")
    if bullish_flow:
        score += 3
        notes.append(f"bullish P/C={put_call_ratio}")
    if bearish_flow:
        score -= 3
        notes.append(f"bearish P/C={put_call_ratio}")
    if sweep_detected:
        score += 2
        notes.append("block sweep detected")
    if source == "polygon":
        notes.append("(polygon)")

    note = ", ".join(notes) if notes else f"P/C={put_call_ratio}, vol/OI={vol_oi_ratio}"

    return {
        "unusual":        unusual,
        "put_call_ratio": put_call_ratio,
        "vol_oi_ratio":   vol_oi_ratio,
        "call_volume":    call_vol,
        "put_volume":     put_vol,
        "bullish_flow":   bool(bullish_flow),
        "bearish_flow":   bool(bearish_flow),
        "sweep_detected": sweep_detected,
        "signal_score":   max(-5, min(5, score)),
        "note":           note,
        "source":         source,
    }


# ── Public API ────────────────────────────────────────────────────────────────

_BASE_RESULT = {
    "unusual": False, "put_call_ratio": None, "vol_oi_ratio": None,
    "call_volume": 0, "put_volume": 0,
    "bullish_flow": False, "bearish_flow": False,
    "sweep_detected": False, "signal_score": 0,
    "note": "no options data", "source": "none",
}


def get_options_signal(ticker: str) -> dict:
    """
    Return options flow signal for a ticker.
    Tries Polygon first (if POLYGON_API_KEY set), falls back to yfinance.
    """
    raw = _get_polygon_options(ticker) or _get_yfinance_options(ticker)
    if not raw:
        return dict(_BASE_RESULT)
    try:
        return _compute_signal(raw)
    except Exception as exc:
        return {**_BASE_RESULT, "note": f"signal compute error: {exc}"}


def batch_options_signals(tickers: list[str]) -> dict[str, dict]:
    """Fetch options signals for multiple tickers. Returns {ticker: signal_dict}."""
    return {t: get_options_signal(t) for t in tickers}


if __name__ == "__main__":
    import pprint
    key = _polygon_key()
    print(f"Polygon key: {'set ✓' if key else 'NOT SET — using yfinance fallback'}")
    for ticker in ["NVDA", "AAPL", "SPY"]:
        print(f"\n=== {ticker} ===")
        pprint.pprint(get_options_signal(ticker))
