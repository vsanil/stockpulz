"""
paper_trader.py — Simulated paper trading portfolio stored in GitHub Gist.
Per-user: each user has their own paper portfolio, keyed by chat_id.

Paper trades let you test the bot's picks without risking real money.
All commands mirror the real /bought /sold /portfolio flow but work on
a separate per-user paper portfolio stored in user_paper.json.

Telegram commands:
  /paper_buy AAPL 10        → simulate buying 10 shares of AAPL at live price
  /paper_buy AAPL 182.50 10 → simulate buying 10 shares at $182.50
  /paper_sell AAPL          → simulate selling entire AAPL position at live price
  /paper_portfolio          → show current paper portfolio + unrealized P&L
  /paper_perf               → show paper trading win rate, total return
  /paper_add_cash 5000      → add cash to paper portfolio
  /paper_reset              → wipe paper portfolio (start over)
"""

from datetime import date
import yfinance as yf
from config_manager import load_user_paper, save_user_paper


def _live_price(ticker: str) -> float | None:
    try:
        return float(yf.Ticker(ticker).fast_info.last_price)
    except Exception:
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def paper_buy(ticker: str, shares: float, chat_id: str, price: float | None = None) -> str:
    """Simulate buying shares for a user. Returns Telegram-formatted confirmation."""
    ticker = ticker.upper()
    live   = _live_price(ticker)
    if live is None:
        return f"❌ Could not fetch price for <b>{ticker}</b>."

    buy_price = price if price else live
    cost      = round(buy_price * shares, 2)

    data = load_user_paper(chat_id)
    if cost > data["cash"]:
        return (f"❌ Insufficient paper cash.\n"
                f"Need ${cost:,.2f}, have ${data['cash']:,.2f}")

    # Check if already holding this ticker
    existing = next((p for p in data["positions"] if p["ticker"] == ticker), None)
    if existing:
        # Average down/up
        total_shares = existing["shares"] + shares
        avg_price    = (existing["avg_price"] * existing["shares"] + buy_price * shares) / total_shares
        existing["shares"]    = round(total_shares, 4)
        existing["avg_price"] = round(avg_price, 2)
        existing["cost_basis"] = round(avg_price * total_shares, 2)
    else:
        data["positions"].append({
            "ticker":      ticker,
            "shares":      round(shares, 4),
            "avg_price":   round(buy_price, 2),
            "cost_basis":  cost,
            "bought_date": date.today().isoformat(),
        })

    data["cash"] = round(data["cash"] - cost, 2)
    save_user_paper(chat_id, data)

    return (
        f"📄 <b>Paper Buy</b>\n"
        f"<b>{ticker}</b> × {shares} shares @ ${buy_price:,.2f}\n"
        f"Cost: <b>${cost:,.2f}</b>\n"
        f"Remaining cash: ${data['cash']:,.2f}"
    )


def paper_sell(ticker: str, chat_id: str, shares: float | None = None,
               price: float | None = None) -> str:
    """Simulate selling for a user. If shares=None, sells entire position."""
    ticker = ticker.upper()
    live   = _live_price(ticker)
    if live is None:
        return f"❌ Could not fetch price for <b>{ticker}</b>."

    sell_price = price if price else live
    data       = load_user_paper(chat_id)
    position   = next((p for p in data["positions"] if p["ticker"] == ticker), None)

    if not position:
        return f"❌ No open paper position for <b>{ticker}</b>."

    sell_shares = shares if shares else position["shares"]
    if sell_shares > position["shares"]:
        sell_shares = position["shares"]

    proceeds   = round(sell_price * sell_shares, 2)
    cost       = round(position["avg_price"] * sell_shares, 2)
    gain       = round(proceeds - cost, 2)
    gain_pct   = round(gain / cost * 100, 2) if cost > 0 else 0

    # Record in history
    data["history"].append({
        "ticker":      ticker,
        "shares":      sell_shares,
        "buy_price":   position["avg_price"],
        "sell_price":  round(sell_price, 2),
        "gain":        gain,
        "gain_pct":    gain_pct,
        "closed_date": date.today().isoformat(),
    })

    # Update position
    remaining = round(position["shares"] - sell_shares, 4)
    if remaining <= 0.001:
        data["positions"] = [p for p in data["positions"] if p["ticker"] != ticker]
    else:
        position["shares"]    = remaining
        position["cost_basis"] = round(position["avg_price"] * remaining, 2)

    data["cash"] = round(data["cash"] + proceeds, 2)
    save_user_paper(chat_id, data)

    emoji = "✅" if gain >= 0 else "❌"
    return (
        f"📄 <b>Paper Sell</b>\n"
        f"<b>{ticker}</b> × {sell_shares} shares @ ${sell_price:,.2f}\n"
        f"Proceeds: ${proceeds:,.2f} | {emoji} <b>{gain_pct:+.1f}%</b> (${gain:+.2f})\n"
        f"Cash: ${data['cash']:,.2f}"
    )


def paper_portfolio(chat_id: str) -> str:
    """Show a user's current paper portfolio with unrealized P&L."""
    data      = load_user_paper(chat_id)
    positions = data["positions"]

    if not positions:
        return (
            "📄 <b>Paper Portfolio</b>\n\n"
            "No open positions.\n"
            f"Available cash: <b>${data['cash']:,.2f}</b>\n\n"
            "Use /paper_buy to simulate a trade."
        )

    lines      = [f"📄 <b>PAPER PORTFOLIO</b>\n"]
    total_val  = 0.0
    total_cost = 0.0

    for p in positions:
        live = _live_price(p["ticker"])
        if live is None:
            live = p["avg_price"]
        val      = live * p["shares"]
        cost     = p["avg_price"] * p["shares"]
        unrealzd = val - cost
        pct      = unrealzd / cost * 100 if cost > 0 else 0
        emoji    = "📈" if unrealzd >= 0 else "📉"
        total_val  += val
        total_cost += cost

        lines.append(
            f"{emoji} <b>{p['ticker']}</b> × {p['shares']} "
            f"@ ${p['avg_price']:,.2f} → <b>${live:,.2f}</b>  "
            f"<b>{pct:+.1f}%</b> (${unrealzd:+.2f})"
        )

    total_unrealized = total_val - total_cost
    total_pct        = total_unrealized / total_cost * 100 if total_cost > 0 else 0
    portfolio_value  = total_val + data["cash"]
    starting         = data["starting_cash"]
    overall_return   = (portfolio_value - starting) / starting * 100

    lines += [
        "",
        f"💵 Cash: <b>${data['cash']:,.2f}</b>",
        f"📊 Positions value: <b>${total_val:,.2f}</b> ({total_pct:+.1f}% unrealized)",
        f"🏦 Total portfolio: <b>${portfolio_value:,.2f}</b>",
        f"📈 Overall return: <b>{overall_return:+.1f}%</b> vs ${starting:,.0f} start",
    ]

    return "\n".join(lines)


def paper_performance(chat_id: str) -> str:
    """Show paper trading win rate and P&L stats from a user's closed trades."""
    data    = load_user_paper(chat_id)
    history = data["history"]

    if not history:
        return (
            "📄 <b>Paper Trading Performance</b>\n\n"
            "No closed trades yet. Use /paper_sell to close a position."
        )

    wins     = [t for t in history if t["gain"] >= 0]
    losses   = [t for t in history if t["gain"] < 0]
    total_gl = sum(t["gain"] for t in history)
    avg_ret  = sum(t["gain_pct"] for t in history) / len(history)
    win_rate = len(wins) / len(history) * 100
    best     = max(history, key=lambda t: t["gain_pct"])
    worst    = min(history, key=lambda t: t["gain_pct"])

    portfolio_value = sum(_live_price(p["ticker"]) or p["avg_price"] * p["shares"]
                         for p in data["positions"]) + data["cash"]
    starting = data["starting_cash"]
    total_ret = (portfolio_value - starting) / starting * 100

    return (
        f"📄 <b>PAPER TRADING PERFORMANCE</b>\n\n"
        f"<b>Closed trades:</b> {len(history)} "
        f"({len(wins)}W / {len(losses)}L)\n"
        f"<b>Win rate:</b> <b>{win_rate:.1f}%</b>\n"
        f"<b>Avg return:</b> {avg_ret:+.1f}%\n"
        f"<b>Total P&L:</b> ${total_gl:+.2f}\n\n"
        f"📈 Best: <b>{best['ticker']}</b> {best['gain_pct']:+.1f}%\n"
        f"📉 Worst: <b>{worst['ticker']}</b> {worst['gain_pct']:+.1f}%\n\n"
        f"🏦 Portfolio return: <b>{total_ret:+.1f}%</b>\n"
        f"<i>(vs ${starting:,.0f} starting capital)</i>"
    )


def paper_add_cash(amount: float, chat_id: str) -> str:
    """Add cash to a user's paper portfolio (and increase starting_cash baseline)."""
    if amount <= 0:
        return "❌ Amount must be greater than zero."
    data = load_user_paper(chat_id)
    data["cash"]          = round(data["cash"] + amount, 2)
    data["starting_cash"] = round(data["starting_cash"] + amount, 2)
    save_user_paper(chat_id, data)
    return (
        f"💵 <b>Cash Added</b>\n"
        f"Added: <b>${amount:,.2f}</b>\n"
        f"Available cash: <b>${data['cash']:,.2f}</b>\n"
        f"Starting baseline: ${data['starting_cash']:,.2f}"
    )


def paper_reset(chat_id: str, starting_cash: float | None = None) -> str:
    """Wipe a user's paper portfolio and start fresh with the given cash (or keep existing amount)."""
    current = load_user_paper(chat_id)
    amount  = starting_cash if starting_cash is not None else current.get("starting_cash", 10_000.0)
    save_user_paper(chat_id, {"positions": [], "history": [], "starting_cash": amount, "cash": amount})
    return f"🔄 Paper portfolio reset. Starting cash: <b>${amount:,.2f}</b>"


def paper_cancel(ticker: str, chat_id: str) -> str:
    """Remove a paper position without recording it as a sale (undo an accidental paper_buy)."""
    ticker  = ticker.upper()
    data    = load_user_paper(chat_id)
    positions = data.get("positions", [])
    match   = next((p for p in positions if p["ticker"] == ticker), None)
    if not match:
        return f"⚠️ No paper position found for <b>{ticker}</b>."
    # Refund the cash
    cost         = float(match.get("entry_price", 0)) * float(match.get("shares", 0))
    data["cash"] = round(data.get("cash", 0) + cost, 2)
    data["positions"] = [p for p in positions if p["ticker"] != ticker]
    save_user_paper(chat_id, data)
    return (
        f"🗑 <b>Paper position removed: {ticker}</b>\n"
        f"<code>${cost:,.2f}</code> refunded to your paper cash.\n"
        f"<i>Not counted as a sale — use /paper_sell to record a simulated trade.</i>"
    )


def paper_edit(ticker: str, chat_id: str, new_price: float | None = None,
               new_shares: float | None = None) -> str:
    """Edit entry price or shares on an existing paper position."""
    ticker    = ticker.upper()
    data      = load_user_paper(chat_id)
    positions = data.get("positions", [])
    match     = next((p for p in positions if p["ticker"] == ticker), None)
    if not match:
        return f"⚠️ No paper position found for <b>{ticker}</b>."

    old_price  = float(match.get("entry_price", 0))
    old_shares = float(match.get("shares", 0))
    old_cost   = old_price * old_shares

    if new_price is not None:
        match["entry_price"] = round(new_price, 4)
    if new_shares is not None:
        match["shares"] = round(new_shares, 6)

    new_cost     = float(match["entry_price"]) * float(match["shares"])
    cash_delta   = old_cost - new_cost          # positive = cash returned, negative = cash used
    data["cash"] = round(data.get("cash", 0) + cash_delta, 2)
    save_user_paper(chat_id, data)

    changes = []
    if new_price  is not None: changes.append(f"price → <code>${new_price}</code>")
    if new_shares is not None: changes.append(f"shares → <code>{new_shares}</code>")
    return (
        f"✏️ <b>Paper {ticker} updated:</b> {', '.join(changes)}\n"
        f"Cash adjusted by <code>{'+' if cash_delta >= 0 else ''}{cash_delta:,.2f}</code>"
    )
