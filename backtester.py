"""
backtester.py — Simple historical backtest of the screener strategy.

Approach:
  1. Pull 1 year of daily OHLCV for the same universe the screener evaluates
  2. At each of 26 weekly intervals apply the same technical scoring as screener.py
  3. For each simulated pick, measure the forward 21-day (1 month) return
  4. Compute win rate, avg return, Sharpe-style ratio vs SPY benchmark

Uses the same ticker universe as screener.py (600 tickers via get_stock_universe()).
26 weekly intervals gives statistically meaningful results (~130 simulated picks).

This is a replay backtest, not a walk-forward. It illustrates strategy
quality without claiming production accuracy.

Usage:
  python backtester.py          → runs and prints summary
  from backtester import run_backtest  → returns result dict for Telegram
"""

import math
import warnings
import yfinance as yf
import pandas as pd
import ta

from screener import get_stock_universe

warnings.filterwarnings("ignore")

# ── Config ─────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS       = 365   # 1 year of data — supports 26 weekly intervals
FORWARD_HOLD_DAYS   = 21    # 1-month forward return window
PICKS_PER_PERIOD    = 5     # simulate picking top 5 per period
PERIODS             = 26    # ~26 weekly intervals over the year (more robust statistics)


def _st_score(close: pd.Series, volume: pd.Series) -> int:
    """Apply short-term scoring logic (same as screener.py) to a price series."""
    score = 0
    try:
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if not pd.isna(rsi_val) and 35 <= float(rsi_val) <= 55:
            score += 25

        macd   = ta.trend.MACD(close, window_fast=12, window_slow=26, window_sign=9)
        ml, ms = macd.macd(), macd.macd_signal()
        for i in range(-3, 0):
            if (not pd.isna(ml.iloc[i]) and not pd.isna(ms.iloc[i]) and
                    ml.iloc[i] > ms.iloc[i] and ml.iloc[i - 1] <= ms.iloc[i - 1]):
                score += 25
                break

        if len(volume) >= 21:
            vr = float(volume.iloc[-1] / volume.iloc[-21:-1].mean())
            if vr > 1.5:
                score += 20

        ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        price = float(close.iloc[-1])
        w52h  = float(close.rolling(252).max().iloc[-1]) if len(close) >= 252 else price
        if not pd.isna(ema20) and float(ema20) <= price <= w52h:
            score += 15

        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_low = bb.bollinger_lband().iloc[-1]
        bb_mid = bb.bollinger_mavg().iloc[-1]
        if not pd.isna(bb_low) and not pd.isna(bb_mid) and float(bb_low) < price < float(bb_mid):
            score += 15
    except Exception:
        pass
    return score


def run_backtest(universe: list[str] | None = None) -> dict:
    """
    Run the historical backtest. Returns a result dict suitable for Telegram display.

    Returns:
        {
            "universe_size":   int,
            "periods_tested":  int,
            "total_picks":     int,
            "win_rate":        float,     # %
            "avg_return":      float,     # %
            "avg_spy_return":  float,     # benchmark %
            "alpha":           float,     # avg_return - avg_spy_return
            "sharpe_approx":   float,     # simplified Sharpe
            "best_trade":      tuple,     # (ticker, return_pct)
            "worst_trade":     tuple,     # (ticker, return_pct)
            "note":            str,
        }
    """
    if universe:
        tickers = universe
    else:
        print("[backtester] Building ticker universe from screener...")
        tickers = get_stock_universe()

    print(f"[backtester] Downloading {len(tickers)} tickers + SPY ({LOOKBACK_DAYS} days)...")

    try:
        data = yf.download(
            tickers + ["SPY"],
            period=f"{LOOKBACK_DAYS + FORWARD_HOLD_DAYS + 30}d",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        close_df  = data["Close"] if "Close" in data else data.xs("Close", axis=1, level=0)
        volume_df = data["Volume"] if "Volume" in data else data.xs("Volume", axis=1, level=0)
    except Exception as exc:
        return {"note": f"Data download failed: {exc}"}

    total_bars = len(close_df)
    if total_bars < FORWARD_HOLD_DAYS + 60:
        return {"note": "Insufficient historical data for backtest."}

    spy_close = close_df.get("SPY", pd.Series(dtype=float))
    all_trades: list[dict] = []

    # Weekly intervals across the backtest window
    step = max(1, (total_bars - FORWARD_HOLD_DAYS - 60) // PERIODS)
    test_indices = list(range(60, total_bars - FORWARD_HOLD_DAYS, step))[:PERIODS]

    for bar_idx in test_indices:
        period_scores = {}

        for ticker in tickers:
            if ticker not in close_df.columns:
                continue
            close  = close_df[ticker].iloc[:bar_idx].dropna()
            volume = volume_df[ticker].iloc[:bar_idx].dropna() if ticker in volume_df else pd.Series()
            if len(close) < 30:
                continue
            score = _st_score(close, volume)
            if score > 0:
                period_scores[ticker] = score

        # Pick top N by score
        top_picks = sorted(period_scores, key=period_scores.get, reverse=True)[:PICKS_PER_PERIOD]

        for ticker in top_picks:
            entry = close_df[ticker].iloc[bar_idx]
            exit_idx = bar_idx + FORWARD_HOLD_DAYS
            if exit_idx >= total_bars:
                continue
            exit_p = close_df[ticker].iloc[exit_idx]
            if pd.isna(entry) or pd.isna(exit_p) or entry == 0:
                continue
            ret = (exit_p - entry) / entry * 100

            # SPY benchmark for same period
            spy_ret = None
            if len(spy_close) > exit_idx:
                spy_entry = spy_close.iloc[bar_idx]
                spy_exit  = spy_close.iloc[exit_idx]
                if spy_entry and spy_exit:
                    spy_ret = (spy_exit - spy_entry) / spy_entry * 100

            all_trades.append({
                "ticker":   ticker,
                "return":   round(ret, 2),
                "spy_ret":  round(spy_ret, 2) if spy_ret else None,
                "entry_bar": bar_idx,
            })

    if not all_trades:
        return {"note": "No valid trades in backtest period."}

    returns     = [t["return"] for t in all_trades]
    spy_returns = [t["spy_ret"] for t in all_trades if t["spy_ret"] is not None]
    wins        = [r for r in returns if r > 0]

    avg_ret     = sum(returns) / len(returns)
    avg_spy     = sum(spy_returns) / len(spy_returns) if spy_returns else 0
    alpha       = avg_ret - avg_spy
    win_rate    = len(wins) / len(returns) * 100

    # Simplified Sharpe: avg return / std deviation (not annualized — illustrative only)
    if len(returns) > 1:
        mean  = avg_ret
        std   = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns))
        sharpe = round(mean / std, 2) if std > 0 else 0
    else:
        sharpe = 0

    best  = max(all_trades, key=lambda t: t["return"])
    worst = min(all_trades, key=lambda t: t["return"])

    return {
        "universe_size":  len(tickers),
        "periods_tested": len(test_indices),
        "total_picks":    len(all_trades),
        "win_rate":       round(win_rate, 1),
        "avg_return":     round(avg_ret, 2),
        "avg_spy_return": round(avg_spy, 2),
        "alpha":          round(alpha, 2),
        "sharpe_approx":  sharpe,
        "best_trade":     (best["ticker"], best["return"]),
        "worst_trade":    (worst["ticker"], worst["return"]),
        "note":           f"Backtest: {LOOKBACK_DAYS}d history, {FORWARD_HOLD_DAYS}d hold, {len(test_indices)} periods",
    }


def format_backtest_message(result: dict) -> str:
    """Format backtest result as Telegram HTML message."""
    if "note" in result and result.get("total_picks", 0) == 0:
        return f"📊 <b>Backtest</b>\n\n❌ {result['note']}"

    alpha_str = f"{result['alpha']:+.2f}%"
    alpha_emoji = "✅" if result["alpha"] > 0 else "❌"
    sharpe_emoji = "✅" if result["sharpe_approx"] > 0.5 else "⚠️"

    return (
        f"📊 <b>STRATEGY BACKTEST</b>\n\n"
        f"<b>Universe:</b> {result.get('universe_size', '—')} tickers  |  1-year history\n"
        f"<b>Periods tested:</b> {result['periods_tested']} weekly intervals ({result['total_picks']} simulated picks)\n\n"
        f"<b>Win rate:</b>  <b>{result['win_rate']}%</b>  {'✅' if result['win_rate'] >= 55 else '⚠️'}\n"
        f"<b>Avg return:</b> <b>{result['avg_return']:+.2f}%</b> (21-day hold)\n"
        f"<b>SPY benchmark:</b> {result['avg_spy_return']:+.2f}%\n"
        f"<b>Alpha:</b> <b>{alpha_str}</b> {alpha_emoji}\n"
        f"<b>Sharpe (approx):</b> {result['sharpe_approx']} {sharpe_emoji}\n\n"
        f"📈 Best: <b>{result['best_trade'][0]}</b> +{result['best_trade'][1]}%\n"
        f"📉 Worst: <b>{result['worst_trade'][0]}</b> {result['worst_trade'][1]}%\n\n"
        f"<i>{result['note']}</i>\n"
        f"<i>⚠️ Past performance does not guarantee future results.</i>"
    )


if __name__ == "__main__":
    result = run_backtest()
    print(format_backtest_message(result))
