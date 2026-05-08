"""
trade_logger.py — Persistent trade tracking for short-term picks.
Per-user: each user's trades are stored separately, keyed by chat_id.

Lifecycle:
  Morning run   → open_trades(picks, chat_id)          — log each ST pick
  Confirmation  → check_and_close_trades(prices, chat_id) — close if target/stop hit
  /perf command → get_performance_stats(chat_id)       — all-time stats
  Saturday      → get_weekly_closed_trades(chat_id)    — this week's closed trades

Only short-term picks are tracked (they have clear entry/target/stop).
Long-term DCA picks are tracked separately via weekly_picks.json.
Trades expire after 28 days if neither target nor stop is hit.
"""

from datetime import date
from config_manager import load_user_trade_log, save_user_trade_log


# ── Open trades (morning run) ─────────────────────────────────────────────────

def open_trades(picks: dict, chat_id: str) -> None:
    """
    Add today's short-term picks (stocks + crypto) to a user's open trades list.
    Skips duplicates — safe to call multiple times.
    """
    today = date.today().isoformat()
    log   = load_user_trade_log(chat_id)

    open_tickers = {t["ticker"] for t in log["open"]}
    new_count    = 0

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})

    for s in stocks.get("short_term", []):
        ticker = s.get("ticker")
        if not ticker or ticker in open_tickers:
            continue
        entry = s.get("entry_price")
        log["open"].append({
            "ticker":             ticker,
            "asset_type":         "stock",
            "entry_price":        entry,
            "target_price":       s.get("target_price"),
            "stop_loss":          s.get("stop_loss"),
            "trailing_stop_pct":  s.get("trailing_stop_pct"),   # None = use fixed stop only
            "highest_price_seen": entry,                         # high-water mark for trailing stop
            "allocation":         s.get("allocation"),
            "conviction":         s.get("conviction"),
            "thesis":             s.get("thesis", ""),
            "opened_date":        today,
        })
        open_tickers.add(ticker)
        new_count += 1

    for c in crypto.get("short_term", []):
        ticker = c.get("symbol", "").upper()
        if not ticker or ticker in open_tickers:
            continue
        log["open"].append({
            "ticker":       ticker,
            "asset_type":   "crypto",
            "entry_price":  c.get("entry_price"),
            "target_price": c.get("target_price"),
            "stop_loss":    c.get("stop_loss"),
            "allocation":   c.get("allocation"),
            "conviction":   c.get("conviction"),
            "thesis":       c.get("thesis", ""),
            "opened_date":  today,
        })
        open_tickers.add(ticker)
        new_count += 1

    if new_count:
        save_user_trade_log(chat_id, log)
        print(f"[trade_logger] Opened {new_count} new trade(s) for {chat_id}.")
    else:
        print(f"[trade_logger] No new trades to open for {chat_id} (already logged or no picks).")


# ── Close trades (confirmation run) ──────────────────────────────────────────

def check_and_close_trades(current_prices: dict, chat_id: str) -> list[dict]:
    """
    Compare open trades against current prices for a specific user.
    Closes trades where target hit, stop hit, or open > 28 days.
    Returns list of newly closed trades (empty if none).
    """
    today = date.today().isoformat()
    log   = load_user_trade_log(chat_id)

    if not log["open"]:
        return []

    still_open    = []
    newly_closed  = []

    for trade in log["open"]:
        ticker  = trade["ticker"]
        current = current_prices.get(ticker)

        if current is None:
            still_open.append(trade)
            continue

        entry  = trade.get("entry_price")
        target = trade.get("target_price")
        stop   = trade.get("stop_loss")

        if not entry:
            still_open.append(trade)
            continue

        outcome = None

        # Check stop first (worst case priority)
        if stop and float(current) <= float(stop):
            outcome = "stop"
        elif target and float(current) >= float(target):
            outcome = "target"
        else:
            # Expire after 28 calendar days
            try:
                days_open = (date.today() - date.fromisoformat(trade["opened_date"])).days
                if days_open >= 28:
                    outcome = "expired"
            except Exception:
                pass

        if outcome:
            return_pct  = (float(current) - float(entry)) / float(entry) * 100
            allocation  = float(trade.get("allocation") or 0)
            gain_usd    = round(allocation * return_pct / 100, 2)
            closed      = {
                **trade,
                "closed_date":  today,
                "closed_price": round(float(current), 2),
                "outcome":      outcome,
                "return_pct":   round(return_pct, 2),
                "gain_usd":     gain_usd,
            }
            log["closed"].append(closed)
            newly_closed.append(closed)
            print(f"[trade_logger] Closed {ticker} — {outcome} "
                  f"@ ${current} ({return_pct:+.1f}%) for {chat_id}")
        else:
            still_open.append(trade)

    if newly_closed:
        log["open"] = still_open
        save_user_trade_log(chat_id, log)

    return newly_closed


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_performance_stats(chat_id: str, asset_type: str | None = None) -> dict | None:
    """
    Compute all-time stats from closed trades for a specific user.
    Pass asset_type="stock" or "crypto" to filter, or None for combined.
    """
    log    = load_user_trade_log(chat_id)
    closed = log.get("closed", [])

    if asset_type:
        closed = [t for t in closed if t.get("asset_type") == asset_type]

    if not closed:
        return None

    returns          = [(t["ticker"], t["return_pct"]) for t in closed]
    wins             = sum(1 for _, r in returns if r > 0)
    avg_return       = sum(r for _, r in returns) / len(returns)
    total_gain_usd   = sum(t.get("gain_usd", 0) for t in closed)
    total_deployed   = sum(t.get("allocation", 0) for t in closed)
    open_count       = len(log.get("open", []))

    by_outcome = {}
    for t in closed:
        o = t.get("outcome", "unknown")
        by_outcome[o] = by_outcome.get(o, 0) + 1

    # Streak: consecutive wins from most recent closed trade
    sorted_by_date = sorted(closed, key=lambda t: t.get("closed_date", ""), reverse=True)
    streak = 0
    for t in sorted_by_date:
        if t["return_pct"] > 0:
            streak += 1
        else:
            break

    # Cumulative return % = total gain / total deployed
    cum_return_pct = round(total_gain_usd / total_deployed * 100, 1) if total_deployed > 0 else 0.0

    return {
        "count":                len(closed),
        "wins":                 wins,
        "win_rate":             round(wins / len(closed) * 100),
        "avg_return":           round(avg_return, 1),
        "best":                 max(returns, key=lambda x: x[1]),
        "worst":                min(returns, key=lambda x: x[1]),
        "total_gain_usd":       round(total_gain_usd, 2),
        "total_deployed_usd":   round(total_deployed, 2),
        "cumulative_return_pct": cum_return_pct,
        "streak":               streak,
        "targets_hit":          by_outcome.get("target", 0),
        "stops_hit":            by_outcome.get("stop", 0),
        "expired":              by_outcome.get("expired", 0),
        "open_count":           open_count,
    }


def manual_open_trade(ticker: str, bought_price: float, chat_id: str,
                      asset_type: str = "stock",
                      shares: float | None = None, allocation: float | None = None,
                      target_price: float | None = None, stop_loss: float | None = None,
                      stop_loss_pct: float = 7.0, target_gain_pct: float = 15.0) -> dict:
    """
    Log a trade the user actually placed.
    If target/stop not provided, uses stop_loss_pct / target_gain_pct (caller should pass
    per-user values; defaults are 7% stop and 15% target).
    Returns the trade dict that was saved.
    """
    today = date.today().isoformat()
    log   = load_user_trade_log(chat_id)

    # Default target/stop from per-user thresholds
    if target_price is None:
        target_price = round(bought_price * (1 + target_gain_pct / 100), 2)
    if stop_loss is None:
        stop_loss = round(bought_price * (1 - stop_loss_pct / 100), 2)

    # Derive allocation from shares if provided
    if allocation is None and shares is not None:
        allocation = round(bought_price * shares, 2)

    trade = {
        "ticker":       ticker.upper(),
        "asset_type":   asset_type,
        "entry_price":  round(bought_price, 2),
        "target_price": target_price,
        "stop_loss":    stop_loss,
        "allocation":   allocation,
        "conviction":   None,
        "thesis":       "Manually logged",
        "opened_date":  today,
        "shares":       shares,
        "manual":       True,
    }

    # Remove existing open entry for same ticker to avoid duplicates
    log["open"] = [t for t in log["open"] if t["ticker"] != ticker.upper()]
    log["open"].append(trade)
    save_user_trade_log(chat_id, log)
    print(f"[trade_logger] Manually opened {ticker.upper()} @ ${bought_price} for {chat_id}")
    return trade


def manual_close_trade(ticker: str, sold_price: float, chat_id: str,
                       shares_sold: float | None = None) -> dict | None:
    """
    Log that the user sold a position (full or partial).
    - shares_sold=None  → close the entire position
    - shares_sold=N     → partial close: reduce open shares, log closed portion

    Returns closed trade dict (with partial=True when applicable), or None if not found.
    """
    today = date.today().isoformat()
    log   = load_user_trade_log(chat_id)

    match = None
    remaining = []
    for t in log["open"]:
        if t["ticker"] == ticker.upper() and match is None:
            match = t
        else:
            remaining.append(t)

    if not match:
        return None

    entry          = float(match.get("entry_price") or sold_price)
    total_shares   = match.get("shares")
    total_alloc    = float(match.get("allocation") or 0)

    # Infer total_shares from allocation if needed
    if total_shares is None and total_alloc and entry:
        total_shares = total_alloc / entry

    # Determine how many shares are being sold
    if shares_sold is None or total_shares is None:
        # Full close
        sold_shares  = total_shares
        closed_alloc = total_alloc
        partial      = False
        log["open"]  = remaining          # remove from open
    else:
        sold_shares  = min(float(shares_sold), float(total_shares))
        frac         = sold_shares / float(total_shares)
        closed_alloc = round(total_alloc * frac, 2) if total_alloc else round(entry * sold_shares, 2)
        partial      = sold_shares < float(total_shares)
        if partial:
            # Keep the remaining shares in open
            leftover_shares = round(float(total_shares) - sold_shares, 6)
            leftover_alloc  = round(total_alloc - closed_alloc, 2) if total_alloc else round(entry * leftover_shares, 2)
            updated = {**match, "shares": leftover_shares, "allocation": leftover_alloc}
            log["open"] = remaining + [updated]
        else:
            log["open"] = remaining       # full close

    if not closed_alloc and sold_shares:
        closed_alloc = round(entry * float(sold_shares), 2)

    return_pct = (sold_price - entry) / entry * 100
    gain_usd   = round(closed_alloc * return_pct / 100, 2)

    closed = {
        **match,
        "closed_date":  today,
        "closed_price": round(sold_price, 2),
        "outcome":      "manual",
        "return_pct":   round(return_pct, 2),
        "gain_usd":     gain_usd,
        "shares":       round(float(sold_shares), 6) if sold_shares else match.get("shares"),
        "allocation":   closed_alloc,
        "partial":      partial,
    }
    log["closed"].append(closed)
    save_user_trade_log(chat_id, log)
    tag = " (partial)" if partial else ""
    print(f"[trade_logger] Manually closed {ticker.upper()} @ ${sold_price} ({return_pct:+.1f}%){tag} for {chat_id}")
    return closed


def cancel_trade(ticker: str, chat_id: str) -> dict | None:
    """
    Remove an open position without recording it as a closed trade.
    Used to undo an accidental /bought. Returns removed trade or None if not found.
    """
    log   = load_user_trade_log(chat_id)
    match = None
    remaining = []
    for t in log["open"]:
        if t["ticker"] == ticker.upper() and match is None:
            match = t
        else:
            remaining.append(t)
    if match:
        log["open"] = remaining
        save_user_trade_log(chat_id, log)
        print(f"[trade_logger] Cancelled open position: {ticker.upper()} for {chat_id}")
    return match


def reopen_trade(ticker: str, chat_id: str) -> dict | None:
    """
    Move the most recently closed trade for a ticker back to open.
    Used to undo an accidental /sold. Returns reopened trade or None if not found.
    """
    log    = load_user_trade_log(chat_id)
    closed = log.get("closed", [])

    # Find the most recent closed entry for this ticker
    match_idx = None
    for i in range(len(closed) - 1, -1, -1):
        if closed[i]["ticker"] == ticker.upper():
            match_idx = i
            break

    if match_idx is None:
        return None

    trade = closed.pop(match_idx)
    # Strip closed-only fields before moving back to open
    for field in ("closed_date", "closed_price", "outcome", "return_pct", "gain_usd"):
        trade.pop(field, None)

    log["closed"] = closed
    log["open"].append(trade)
    save_user_trade_log(chat_id, log)
    print(f"[trade_logger] Reopened position: {ticker.upper()} for {chat_id}")
    return trade


def update_trailing_stops(current_prices: dict, chat_id: str,
                          default_trailing_pct: float = 5.0) -> list[dict]:
    """
    Update high-water marks and evaluate trailing stops for a user's open trades.
    For each trade:
      1. If current price > highest_price_seen → update high-water mark
      2. Compute trailing stop = highest_price_seen * (1 - trailing_pct / 100)
      3. If current price <= trailing stop → close the trade (outcome = "trailing_stop")

    Returns list of newly closed trades (same format as check_and_close_trades).
    Only applies to trades where trailing_stop_pct is set, or uses default_trailing_pct.
    """
    today = date.today().isoformat()
    log   = load_user_trade_log(chat_id)

    if not log["open"]:
        return []

    still_open    = []
    newly_closed  = []
    hwm_updated   = False   # track if any high-water mark changed

    for trade in log["open"]:
        ticker  = trade["ticker"]
        current = current_prices.get(ticker)

        if current is None:
            still_open.append(trade)
            continue

        current = float(current)
        entry   = float(trade.get("entry_price") or current)

        # Update high-water mark
        hwm = float(trade.get("highest_price_seen") or entry)
        if current > hwm:
            trade["highest_price_seen"] = round(current, 2)
            hwm         = current
            hwm_updated = True

        # Compute trailing stop level
        trail_pct     = float(trade.get("trailing_stop_pct") or default_trailing_pct)
        trailing_stop = hwm * (1 - trail_pct / 100)

        # Only trigger if price is up from entry (trailing stop only locks in gains)
        # Below entry, the regular stop_loss in check_and_close_trades handles it
        if current >= entry and current <= trailing_stop:
            return_pct = (current - entry) / entry * 100
            allocation = float(trade.get("allocation") or 0)
            gain_usd   = round(allocation * return_pct / 100, 2)
            closed = {
                **trade,
                "closed_date":    today,
                "closed_price":   round(current, 2),
                "outcome":        "trailing_stop",
                "trailing_stop_level": round(trailing_stop, 2),
                "highest_reached": round(hwm, 2),
                "return_pct":     round(return_pct, 2),
                "gain_usd":       gain_usd,
            }
            log["closed"].append(closed)
            newly_closed.append(closed)
            print(f"[trade_logger] Trailing stop hit: {ticker} @ ${current:.2f} "
                  f"(trail stop ${trailing_stop:.2f}, peak ${hwm:.2f}, {return_pct:+.1f}%) for {chat_id}")
        else:
            still_open.append(trade)

    if newly_closed:
        log["open"] = still_open
        save_user_trade_log(chat_id, log)
    elif hwm_updated:
        # Save updated high-water marks even if no trailing stop triggered
        log["open"] = still_open
        save_user_trade_log(chat_id, log)

    return newly_closed


def get_weekly_closed_trades(chat_id: str) -> list[dict]:
    """Return trades closed this calendar week (Mon–today) for a specific user."""
    from datetime import timedelta
    today    = date.today()
    week_start = today - timedelta(days=today.weekday())   # Monday

    log    = load_user_trade_log(chat_id)
    closed = log.get("closed", [])

    result = []
    for t in closed:
        try:
            d = date.fromisoformat(t.get("closed_date", ""))
            if week_start <= d <= today:
                result.append(t)
        except ValueError:
            pass
    return result
