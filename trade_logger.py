"""
trade_logger.py — Persistent trade tracking for short-term picks.
Per-user: each user's trades are stored separately, keyed by chat_id.

Lifecycle:
  Morning run   → open_trades(picks, chat_id)          — log each ST pick
  Confirmation  → check_and_close_trades(prices, chat_id) — close if target/stop hit
  Saturday      → get_weekly_closed_trades(chat_id)    — this week's closed trades
  /bought       → add_holding(ticker, chat_id)         — user-added portfolio holding
  /sold         → remove_holding(ticker, chat_id)      — remove holding from portfolio

Only short-term picks are tracked (they have clear entry/target/stop).
Long-term DCA picks are tracked separately via weekly_picks.json.
Trades expire after 28 days if neither target nor stop is hit.
"""
from __future__ import annotations

from datetime import date
from config_manager import (load_user_trade_log, et_today,
                            mutate_user_trade_log, NO_WRITE)


# ── Open trades (morning run) ─────────────────────────────────────────────────

def open_trades(picks: dict, chat_id: str) -> None:
    """
    Add today's short-term and long-term picks (stocks + crypto) to a user's open trades list.
    Skips duplicates — safe to call multiple times.
    """
    today = et_today().isoformat()

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})

    # The duplicate check must run against the log as it stands at WRITE time —
    # this runs in a scheduled job, so a user logging a position from the
    # mini-app in the same window was previously erased by this save.
    def _mut(log):
        log = log or {"open": [], "closed": [], "watchlist": []}
        log.setdefault("open", [])
        open_tickers = {t["ticker"] for t in log["open"]}
        new_count    = 0

        for timeframe in ("short_term", "long_term"):
            for s in stocks.get(timeframe, []):
                ticker = s.get("ticker")
                if not ticker or ticker in open_tickers:
                    continue
                log["open"].append({
                    "ticker":       ticker,
                    "asset_type":   "stock",
                    "entry_price":  s.get("entry_price"),
                    "target_price": s.get("target_price"),
                    "stop_loss":    s.get("stop_loss"),
                    "allocation":   s.get("allocation"),
                    "conviction":   s.get("conviction"),
                    "thesis":       s.get("thesis", ""),
                    "timeframe":    timeframe,
                    "opened_date":  today,
                })
                open_tickers.add(ticker)
                new_count += 1

        for timeframe in ("short_term", "long_term"):
            for c in crypto.get(timeframe, []):
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
                    "timeframe":    timeframe,
                    "opened_date":  today,
                })
                open_tickers.add(ticker)
                new_count += 1

        if not new_count:
            return NO_WRITE, 0
        return log, new_count

    new_count = mutate_user_trade_log(chat_id, _mut)
    if new_count:
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
    today = et_today().isoformat()

    # Which trades qualify depends on the CURRENT open list — this runs in the
    # confirmation job, so evaluating a snapshot taken earlier could close a
    # position the user already closed, or drop one they just opened.
    def _mut(log):
        log = log or {"open": [], "closed": [], "watchlist": []}
        log.setdefault("open", [])
        log.setdefault("closed", [])
        if not log["open"]:
            return NO_WRITE, []

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
                    days_open = (et_today() - date.fromisoformat(trade["opened_date"])).days
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
            else:
                still_open.append(trade)

        if not newly_closed:
            return NO_WRITE, []
        log["open"] = still_open
        return log, newly_closed

    newly_closed = mutate_user_trade_log(chat_id, _mut)
    # Logged AFTER the write lands, so a CAS retry can't print a close twice.
    for c in newly_closed:
        print(f"[trade_logger] Closed {c['ticker']} — {c['outcome']} "
              f"@ ${c['closed_price']} ({c['return_pct']:+.1f}%) for {chat_id}")

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

    all_returns      = [float(t["return_pct"]) for t in closed]
    returns          = [(t["ticker"], t["return_pct"]) for t in closed]
    win_rets         = [r for r in all_returns if r > 0]
    loss_rets        = [r for r in all_returns if r <= 0]
    wins             = len(win_rets)
    avg_return       = sum(all_returns) / len(all_returns)
    avg_gain         = sum(win_rets)  / len(win_rets)  if win_rets  else 0.0
    avg_loss         = sum(loss_rets) / len(loss_rets) if loss_rets else 0.0
    win_rate_frac    = wins / len(closed)
    expectancy       = win_rate_frac * avg_gain + (1 - win_rate_frac) * avg_loss
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
        "count":                 len(closed),
        "wins":                  wins,
        "losses":                len(loss_rets),
        "win_rate":              round(win_rate_frac * 100),
        "avg_return":            round(avg_return, 1),
        "avg_gain":              round(avg_gain, 1),
        "avg_loss":              round(avg_loss, 1),
        "expectancy":            round(expectancy, 2),
        "best":                  max(returns, key=lambda x: x[1]),
        "worst":                 min(returns, key=lambda x: x[1]),
        "total_gain_usd":        round(total_gain_usd, 2),
        "total_deployed_usd":    round(total_deployed, 2),
        "cumulative_return_pct": cum_return_pct,
        "streak":                streak,
        "targets_hit":           by_outcome.get("target", 0),
        "stops_hit":             by_outcome.get("stop", 0),
        "expired":               by_outcome.get("expired", 0),
        "open_count":            open_count,
    }


def add_holding(ticker: str, chat_id: str, picks: dict | None = None,
                entry_override=None, stop_override=None,
                shares_override=None, timeframe_override=None,
                target_override=None, asset_type_override=None,
                source: str | None = None, levels_source: str | None = None) -> tuple[dict, bool]:
    """
    Add a ticker to a user's portfolio (no price/qty needed).
    Uses today's pick levels for target/stop alerts if available.
    entry_override / stop_override / target_override: user-supplied prices from
    the Mini App that take priority over (or fill in for) AI pick levels.
    Returns (trade_dict, already_existed).
    """
    today  = et_today().isoformat()
    ticker = ticker.upper()

    # Everything below up to `trade` is read-only preparation (pick lookup, user
    # settings, override resolution) — it does NOT touch the trade log, so it is
    # deliberately done BEFORE the mutator. Only the duplicate check and the
    # append happen against the freshly-read log, so a concurrent write to the
    # same user (a scheduled job) can no longer be clobbered by this one.

    # Look up pick levels from today's picks
    entry = target = stop = None
    asset_type = "stock"
    timeframe  = None
    _SECTION_TYPE = {"stocks": "stock", "crypto": "crypto", "etfs": "etf", "commodities": "commodity"}
    if picks:
        for section_name, section in [("stocks", picks.get("stocks", {})),
                                       ("crypto", picks.get("crypto", {})),
                                       ("etfs",   picks.get("etfs",   {})),
                                       ("commodities", picks.get("commodities", {}))]:
            for tf in ("short_term", "long_term"):
                for p in section.get(tf, []):
                    sym = (p.get("ticker") or p.get("symbol", "")).upper()
                    if sym == ticker:
                        entry     = p.get("entry_price")
                        target    = p.get("target_price")
                        stop      = p.get("stop_loss")
                        timeframe = tf
                        # Trust the pick's own asset_type, else map by section —
                        # not a blind "stock" default that mislabels etf/commodity.
                        asset_type = p.get("asset_type") or _SECTION_TYPE.get(section_name, "stock")
                        break
                if timeframe:
                    break
            if timeframe:
                break

    # Caller-supplied type (mini-app sends asset_type) wins; otherwise, for a
    # ticker not in today's picks, classify crypto via the canonical set rather
    # than defaulting to "stock".
    if asset_type_override:
        asset_type = asset_type_override
    elif not timeframe:
        try:
            from price_checker import CRYPTO_SYMBOLS
            if ticker in CRYPTO_SYMBOLS:
                asset_type = "crypto"
        except Exception:
            pass

    # Fill a missing stop/target from the user's % settings — otherwise
    # stop_loss_pct / target_gain_pct are written but never do anything. Only
    # fills gaps; an AI pick stop or an explicit override always wins.
    if entry:
        try:
            from config_manager import get_user_config
            ucfg = get_user_config(chat_id)
            if stop is None and ucfg.get("stop_loss_pct"):
                stop = round(float(entry) * (1 - float(ucfg["stop_loss_pct"]) / 100), 2)
            if target is None and ucfg.get("target_gain_pct"):
                target = round(float(entry) * (1 + float(ucfg["target_gain_pct"]) / 100), 2)
        except Exception:
            pass

    # User-supplied overrides take priority over (or supplement) AI pick levels
    if entry_override is not None:
        entry = float(entry_override)
    if stop_override is not None:
        stop = float(stop_override)
    if target_override is not None:
        target = float(target_override)
    if timeframe_override is not None:
        timeframe = timeframe_override  # caller-supplied beats pick lookup

    trade = {
        "ticker":       ticker,
        "asset_type":   asset_type,
        "timeframe":    timeframe,      # 'short_term' | 'long_term' | None
        "entry_price":  entry,
        "target_price": target,
        "stop_loss":    stop,
        "opened_date":  today,
        "manual":       True,
        "shares":       float(shares_override) if shares_override is not None else None,
    }
    # Provenance. `manual` is True for every add_holding call (it's the "I Bought
    # This" path) so it can NOT distinguish a bot from a human — `source` can.
    # close_trade spreads **trade, so this carries into the closed record too.
    if source:
        trade["source"] = source
    if levels_source:
        trade["levels_source"] = levels_source

    def _mut(log):
        log = log or {"open": [], "closed": [], "watchlist": []}
        log.setdefault("open", [])
        # Already watching — update price levels if overrides supplied, don't duplicate
        for t in log["open"]:
            if t["ticker"] == ticker:
                updated = False
                if entry_override is not None:
                    t["entry_price"] = float(entry_override)
                    updated = True
                if stop_override is not None:
                    t["stop_loss"] = float(stop_override)
                    updated = True
                if target_override is not None:
                    t["target_price"] = float(target_override)
                    updated = True
                if not updated:
                    return NO_WRITE, {"trade": t, "existed": True, "updated": False}
                return log, {"trade": t, "existed": True, "updated": True}
        log["open"].append(trade)
        return log, {"trade": trade, "existed": False, "updated": False}

    res = mutate_user_trade_log(chat_id, _mut)
    if res["existed"]:
        if res["updated"]:
            print(f"[trade_logger] Updated levels for existing {ticker} ({chat_id})")
        return res["trade"], True

    print(f"[trade_logger] Added holding: {ticker} entry={entry} stop={stop} for {chat_id}")
    return trade, False


#: Exit reasons worth distinguishing. "manual" means a HUMAN chose to sell —
#: it must not be used as a catch-all, or the reason becomes unmeasurable.
VALID_OUTCOMES = ("manual", "target", "stop", "expired")


def close_trade(ticker: str, chat_id: str, exit_price: float | None = None,
                outcome: str = "manual") -> dict | None:
    """
    Close an open trade, recording P&L in closed history.
    Returns the closed trade dict, or None if ticker not found in open trades.

    `outcome` is WHY it closed. It used to be hardcoded to "manual" for every
    caller, including the synthetic bot — which knows perfectly well whether it
    sold at target or at stop and was throwing that away. The consequence was
    that stop-vs-target could not be measured at all, and that is the one number
    that distinguishes "the pick was wrong" from "the stop was too tight" —
    problems with opposite fixes. Callers that genuinely represent a human
    decision (the Mini App sell flow) keep the default.
    """
    outcome = outcome if outcome in VALID_OUTCOMES else "manual"
    today  = et_today().isoformat()
    ticker = ticker.upper()

    # Mutator form: the log is read FRESH inside the storage lock, microseconds
    # before the write. The old load→modify→save left a seconds-wide window in
    # which a scheduled job writing the SAME user was silently clobbered.
    def _mut(log):
        log = log or {"open": [], "closed": [], "watchlist": []}
        for i, trade in enumerate(log.get("open", [])):
            if trade["ticker"] == ticker:
                entry      = trade.get("entry_price")
                return_pct = 0.0
                gain_usd   = 0.0
                if entry and exit_price:
                    return_pct = (float(exit_price) - float(entry)) / float(entry) * 100
                    allocation = float(trade.get("allocation") or 0)
                    gain_usd   = round(allocation * return_pct / 100, 2)
                closed = {
                    **trade,
                    "closed_date":  today,
                    "closed_price": round(float(exit_price), 4) if exit_price else None,
                    "outcome":      outcome,
                    "return_pct":   round(return_pct, 2),
                    "gain_usd":     gain_usd,
                }
                log["open"].pop(i)
                log.setdefault("closed", []).append(closed)
                return log, closed
        return log, None            # not found — write is a no-op rewrite

    closed = mutate_user_trade_log(chat_id, _mut)
    if closed:
        print(f"[trade_logger] Closed {ticker} manually @ ${exit_price} ({closed['return_pct']:+.1f}%) for {chat_id}")
    return closed


def remove_holding(ticker: str, chat_id: str) -> bool:
    """
    Remove a ticker from a user's portfolio.
    Returns True if found and removed, False if not in portfolio.
    """
    ticker = ticker.upper()

    def _mut(log):
        log = log or {"open": [], "closed": [], "watchlist": []}
        log.setdefault("open", [])
        before = len(log["open"])
        log["open"] = [t for t in log["open"] if t["ticker"] != ticker]
        if len(log["open"]) == before:
            return NO_WRITE, False
        return log, True

    removed = mutate_user_trade_log(chat_id, _mut)
    if removed:
        print(f"[trade_logger] Removed holding: {ticker} for {chat_id}")
    return removed


def get_weekly_closed_trades(chat_id: str) -> list[dict]:
    """Return trades closed this calendar week (Mon–today) for a specific user."""
    from datetime import timedelta
    today    = et_today()
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
