"""
options_flow.py — Detect unusual options activity + IV rank.

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
  - iv_rank:         int   — 0-100 percentile of ATM IV vs 1-yr realised vol range
  - iv_pct:          float — estimated ATM implied volatility % (annualised)
  - iv_label:        str   — "LOW" / "NORMAL" / "ELEVATED" / "EXTREME"
"""

import os
import math
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

_MAX_EXPIRIES = 4   # how many expiry dates to aggregate across


def _get_yfinance_options(ticker: str) -> dict | None:
    """
    Fetch options data via yfinance across the next _MAX_EXPIRIES expiry dates.

    Improvements over single-expiry fetch:
      - Aggregates volume and OI across near-term monthly + weekly expiries,
        giving a far more accurate put/call ratio.
      - Tracks max single-contract volume across all expiries for sweep detection.
      - Finds the peak-OI strike (gamma pin level) and measures its distance
        from the current price — useful for short-term range prediction.
    """
    try:
        tk   = yf.Ticker(ticker)
        exps = tk.options
        if not exps:
            return None

        # Current price for gamma pin distance calculation
        try:
            current_price = tk.fast_info.get("last_price") or tk.fast_info.get("previous_close")
        except Exception:
            current_price = None

        total_call_vol = 0
        total_put_vol  = 0
        total_call_oi  = 0
        total_put_oi   = 0
        max_single_vol = 0

        # {strike: total_oi} across all expiries — for gamma pin detection
        strike_oi: dict[float, int] = {}

        for exp in exps[:_MAX_EXPIRIES]:
            try:
                chain = tk.option_chain(exp)
            except Exception:
                continue

            calls = chain.calls
            puts  = chain.puts

            if not calls.empty:
                total_call_vol += int(calls["volume"].fillna(0).sum())
                total_call_oi  += int(calls["openInterest"].fillna(0).sum())
                max_single_vol  = max(max_single_vol,
                                      int(calls["volume"].fillna(0).max()))
                # Aggregate OI by strike (calls + puts combined for gamma pin)
                for _, row in calls[["strike", "openInterest"]].iterrows():
                    s = float(row["strike"])
                    strike_oi[s] = strike_oi.get(s, 0) + int(row["openInterest"] or 0)

            if not puts.empty:
                total_put_vol  += int(puts["volume"].fillna(0).sum())
                total_put_oi   += int(puts["openInterest"].fillna(0).sum())
                max_single_vol  = max(max_single_vol,
                                      int(puts["volume"].fillna(0).max()))
                for _, row in puts[["strike", "openInterest"]].iterrows():
                    s = float(row["strike"])
                    strike_oi[s] = strike_oi.get(s, 0) + int(row["openInterest"] or 0)

        if total_call_vol + total_put_vol == 0:
            return None

        # Gamma pin: strike with highest aggregate OI
        gamma_pin_strike   = None
        gamma_pin_dist_pct = None
        if strike_oi and current_price and current_price > 0:
            gamma_pin_strike = max(strike_oi, key=strike_oi.__getitem__)
            gamma_pin_dist_pct = round(
                abs(gamma_pin_strike - current_price) / current_price * 100, 2
            )

        return {
            "call_volume":       total_call_vol,
            "put_volume":        total_put_vol,
            "call_oi":           total_call_oi,
            "put_oi":            total_put_oi,
            "max_single_vol":    max_single_vol,
            "gamma_pin_strike":  gamma_pin_strike,
            "gamma_pin_dist_pct": gamma_pin_dist_pct,
            "current_price":     current_price,
            "expiries_loaded":   min(len(exps), _MAX_EXPIRIES),
            "source":            "yfinance",
        }
    except Exception:
        return None


# ── Signal computation ────────────────────────────────────────────────────────

def _compute_signal(raw: dict) -> dict:
    """
    Compute options flow signals from aggregated volume/OI data.

    Signals:
      - unusual:          vol/OI > 2x — elevated speculative activity
      - bullish/bearish:  put/call ratio direction
      - sweep_detected:   large single-contract block relative to total volume
      - gamma_pin:        price within 2% of peak-OI strike — short-term magnet
      - expiries_loaded:  how many expiry dates were aggregated (data quality indicator)
    """
    call_vol = raw["call_volume"]
    put_vol  = raw["put_volume"]
    call_oi  = raw["call_oi"]
    put_oi   = raw["put_oi"]
    max_vol  = raw.get("max_single_vol", 0)
    source   = raw.get("source", "unknown")

    gamma_pin_strike   = raw.get("gamma_pin_strike")
    gamma_pin_dist_pct = raw.get("gamma_pin_dist_pct")
    expiries_loaded    = raw.get("expiries_loaded", 1)

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

    # Gamma pin: price within 2% of peak-OI strike — price tends to get
    # attracted to this level heading into expiry (market-maker hedging)
    gamma_pin = bool(
        gamma_pin_dist_pct is not None and gamma_pin_dist_pct <= 2.0
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
    if gamma_pin and gamma_pin_strike:
        # Gamma pin is directionally neutral — note it but don't score it
        notes.append(f"gamma pin @{gamma_pin_strike} ({gamma_pin_dist_pct}% away)")
    if expiries_loaded > 1:
        notes.append(f"{expiries_loaded} expiries")
    if source == "polygon":
        notes.append("(polygon)")

    note = ", ".join(notes) if notes else f"P/C={put_call_ratio}, vol/OI={vol_oi_ratio}"

    return {
        "unusual":            unusual,
        "put_call_ratio":     put_call_ratio,
        "vol_oi_ratio":       vol_oi_ratio,
        "call_volume":        call_vol,
        "put_volume":         put_vol,
        "bullish_flow":       bool(bullish_flow),
        "bearish_flow":       bool(bearish_flow),
        "sweep_detected":     sweep_detected,
        "gamma_pin":          gamma_pin,
        "gamma_pin_strike":   gamma_pin_strike,
        "gamma_pin_dist_pct": gamma_pin_dist_pct,
        "expiries_loaded":    expiries_loaded,
        "signal_score":       max(-5, min(5, score)),
        "note":               note,
        "source":             source,
    }


# ── IV Rank ───────────────────────────────────────────────────────────────────

def _atm_iv_from_chain(chain, current_price: float) -> float | None:
    """
    Extract the implied volatility of the nearest ATM call from a single
    option chain expiry.  Returns annualised IV as a fraction (e.g. 0.32 = 32%).
    """
    if chain is None:
        return None
    calls = chain.calls
    if calls.empty or current_price <= 0:
        return None
    try:
        calls = calls[calls["impliedVolatility"].notna() & (calls["impliedVolatility"] > 0)]
        if calls.empty:
            return None
        # Pick the call strike closest to current price
        idx = (calls["strike"] - current_price).abs().idxmin()
        return float(calls.loc[idx, "impliedVolatility"])
    except Exception:
        return None


def get_iv_rank(ticker: str) -> dict:
    """
    Compute IV rank for a ticker using yfinance.

    Method
    ------
    1. Fetch the nearest-expiry ATM call IV as a proxy for current IV.
    2. Fetch 1-year of daily closes to build a 20-day rolling realised vol
       distribution — used as the historical baseline since free-tier APIs
       don't provide historical IV.  This is a practical proxy: high realized
       vol periods correlate with high IV, so the percentile rank is directionally
       correct.
    3. iv_rank = percentile of current_iv within the 252-day realised vol
       distribution (0 = historically cheapest, 100 = most expensive).

    Returns
    -------
    dict with:
      iv_rank   int   0-100 (None if unavailable)
      iv_pct    float annualised ATM IV in % (None if unavailable)
      iv_label  str   "LOW" / "NORMAL" / "ELEVATED" / "EXTREME"
      rv_pct    float 20-day realised vol % (annualised) — for context
    """
    null = {"iv_rank": None, "iv_pct": None, "iv_label": None, "rv_pct": None}
    try:
        tk         = yf.Ticker(ticker)
        exps       = tk.options
        if not exps:
            return null

        # Current price
        try:
            current_price = (
                tk.fast_info.get("last_price") or
                tk.fast_info.get("previous_close") or 0
            )
        except Exception:
            current_price = 0

        if current_price <= 0:
            return null

        # ATM IV from nearest expiry
        try:
            chain = tk.option_chain(exps[0])
        except Exception:
            return null

        atm_iv = _atm_iv_from_chain(chain, current_price)
        if atm_iv is None or atm_iv <= 0:
            return null

        # 1-year daily history for realised vol distribution
        try:
            hist = tk.history(period="1y", interval="1d", auto_adjust=True, timeout=10)
        except Exception:
            return null

        if hist is None or len(hist) < 30:
            return null

        closes = hist["Close"].dropna()
        if len(closes) < 21:
            return null

        # Log-return daily series
        log_rets = closes.pct_change().dropna()

        # 20-day rolling realised vol (annualised)
        rv_series = log_rets.rolling(20).std().dropna() * math.sqrt(252)
        if rv_series.empty:
            return null

        rv_values   = rv_series.values
        current_rv  = float(rv_values[-1])

        # IV rank: what percentile is atm_iv within the rv distribution?
        n_below  = sum(1 for v in rv_values if v <= atm_iv)
        iv_rank  = round(n_below / len(rv_values) * 100)

        iv_pct   = round(atm_iv * 100, 1)
        rv_pct   = round(current_rv * 100, 1)

        if iv_rank <= 25:
            iv_label = "LOW"
        elif iv_rank <= 60:
            iv_label = "NORMAL"
        elif iv_rank <= 80:
            iv_label = "ELEVATED"
        else:
            iv_label = "EXTREME"

        return {
            "iv_rank":  iv_rank,
            "iv_pct":   iv_pct,
            "iv_label": iv_label,
            "rv_pct":   rv_pct,
        }

    except Exception:
        return null


# ── Public API ────────────────────────────────────────────────────────────────

_BASE_RESULT = {
    "unusual": False, "put_call_ratio": None, "vol_oi_ratio": None,
    "call_volume": 0, "put_volume": 0,
    "bullish_flow": False, "bearish_flow": False,
    "sweep_detected": False,
    "gamma_pin": False, "gamma_pin_strike": None, "gamma_pin_dist_pct": None,
    "expiries_loaded": 0,
    "signal_score": 0,
    "iv_rank": None, "iv_pct": None, "iv_label": None, "rv_pct": None,
    "note": "no options data", "source": "none",
}


def get_options_signal(ticker: str) -> dict:
    """
    Return options flow signal + IV rank for a ticker.
    Tries Polygon first (if POLYGON_API_KEY set), falls back to yfinance.
    IV rank is computed via yfinance regardless of which flow source is used.
    """
    raw = _get_polygon_options(ticker) or _get_yfinance_options(ticker)
    if not raw:
        return dict(_BASE_RESULT)
    try:
        result   = _compute_signal(raw)
        iv_data  = get_iv_rank(ticker)
        result.update(iv_data)
        return result
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
