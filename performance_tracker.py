"""
performance_tracker.py — Saturday weekly P&L recap + community benchmark.

Loads this week's picks from the Gist, fetches current prices via yfinance
(stocks) and CoinGecko (crypto), then computes compact performance stats.
"""
from __future__ import annotations

import math
import requests
import yfinance as yf

from config_manager import human_trades,  load_weekly_picks

# Honesty floors — a percentage off a handful of trades is noise, not a record.
_MIN_COMMUNITY_TRADES = 10   # public "community track record"
_MIN_RECENT_TRADES    = 5    # morning perf bar

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"


def _spy_return(period: str) -> float | None:
    """
    SPY % return over the period, NaN-safe. Drops NaN closes (yfinance often
    returns a trailing NaN/partial bar) so a flaky fetch doesn't blank the
    benchmark — and never returns NaN, which would break the JSON response.
    Returns the raw float (caller rounds) or None.
    """
    try:
        closes = yf.Ticker("SPY").history(period=period)["Close"].dropna()
        if len(closes) >= 2:
            first, last = float(closes.iloc[0]), float(closes.iloc[-1])
            if first > 0 and math.isfinite(first) and math.isfinite(last):
                return (last - first) / first * 100
    except Exception as exc:
        print(f"[performance_tracker] SPY fetch failed ({period}): {exc}")
    return None


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
    spy_return = _spy_return("5d")

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

    # ── Individual pick outcomes (sorted by return, all picks) ───────────────
    pick_outcomes = []
    for ticker, pct in sorted(stock_returns + crypto_returns, key=lambda x: x[1], reverse=True):
        asset_type = "crypto" if ticker in crypto_entries else "stock"
        entries_list = (
            stock_entries.get(ticker) or
            (crypto_entries.get(ticker, {}).get("entries") if ticker in crypto_entries else None) or
            []
        )
        avg_entry = sum(entries_list) / len(entries_list) if entries_list else None
        cp = current.get(ticker)
        pick_outcomes.append({
            "ticker":  ticker,
            "type":    asset_type,
            "entry":   round(avg_entry, 2) if avg_entry is not None else None,
            "current": round(cp, 2) if cp is not None else None,
            "pct":     pct,
            "status":  "▲" if pct > 0 else "▼",
        })

    return {
        "days_tracked":  len(weekly),
        "stocks":        stats(stock_returns),
        "crypto":        stats(crypto_returns),
        "spy_return":    round(spy_return, 1) if spy_return is not None else None,
        "pick_outcomes": pick_outcomes,
    }


def get_recent_stats(trade_logs: list[dict], days: int = 30) -> dict | None:
    """
    Compute performance stats from closed trades in the last N days across all users.
    Used for the morning message performance bar and /stats command.

    Returns None if fewer than 3 closed trades in the window (not enough to be meaningful).
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    all_closed = []
    for log in trade_logs:
        # human_trades() drops synthetic-bot trades: a robot's mechanical fills
        # must never be presented as a user/community track record.
        for t in human_trades(log.get("closed", [])):
            if t.get("closed_date", "") >= cutoff and t.get("return_pct") is not None:
                all_closed.append(t)

    if len(all_closed) < _MIN_RECENT_TRADES:
        return None

    def _cat_stats(trades):
        if not trades:
            return None
        returns    = [float(t["return_pct"]) for t in trades]
        wins       = [r for r in returns if r > 0]
        losses     = [r for r in returns if r <= 0]
        win_rate   = len(wins) / len(returns)
        avg_gain   = sum(wins)   / len(wins)   if wins   else 0.0
        avg_loss   = sum(losses) / len(losses) if losses else 0.0
        expectancy = win_rate * avg_gain + (1 - win_rate) * avg_loss
        sorted_r   = sorted(returns)
        n          = len(sorted_r)
        median     = (sorted_r[n // 2] if n % 2 == 1
                      else (sorted_r[n // 2 - 1] + sorted_r[n // 2]) / 2)
        return {
            "count":         len(trades),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(win_rate * 100, 1),
            "avg_return":    round(sum(returns) / len(returns), 1),
            "median_return": round(median, 1),
            "avg_gain":      round(avg_gain, 1),
            "avg_loss":      round(avg_loss, 1),
            "expectancy":    round(expectancy, 2),
        }

    stocks = [t for t in all_closed if t.get("asset_type") == "stock"]
    crypto = [t for t in all_closed if t.get("asset_type") == "crypto"]

    _sr = _spy_return(f"{min(days, 59)}d")
    spy_return = round(_sr, 1) if _sr is not None else None

    return {
        "days":       days,
        "total":      _cat_stats(all_closed),
        "stocks":     _cat_stats(stocks),
        "crypto":     _cat_stats(crypto),
        "spy_return": spy_return,
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
        closed = human_trades(log.get("closed", []))
        for trade in closed:
            if trade.get("return_pct") is not None:
                all_closed.append(trade)

    # Minimum sample for a PUBLIC track record. A win rate off 1-3 trades is
    # noise, and it is exactly the number a prospective user judges the app by:
    # after excluding synthetic-bot trades this briefly read "100% win rate"
    # off 3 winners, which is far more misleading than showing nothing.
    # Below the floor, callers get None and render their existing empty state.
    if len(all_closed) < _MIN_COMMUNITY_TRADES:
        print(f"[performance_tracker] community stats hidden — only "
              f"{len(all_closed)} human trades (need {_MIN_COMMUNITY_TRADES}+).")
        return None

    # Fetch SPY 30-day return as benchmark (NaN-safe, drops NaN closes)
    _sr30 = _spy_return("1mo")
    spy_return_30d = round(_sr30, 1) if _sr30 is not None else None

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
        recent = sorted(human_trades(log.get("closed", [])), key=lambda t: t.get("closed_date", ""), reverse=True)[:5]
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
