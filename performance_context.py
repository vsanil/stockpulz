"""
performance_context.py — Aggregate closed trade performance across all users.

Produces a context string injected into Claude's morning prompt.
This closes the feedback loop: Claude learns which conviction levels,
asset types, and setup types have been producing wins vs losses.

Called once per morning run — ~200ms, no external API needed.
"""

from datetime import date, timedelta
from config_manager import get_allowed_users, load_user_trade_log


def get_performance_stats(lookback_days: int = 30) -> dict:
    """
    Return structured performance stats for a given lookback window.
    Used by the Mini App for the 30/60/90-day comparison table.
    Returns {} if fewer than 5 trades.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    all_trades: list[dict] = []
    try:
        for uid in get_allowed_users():
            log = load_user_trade_log(uid)
            for t in log.get("closed", []):
                if t.get("closed_date", "") >= cutoff:
                    all_trades.append(t)
    except Exception as exc:
        print(f"[performance_context] get_performance_stats error: {exc}")
        return {}

    returns = [float(t["return_pct"]) for t in all_trades if "return_pct" in t]
    if len(returns) < 5:
        return {}

    wins     = [r for r in returns if r > 0]
    losses   = [r for r in returns if r <= 0]
    win_rate = round(len(wins) / len(returns) * 100, 1)
    avg_ret  = round(sum(returns) / len(returns), 2)

    return {
        "lookback_days": lookback_days,
        "trades":        len(returns),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      win_rate,
        "avg_ret":       avg_ret,
    }


def get_performance_context(lookback_days: int = 30) -> str:
    """
    Aggregate closed trades from all users over the last N days.

    Returns a multi-line string for injection into Claude's prompt.
    Returns "" if fewer than 5 trades (not enough signal).
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

    all_trades: list[dict] = []
    try:
        for uid in get_allowed_users():
            log = load_user_trade_log(uid)
            for t in log.get("closed", []):
                if t.get("closed_date", "") >= cutoff:
                    all_trades.append(t)
    except Exception as exc:
        print(f"[performance_context] Error loading trades: {exc}")
        return ""

    if len(all_trades) < 5:
        print(f"[performance_context] Only {len(all_trades)} trades — skipping (need 5+).")
        return ""

    returns = [float(t["return_pct"]) for t in all_trades if "return_pct" in t]
    if not returns:
        return ""

    wins     = [r for r in returns if r > 0]
    win_rate = round(len(wins) / len(returns) * 100)
    avg_ret  = round(sum(returns) / len(returns), 1)

    # ── By conviction ─────────────────────────────────────────────────────────
    conv_stats: dict[int, list[float]] = {}
    for t in all_trades:
        c = t.get("conviction")
        if c and "return_pct" in t:
            conv_stats.setdefault(int(c), []).append(float(t["return_pct"]))

    conv_lines = []
    for c in sorted(conv_stats):
        rets = conv_stats[c]
        if len(rets) >= 2:
            wr = round(sum(1 for r in rets if r > 0) / len(rets) * 100)
            conv_lines.append(f"★{c}: {wr}% ({len(rets)} trades)")

    # ── By asset type ─────────────────────────────────────────────────────────
    type_stats: dict[str, list[float]] = {}
    for t in all_trades:
        at = t.get("asset_type", "stock")
        if "return_pct" in t:
            type_stats.setdefault(at, []).append(float(t["return_pct"]))

    type_lines = []
    for at, rets in type_stats.items():
        if len(rets) >= 2:
            wr = round(sum(1 for r in rets if r > 0) / len(rets) * 100)
            type_lines.append(f"{at}: {wr}% win ({len(rets)} trades)")

    # ── By outcome ────────────────────────────────────────────────────────────
    outcomes: dict[str, int] = {}
    for t in all_trades:
        o = t.get("outcome", "unknown")
        outcomes[o] = outcomes.get(o, 0) + 1

    # ── Build summary ─────────────────────────────────────────────────────────
    lines = [
        f"BOT TRACK RECORD — LAST {lookback_days} DAYS ({len(all_trades)} closed trades across all users):",
        f"  Overall: {win_rate}% win rate, avg return {avg_ret:+.1f}%",
    ]

    if conv_lines:
        lines.append(f"  By conviction: {' | '.join(conv_lines)}")
        # Warn if low-conviction picks are dragging performance
        low_conv = [c for c in sorted(conv_stats) if c <= 2 and len(conv_stats[c]) >= 2]
        hi_conv  = [c for c in sorted(conv_stats) if c >= 4 and len(conv_stats[c]) >= 2]
        if low_conv:
            low_wr = round(sum(1 for r in conv_stats[low_conv[0]] if r > 0) / len(conv_stats[low_conv[0]]) * 100)
            if low_wr < 45:
                lines.append(f"  NOTE: ★{low_conv[0]} conviction picks underperforming ({low_wr}% win) — raise minimum threshold.")
        if hi_conv:
            hi_wr = round(sum(1 for r in conv_stats[hi_conv[-1]] if r > 0) / len(conv_stats[hi_conv[-1]]) * 100)
            if hi_wr >= 70:
                lines.append(f"  NOTE: ★{hi_conv[-1]} conviction picks outperforming ({hi_wr}% win) — prioritise high-conviction setups.")

    if type_lines:
        lines.append(f"  By asset type: {' | '.join(type_lines)}")

    if outcomes:
        out_str = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items(), key=lambda x: -x[1]))
        lines.append(f"  Outcomes: {out_str}")

    result = "\n".join(lines)
    print(f"[performance_context] Generated context: {len(all_trades)} trades, {win_rate}% win rate.")
    return result
