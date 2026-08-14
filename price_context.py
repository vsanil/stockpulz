"""
price_context.py — Compute multi-day price context for stock candidates.

Gives Claude the chart setup behind each candidate:
  - MA position (above/below 20/50/200 MA)
  - Consolidation detection (tight ATR for N days = coiling)
  - Support/resistance levels from recent swing highs/lows
  - Breakout detection (today's close above resistance + volume spike)
  - Trend direction (1M and 3M)
  - Volume trend (up-day vs down-day volume balance)

Called from ai_analyzer._build_stock_candidates() alongside news/sentiment fetching.
Returns a compact context_str that Claude reads as natural language chart notes.
"""
from __future__ import annotations

import yfinance as yf
import pandas as pd


def get_price_context(ticker: str, hist: pd.DataFrame | None = None) -> dict:
    """
    Compute technical chart context from price history.

    Args:
        ticker: Stock ticker symbol.
        hist:   Optional pre-fetched DataFrame (Close/High/Low/Volume, daily).
                If None, fetches 6-month daily history via yfinance.

    Returns a dict with keys:
        ma_position, consolidating, consolidation_days, breakout,
        support, resistance, trend_1m, trend_3m, volume_trend, context_str.
    Returns {} on error or insufficient data.
    """
    try:
        if hist is None:
            hist = yf.Ticker(ticker).history(period="6mo", interval="1d")

        if hist is None or len(hist) < 20:
            return {}

        close  = hist["Close"]
        high   = hist["High"]
        low    = hist["Low"]
        volume = hist["Volume"]

        last_close = float(close.iloc[-1])

        # ── Moving averages ───────────────────────────────────────────────────
        ma20  = close.rolling(20).mean()
        ma50  = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()

        def _last(series):
            s = series.dropna()
            return float(s.iloc[-1]) if len(s) > 0 else None

        last_ma20  = _last(ma20)
        last_ma50  = _last(ma50)
        last_ma200 = _last(ma200)

        above_20  = last_ma20  is not None and last_close > last_ma20
        above_50  = last_ma50  is not None and last_close > last_ma50
        above_200 = last_ma200 is not None and last_close > last_ma200

        if above_20 and above_50 and above_200:
            ma_position = "above all MAs (bullish)"
        elif above_50 and above_200:
            ma_position = "above 50/200MA (healthy)"
        elif above_200 and not above_50:
            ma_position = "above 200MA, below 50MA (mixed)"
        elif not above_200:
            ma_position = "below 200MA (bearish)"
        else:
            ma_position = "mixed MA position"

        # ── ATR-based consolidation ───────────────────────────────────────────
        daily_range = high - low
        atr_full   = float(daily_range.tail(60).mean())
        atr_recent = float(daily_range.tail(14).mean())
        consolidating = atr_full > 0 and (atr_recent < atr_full * 0.70)

        # Count consecutive low-range days from most recent
        consol_days = 0
        threshold   = atr_full * 0.75
        for r in reversed(daily_range.values):
            if r <= threshold:
                consol_days += 1
            else:
                break

        # ── Support / resistance from 20-day swing highs/lows ────────────────
        recent = close.tail(30)
        resistance = None
        support    = None
        if len(recent) >= 10:
            # Resistance: highest close in the 5 days ending 1 day before today
            resistance = float(recent.iloc[-6:-1].max())
            # Support: lowest close in same window
            support    = float(recent.iloc[-6:-1].min())

        # ── Breakout: today's close above recent resistance + elevated volume ─
        avg_vol   = float(volume.tail(20).mean())
        today_vol = float(volume.iloc[-1])
        breakout  = bool(
            resistance is not None
            and last_close > resistance * 1.005
            and avg_vol > 0
            and today_vol > avg_vol * 1.3
        )

        # ── Trend direction ───────────────────────────────────────────────────
        def _trend(n: int) -> str:
            if len(close) < n + 1:
                return "flat"
            pct = (last_close - float(close.iloc[-n])) / float(close.iloc[-n]) * 100
            if pct > 3:
                return "uptrend"
            if pct < -3:
                return "downtrend"
            return "flat"

        trend_1m = _trend(21)
        trend_3m = _trend(63)

        # ── Volume balance (up-day vs down-day) ───────────────────────────────
        recent_20  = hist.tail(20)
        # 🔴 pandas .mean() of an EMPTY selection is NaN, and every comparison
        # against NaN is False — so a window with NO down-days (or no up-days)
        # fell through to "neutral volume", losing the signal exactly when it is
        # strongest. Same family as _is_pos / plausible_price: NaN does not
        # announce itself, it just makes conditions quietly false.
        def _mean_vol(mask) -> float:
            if len(recent_20) == 0:
                return 0.0
            v = float(recent_20.loc[mask, "Volume"].mean())
            return v if v == v else 0.0          # v != v  ⇒  NaN

        up_vol   = _mean_vol(recent_20["Close"] >= recent_20["Open"])
        down_vol = _mean_vol(recent_20["Close"] <  recent_20["Open"])
        if up_vol > down_vol * 1.25:
            vol_trend = "accumulation (up-day vol dominates)"
        elif down_vol > up_vol * 1.25:
            vol_trend = "distribution (down-day vol dominates)"
        else:
            vol_trend = "neutral volume"

        # ── Assemble human-readable context string ────────────────────────────
        parts = [ma_position]
        if consolidating and consol_days >= 7:
            parts.append(f"{consol_days}-day consolidation (coiling)")
        if breakout:
            parts.append(f"BREAKOUT above ${resistance:.2f} resistance w/ high volume today")
        elif resistance is not None:
            parts.append(f"resistance ~${resistance:.2f}")
        if support is not None and not breakout:
            parts.append(f"support ~${support:.2f}")
        parts.append(f"1M {trend_1m}, 3M {trend_3m}")
        if vol_trend != "neutral volume":
            parts.append(vol_trend)

        return {
            "ma_position":        ma_position,
            "consolidating":      consolidating,
            "consolidation_days": consol_days,
            "breakout":           breakout,
            "support":            round(support, 2)    if support    is not None else None,
            "resistance":         round(resistance, 2) if resistance is not None else None,
            "trend_1m":           trend_1m,
            "trend_3m":           trend_3m,
            "volume_trend":       vol_trend,
            "context_str":        " · ".join(parts),
        }

    except Exception as exc:
        print(f"[price_context] Error for {ticker}: {exc}")
        return {}
