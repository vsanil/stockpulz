"""
market_regime.py — Detect current market regime using VIX + SPY trend.

Regime classification:
  bull     → SPY above 50 & 200 MA, VIX < 20
  volatile → VIX > 30 (regardless of trend)
  bear     → SPY below 200 MA
  neutral  → everything else

Macro enrichment (fetched once per run, Gist-cached 4h):
  fear_greed       — CNN Fear & Greed via alternative.me (free, no key)
  rate_trend       — FRED Fed Funds + 10Y yield (free, needs FRED_API_KEY env var)
  sector_rotation  — 5-day performance of 11 sector ETFs (yfinance, free)

All enrichments fail silently — regime classification never depends on them.
User count has zero impact on fetch volume; all users read from the same cache.
"""
from __future__ import annotations

import os
import time
import requests
import yfinance as yf

from config_manager import load_macro_cache, save_macro_cache

# VIX thresholds
VIX_LOW           = 15   # calm market
VIX_CAUTION       = 20   # bull → neutral edge; above this VIX bull regime ends
VIX_ELEVATED_RISK = 22   # elevated regime: uptrend intact but risk rising
VIX_HIGH          = 28   # volatile regime: extreme fear (was 30 — lowered for earlier warning)

# Hysteresis band: SPY must be this % ABOVE/BELOW a moving average to confirm
# a bull or bear regime transition.  Prevents daily whipsawing when price
# straddles the MA by a few cents.
MA_HYSTERESIS_PCT = 0.005   # 0.5% band on each side of the MA

# External API endpoints
_FNG_URL  = "https://api.alternative.me/fng/?limit=2"
_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
_HEADERS  = {"User-Agent": "StockPulz/1.0 (personal trading bot)"}

# 11 GICS sector ETFs — full coverage
SECTOR_ETFS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLV":  "Health Care",
    "XLE":  "Energy",
    "XLI":  "Industrials",
    "XLC":  "Communication",
    "XLY":  "Consumer Disc",
    "XLP":  "Consumer Staples",
    "XLU":  "Utilities",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
}


# ── Fear & Greed ──────────────────────────────────────────────────────────────

def _get_fear_greed() -> dict | None:
    """
    Fetch CNN Fear & Greed index from alternative.me (free, no key required).
    Returns dict with value (0-100), label, and 1-day trend direction.
    Never raises — returns None on any failure.
    """
    try:
        resp = requests.get(_FNG_URL, headers=_HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if not data:
            return None

        today     = data[0]
        yesterday = data[1] if len(data) > 1 else None

        value = int(today.get("value", 0))
        label = today.get("value_classification", "Unknown")

        trend = "stable"
        if yesterday:
            prev = int(yesterday.get("value", value))
            diff = value - prev
            if diff >= 5:
                trend = "rising"
            elif diff <= -5:
                trend = "falling"

        return {"value": value, "label": label, "trend": trend}
    except Exception as exc:
        print(f"[market_regime] Fear & Greed fetch failed: {exc}")
        return None


# ── FRED rate trend ───────────────────────────────────────────────────────────

def _get_fred_series(series_id: str, api_key: str) -> float | None:
    """Fetch the most recent observation for a FRED series. Returns float or None."""
    try:
        params = {
            "series_id":  series_id,
            "api_key":    api_key,
            "limit":      3,
            "sort_order": "desc",
            "file_type":  "json",
        }
        resp = requests.get(_FRED_URL, params=params, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        obs = resp.json().get("observations", [])
        for o in obs:
            v = o.get("value", ".")
            if v != ".":
                return float(v)
        return None
    except Exception as exc:
        print(f"[market_regime] FRED fetch failed for {series_id}: {exc}")
        return None


def _get_fred_rates() -> dict | None:
    """
    Fetch Fed Funds rate and 10Y Treasury yield from FRED.
    Requires FRED_API_KEY env var — skips gracefully if absent.
    Returns dict with fed_funds, yield_10y, rate_regime, and summary.
    Never raises.
    """
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        return None

    try:
        fed_funds = _get_fred_series("FEDFUNDS", api_key)
        time.sleep(0.3)
        yield_10y = _get_fred_series("DGS10", api_key)

        if fed_funds is None and yield_10y is None:
            return None

        parts = []
        if fed_funds is not None:
            parts.append(f"Fed Funds {fed_funds:.2f}%")
        if yield_10y is not None:
            parts.append(f"10Y yield {yield_10y:.2f}%")

        summary = ", ".join(parts) if parts else "rates unavailable"

        rate_regime = "high"
        if fed_funds is not None:
            if fed_funds < 2.0:
                rate_regime = "low"
            elif fed_funds < 4.0:
                rate_regime = "moderate"

        return {
            "fed_funds":   fed_funds,
            "yield_10y":   yield_10y,
            "rate_regime": rate_regime,
            "summary":     summary,
        }
    except Exception as exc:
        print(f"[market_regime] FRED rates fetch failed: {exc}")
        return None


# ── Sector rotation ───────────────────────────────────────────────────────────

def _get_sector_rotation() -> dict | None:
    """
    Fetch 5-day performance for 11 GICS sector ETFs via yfinance (free, no key).

    Returns dict with:
      leaders   — top 3 performing sectors with 5d % (e.g. ["Technology +3.2%", ...])
      laggards  — bottom 3 performing sectors (worst first)
      all       — {ticker: pct_5d} for all 11 sectors
      summary   — one-line string for the AI prompt

    Never raises — returns None on any failure.
    """
    try:
        tickers = list(SECTOR_ETFS.keys())
        # 30d window: enough for a 20-trading-day lookback even after weekends/holidays
        hist = yf.download(
            tickers,
            period="30d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )

        perf: dict[str, dict] = {}
        for ticker, name in SECTOR_ETFS.items():
            try:
                if hasattr(hist.columns, "levels"):
                    closes = hist[ticker]["Close"].dropna()
                else:
                    # Single-ticker fallback
                    closes = hist["Close"].dropna()
                if len(closes) < 5:
                    continue

                pct_5d = (float(closes.iloc[-1]) - float(closes.iloc[-5])) / float(closes.iloc[-5]) * 100

                # 20-day window — blends in the medium-term trend to smooth noise
                pct_20d: float | None = None
                if len(closes) >= 20:
                    pct_20d = (float(closes.iloc[-1]) - float(closes.iloc[-20])) / float(closes.iloc[-20]) * 100

                # Blended score: 40% recent (5d) + 60% medium-term (20d)
                # The 60/40 split gives stable trend signal while preserving
                # near-term sensitivity. Falls back to 5d only if 20d unavailable.
                if pct_20d is not None:
                    blended = 0.4 * pct_5d + 0.6 * pct_20d
                else:
                    blended = pct_5d

                perf[ticker] = {
                    "name":    name,
                    "pct_5d":  round(pct_5d, 1),
                    "pct_20d": round(pct_20d, 1) if pct_20d is not None else None,
                    "blended": round(blended, 1),
                }
            except Exception:
                pass

        if not perf:
            return None

        # Sort by blended score — more stable than pure 5d ranking
        sorted_items = sorted(perf.items(), key=lambda x: x[1]["blended"], reverse=True)
        leaders  = [f"{v['name']} {v['blended']:+.1f}%" for _, v in sorted_items[:3] if v["blended"] > 0]
        laggards = [f"{v['name']} {v['blended']:+.1f}%" for _, v in reversed(sorted_items[-3:]) if v["blended"] < 0]

        best  = sorted_items[0][1]  if sorted_items else None
        worst = sorted_items[-1][1] if sorted_items else None

        summary_parts = []
        if best  and best["blended"]  > 0.5:
            summary_parts.append(f"{best['name']} leading ({best['blended']:+.1f}% blended)")
        if worst and worst["blended"] < -0.5:
            summary_parts.append(f"{worst['name']} lagging ({worst['blended']:+.1f}% blended)")
        summary = " | ".join(summary_parts) if summary_parts else "sector rotation flat"

        print(f"[market_regime] Sector rotation: {summary}")
        return {
            "leaders":  leaders,
            "laggards": laggards,
            "all":      {k: v["blended"] for k, v in perf.items()},
            "all_5d":   {k: v["pct_5d"]  for k, v in perf.items()},
            "all_20d":  {k: v["pct_20d"] for k, v in perf.items()
                         if v["pct_20d"] is not None},
            "summary":  summary,
        }
    except Exception as exc:
        print(f"[market_regime] Sector rotation fetch failed: {exc}")
        return None


# ── Main regime function ──────────────────────────────────────────────────────

def get_market_regime() -> dict:
    """
    Fetch VIX, SPY MAs, Fear & Greed, FRED rates, and sector rotation.
    Classify market regime.

    Fear & Greed, FRED, and sector rotation are loaded from Gist cache (4h TTL)
    if available, fetched live otherwise. VIX + SPY are always fetched live.

    Returns:
        {
            "regime":          "bull" | "bear" | "volatile" | "neutral",
            "vix":             float | None,
            "spy_price":       float | None,
            "spy_ma50":        float | None,
            "spy_ma200":       float | None,
            "spy_above_50ma":  bool | None,
            "spy_above_200ma": bool | None,
            "note":            str,
            "fear_greed":      { value, label, trend } | None,
            "rate_trend":      { fed_funds, yield_10y, rate_regime, summary } | None,
            "sector_rotation": { leaders, laggards, all, summary } | None,
        }
    """
    vix            = None
    spy_price      = None
    spy_ma50       = None
    spy_ma200      = None
    spy_above_50   = None
    spy_above_200  = None

    # ── VIX (always live) ─────────────────────────────────────────────────────
    try:
        vix = float(yf.Ticker("^VIX").fast_info.last_price)
    except Exception as exc:
        print(f"[market_regime] VIX fetch failed: {exc}")

    # ── SPY moving averages (always live) ────────────────────────────────────
    try:
        hist = yf.Ticker("SPY").history(period="1y")
        if len(hist) >= 200:
            spy_price     = float(hist["Close"].iloc[-1])
            spy_ma50      = float(hist["Close"].rolling(50).mean().iloc[-1])
            spy_ma200     = float(hist["Close"].rolling(200).mean().iloc[-1])
            spy_above_50  = spy_price > spy_ma50
            spy_above_200 = spy_price > spy_ma200
    except Exception as exc:
        print(f"[market_regime] SPY history fetch failed: {exc}")

    # ── Macro signals (Gist-cached 4h) ───────────────────────────────────────
    fear_greed      = None
    rate_trend      = None
    sector_rotation = None

    macro_cache = load_macro_cache()
    if macro_cache:
        fear_greed      = macro_cache.get("fear_greed")
        rate_trend      = macro_cache.get("rate_trend")
        sector_rotation = macro_cache.get("sector_rotation")
        print("[market_regime] Macro signals loaded from cache.")
    else:
        print("[market_regime] Macro cache miss — fetching live.")
        fear_greed      = _get_fear_greed()
        rate_trend      = _get_fred_rates()
        sector_rotation = _get_sector_rotation()
        save_macro_cache(fear_greed, rate_trend, sector_rotation)

    # ── Hysteresis: require SPY to be clearly above/below MA before transitioning ──
    # This prevents daily regime flipping when price straddles the moving average.
    # "firmly above 200MA" = price > MA200 × 1.005 (0.5% buffer)
    # "firmly below 200MA" = price < MA200 × 0.995
    spy_firmly_above_200 = (
        spy_price is not None and spy_ma200 is not None
        and spy_price > spy_ma200 * (1 + MA_HYSTERESIS_PCT)
    )
    spy_firmly_below_200 = (
        spy_price is not None and spy_ma200 is not None
        and spy_price < spy_ma200 * (1 - MA_HYSTERESIS_PCT)
    )

    # ── Classify regime ───────────────────────────────────────────────────────
    # Priority: volatile > bear > elevated > bull > neutral
    if vix is not None and vix >= VIX_HIGH:
        regime = "volatile"
        note   = f"VIX={vix:.1f} — extreme fear, tighten stops, reduce size"

    elif spy_firmly_below_200:
        regime = "bear"
        note   = (f"SPY decisively below 200-day MA (${spy_price:.0f} vs MA ${spy_ma200:.0f}) "
                  "— defensive posture, avoid momentum plays")

    elif spy_above_200 is False and not spy_firmly_below_200:
        # SPY just barely below MA200 (within 0.5%) — treat as neutral, not full bear
        regime = "neutral"
        vix_str = f", VIX={vix:.1f}" if vix else ""
        note    = f"SPY near 200-day MA{vix_str} — mixed signals, cautious stance"

    elif spy_firmly_above_200 and spy_above_50 is True:
        # SPY is clearly in uptrend — check VIX for exact regime
        vix_str = f"VIX={vix:.1f}" if vix else ""
        if vix is not None and vix >= VIX_ELEVATED_RISK:
            regime = "elevated"
            note   = (f"SPY in uptrend but {vix_str} elevated risk — "
                      "reduce position size, favour quality setups")
        elif vix is None or vix < VIX_CAUTION:
            regime = "bull"
            note   = f"SPY in uptrend{', ' + vix_str if vix_str else ''} — favorable conditions"
        else:
            # VIX_CAUTION <= VIX < VIX_ELEVATED_RISK: slight caution
            regime = "neutral"
            note   = f"SPY in uptrend but {vix_str} caution — monitor closely"

    else:
        regime = "neutral"
        vix_str = f", VIX={vix:.1f}" if vix else ""
        note    = f"Mixed signals{vix_str} — normal caution applies"

    # Append macro colour
    if fear_greed:
        note += f" | Sentiment: {fear_greed['label']} ({fear_greed['value']})"
    if rate_trend:
        note += f" | {rate_trend['summary']}"
    if sector_rotation and sector_rotation.get("summary"):
        note += f" | {sector_rotation['summary']}"

    return {
        "regime":          regime,
        "vix":             round(vix, 1) if vix is not None else None,
        "spy_price":       round(spy_price, 2) if spy_price else None,
        "spy_ma50":        round(spy_ma50, 2) if spy_ma50 else None,
        "spy_ma200":       round(spy_ma200, 2) if spy_ma200 else None,
        "spy_above_50ma":       spy_above_50,
        "spy_above_200ma":      spy_above_200,
        "spy_firmly_above_200": spy_firmly_above_200,
        "spy_firmly_below_200": spy_firmly_below_200,
        "note":                 note,
        "fear_greed":      fear_greed,
        "rate_trend":      rate_trend,
        "sector_rotation": sector_rotation,
    }


def regime_pick_multiplier(regime: str) -> float:
    """
    Return a multiplier (0.5–1.0) to scale down pick counts in risky regimes.
    Screener multiplies max_picks by this before selecting candidates.

    elevated: new intermediate regime — VIX rising but SPY still in uptrend.
              Use 0.8× to reduce exposure without going full defensive.
    """
    return {
        "bull":     1.0,
        "neutral":  1.0,
        "elevated": 0.8,   # VIX elevated but uptrend intact — trim size
        "volatile": 0.6,
        "bear":     0.5,
    }.get(regime, 1.0)


def regime_emoji(regime: str) -> str:
    return {
        "bull":     "🐂",
        "bear":     "🐻",
        "volatile": "⚡",
        "elevated": "⚠️",
        "neutral":  "➡️",
    }.get(regime, "")


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_market_regime())
