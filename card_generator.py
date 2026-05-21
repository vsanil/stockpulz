"""
card_generator.py — Generate a shareable performance card PNG for a user.

Produces a dark-themed 800×420 image showing:
  - Win rate (big number, color-coded)
  - Total closed trades & net P&L estimate
  - Streak (current winning or losing)
  - Best single trade
  - A small sparkline of cumulative return over closed trades

Returns the image as bytes (PNG) or None on failure.
"""

import io
from typing import Optional

# ── Colour palette ────────────────────────────────────────────────────────────
_BG      = "#0f0f1a"
_CARD    = "#1a1a2e"
_ACCENT  = "#7c6af7"   # purple
_GREEN   = "#2ecc71"
_RED     = "#e74c3c"
_GOLD    = "#f39c12"
_TEXT    = "#e8e8f0"
_MUTED   = "#8888aa"
_WHITE   = "#ffffff"


def generate_performance_card(chat_id: str) -> Optional[bytes]:
    """
    Build the performance card for a user.  Returns PNG bytes or None.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyBboxPatch
        import numpy as np
        from config_manager import load_user_trade_log
    except Exception as exc:
        print(f"[card_generator] import failed: {exc}")
        return None

    try:
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])
    except Exception as exc:
        print(f"[card_generator] log load failed: {exc}")
        return None

    # ── Compute stats ─────────────────────────────────────────────────────────
    total      = len(closed)
    rets       = []
    pnl_vals   = []
    for t in closed:
        r = t.get("return_pct")
        if r is not None:
            try:
                rets.append(float(r))
            except Exception:
                pass
        p = t.get("gain_usd") or t.get("pnl_usd")
        if p is not None:
            try:
                pnl_vals.append(float(p))
            except Exception:
                pass

    wins      = [r for r in rets if r > 0]
    losses    = [r for r in rets if r < 0]
    win_rate  = len(wins) / len(rets) * 100 if rets else 0
    avg_win   = sum(wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(losses) / len(losses) if losses else 0
    net_pnl   = sum(pnl_vals)
    best_ret  = max(rets)  if rets else 0
    worst_ret = min(rets)  if rets else 0

    # Best trade ticker
    best_trade = ""
    if rets:
        idx = rets.index(max(rets))
        if idx < len(closed):
            best_trade = closed[idx].get("ticker", "")

    # Streak
    streak = 0
    streak_label = ""
    if rets:
        streak_dir = "W" if rets[-1] > 0 else "L"
        for r in reversed(rets):
            if (r > 0 and streak_dir == "W") or (r <= 0 and streak_dir == "L"):
                streak += 1
            else:
                break
        streak_label = f"{streak}{'W' if streak_dir == 'W' else 'L'}"

    # Cumulative return sparkline (equal-weight per trade)
    cum_rets = []
    cumsum = 0.0
    for r in rets:
        cumsum += r
        cum_rets.append(cumsum)

    # ── Draw ──────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(8, 4.2), facecolor=_BG)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 4.2)
    ax.axis("off")
    ax.set_facecolor(_BG)

    def txt(x, y, s, size=12, color=_TEXT, weight="normal", ha="left", va="bottom", alpha=1.0):
        ax.text(x, y, s, fontsize=size, color=color, fontweight=weight,
                ha=ha, va=va, alpha=alpha,
                fontfamily="DejaVu Sans")

    # Background card
    card = FancyBboxPatch((0.15, 0.15), 7.7, 3.9,
                          boxstyle="round,pad=0.05",
                          linewidth=0, facecolor=_CARD, zorder=1)
    ax.add_patch(card)

    # Header bar
    header = FancyBboxPatch((0.15, 3.55), 7.7, 0.5,
                            boxstyle="round,pad=0.0",
                            linewidth=0, facecolor=_ACCENT, zorder=2)
    ax.add_patch(header)
    txt(4.0, 3.77, "StockPulz  ·  Performance Card",
        size=13, color=_WHITE, weight="bold", ha="center", va="center")

    # ── Big win-rate ──────────────────────────────────────────────────────────
    wr_color = _GREEN if win_rate >= 55 else (_GOLD if win_rate >= 45 else _RED)
    txt(1.1, 2.95, f"{win_rate:.0f}%", size=38, color=wr_color, weight="bold", ha="center")
    txt(1.1, 2.35, "Win Rate", size=11, color=_MUTED, ha="center")

    # Divider line
    ax.plot([2.3, 2.3], [0.35, 3.45], color=_MUTED, linewidth=0.5, alpha=0.4, zorder=3)

    # ── Stat grid (right side) ────────────────────────────────────────────────
    stats = [
        ("Trades",       str(total),                          _TEXT),
        ("Net P&L",      f"${net_pnl:+,.0f}" if pnl_vals else "—", _GREEN if net_pnl >= 0 else _RED),
        ("Avg Win",      f"+{avg_win:.1f}%"  if wins   else "—",    _GREEN),
        ("Avg Loss",     f"{avg_loss:.1f}%"  if losses else "—",    _RED),
        ("Best trade",   f"+{best_ret:.1f}% {best_trade}",          _GOLD),
        ("Streak",       streak_label or "—",                        _GREEN if streak_dir == "W" else _RED if rets else _MUTED),
    ]

    col_x = [2.55, 5.3]
    row_y = [3.0, 2.35, 1.7]
    for i, (label, val, color) in enumerate(stats):
        cx = col_x[i % 2]
        cy = row_y[i // 2]
        txt(cx, cy + 0.28, val,   size=15, color=color, weight="bold")
        txt(cx, cy,         label, size=9,  color=_MUTED)

    # ── Sparkline ─────────────────────────────────────────────────────────────
    if len(cum_rets) >= 2:
        spark_ax = fig.add_axes([0.06, 0.06, 0.88, 0.22])
        spark_ax.set_facecolor(_BG)
        spark_ax.axis("off")
        xs = list(range(len(cum_rets)))
        ys = cum_rets
        final = ys[-1]
        spline_color = _GREEN if final >= 0 else _RED
        spark_ax.plot(xs, ys, color=spline_color, linewidth=1.8, solid_capstyle="round")
        spark_ax.fill_between(xs, ys, alpha=0.15, color=spline_color)
        spark_ax.axhline(0, color=_MUTED, linewidth=0.5, alpha=0.4)
        spark_ax.text(0.01, 0.85, "Cumulative return across closed trades",
                      transform=spark_ax.transAxes,
                      fontsize=7, color=_MUTED, va="top")

    # ── Footer ────────────────────────────────────────────────────────────────
    txt(4.0, 0.25, "StockPulz · stockpulz.com",
        size=8, color=_MUTED, ha="center", alpha=0.6)

    # ── Export ────────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=_BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_trade_card(trade: dict) -> Optional[bytes]:
    """
    Generate a single-trade shareable PNG.

    Shows: ticker + return %, entry → exit, held days, P&L estimate,
    and a mini entry→exit price bar.
    Returns PNG bytes or None on failure.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch
    except Exception as exc:
        print(f"[card_generator] generate_trade_card import failed: {exc}")
        return None

    ticker    = (trade.get("ticker") or "?").upper()
    ret       = trade.get("return_pct")
    entry     = trade.get("entry_price")
    exit_p    = trade.get("exit_price") or trade.get("sold_price")
    held      = trade.get("held_days") or trade.get("days_held")
    pnl       = trade.get("gain_usd") or trade.get("pnl_usd")
    closed_d  = str(trade.get("closed_date") or trade.get("closed_at", ""))[:10]

    ret_f     = float(ret)  if ret    is not None else None
    entry_f   = float(entry) if entry is not None else None
    exit_f    = float(exit_p) if exit_p is not None else None
    pnl_f     = float(pnl)   if pnl   is not None else None

    win       = ret_f is not None and ret_f > 0
    ret_color = _GREEN if win else _RED
    ret_str   = f"{ret_f:+.1f}%" if ret_f is not None else "?"

    fig = plt.figure(figsize=(6, 3.2), facecolor=_BG)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 6)
    ax.set_ylim(0, 3.2)
    ax.axis("off")
    ax.set_facecolor(_BG)

    def txt(x, y, s, size=11, color=_TEXT, weight="normal", ha="left", va="bottom", alpha=1.0):
        ax.text(x, y, s, fontsize=size, color=color, fontweight=weight,
                ha=ha, va=va, alpha=alpha, fontfamily="DejaVu Sans")

    # Background card
    card = FancyBboxPatch((0.1, 0.1), 5.8, 3.0,
                          boxstyle="round,pad=0.05",
                          linewidth=0, facecolor=_CARD, zorder=1)
    ax.add_patch(card)

    # Header bar
    hdr_color = _GREEN if win else _RED
    header = FancyBboxPatch((0.1, 2.65), 5.8, 0.45,
                            boxstyle="round,pad=0.0",
                            linewidth=0, facecolor=hdr_color, alpha=0.85, zorder=2)
    ax.add_patch(header)
    emoji = "✅" if win else "❌"
    txt(3.0, 2.87, f"{emoji}  StockPulz  ·  Trade Result",
        size=11, color=_WHITE, weight="bold", ha="center", va="center")

    # Big ticker + return
    txt(1.5, 2.1, ticker, size=32, color=_WHITE, weight="bold", ha="center")
    txt(4.2, 2.1, ret_str, size=32, color=ret_color, weight="bold", ha="center")

    # Entry → Exit row
    entry_str = f"${entry_f:,.2f}" if entry_f else "—"
    exit_str  = f"${exit_f:,.2f}"  if exit_f  else "—"
    txt(3.0, 1.55, f"{entry_str}  →  {exit_str}",
        size=12, color=_MUTED, ha="center")

    # Stats row
    stat_parts = []
    if held:    stat_parts.append(f"held {held}d")
    if pnl_f is not None:
        sign = "+" if pnl_f >= 0 else ""
        stat_parts.append(f"est. {sign}${abs(pnl_f):,.0f}")
    if closed_d: stat_parts.append(closed_d)
    if stat_parts:
        txt(3.0, 1.15, "  ·  ".join(stat_parts),
            size=10, color=_MUTED, ha="center", alpha=0.85)

    # Mini entry→exit bar
    if entry_f and exit_f:
        bar_ax = fig.add_axes([0.12, 0.12, 0.76, 0.22])
        bar_ax.set_facecolor(_BG)
        bar_ax.axis("off")
        lo, hi = min(entry_f, exit_f), max(entry_f, exit_f)
        span   = hi - lo if hi > lo else 1.0
        bar_ax.barh(0, 1.0, color=_MUTED, alpha=0.15, height=0.6)
        fill = (exit_f - entry_f) / (span * 1.2) if win else (entry_f - exit_f) / (span * 1.2)
        fill = max(0.05, min(fill, 1.0))
        bar_ax.barh(0, fill, color=ret_color, alpha=0.7, height=0.6)
        bar_ax.set_xlim(0, 1)
        bar_ax.set_ylim(-0.5, 0.5)

    # Footer
    txt(3.0, 0.28, "StockPulz · stockpulz.com",
        size=7, color=_MUTED, ha="center", alpha=0.5)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=_BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
