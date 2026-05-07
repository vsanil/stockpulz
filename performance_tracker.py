"""
performance_tracker.py — Saturday weekly P&L recap + community benchmark.

Loads this week's picks from the Gist, fetches current prices via yfinance
(stocks) and CoinGecko (crypto), then computes compact performance stats.
"""

import math
import requests
import yfinance as yf

from config_manager import load_weekly_picks

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"


def build_weekly_recap() -> dict | None:
    """
    Returns a recap dict, or None if there are no picks this week.

    Shape:
    {
        "days_tracked": 4,
        "stocks":  { "count": 8, "wins": 6, "avg_return": 1.9,
                     "best": ("NVDA", 4.8), "worst": ("AAPL", -1.2) },
        "crypto":  { ... same ... },
        "spy_return": 0.6,   # S&P 500 weekly return %, or None
    }
    """
    weekly = load_weekly_picks()
    if not weekly:
        print("[performance_tracker] No weekly picks found.")
        return None

    print(f"[performance_tracker] Loaded picks for {len(weekly)} day(s).")

    # ── Collect all entry prices ───────────────────────────────────────────────
    stock_entries: dict[str, list[float]] = {}  # ticker → [entry_price, ...]
    crypto_entries: dict[str, dict] = {}         # symbol → {id, entries: [...]}

    for _date, picks in weekly.items():
        stocks = picks.get("stocks", picks)
        crypto = picks.get("crypto", {})

        for section in ("short_term", "long_term"):
            for s in stocks.get(section, []):
                t  = s.get("ticker")
                ep = s.get("entry_price")
                if t and ep:
                    stock_entries.setdefault(t, []).append(float(ep))

            for c in crypto.get(section, []):
                sym = c.get("symbol", "").upper()
                cid = c.get("id", "")
                ep  = c.get("entry_price")
                if sym and ep:
                    if sym not in crypto_entries:
                        crypto_entries[sym] = {"id": cid, "entries": []}
                    crypto_entries[sym]["entries"].append(float(ep))

    # ── Fetch current prices ───────────────────────────────────────────────────
    current: dict[str, float] = {}

    # Stocks + SPY via yfinance
    all_tickers = list(stock_entries.keys()) + ["SPY"]
    for ticker in all_tickers:
        try:
            price = yf.Ticker(ticker).fast_info.last_price
            if price:
                current[ticker] = float(price)
        except Exception as exc:
            print(f"[performance_tracker] Could not fetch {ticker}: {exc}")

    # S&P 500 weekly return (5-day window)
    spy_return = None
    try:
        hist = yf.Ticker("SPY").history(period="5d")
        if len(hist) >= 2:
            spy_return = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100
    except Exception as exc:
        print(f"[performance_tracker] SPY benchmark failed: {exc}")

    # Crypto via CoinGecko bulk call
    if crypto_entries:
        try:
            ids  = ",".join(v["id"] for v in crypto_entries.values() if v["id"])
            resp = requests.get(
                COINGECKO_SIMPLE,
                params={"ids": ids, "vs_currencies": "usd"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            for sym, info in crypto_entries.items():
                price = data.get(info["id"], {}).get("usd")
                if price:
                    current[sym] = float(price)
        except Exception as exc:
            print(f"[performance_tracker] Could not fetch crypto prices: {exc}")

    # ── Compute returns ────────────────────────────────────────────────────────
    def calc_returns(entries_map, key_field="ticker"):
        result = []
        for key, val in entries_map.items():
            entries = val if isinstance(val, list) else val["entries"]
            cp = current.get(key)
            if cp and entries:
                avg_entry = sum(entries) / len(entries)
                ret = (cp - avg_entry) / avg_entry * 100
                result.append((key, round(ret, 1)))
        return result

    stock_returns  = calc_returns(stock_entries)
    crypto_returns = calc_returns(crypto_entries)

    def stats(returns):
        if not returns:
            return None
        vals  = [r for _, r in returns]
        wins  = [r for r in vals if r > 0]
        losses = [r for r in vals if r <= 0]
        avg   = sum(vals) / len(vals)

        # Simplified Sharpe: avg / std (illustrative, not annualized)
        sharpe = None
        if len(vals) > 1:
            import math
            std = math.sqrt(sum((r - avg) ** 2 for r in vals) / len(vals))
            sharpe = round(avg / std, 2) if std > 0 else None

        # Max drawdown (largest single loss)
        max_dd = min(vals) if vals else None

        return {
            "count":      len(returns),
            "wins":       len(wins),
            "win_rate":   round(len(wins) / len(vals) * 100, 1),
            "avg_return": round(avg, 1),
            "avg_gain":   round(sum(wins) / len(wins), 1) if wins else None,
            "avg_loss":   round(sum(losses) / len(losses), 1) if losses else None,
            "sharpe":     sharpe,
            "max_loss":   round(max_dd, 1) if max_dd is not None else None,
            "best":       max(returns, key=lambda x: x[1]),
            "worst":      min(returns, key=lambda x: x[1]),
        }

    return {
        "days_tracked": len(weekly),
        "stocks":       stats(stock_returns),
        "crypto":       stats(crypto_returns),
        "spy_return":   round(spy_return, 1) if spy_return is not None else None,
    }


def build_community_stats(user_trade_logs: list[dict]) -> dict | None:
    """
    Aggregate performance across all users' trade logs.
    Used by /community command to show StockPulz vs market benchmark.

    Args:
        user_trade_logs: list of trade log dicts from load_user_trade_log() per user.

    Returns dict:
    {
        "total_users":      3,
        "total_trades":     42,
        "win_rate":         68.4,
        "avg_return":       2.3,
        "total_wins":       29,
        "total_losses":     13,
        "spy_return_30d":   1.8,   # SPY 30-day return (benchmark)
        "alpha":            0.5,   # avg_return - spy_return_30d / 30 * avg_hold_days (approx)
        "best_pick":        ("NVDA", 12.4),
        "worst_pick":       ("AAPL", -3.1),
        "hot_streak_users": 1,     # users on a 3+ win streak
    }
    or None if no data.
    """
    all_closed = []
    for log in user_trade_logs:
        closed = log.get("closed", [])
        for trade in closed:
            if trade.get("return_pct") is not None:
                all_closed.append(trade)

    if not all_closed:
        return None

    # Fetch SPY 30-day return as benchmark
    spy_return_30d = None
    try:
        hist = yf.Ticker("SPY").history(period="1mo")
        if len(hist) >= 2:
            spy_return_30d = round(
                (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100, 1
            )
    except Exception as exc:
        print(f"[performance_tracker] SPY 30d fetch failed: {exc}")

    returns  = [float(t["return_pct"]) for t in all_closed]
    wins     = [r for r in returns if r > 0]
    losses   = [r for r in returns if r <= 0]
    avg_ret  = round(sum(returns) / len(returns), 1) if returns else 0
    win_rate = round(len(wins) / len(returns) * 100, 1) if returns else 0

    best_trade  = max(all_closed, key=lambda t: float(t.get("return_pct", 0)), default=None)
    worst_trade = min(all_closed, key=lambda t: float(t.get("return_pct", 0)), default=None)

    # Count users on hot streak (≥3 consecutive wins in their most recent trades)
    hot_streak_users = 0
    for log in user_trade_logs:
        recent = sorted(log.get("closed", []), key=lambda t: t.get("closed_date", ""), reverse=True)[:5]
        streak = 0
        for t in recent:
            if float(t.get("return_pct", 0)) > 0:
                streak += 1
            else:
                break
        if streak >= 3:
            hot_streak_users += 1

    # Simple alpha: community avg return vs SPY (not annualised — just directional)
    alpha = round(avg_ret - spy_return_30d, 1) if spy_return_30d is not None else None

    return {
        "total_users":      len(user_trade_logs),
        "total_trades":     len(all_closed),
        "win_rate":         win_rate,
        "avg_return":       avg_ret,
        "total_wins":       len(wins),
        "total_losses":     len(losses),
        "spy_return_30d":   spy_return_30d,
        "alpha":            alpha,
        "best_pick":        (best_trade["ticker"],  round(float(best_trade["return_pct"]),  1)) if best_trade  else None,
        "worst_pick":       (worst_trade["ticker"], round(float(worst_trade["return_pct"]), 1)) if worst_trade else None,
        "hot_streak_users": hot_streak_users,
    }
