"""
agent.py — Main daily runner. Called by GitHub Actions cron job.

Three run modes (auto-detected by ET time, or forced via RUN_MODE env var):
  morning      → 8:00 AM ET  — full screener + Claude analysis + save picks
  confirmation → 10:30 AM ET — fetch live prices, compare to morning picks
  weekly       → Saturday 8 AM — runs crypto morning picks THEN weekly recap

Env vars:
  DRY_RUN=true    → print message, don't send
  MOCK_DATA=true  → skip live screeners (fast test)
  RUN_MODE=morning|confirmation|weekly → override auto-detection
"""

import os
import sys
import time
from datetime import datetime, date, timedelta

import pytz

from config_manager import (
    get_config, save_picks, load_picks, save_weekly_pick,
    get_dynamic_pick_counts, get_user_config,
    load_user_trade_log,
    save_screener_cache, load_screener_cache,
)
from trade_logger import check_and_close_trades, update_trailing_stops
from price_alert_manager import check_all_alerts
from screener import run_screener
from crypto_screener import run_crypto_screener
from ai_analyzer import analyze_with_claude, personalize_picks, generate_trade_debrief
from price_checker import get_current_prices
from telegram_notifier import (
    format_daily_message, format_confirmation_message,
    format_weekly_recap_message, send_message,
)

ET        = pytz.timezone("America/New_York")
DRY_RUN   = os.environ.get("DRY_RUN",   "false").lower() == "true"
MOCK_DATA = os.environ.get("MOCK_DATA", "false").lower() == "true"

CRYPTO_RETRY_DELAYS = [15, 30, 60, 120]   # seconds between retries (4 attempts after first)

VIX_ALERT_THRESHOLD = 25   # warn when VIX exceeds this level


# ── US Market holiday detector ────────────────────────────────────────────────

def is_market_holiday(d: date) -> bool:
    """Return True if d is a US stock market holiday (NYSE/NASDAQ)."""
    y = d.year

    def _observed(fixed: date) -> date:
        """Shift fixed holiday to observed date when it falls on a weekend."""
        if fixed.weekday() == 5:  # Saturday → Friday
            return fixed - timedelta(days=1)
        if fixed.weekday() == 6:  # Sunday → Monday
            return fixed + timedelta(days=1)
        return fixed

    def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """Return the nth occurrence of weekday (0=Mon…6=Sun) in the given month."""
        first  = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    def _last_weekday(year: int, month: int, weekday: int) -> date:
        """Return the last occurrence of weekday in the given month."""
        import calendar
        last = date(year, month, calendar.monthrange(year, month)[1])
        return last - timedelta(days=(last.weekday() - weekday) % 7)

    def _easter(year: int) -> date:
        """Computus algorithm — returns Easter Sunday."""
        a = year % 19
        b, c = divmod(year, 100)
        d2, e = divmod(b, 4)
        f  = (b + 8) // 25
        g  = (b - f + 1) // 3
        h  = (19 * a + b - d2 - g + 15) % 30
        i, k = divmod(c, 4)
        l  = (32 + 2 * e + 2 * i - h - k) % 7
        m  = (a + 11 * h + 22 * l) // 451
        mo, day = divmod(h + l - 7 * m + 114, 31)
        return date(year, mo, day + 1)

    holidays = {
        _observed(date(y, 1, 1)),           # New Year's Day
        _nth_weekday(y, 1, 0, 3),           # MLK Day (3rd Mon Jan)
        _nth_weekday(y, 2, 0, 3),           # Presidents' Day (3rd Mon Feb)
        _easter(y) - timedelta(days=2),     # Good Friday
        _last_weekday(y, 5, 0),             # Memorial Day (last Mon May)
        _observed(date(y, 6, 19)),          # Juneteenth
        _observed(date(y, 7, 4)),           # Independence Day
        _nth_weekday(y, 9, 0, 1),           # Labor Day (1st Mon Sep)
        _nth_weekday(y, 11, 3, 4),          # Thanksgiving (4th Thu Nov)
        _observed(date(y, 12, 25)),         # Christmas
    }
    return d in holidays


# ── Mock data for fast testing ────────────────────────────────────────────────

MOCK_STOCK_CANDIDATES = {
    "short_term": [
        {"ticker": "AAPL", "company": "Apple Inc", "sector": "Technology",
         "current_price": 182.50, "score": 85, "rsi": 48.2,
         "macd_crossover": True, "volume_ratio": 1.8},
        {"ticker": "NVDA", "company": "NVIDIA Corp", "sector": "Technology",
         "current_price": 875.00, "score": 75, "rsi": 52.1,
         "macd_crossover": False, "volume_ratio": 2.1},
    ],
    "long_term": [
        {"ticker": "MSFT", "company": "Microsoft Corp", "sector": "Technology",
         "current_price": 415.00, "score": 90, "pe_ratio": 32,
         "revenue_growth": 0.17, "debt_to_equity": 0.45, "market_cap": 3_000_000_000_000},
        {"ticker": "JNJ", "company": "Johnson & Johnson", "sector": "Health Care",
         "current_price": 155.00, "score": 80, "pe_ratio": 14,
         "revenue_growth": 0.06, "debt_to_equity": 0.5, "market_cap": 400_000_000_000},
    ],
}

MOCK_CRYPTO_CANDIDATES = {
    "short_term": [
        {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
         "current_price": 65000, "score": 80, "rsi": 55.0,
         "volume_ratio": 1.7, "price_change_24h_pct": 3.2, "price_change_7d_pct": 8.1},
        {"id": "solana", "symbol": "SOL", "name": "Solana",
         "current_price": 145.00, "score": 72, "rsi": 58.0,
         "volume_ratio": 2.1, "price_change_24h_pct": 4.5, "price_change_7d_pct": 12.3},
    ],
    "long_term": [
        {"id": "ethereum", "symbol": "ETH", "name": "Ethereum",
         "current_price": 3200, "score": 85, "market_cap": 385_000_000_000,
         "price_change_30d_pct": 12.5, "pct_below_ath": 34.0, "ma30": 2950.0},
        {"id": "chainlink", "symbol": "LINK", "name": "Chainlink",
         "current_price": 14.50, "score": 70, "market_cap": 8_500_000_000,
         "price_change_30d_pct": 18.2, "pct_below_ath": 55.0, "ma30": 13.20},
    ],
}


# ── Mode detection ────────────────────────────────────────────────────────────

def detect_run_mode(now_et: datetime) -> str:
    """Auto-detect run mode by ET hour/weekday. Override with RUN_MODE env var."""
    forced = os.environ.get("RUN_MODE", "").lower()
    if forced in ("morning", "confirmation", "weekly", "close_check", "prescreener", "price_alerts"):
        return forced
    if now_et.weekday() == 5 and now_et.hour < 10:   # Saturday morning
        return "weekly"
    if now_et.hour < 10:
        return "morning"
    if now_et.hour >= 15:
        return "close_check"   # 3:30 PM ET — silent unless a trade closed
    return "confirmation"


# ── Crypto screener with retry ────────────────────────────────────────────────

def _run_crypto_with_retry() -> dict:
    """
    Run crypto screener with up to 5 attempts and increasing delays.
    Sends a Telegram alert on first failure, recovery alert if it succeeds late,
    and a final failure alert if all attempts are exhausted.
    """
    empty = {"short_term": [], "long_term": []}

    for attempt, delay in enumerate([0] + CRYPTO_RETRY_DELAYS, start=1):
        if delay:
            print(f"[agent] Crypto retry {attempt}/5 — waiting {delay}s...")
            time.sleep(delay)
        try:
            result = run_crypto_screener()
            if result.get("short_term") or result.get("long_term"):
                if attempt > 1:
                    _alert(f"✅ Crypto screener recovered on attempt {attempt}/5.", admin_only=True)
                return result
            raise ValueError("Screener returned empty results")
        except Exception as exc:
            print(f"[agent] Crypto screener attempt {attempt}/5 failed: {exc}")
            if attempt == 3:
                _alert(f"⚠️ Crypto screener still failing after 3 attempts — retrying ({exc}).", admin_only=True)
            elif attempt == len(CRYPTO_RETRY_DELAYS) + 1:
                _alert("❌ Crypto screener failed after 5 attempts. Skipping crypto today.", admin_only=True)

    return empty


# ── Midnight pre-screener (runs at midnight ET, caches candidates for 8 AM) ───

def run_prescreener(config: dict):
    """
    Midnight run — scores all 600 tickers and saves top candidates to Gist.
    No Claude call, no Telegram message. Runs silently in ~90s.
    The 8 AM morning run loads this cache and skips straight to Claude.
    """
    print("[agent] Running midnight pre-screener...")

    if is_market_holiday(datetime.now(ET).date()):
        print("[agent] Market holiday tomorrow — skipping pre-screener.")
        return

    stock_results = {"short_term": [], "long_term": []}
    try:
        stock_results = run_screener(
            watchlist=config.get("watchlist", []),
            excluded_sectors=config.get("excluded_sectors", []),
        )
        print(f"[agent] Pre-screener: "
              f"{len(stock_results['short_term'])} ST, "
              f"{len(stock_results['long_term'])} LT candidates cached.")
    except Exception as exc:
        print(f"[agent] Pre-screener stock screener failed: {exc}")

    crypto_results = {"short_term": [], "long_term": []}
    if config.get("crypto_enabled", True):
        try:
            crypto_results = _run_crypto_with_retry()
            print(f"[agent] Pre-screener: "
                  f"{len(crypto_results['short_term'])} crypto ST, "
                  f"{len(crypto_results['long_term'])} crypto LT cached.")
        except Exception as exc:
            print(f"[agent] Pre-screener crypto screener failed: {exc}")

    # Only cache if the stock screener returned usable results.
    # An empty cache would cause the morning run to skip live screening
    # and send a briefing with no stock picks.
    has_stocks = bool(stock_results.get("short_term") or stock_results.get("long_term"))
    if has_stocks:
        try:
            save_screener_cache(stock_results, crypto_results)
            print("[agent] Midnight pre-screener complete. Morning run will use cache.")
        except Exception as exc:
            print(f"[agent] Pre-screener cache save failed: {exc}")
    else:
        print("[agent] Pre-screener: stock results empty — cache NOT saved. "
              "Morning run will fall back to live screener.")


# ── Morning run ───────────────────────────────────────────────────────────────

def run_morning(config: dict, now_et: datetime):
    """Full screener + Claude analysis + save picks + send morning message."""
    is_weekend = now_et.weekday() >= 5
    is_holiday = (not is_weekend) and is_market_holiday(now_et.date())

    if is_weekend and not config.get("crypto_enabled", True):
        print("[agent] Weekend + crypto disabled. Nothing to run.")
        return

    if is_holiday:
        print("[agent] US market holiday — stock screener skipped.")
        _alert("🏖️ <b>Market Closed</b> — US holiday today. No stock picks.\n"
               "<i>Crypto runs 24/7 — picks below if any signals found.</i>")

    if MOCK_DATA:
        print("[agent] Using mock data — skipping live screeners.")
        stock_candidates  = MOCK_STOCK_CANDIDATES
        crypto_candidates = MOCK_CRYPTO_CANDIDATES
    else:
        stock_candidates = {"short_term": [], "long_term": []}
        macro_context    = {}
        cache            = None   # screener cache — set inside weekday block

        if not is_weekend and not is_holiday:
            # ── Macro context (SPY%, 10Y yield, VIX) — always fetched live ───
            try:
                import yfinance as yf
                spy_hist = yf.Ticker("SPY").history(period="2d")
                tnx_hist = yf.Ticker("^TNX").history(period="1d")
                vix_hist = yf.Ticker("^VIX").history(period="1d")

                if len(spy_hist) >= 2:
                    spy_prev = float(spy_hist["Close"].iloc[-2])
                    spy_curr = float(spy_hist["Close"].iloc[-1])
                    macro_context["spy_pct"]   = round((spy_curr - spy_prev) / spy_prev * 100, 2)
                    macro_context["spy_price"] = round(spy_curr, 2)
                if not tnx_hist.empty:
                    macro_context["tnx_yield"] = round(float(tnx_hist["Close"].iloc[-1]), 2)
                if not vix_hist.empty:
                    vix = float(vix_hist["Close"].iloc[-1])
                    macro_context["vix"] = round(vix, 1)
                    print(f"[agent] VIX = {vix:.1f}")
                    if vix > VIX_ALERT_THRESHOLD:
                        _alert(
                            f"⚠️ <b>High Volatility Alert</b> — VIX = <code>{vix:.1f}</code>\n"
                            f"Market fear is elevated. Consider tightening stop-losses "
                            f"and reducing short-term position sizes today."
                        )
            except Exception as exc:
                print(f"[agent] Macro context fetch failed (non-critical): {exc}")

            # ── Stock screener: use midnight cache if fresh, else run live ────
            cache = None
            try:
                cache = load_screener_cache()
            except Exception as exc:
                print(f"[agent] Screener cache load failed (non-critical): {exc}")

            if cache:
                print("[agent] Using midnight screener cache — skipping live screener.")
                stock_candidates = cache["stocks"]
            else:
                print("[agent] No fresh screener cache — running live stock screener...")
                try:
                    stock_candidates = run_screener(
                        watchlist=config.get("watchlist", []),
                        excluded_sectors=config.get("excluded_sectors", []),
                    )
                except Exception as exc:
                    print(f"[agent] Stock screener failed: {exc}")
                    _alert(f"⚠ Stock screener error: {exc}", admin_only=True)

        # ── Crypto: use midnight cache if fresh, else run live ────────────────
        crypto_candidates = {"short_term": [], "long_term": []}
        if config.get("crypto_enabled", True):
            if cache and cache.get("crypto"):
                print("[agent] Using midnight screener cache for crypto.")
                crypto_candidates = cache["crypto"]
            else:
                print("[agent] Running crypto screener (with retry)...")
                crypto_candidates = _run_crypto_with_retry()

    has_stocks = bool(stock_candidates["short_term"] or stock_candidates["long_term"])
    has_crypto = bool(crypto_candidates["short_term"] or crypto_candidates["long_term"])

    if not has_stocks and not has_crypto:
        _alert("⚠ Both screeners returned no candidates today. No picks sent.", admin_only=True)
        return

    # Apply dynamic pick counts based on current budget
    dynamic_counts = get_dynamic_pick_counts(config)
    config = {**config, **dynamic_counts}
    print(f"[agent] Dynamic pick counts: {dynamic_counts}")

    # Recent losers — from owner's trade log (used to adjust Claude prompt)
    recent_losers: list[str] = []
    try:
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        if owner:
            log = load_user_trade_log(owner)
            cutoff = (now_et.date() - timedelta(days=14)).isoformat()
            recent_losers = [
                t["ticker"] for t in log.get("closed", [])
                if t.get("return_pct", 0) < 0 and t.get("closed_date", "") >= cutoff
            ]
            if recent_losers:
                print(f"[agent] Recent losers (last 14d): {recent_losers}")
    except Exception as exc:
        print(f"[agent] Recent losers fetch failed (non-critical): {exc}")

    print("[agent] Running Claude analysis...")
    try:
        picks = analyze_with_claude(
            stock_candidates, config,
            crypto_results=crypto_candidates if has_crypto else None,
            recent_losers=recent_losers,
        )
    except Exception as exc:
        print(f"[agent] Claude analysis failed: {exc}")
        _alert("⚠ Agent error today. Claude analysis unavailable.", admin_only=True)
        return

    # Attach macro context so the formatter can display it
    if macro_context:
        picks["macro_context"] = macro_context

    # Save picks to Gist for 10:30 AM confirmation run + weekly recap
    save_picks(picks)
    if not now_et.weekday() >= 5:   # Don't count weekend crypto-only as a "week day"
        try:
            save_weekly_pick(picks)
        except Exception as exc:
            print(f"[agent] Weekly picks save failed (non-critical): {exc}")

    # NOTE: open_trades() is intentionally NOT called here.
    # /positions and /perf only reflect trades the user explicitly logs via /bought.
    # Auto-logging bot picks caused /positions to show positions the user never placed.

    _send_morning_personalised(picks, config, label="8:00 AM Morning Briefing")


# ── Confirmation run ──────────────────────────────────────────────────────────

def run_confirmation():
    """Load morning picks, fetch live prices, send comparison message."""
    print("[agent] Loading morning picks from Gist...")
    picks = load_picks()

    if not picks:
        print("[agent] No picks found for today — skipping confirmation.")
        return

    # ── Idempotency guard — only send once per day ────────────────────────────
    # Render retries failed cron runs; without this each retry would re-send
    today = date.today().isoformat()
    if picks.get("_confirmation_sent_date") == today:
        print("[agent] Confirmation already sent today — skipping duplicate.")
        return

    print("[agent] Fetching current prices...")
    try:
        current_prices = get_current_prices(picks)
    except Exception as exc:
        print(f"[agent] Price fetch failed: {exc}")
        _alert("⚠ Could not fetch prices for 10:30 AM check.", admin_only=True)
        return

    # ── Per-user trailing stops + trade close checks ──────────────────────────
    for uid in _all_recipients():
        try:
            trail_closed = update_trailing_stops(current_prices, uid)
            for trade in trail_closed:
                sign = "+" if trade["return_pct"] >= 0 else ""
                send_message(
                    f"🔒 <b>{trade['ticker']} TRAILING STOP HIT</b>\n"
                    f"Sold @ <code>${trade['closed_price']}</code>  "
                    f"<b>{sign}{trade['return_pct']:.1f}%</b>  (${trade['gain_usd']:+.2f})\n"
                    f"<i>Peak ${trade.get('highest_reached', '?')} → "
                    f"Trail stop ${trade.get('trailing_stop_level', '?')}</i>",
                    chat_id=uid,
                )
        except Exception as exc:
            print(f"[agent] Trailing stop update failed for {uid} (non-critical): {exc}")

        try:
            closed = check_and_close_trades(current_prices, uid)
            for trade in closed:
                emoji = "✅" if trade["outcome"] == "target" else ("🔴" if trade["outcome"] == "stop" else "⏱")
                sign  = "+" if trade["return_pct"] >= 0 else ""
                close_msg = (
                    f"{emoji} <b>{trade['ticker']}</b> {trade['outcome'].upper()} HIT "
                    f"@ <code>${trade['closed_price']}</code>  "
                    f"<b>{sign}{trade['return_pct']:.1f}%</b>  "
                    f"(${trade['gain_usd']:+.2f})"
                )
                # Feature 2: post-trade debrief (Haiku — non-blocking, non-critical)
                try:
                    debrief = generate_trade_debrief(trade)
                    if debrief:
                        close_msg += f"\n\n📖 <i>{debrief}</i>"
                except Exception as db_exc:
                    print(f"[agent] Trade debrief failed (non-critical): {db_exc}")
                send_message(close_msg, chat_id=uid)
        except Exception as exc:
            print(f"[agent] Trade close check failed for {uid} (non-critical): {exc}")

    # ── Price alerts ──────────────────────────────────────────────────────────
    try:
        fired = check_all_alerts(send_fn=_alert)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed (non-critical): {exc}")

    # ── Per-user earnings warnings + portfolio alerts ─────────────────────────
    import yfinance as yf
    for uid in _all_recipients():
        try:
            from earnings_checker import get_upcoming_earnings
            log = load_user_trade_log(uid)
            open_stock_tickers = [
                t["ticker"] for t in log.get("open", [])
                if t.get("asset_type") == "stock"
            ]
            if open_stock_tickers:
                upcoming = get_upcoming_earnings(open_stock_tickers, days_ahead=3)
                for ticker, earnings_date in upcoming.items():
                    send_message(
                        f"🗓️ <b>Earnings Warning</b> — <b>{ticker}</b> reports <b>{earnings_date}</b>\n"
                        f"You have an open position. Earnings can cause sharp moves — "
                        f"consider closing before the announcement.",
                        chat_id=uid,
                    )
        except Exception as exc:
            print(f"[agent] Earnings warning check failed for {uid} (non-critical): {exc}")

        try:
            log = load_user_trade_log(uid)
            manual_open = [t for t in log.get("open", []) if t.get("manual")]
            if manual_open:
                syms  = [t["ticker"] for t in manual_open]
                pdata = yf.download(" ".join(syms), period="1d", interval="1m",
                                    progress=False, auto_adjust=True)
                for t in manual_open:
                    ticker  = t["ticker"]
                    entry   = float(t.get("entry_price") or 0)
                    target  = t.get("target_price")
                    stop    = t.get("stop_loss")
                    try:
                        if len(syms) == 1:
                            cur = float(pdata["Close"].dropna().iloc[-1])
                        else:
                            cur = float(pdata["Close"][ticker].dropna().iloc[-1])
                    except Exception:
                        continue
                    if not entry:
                        continue
                    ret_pct = (cur - entry) / entry * 100
                    sign    = "+" if ret_pct >= 0 else ""
                    if stop and cur <= float(stop) * 1.02:
                        send_message(
                            f"⚠️ <b>{ticker} NEAR STOP LOSS</b>\n"
                            f"Current <code>${cur:.2f}</code>  Stop <code>${stop}</code>  "
                            f"Return {sign}{ret_pct:.1f}%\n"
                            f"<i>Consider cutting the position to limit losses.</i>",
                            chat_id=uid,
                        )
                    elif target and cur >= float(target) * 0.98:
                        send_message(
                            f"🎯 <b>{ticker} NEAR TARGET</b>\n"
                            f"Current <code>${cur:.2f}</code>  Target <code>${target}</code>  "
                            f"Return {sign}{ret_pct:.1f}%\n"
                            f"<i>Consider taking profit or raising your stop.</i>",
                            chat_id=uid,
                        )
        except Exception as exc:
            print(f"[agent] Portfolio alert check failed for {uid} (non-critical): {exc}")

    message = format_confirmation_message(picks, current_prices)
    _send_or_print(message, label="10:30 AM Confirmation")

    # Mark as sent so retries don't fire again today
    if not DRY_RUN:
        try:
            picks["_confirmation_sent_date"] = today
            save_picks(picks)
        except Exception as exc:
            print(f"[agent] Could not stamp confirmation sent flag (non-critical): {exc}")


# ── Close check (3:30 PM — silent unless a trade closed) ─────────────────────

def run_close_check():
    """3:30 PM run. Checks trades silently — only sends a message if target/stop hit."""
    print("[agent] Running 3:30 PM close check...")
    picks = load_picks()
    if not picks:
        print("[agent] No picks for today — nothing to check.")
        return

    try:
        current_prices = get_current_prices(picks)
    except Exception as exc:
        print(f"[agent] Price fetch failed: {exc}")
        return

    for uid in _all_recipients():
        try:
            trail_closed = update_trailing_stops(current_prices, uid)
            for trade in trail_closed:
                sign = "+" if trade["return_pct"] >= 0 else ""
                send_message(
                    f"🔒 <b>{trade['ticker']} TRAILING STOP HIT</b>\n"
                    f"Sold @ <code>${trade['closed_price']}</code>  "
                    f"<b>{sign}{trade['return_pct']:.1f}%</b>  (${trade['gain_usd']:+.2f})\n"
                    f"<i>Peak ${trade.get('highest_reached', '?')} → "
                    f"Trail stop ${trade.get('trailing_stop_level', '?')}</i>",
                    chat_id=uid,
                )
        except Exception as exc:
            print(f"[agent] Trailing stop update failed for {uid} (non-critical): {exc}")

        try:
            closed = check_and_close_trades(current_prices, uid)
            for trade in closed:
                emoji = "✅" if trade["outcome"] == "target" else ("🔴" if trade["outcome"] == "stop" else "⏱")
                sign  = "+" if trade["return_pct"] >= 0 else ""
                close_msg = (
                    f"{emoji} <b>{trade['ticker']}</b> {trade['outcome'].upper()} HIT "
                    f"@ <code>${trade['closed_price']}</code>  "
                    f"<b>{sign}{trade['return_pct']:.1f}%</b>  "
                    f"(${trade['gain_usd']:+.2f})"
                )
                # Feature 2: post-trade debrief (Haiku)
                try:
                    debrief = generate_trade_debrief(trade)
                    if debrief:
                        close_msg += f"\n\n📖 <i>{debrief}</i>"
                except Exception as db_exc:
                    print(f"[agent] Trade debrief failed (non-critical): {db_exc}")
                send_message(close_msg, chat_id=uid)
            if not closed:
                print(f"[agent] 3:30 PM close check for {uid}: no trades hit.")
        except Exception as exc:
            print(f"[agent] Trade close check failed for {uid} (non-critical): {exc}")

    # ── Price alerts ──────────────────────────────────────────────────────────
    try:
        fired = check_all_alerts(send_fn=_alert)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed (non-critical): {exc}")

    # ── End-of-day portfolio summary per user (manually logged trades) ────────
    import yfinance as yf
    for uid in _all_recipients():
        try:
            log = load_user_trade_log(uid)
            manual_open = [t for t in log.get("open", []) if t.get("manual")]
            if not manual_open:
                continue
            syms   = [t["ticker"] for t in manual_open]
            pdata  = yf.download(" ".join(syms), period="1d", interval="1m",
                                 progress=False, auto_adjust=True)
            alerts = []
            for t in manual_open:
                ticker = t["ticker"]
                entry  = float(t.get("entry_price") or 0)
                target = t.get("target_price")
                stop   = t.get("stop_loss")
                try:
                    cur = float(pdata["Close"].dropna().iloc[-1]) if len(syms) == 1 \
                          else float(pdata["Close"][ticker].dropna().iloc[-1])
                except Exception:
                    continue
                if not entry:
                    continue
                ret_pct = (cur - entry) / entry * 100
                sign    = "+" if ret_pct >= 0 else ""
                emoji   = "🟢" if ret_pct >= 0 else "🔴"
                to_t    = f"  {((float(target)/cur-1)*100):+.1f}% to target" if target else ""
                to_s    = f"  {((float(stop)/cur-1)*100):+.1f}% to stop" if stop else ""
                alerts.append(f"{emoji} <b>{ticker}</b> <code>${cur:.2f}</code>  {sign}{ret_pct:.1f}%{to_t}{to_s}")
            if alerts:
                send_message(
                    "📊 <b>End-of-day portfolio check</b>\n\n" + "\n".join(alerts),
                    chat_id=uid,
                )
        except Exception as exc:
            print(f"[agent] End-of-day portfolio summary failed for {uid} (non-critical): {exc}")


# ── Weekly recap (Saturday morning) ──────────────────────────────────────────

def run_weekly_recap(config: dict, now_et: datetime):
    """Saturday: run crypto morning picks, then send a compact weekly recap."""
    # Step 1: Saturday crypto morning picks (markets closed, crypto runs 24/7)
    run_morning(config, now_et)

    # Step 2: Weekly performance recap — personalised per user
    print("[agent] Building weekly recap...")
    try:
        from performance_tracker import build_weekly_recap
        recap = build_weekly_recap()
        if recap:
            recipients = _all_recipients()
            print(f"[agent] Sending personalised weekly recap to {len(recipients)} user(s)...")
            for uid in recipients:
                try:
                    user_cfg = {**config, **get_user_config(uid)}
                    if user_cfg.get("paused"):
                        print(f"[agent] Skipping weekly recap for {uid} — picks paused.")
                        continue
                    message = format_weekly_recap_message(recap, config=user_cfg)
                    if DRY_RUN:
                        print(f"\n{'='*60}\nDRY RUN — Weekly Recap for {uid}:\n{'='*60}\n{message}")
                    else:
                        send_message(message, chat_id=uid)
                except Exception as exc:
                    print(f"[agent] Weekly recap failed for {uid}: {exc}")
        else:
            print("[agent] No weekly picks data — skipping recap.")
    except Exception as exc:
        print(f"[agent] Weekly recap failed (non-critical): {exc}")


# ── Intraday price-alert-only run (every 30 min during market hours) ─────────

def run_price_alerts():
    """
    Lightweight run: check price alerts + trailing stops only.
    No Claude call, no screener — just yfinance price fetch.
    Designed to run every 30 minutes during market hours.
    """
    print("[agent] Running intraday price alerts check...")

    # Check user-set price alerts (above/below thresholds)
    try:
        fired = check_all_alerts(send_fn=_alert)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
        else:
            print("[agent] No price alerts triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed: {exc}")

    # Also check trailing stops on open positions using live prices — per user
    import yfinance as yf
    for uid in _all_recipients():
        try:
            log = load_user_trade_log(uid)
            open_trades = log.get("open", [])
            if not open_trades:
                continue

            tickers = list({t["ticker"] for t in open_trades})
            raw = yf.download(" ".join(tickers), period="1d", interval="1m",
                              progress=False, auto_adjust=True)
            if hasattr(raw["Close"], "iloc"):
                current_prices = {t: float(raw["Close"][t].dropna().iloc[-1])
                                  for t in tickers if t in raw["Close"].columns}
            else:
                current_prices = {tickers[0]: float(raw["Close"].dropna().iloc[-1])} if tickers else {}

            trail_closed = update_trailing_stops(current_prices, uid)
            for trade in trail_closed:
                sign = "+" if trade["return_pct"] >= 0 else ""
                send_message(
                    f"🔒 <b>{trade['ticker']} TRAILING STOP HIT</b>\n"
                    f"Sold @ <code>${trade['closed_price']}</code>  "
                    f"<b>{sign}{trade['return_pct']:.1f}%</b>  (${trade['gain_usd']:+.2f})\n"
                    f"<i>Peak ${trade.get('highest_reached', '?')} → "
                    f"Trail stop ${trade.get('trailing_stop_level', '?')}</i>",
                    chat_id=uid,
                )

            newly_closed = check_and_close_trades(current_prices, uid)
            for trade in newly_closed:
                emoji = "✅" if trade["outcome"] == "target" else ("🔴" if trade["outcome"] == "stop" else "⏱")
                sign  = "+" if trade["return_pct"] >= 0 else ""
                send_message(
                    f"{emoji} <b>{trade['ticker']}</b> {trade['outcome'].upper()} HIT "
                    f"@ <code>${trade['closed_price']}</code>  "
                    f"<b>{sign}{trade['return_pct']:.1f}%</b>  "
                    f"(${trade['gain_usd']:+.2f})",
                    chat_id=uid,
                )
        except Exception as exc:
            print(f"[agent] Intraday trade check failed for {uid} (non-critical): {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now_et = datetime.now(ET)
    mode   = detect_run_mode(now_et)

    print(f"[agent] Starting [{mode.upper()}{'  DRY RUN' if DRY_RUN else ''}] "
          f"at {now_et.strftime('%Y-%m-%d %H:%M ET')}")

    config = get_config()
    if not config.get("enabled", True):
        print("[agent] Agent is paused. Skipping.")
        return

    if mode == "prescreener":
        run_prescreener(config)
    elif mode == "morning":
        run_morning(config, now_et)
    elif mode == "weekly":
        run_weekly_recap(config, now_et)
    elif mode == "close_check":
        run_close_check()
    elif mode == "price_alerts":
        run_price_alerts()
    else:
        run_confirmation()

    print(f"[agent] Done ({mode}) for {now_et.strftime('%Y-%m-%d')}.")


def _all_recipients() -> list[str]:
    """Return all allowed chat_ids (always includes owner)."""
    try:
        from config_manager import get_allowed_users
        return get_allowed_users()
    except Exception:
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        return [owner] if owner else []


def _send_or_print(message: str, label: str = ""):
    """Broadcast a scheduled message to all allowed users."""
    if DRY_RUN:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — {label} (not sent):")
        print("=" * 60)
        print(message)
        print(f"\nLength: {len(message)} chars")
        print("=" * 60)
    else:
        recipients = _all_recipients()
        print(f"[agent] Broadcasting {label} to {len(recipients)} user(s)...")
        for uid in recipients:
            success = send_message(message, chat_id=uid)
            if not success:
                print(f"[agent] WARNING: Message failed to send to {uid}.")


def _send_morning_personalised(picks: dict, global_config: dict, label: str = ""):
    """
    Send the morning briefing to all users, personalised per user's config.
    Each user gets their own budget/pick_mode applied, the same underlying picks,
    and a Haiku-generated personal note per pick explaining portfolio fit.
    """
    recipients = _all_recipients()
    print(f"[agent] Sending personalised {label} to {len(recipients)} user(s)...")
    for uid in recipients:
        try:
            user_cfg = {**global_config, **get_user_config(uid)}
            if user_cfg.get("paused"):
                print(f"[agent] Skipping {uid} — picks paused by user.")
                continue

            # ── Personalised notes (Feature 1) ────────────────────────────────
            # Load user's open positions and ask Haiku why each pick fits their portfolio.
            personal_notes: dict = {}
            try:
                log = load_user_trade_log(uid)
                open_positions = log.get("open", [])
                risk_profile   = user_cfg.get("risk_profile", "moderate")
                personal_notes = personalize_picks(picks, open_positions, risk_profile)
            except Exception as pn_exc:
                print(f"[agent] personalize_picks failed for {uid} (non-critical): {pn_exc}")

            message = format_daily_message(picks, user_cfg, personal_notes=personal_notes)
            if DRY_RUN:
                print(f"\n{'=' * 60}\nDRY RUN — {label} for {uid}:\n{'=' * 60}\n{message}\n")
            else:
                success = send_message(message, chat_id=uid)
                if not success:
                    print(f"[agent] WARNING: Morning message failed to send to {uid}.")
        except Exception as exc:
            print(f"[agent] WARNING: Could not send morning message to {uid}: {exc}")


def _alert(text: str, admin_only: bool = False):
    """
    Send an operational alert.
    admin_only=True  → owner only (errors, warnings, system messages)
    admin_only=False → all users (trade closes, earnings warnings, market info)
    """
    print(f"[agent] ALERT: {text}")
    if DRY_RUN:
        return
    if admin_only:
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        if owner:
            send_message(text, chat_id=owner)
    else:
        for uid in _all_recipients():
            send_message(text, chat_id=uid)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print(f"⚠ Unhandled error: {exc}\n{traceback.format_exc()}")
        if not DRY_RUN:
            send_message(f"⚠ Agent crashed: {exc}. Check GitHub Actions logs.")
        sys.exit(1)
