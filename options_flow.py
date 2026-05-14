"""
options_flow.py — Detect unusual options activity.

Primary: Tradier developer API (free tier, real-time options chains).
Fallback: yfinance options data (delayed, limited but free).

Set TRADIER_API_KEY env var to enable Tradier.
Without it, automatically falls back to yfinance (same behaviour as before).

Tradier improvements over yfinance:
  - Real-time options chain data (not delayed)
  - More reliable volume and OI figures
  - Covers more tickers consistently
  - Sweep and block trade detection via volume spike analysis

Signals returned:
  - unusual:         bool  — abnormally high volume vs open interest
  - bullish_flow:    bool  — put/call ratio < 0.7 (calls dominating)
  - bearish_flow:    bool  — put/call ratio > 1.5 (puts dominating)
  - sweep_detected:  bool  — large single-contract block (Tradier only)
  - signal_score:    int   — -5 to +5 composite score
"""

import os
import requests
import yfinance as yf

_TRADIER_BASE = "https://api.tradier.com/v1"
_TRADIER_HEADERS = {
    "Accept": "application/json",
}


def _tradier_key() -> str | None:
    return os.environ.get("TRADIER_API_KEY") or None


# ── Tradier options fetch ─────────────────────────────────────────────────────

def _get_tradier_options(ticker: str) -> dict | None:
    """
    Fetch options chain from Tradier for the nearest 1-2 expirations.
    Returns aggregated call/put volumes and OI, or None on failure.
    """
    key = _tradier_key()
    if not key:
        return None

    headers = {**_TRADIER_HEADERS, "Authorization": f"Bearer {key}"}

    # Step 1: get expiration dates
    try:
        resp = requests.get(
            f"{_TRADIER_BASE}/markets/options/expirations",
            params={"symbol": ticker, "includeAllRoots": "true"},
            headers=headers,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        exps = data.get("expirations", {}).get("date", [])
        if not exps:
            return None
        # Use nearest 2 expirations for better signal
        target_exps = exps[:2] if isinstance(exps, list) else [exps]
    except Exception as exc:
        print(f"[options_flow] Tradier expiry fetch failed for {ticker}: {exc}")
        return None

    # Step 2: fetch chains for each expiration
    total_call_vol = 0
    total_put_vol  = 0
    total_call_oi  = 0
    total_put_oi   = 0
    max_single_contract_vol = 0

    for exp in target_exps:
        try:
            resp = requests.get(
                f"{_TRADIER_BASE}/markets/options/chains",
                params={"symbol": ticker, "expiration": exp, "greeks": "false"},
                headers=headers,
                timeout=5,
            )
            resp.raise_for_status()
            chain_data = resp.json()
            options = chain_data.get("options", {}).get("option", [])
            if not options:
                continue
            if isinstance(options, dict):
                options = [options]
            for opt in options:
                vol = int(opt.get("volume") or 0)
                oi  = int(opt.get("open_interest") or 0)
                opt_type = (opt.get("option_type") or "").lower()
                if opt_type == "call":
                    total_call_vol += vol
                    total_call_oi  += oi
                elif opt_type == "put":
                    total_put_vol += vol
                    total_put_oi  += oi
                # Track largest single contract — proxy for sweep/block
                if vol > max_single_contract_vol:
                    max_single_contract_vol = vol
        except Exception as exc:
            print(f"[options_flow] Tradier chain fetch failed for {ticker} {exp}: {exc}")
            continue

    if total_call_vol + total_put_vol == 0:
        return None

    return {
        "call_volume": total_call_vol,
        "put_volume":  total_put_vol,
        "call_oi":     total_call_oi,
        "put_oi":      total_put_oi,
        "max_single_vol": max_single_contract_vol,
        "source": "tradier",
    }


# ── yfinance fallback ─────────────────────────────────────────────────────────

def _get_yfinance_options(ticker: str) -> dict | None:
    """Fetch options data via yfinance (delayed fallback)."""
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
            "call_volume": int(calls["volume"].fillna(0).sum()),
            "put_volume":  int(puts["volume"].fillna(0).sum()),
            "call_oi":     int(calls["openInterest"].fillna(0).sum()),
            "put_oi":      int(puts["openInterest"].fillna(0).sum()),
            "max_single_vol": 0,
            "source": "yfinance",
        }
    except Exception:
        return None


# ── Signal computation ────────────────────────────────────────────────────────

def _compute_signal(raw: dict) -> dict:
    """Compute options flow signals from raw volume/OI data."""
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

    # Unusual: vol > 2x OI (someone is betting big today)
    unusual = bool(vol_oi_ratio and vol_oi_ratio > 2.0)

    # Flow direction
    bullish_flow = put_call_ratio is not None and put_call_ratio < 0.7
    bearish_flow = put_call_ratio is not None and put_call_ratio > 1.5

    # Sweep detection: single contract block > 20% of total volume (Tradier only)
    sweep_detected = (
        source == "tradier"
        and total_vol > 0
        and max_vol > total_vol * 0.20
        and max_vol > 500
    )

    score  = 0
    notes  = []
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
    if source == "tradier":
        notes.append("(real-time)")

    note = ", ".join(notes) if notes else f"P/C={put_call_ratio}, vol/OI={vol_oi_ratio}"

    return {
        "unusual":         unusual,
        "put_call_ratio":  put_call_ratio,
        "vol_oi_ratio":    vol_oi_ratio,
        "call_volume":     call_vol,
        "put_volume":      put_vol,
        "bullish_flow":    bool(bullish_flow),
        "bearish_flow":    bool(bearish_flow),
        "sweep_detected":  sweep_detected,
        "signal_score":    max(-5, min(5, score)),
        "note":            note,
        "source":          source,
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
    Tries Tradier first (if key set), falls back to yfinance.
    """
    raw = _get_tradier_options(ticker) or _get_yfinance_options(ticker)
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
    key = _tradier_key()
    print(f"Tradier key: {'set' if key else 'NOT SET — using yfinance fallback'}")
    for ticker in ["NVDA", "AAPL", "SPY"]:
        print(f"\n=== {ticker} ===")
        pprint.pprint(get_options_signal(ticker))
