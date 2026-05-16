"""
etf_screener.py — ETF screener using yfinance bulk download.

Scores a curated list of ~35 popular ETFs by momentum + RSI signals.
Returns top candidates in the same {short_term, long_term} structure
used by the stock and crypto screeners.

ST picks: momentum/breakout ETFs (trending up, RSI not overbought)
LT picks: broad market / sector ETFs good for long-term allocation
"""

import warnings
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Curated ETF universe ──────────────────────────────────────────────────────

# Broad market
BROAD_MARKET = ["SPY", "QQQ", "IVV", "VTI", "IWM", "DIA", "VUG", "VTV"]

# Sector ETFs
SECTOR = [
    "XLK",   # Technology
    "XLF",   # Financials
    "XLV",   # Health Care
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLC",   # Communication Services
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLU",   # Utilities
    "XLB",   # Materials
    "XLRE",  # Real Estate
]

# Thematic / factor
THEMATIC = [
    "GLD",   # Gold
    "SLV",   # Silver
    "TLT",   # Long-term bonds
    "HYG",   # High yield bonds
    "EEM",   # Emerging markets
    "EFA",   # International developed
    "VNQ",   # Real estate
    "ARKK",  # Innovation
    "SOXX",  # Semiconductors
    "IBB",   # Biotech
    "ITB",   # Homebuilders
    "XBI",   # Biotech equal weight
]

ALL_ETFS = list(dict.fromkeys(BROAD_MARKET + SECTOR + THEMATIC))  # deduped

# LT-eligible ETFs — broad/diversified, suitable for long-term allocation
LT_ELIGIBLE = set(BROAD_MARKET + ["GLD", "TLT", "EEM", "EFA", "VNQ", "SOXX"])

ST_PICKS = 2   # Max ETF short-term picks
LT_PICKS = 2   # Max ETF long-term picks


def _rsi(closes: pd.Series, period: int = 14) -> float:
    """Compute RSI for a price series. Returns 50 on failure."""
    try:
        delta  = closes.diff()
        gain   = delta.clip(lower=0).rolling(period).mean()
        loss   = (-delta.clip(upper=0)).rolling(period).mean()
        rs     = gain / loss.replace(0, float("nan"))
        rsi_s  = 100 - (100 / (1 + rs))
        val    = rsi_s.dropna().iloc[-1]
        return float(val)
    except Exception:
        return 50.0


def run_etf_screener() -> dict:
    """
    Screen curated ETF list and return top candidates.

    Scoring:
      - 1-month return (momentum)
      - RSI: penalise overbought (>75), reward oversold-recovering (<45)
      - Volume trend: 10-day avg vs 30-day avg

    Returns:
        {"short_term": [...], "long_term": [...]}
        Each entry: {ticker, name, price, return_1m, rsi, thesis, asset_type}
    """
    print(f"[etf_screener] Downloading {len(ALL_ETFS)} ETFs...")
    try:
        raw = yf.download(
            ALL_ETFS,
            period="3mo",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        print(f"[etf_screener] Download failed: {exc}")
        return {"short_term": [], "long_term": []}

    if hasattr(raw.columns, "levels"):
        available = set(raw.columns.get_level_values(0))
    else:
        available = set(ALL_ETFS[:1])

    scores = []
    for ticker in ALL_ETFS:
        if ticker not in available:
            continue
        try:
            if hasattr(raw.columns, "levels"):
                df = raw[ticker].dropna()
            else:
                df = raw.dropna()

            if len(df) < 30:
                continue

            closes  = df["Close"]
            volumes = df["Volume"]

            price   = float(closes.iloc[-1])
            price_1m = float(closes.iloc[-22]) if len(closes) >= 22 else float(closes.iloc[0])
            ret_1m  = (price - price_1m) / price_1m * 100

            rsi_val = _rsi(closes)

            vol_10  = float(volumes.iloc[-10:].mean())
            vol_30  = float(volumes.iloc[-30:].mean())
            vol_ratio = vol_10 / vol_30 if vol_30 > 0 else 1.0

            # Score: momentum + volume trend, penalise overbought
            score = ret_1m * 0.6 + (vol_ratio - 1) * 10
            if rsi_val > 75:
                score -= 5    # overbought penalty
            elif rsi_val < 35:
                score -= 3    # too extended downside

            scores.append({
                "ticker":      ticker,
                "price":       round(price, 2),
                "ret_1m":      round(ret_1m, 2),
                "rsi":         round(rsi_val, 1),
                "vol_ratio":   round(vol_ratio, 2),
                "score":       round(score, 3),
                "lt_eligible": ticker in LT_ELIGIBLE,
            })
        except Exception as exc:
            print(f"[etf_screener] Score error for {ticker}: {exc}")
            continue

    if not scores:
        print("[etf_screener] No ETFs scored.")
        return {"short_term": [], "long_term": []}

    scores.sort(key=lambda x: x["score"], reverse=True)

    # ST: top momentum picks (not overbought)
    st_pool = [s for s in scores if s["rsi"] <= 75][:ST_PICKS]

    # LT: top LT-eligible by score
    lt_pool = [s for s in scores if s["lt_eligible"]][:LT_PICKS]

    # Avoid duplicate tickers between ST and LT
    lt_tickers = {s["ticker"] for s in st_pool}
    lt_pool = [s for s in lt_pool if s["ticker"] not in lt_tickers][:LT_PICKS]

    def _entry(s: dict) -> dict:
        """Format a scored ETF into screener candidate dict."""
        ticker  = s["ticker"]
        ret_str = f"+{s['ret_1m']:.1f}%" if s["ret_1m"] >= 0 else f"{s['ret_1m']:.1f}%"
        thesis  = f"1-month return {ret_str}, RSI {s['rsi']:.0f}"
        return {
            "ticker":     ticker,
            "price":      s["price"],
            "ret_1m":     s["ret_1m"],
            "rsi":        round(s["rsi"], 1),
            "vol_ratio":  round(s.get("vol_ratio", 1.0), 2),
            "thesis":     thesis,
            "asset_type": "etf",
        }

    result = {
        "short_term": [_entry(s) for s in st_pool],
        "long_term":  [_entry(s) for s in lt_pool],
    }

    n_st = len(result["short_term"])
    n_lt = len(result["long_term"])
    print(f"[etf_screener] Done — {n_st} ST, {n_lt} LT candidates.")
    return result
