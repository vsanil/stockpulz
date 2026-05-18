"""
agent.py — Main daily runner. Called by GitHub Actions cron job.

Three run modes (auto-detected by ET time, or forced via RUN_MODE env var):
  morning      → 8:00 AM ET  — full screener + Claude analysis + save picks
  confirmation → 10:30 AM ET — fetch live prices, compare to morning picks
  weekly       → Saturday 8 AM — runs crypto morning picks THEN weekly recap
  week_ahead   → Sunday 8 AM  — standalone Week Ahead brief (earnings + regime)

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
    get_config, update_config, save_picks, load_picks, save_weekly_pick,
    get_dynamic_pick_counts, get_user_config,
    load_user_trade_log, save_user_trade_log,
    save_screener_cache, load_screener_cache,
)
from etf_screener import run_etf_screener
from trade_logger import check_and_close_trades
from price_alert_manager import check_all_alerts, add_alert
from screener import run_screener
from crypto_screener import run_crypto_screener
from ai_analyzer import analyze_with_claude, personalize_picks_batch, generate_trade_debrief
from position_sizer import apply_portfolio_sizing, portfolio_summary
from price_checker import get_current_prices
from formatters import (
    format_daily_message, format_confirmation_message, format_weekly_recap_message,
    format_eod_summary, format_eod_full_summary, format_week_ahead, build_picks_keyboard, _p,
)
from telegram_api import send_message, send_inline_keyboard, broadcast_all
from cache_layer import cache_get, cache_set

ET        = pytz.timezone("America/New_York")
DRY_RUN   = os.environ.get("DRY_RUN",   "false").lower() == "true"
MOCK_DATA = os.environ.get("MOCK_DATA", "false").lower() == "true"

# Default conviction threshold — picks below this are filtered before broadcast.
# Users can lower to 3 in /settings (aggressive mode already allows ★★★).
DEFAULT_MIN_CONVICTION = 4


# ── Conviction gate ───────────────────────────────────────────────────────────

def _filter_by_conviction(picks: dict, min_conviction: int) -> dict:
    """
    Remove any pick with conviction < min_conviction from every section.
    Modifies a shallow copy — does not mutate the original.
    Returns the filtered picks dict.
    """
    def _clean(lst: list) -> list:
        return [p for p in lst if int(p.get("conviction") or 0) >= min_conviction]

    result = dict(picks)   # shallow copy — preserve macro_context, etc.

    if "stocks" in result:
        s = result["stocks"]
        result["stocks"] = {
            "short_term": _clean(s.get("short_term", [])),
            "long_term":  _clean(s.get("long_term",  [])),
        }
    if "crypto" in result:
        c = result["crypto"]
        result["crypto"] = {
            "short_term": _clean(c.get("short_term", [])),
            "long_term":  _clean(c.get("long_term",  [])),
        }
    if "etfs" in result:
        e = result["etfs"]
        result["etfs"] = {
            "short_term": _clean(e.get("short_term", [])),
            "long_term":  _clean(e.get("long_term",  [])),
        }
    if "commodities" in result:
        cm = result["commodities"]
        result["commodities"] = {
            "short_term": _clean(cm.get("short_term", [])),
            "long_term":  _clean(cm.get("long_term",  [])),
        }
    # options_plays: only keep if their parent stock pick survived
    surviving_tickers = {
        p.get("ticker") for section in ("short_term", "long_term")
        for p in result.get("stocks", {}).get(section, [])
    }
    result["options_plays"] = [
        op for op in picks.get("options_plays", [])
        if op.get("ticker") in surviving_tickers
    ]
    return result


def _picks_are_empty(picks: dict) -> bool:
    """Return True if every category in picks has no entries after filtering."""
    for key in ("stocks", "crypto", "etfs", "commodities"):
        section = picks.get(key, {})
        if section.get("short_term") or section.get("long_term"):
            return False
    return True


def _log_cron_run(mode: str) -> None:
    """Record the last run timestamp for a cron mode in the shared config."""
    try:
        update_config(f"cron_last_{mode}", datetime.utcnow().isoformat())
    except Exception as exc:
        print(f"[agent] cron log failed for {mode}: {exc}")

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
    ],
    "long_term": [],
}

# Pre-built picks in analyze_with_claude() output format — skips the Claude API call entirely.
# Covers every keyboard/message section: 2 ST stocks, 1 LT stock, 1 crypto, 1 commodity.
MOCK_PICKS = {
    "stocks": {
        "short_term": [
            {
                "ticker": "AAPL", "company": "Apple Inc", "sector": "Technology",
                "entry_price": 182.50, "target_price": 196.00, "stop_loss": 174.00,
                "conviction": 5, "hold_days": "3–5",
                "thesis": "Bullish MACD crossover on high volume. Services revenue re-accelerating into earnings.",
                "catalyst": "WWDC keynote + Vision Pro unit sales update",
                "invalidation": "Daily close below $174",
            },
            {
                "ticker": "NVDA", "company": "NVIDIA Corp", "sector": "Technology",
                "entry_price": 875.00, "target_price": 950.00, "stop_loss": 840.00,
                "conviction": 4, "hold_days": "2–4",
                "thesis": "AI capex cycle intact. RSI reset from overbought; volume confirming accumulation.",
                "catalyst": "GTC conference data-center order commentary",
                "invalidation": "Close below 50-day MA ($840)",
            },
        ],
        "long_term": [
            {
                "ticker": "MSFT", "company": "Microsoft Corp", "sector": "Technology",
                "entry_price": 415.00, "target_price": 480.00, "stop_loss": 390.00,
                "conviction": 5, "hold_days": "30–60",
                "thesis": "Azure + Copilot monetisation driving margin expansion. Best-in-class balance sheet.",
                "catalyst": "Q3 cloud revenue beat expected",
                "invalidation": "Revenue growth decelerates below 12% YoY",
            },
        ],
    },
    "crypto": {
        "short_term": [
            {
                "symbol": "BTC", "name": "Bitcoin",
                "entry_price": 65000, "target_price": 72000, "stop_loss": 61000,
                "conviction": 4, "hold_days": "5–10",
                "thesis": "Spot ETF inflows accelerating. Weekly RSI in bullish range, hash-rate ATH.",
                "catalyst": "Halving supply squeeze + ETF AUM milestone",
                "invalidation": "Weekly close below $61k",
            },
        ],
        "long_term": [],
    },
    "etfs": {"short_term": [], "long_term": []},
    "commodities": {
        "short_term": [
            {
                "ticker": "GLD", "company": "SPDR Gold Shares", "sector": "Commodities",
                "entry_price": 218.00, "target_price": 232.00, "stop_loss": 210.00,
                "conviction": 4, "hold_days": "7–14",
                "thesis": "Gold breaking multi-year resistance. Safe-haven demand + weakening DXY.",
                "catalyst": "Fed pause + geopolitical risk premium",
                "invalidation": "DXY reclaims 105",
            },
        ],
        "long_term": [],
    },
    "options_plays": [],
    "macro_context": "SPY +0.4% · VIX 18.2 · 10Y 4.32% — Risk-on tone, momentum favours longs. <i>[MOCK DATA]</i>",
}


# ── Mode detection ────────────────────────────────────────────────────────────

def detect_run_mode(now_et: datetime) -> str:
    """Auto-detect run mode by ET hour/weekday. Override with RUN_MODE env var."""
    forced = os.environ.get("RUN_MODE", "").lower()
    if forced in ("morning", "confirmation", "weekly", "close_check", "eod_summary", "prescreener", "price_alerts", "week_ahead", "premarket"):
        return forced
    if now_et.weekday() == 6 and now_et.hour < 14:   # Sunday morning → week ahead briefing
        return "week_ahead"
    if now_et.weekday() == 5 and now_et.hour < 10:   # Saturday morning
        return "weekly"
    if now_et.weekday() < 5 and now_et.hour == 8 and now_et.minute >= 40:
        return "premarket"   # 8:40–8:59 AM ET weekday → pre-market pulse
    if now_et.hour < 10:
        return "morning"
    if now_et.hour >= 16:
        return "eod_summary"  # after market close → full EOD wrap-up
    if now_et.hour == 15:
        return "close_check"  # 3:00–3:59 PM ET → intraday close check
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
    _log_cron_run("prescreener")
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
    _log_cron_run("morning")
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
                # Re-apply LT quality floor — guards against cached results from
                # before the quality gate was added (task #102) or stale midnight
                # runs where some candidates have been scored well below threshold.
                _LT_FLOOR = 25   # same spirit as the liquidity gate in run_screener()
                _before = len(stock_candidates.get("long_term", []))
                stock_candidates["long_term"] = [
                    c for c in stock_candidates.get("long_term", [])
                    if c.get("lt_score", 0) >= _LT_FLOOR
                ]
                _after = len(stock_candidates["long_term"])
                if _before != _after:
                    print(f"[agent] LT quality floor: dropped {_before - _after} "
                          f"cached candidates with lt_score < {_LT_FLOOR}.")
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

    # ── ETF screener (weekdays only — ETFs trade NYSE hours) ─────────────────
    etf_candidates = {"short_term": [], "long_term": []}
    if not is_weekend and not is_holiday:
        try:
            print("[agent] Running ETF screener...")
            etf_candidates = run_etf_screener()
        except Exception as exc:
            print(f"[agent] ETF screener failed (non-critical): {exc}")

    has_stocks = bool(stock_candidates["short_term"] or stock_candidates["long_term"])
    has_crypto = bool(crypto_candidates["short_term"] or crypto_candidates["long_term"])
    has_etfs   = bool(etf_candidates["short_term"] or etf_candidates["long_term"])

    if not has_stocks and not has_crypto and not has_etfs:
        _alert("⚠ All screeners returned no candidates today. No picks sent.", admin_only=True)
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

    if MOCK_DATA:
        print("[agent] MOCK_DATA=true — skipping Claude analysis, using MOCK_PICKS.")
        picks = MOCK_PICKS
        macro_context = {}
    else:
        print("[agent] Running Claude analysis...")
        try:
            picks = analyze_with_claude(
                stock_candidates, config,
                crypto_results=crypto_candidates if has_crypto else None,
                recent_losers=recent_losers,
                etf_results=etf_candidates if has_etfs else None,
            )
        except Exception as exc:
            print(f"[agent] Claude analysis failed: {exc}")
            _alert(f"❌ <b>Morning run FAILED</b> — Claude analysis error:\n<code>{exc}</code>", admin_only=True)
            return

        # Attach macro context so the formatter can display it
        if macro_context:
            picks["macro_context"] = macro_context

    # ── Position sizing ───────────────────────────────────────────────────────
    # Only run if the user has configured a portfolio_size.
    # Attaches a "sizing" dict to every pick and returns portfolio-level warnings.
    if config.get("portfolio", {}).get("portfolio_size"):
        try:
            picks, sizing_warnings = apply_portfolio_sizing(picks, config)
            picks["portfolio_summary"] = portfolio_summary(picks, config)
            if sizing_warnings:
                print(f"[agent] Sizing warnings: {sizing_warnings}")
                picks["sizing_warnings"] = sizing_warnings
        except Exception as exc:
            print(f"[agent] Position sizing failed (non-critical): {exc}")

    # ── Conviction gate — filter before saving or broadcasting ───────────────
    min_conviction = int(config.get("min_conviction", DEFAULT_MIN_CONVICTION))
    picks_before   = picks
    picks          = _filter_by_conviction(picks, min_conviction)

    n_filtered = sum(
        len(picks_before.get(k, {}).get(s, [])) - len(picks.get(k, {}).get(s, []))
        for k in ("stocks", "crypto", "etfs", "commodities")
        for s in ("short_term", "long_term")
    )
    if n_filtered:
        print(f"[agent] Conviction gate dropped {n_filtered} pick(s) below ★{min_conviction}.")

    if _picks_are_empty(picks):
        no_picks_msg = (
            "📭 <b>No high-conviction setups today</b>\n\n"
            "The model screened the market but didn't find picks that meet the quality bar "
            f"(conviction ★★★★+). Cash is a position too — waiting for the right setup "
            "protects your capital.\n\n"
            "<i>Alerts and watchlist monitoring remain active.</i>"
        )
        recipients = _all_recipients()
        outbox = []
        for uid in recipients:
            try:
                if not get_user_config(uid).get("paused"):
                    outbox.append({"chat_id": uid, "text": no_picks_msg})
            except Exception as exc:
                print(f"[agent] no-picks send skipped for {uid}: {exc}")
        if outbox:
            broadcast_all(outbox)
        _alert(
            f"📭 <b>Morning run: no picks broadcast</b> — all filtered below ★{min_conviction}.",
            admin_only=True,
        )
        return

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

    # ── Auto stop-loss + entry alerts for morning picks ───────────────────────
    try:
        _auto_set_pick_alerts(picks, _all_recipients())
    except Exception as exc:
        print(f"[agent] _auto_set_pick_alerts wrapper failed (non-critical): {exc}")

    # ── Monday "Week Ahead" block ─────────────────────────────────────────────
    if now_et.weekday() == 0:   # Monday only
        try:
            from earnings_checker import get_upcoming_earnings
            stocks   = picks.get("stocks", picks)
            crypto   = picks.get("crypto", {})
            all_tickers = (
                [s.get("ticker") for s in stocks.get("short_term", [])] +
                [s.get("ticker") for s in stocks.get("long_term",  [])]
            )
            # Also include each user's watchlist tickers
            for uid in _all_recipients():
                try:
                    from config_manager import get_user_config
                    wl = get_user_config(uid).get("watchlist", [])
                    all_tickers.extend(wl)
                except Exception:
                    pass
            all_tickers = list(dict.fromkeys(t for t in all_tickers if t))
            earnings_week = get_upcoming_earnings(all_tickers, days_ahead=5)
            from market_regime import get_market_regime
            regime = get_market_regime()
            week_msg = format_week_ahead(earnings_week, regime)
            if week_msg:
                for uid in _all_recipients():
                    try:
                        user_cfg = {**config, **get_user_config(uid)}
                        if not user_cfg.get("paused"):
                            send_message(week_msg, chat_id=uid)
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[agent] Week-ahead block failed (non-critical): {exc}")

    # ── Admin run summary ─────────────────────────────────────────────────────
    try:
        stocks = picks.get("stocks", {})
        crypto = picks.get("crypto", {})
        etfs   = picks.get("etfs", {})
        comms  = picks.get("commodities", {})
        n_st     = len(stocks.get("short_term", []))
        n_lt     = len(stocks.get("long_term",  []))
        n_crypto = len(crypto.get("short_term", [])) + len(crypto.get("long_term", []))
        n_etf    = len(etfs.get("short_term", [])) + len(etfs.get("long_term", []))
        n_comm   = len(comms.get("short_term", [])) + len(comms.get("long_term", []))
        n_opts   = len(picks.get("options_plays", []))
        n_users  = len(_all_recipients())
        _alert(
            f"✅ <b>Morning run complete</b>\n"
            f"Sent to {n_users} user(s)  ·  "
            f"📈 {n_st} ST + {n_lt} LT stocks  ·  "
            f"🪙 {n_crypto} crypto  ·  "
            f"📦 {n_etf} ETFs  ·  "
            f"🛢 {n_comm} commodities  ·  "
            f"🎯 {n_opts} options plays",
            admin_only=True,
        )
        # Save timestamp for /dashboard
        update_config("last_morning_run", datetime.utcnow().isoformat())
    except Exception as exc:
        print(f"[agent] Admin run summary failed (non-critical): {exc}")


# ── Trade-close broadcast helper (shared by confirmation + close-check runs) ──

def _broadcast_trade_closes(current_prices: dict) -> None:
    """Check and close trades for all recipients, sending close alerts + debriefs."""
    render_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    for uid in _all_recipients():
        try:
            closed = check_and_close_trades(current_prices, uid)
            for trade in closed:
                ticker  = trade["ticker"]
                outcome = trade["outcome"]
                sign    = "+" if trade["return_pct"] >= 0 else ""
                pct_str = f"<b>{sign}{trade['return_pct']:.1f}%</b>"
                price_str = f"<code>${trade['closed_price']}</code>"
                usd_str   = f"(${trade['gain_usd']:+.2f})"

                if outcome == "target":
                    close_msg = (
                        f"🎯 Your profit target was hit on {ticker} — great trade! "
                        f"Time to lock in your gains.\n"
                        f"Closed @ {price_str}  {pct_str}  {usd_str}"
                    )
                elif outcome == "stop":
                    close_msg = (
                        f"🛡 Your stop-loss triggered on {ticker} — this protection saved you "
                        f"from bigger losses. Sell now to close the position.\n"
                        f"Closed @ {price_str}  {pct_str}  {usd_str}"
                    )
                else:
                    close_msg = (
                        f"⏱ {ticker} position expired after 28 days. "
                        f"Review and decide: hold or close?\n"
                        f"Closed @ {price_str}  {pct_str}  {usd_str}"
                    )

                # Post-trade debrief via Haiku (non-critical)
                try:
                    debrief = generate_trade_debrief(trade)
                    if debrief:
                        close_msg += f"\n\n📖 <i>{debrief}</i>"
                except Exception as db_exc:
                    print(f"[agent] Trade debrief failed (non-critical): {db_exc}")

                # Build keyboard — always include Log as Sold, Dashboard only if URL set
                kb_row = [{"text": "✅ Log as Sold", "callback_data": f"sold_pick|{ticker}"}]
                if render_url:
                    kb_row.append({"text": "📊 Dashboard", "web_app": {"url": f"{render_url}/miniapp"}})
                send_inline_keyboard(close_msg, [kb_row], chat_id=uid)
        except Exception as exc:
            print(f"[agent] Trade close check failed for {uid} (non-critical): {exc}")


# ── Confirmation run ──────────────────────────────────────────────────────────

def run_confirmation():
    """Load morning picks, fetch live prices, send comparison message."""
    _log_cron_run("confirmation")
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

    # ── Per-user trade close checks ───────────────────────────────────────────
    _broadcast_trade_closes(current_prices)

    # ── Trailing stop nudges — suggest stop adjustments for open trades ──────
    for uid in _all_recipients():
        try:
            _check_trailing_stops(current_prices, uid)
        except Exception as exc:
            print(f"[agent] Trailing stop check failed for {uid} (non-critical): {exc}")

    # ── Price alerts ──────────────────────────────────────────────────────────
    try:
        fired = check_all_alerts(send_fn=_alert, send_keyboard_fn=send_inline_keyboard)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed (non-critical): {exc}")

    # ── Per-user earnings warnings ────────────────────────────────────────────
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

    # ── Watchlist signal alerts ───────────────────────────────────────────────
    # Fire a proactive alert when a watchlisted ticker hits a technical signal
    # (RSI < 40 bouncing, or MACD crossover) even if it didn't make today's picks.
    try:
        import yfinance as yf
        import pandas as pd
        pick_symbols = set()
        for s in picks.get("stocks", picks).get("short_term", []):
            pick_symbols.add(s.get("ticker", ""))
        for s in picks.get("stocks", picks).get("long_term", []):
            pick_symbols.add(s.get("ticker", ""))

        for uid in _all_recipients():
            try:
                from config_manager import get_user_config
                u_cfg     = get_user_config(uid)
                if u_cfg.get("paused") or u_cfg.get("skip_watchlist_alerts"):
                    continue
                watchlist = u_cfg.get("watchlist", [])
                # Only scan tickers NOT already in today's picks
                scan = [t for t in watchlist if t and t not in pick_symbols]
                if not scan:
                    continue
                hist = yf.download(" ".join(scan), period="60d", interval="1d",
                                   progress=False, auto_adjust=True)
                closes = hist["Close"] if hasattr(hist["Close"], "columns") else hist[["Close"]].rename(columns={"Close": scan[0]})
                for ticker in scan:
                    try:
                        col = closes[ticker] if ticker in closes.columns else closes.iloc[:, 0]
                        col = col.dropna()
                        if len(col) < 20:
                            continue
                        # RSI-14
                        delta  = col.diff()
                        gain   = delta.clip(lower=0).rolling(14).mean()
                        loss   = (-delta.clip(upper=0)).rolling(14).mean()
                        rs     = gain / loss.replace(0, float("nan"))
                        rsi    = (100 - 100 / (1 + rs)).iloc[-1]
                        # MACD crossover (12/26/9)
                        ema12  = col.ewm(span=12, adjust=False).mean()
                        ema26  = col.ewm(span=26, adjust=False).mean()
                        macd   = ema12 - ema26
                        signal = macd.ewm(span=9, adjust=False).mean()
                        crossed_up = macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2]
                        current_price = round(float(col.iloc[-1]), 2)
                        alerts = []
                        if rsi < 38:
                            alerts.append(f"RSI {rsi:.0f} — oversold")
                        if crossed_up:
                            alerts.append("MACD bullish crossover")
                        if alerts:
                            send_message(
                                f"👀 <b>Watchlist Signal — {ticker}</b>  <code>${current_price:,.2f}</code>\n"
                                f"<i>{' · '.join(alerts)}</i>\n"
                                f"Not in today's picks — but worth a look.",
                                chat_id=uid,
                            )
                    except Exception:
                        continue
            except Exception as exc:
                print(f"[agent] Watchlist scan failed for {uid} (non-critical): {exc}")
    except Exception as exc:
        print(f"[agent] Watchlist signal check failed (non-critical): {exc}")

    message = format_confirmation_message(picks, current_prices)
    # Broadcast to all users who haven't opted out
    if DRY_RUN:
        print(f"\n{'=' * 60}\nDRY RUN — 10:30 AM Confirmation (not sent):\n{'=' * 60}\n{message}")
    else:
        for uid in _all_recipients():
            try:
                ucfg = get_user_config(uid)
                if ucfg.get("paused") or ucfg.get("skip_confirmation"):
                    print(f"[agent] Skipping confirmation for {uid} (paused or opted out).")
                    continue
                send_message(message, chat_id=uid)
            except Exception as exc:
                print(f"[agent] WARNING: Confirmation send failed for {uid}: {exc}")

    # Mark as sent so retries don't fire again today
    if not DRY_RUN:
        try:
            picks["_confirmation_sent_date"] = today
            save_picks(picks)
        except Exception as exc:
            print(f"[agent] Could not stamp confirmation sent flag (non-critical): {exc}")


# ── Auto-stop alerts for AI picks ────────────────────────────────────────────

def _check_picks_stop_loss(current_prices: dict, picks: dict, uid: str) -> None:
    """
    Compare current prices against the stop_loss on today's AI picks.
    Fires a Telegram alert when a pick's current price falls below its stop.

    Uses stop_price from the sizing dict (ATR-based) if available, else stop_loss
    from the pick directly. Only alerts for stock/ETF/commodity picks — crypto
    is volatile enough that a separate threshold is impractical.

    Deduplication: persisted via cache_layer (Redis/Upstash when available,
    in-memory otherwise) so a user sees at most one alert per ticker per day
    even across bot restarts.
    """
    today_str = date.today().isoformat()
    cfg = get_user_config(uid)
    if cfg.get("paused"):
        return

    for cat in ("short_term", "long_term", "etf", "commodities"):
        for pick in picks.get(cat, []):
            ticker = (pick.get("ticker") or "").upper()
            if not ticker:
                continue

            # Determine stop price
            sizing     = pick.get("sizing") or {}
            stop_price = sizing.get("stop_price") or pick.get("stop_loss")
            if not stop_price or stop_price <= 0:
                continue

            current = current_prices.get(ticker)
            if not current or current <= 0:
                continue

            alert_key = f"{uid}:{ticker}:{today_str}"
            if _is_alerted(alert_key):
                continue

            if current < stop_price:
                _mark_alerted(alert_key)
                drop_pct = round((stop_price - current) / stop_price * 100, 1)
                try:
                    send_message(
                        f"🔴 <b>Stop Alert — {ticker}</b>\n"
                        f"Current price <code>${current:,.2f}</code> has breached today's "
                        f"suggested stop of <code>${stop_price:,.2f}</code> (↓{drop_pct}%).\n"
                        f"<i>Review your position — stop levels exist to protect capital.</i>",
                        chat_id=uid,
                    )
                    print(f"[agent] Stop alert fired: {ticker} @ ${current:.2f} < stop ${stop_price:.2f} for {uid}")
                except Exception as exc:
                    print(f"[agent] Stop alert send failed for {uid}/{ticker} (non-critical): {exc}")


# ── Persistent stop-alert deduplication ──────────────────────────────────────
# Keys are stored in cache_layer (Redis via Upstash when configured, in-memory
# otherwise).  TTL = 28 hours so stale keys never survive past overnight.

_ALERTED_STOPS_CACHE_KEY = "alerted_stops"
_ALERTED_STOPS_TTL       = 28 * 3600  # 28 hours


def _is_alerted(key: str) -> bool:
    """Return True if we have already fired an alert for this key today."""
    alerted: list = cache_get(_ALERTED_STOPS_CACHE_KEY) or []
    return key in alerted


def _mark_alerted(key: str) -> None:
    """Persist alert dedup key so restarts don't resend the same alert."""
    alerted: list = cache_get(_ALERTED_STOPS_CACHE_KEY) or []
    if key not in alerted:
        alerted.append(key)
    cache_set(_ALERTED_STOPS_CACHE_KEY, alerted, ttl_seconds=_ALERTED_STOPS_TTL)


# ── Close check (3:30 PM — silent unless a trade closed) ─────────────────────

def run_close_check():
    """3:30 PM run. Checks trades silently — only sends a message if target/stop hit."""
    _log_cron_run("close_check")
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

    _broadcast_trade_closes(current_prices)

    # ── Auto-stop alerts for today's AI picks ────────────────────────────────
    for uid in _all_recipients():
        try:
            _check_picks_stop_loss(current_prices, picks, uid)
        except Exception as exc:
            print(f"[agent] Auto-stop check failed for {uid} (non-critical): {exc}")

    # ── Trailing stop nudges ──────────────────────────────────────────────────
    for uid in _all_recipients():
        try:
            _check_trailing_stops(current_prices, uid)
        except Exception as exc:
            print(f"[agent] Trailing stop check failed for {uid} (non-critical): {exc}")

    # ── Price alerts ──────────────────────────────────────────────────────────
    try:
        fired = check_all_alerts(send_fn=_alert, send_keyboard_fn=send_inline_keyboard)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed (non-critical): {exc}")
    # Note: no always-send summary here — the 4:15 PM eod_summary run covers that.


def run_eod_summary():
    """
    4:15 PM run — after market close.
    Sends a rich end-of-day wrap-up: final close prices, per-category averages,
    and an optional Haiku-generated one-line commentary on how the day went.
    """
    _log_cron_run("eod_summary")
    print("[agent] Running end-of-day summary...")
    picks = load_picks()
    if not picks:
        print("[agent] No picks for today — skipping EOD summary.")
        return

    try:
        current_prices = get_current_prices(picks)
    except Exception as exc:
        print(f"[agent] Price fetch failed for EOD summary: {exc}")
        return

    for uid in _all_recipients():
        try:
            cfg = get_user_config(uid)
            if cfg.get("paused") or cfg.get("skip_eod"):
                continue
            log = load_user_trade_log(uid)
            watchlist = cfg.get("watchlist", [])
            msg = format_eod_full_summary(picks, current_prices, log.get("open", []), watchlist=watchlist)
            if msg:
                if DRY_RUN:
                    print(f"\nDRY RUN — EOD Full Summary for {uid}:\n{msg}")
                else:
                    send_message(msg, chat_id=uid)
        except Exception as exc:
            print(f"[agent] EOD full summary failed for {uid} (non-critical): {exc}")

    for uid in _all_recipients():
        try:
            _check_portfolio_health(uid, current_prices)
        except Exception as exc:
            print(f"[agent] Portfolio health check failed for {uid} (non-critical): {exc}")

    for uid in _all_recipients():
        try:
            _check_hold_or_fold(uid, current_prices)
        except Exception as exc:
            print(f"[agent] Hold/fold nudge failed for {uid} (non-critical): {exc}")


# ── Weekly recap (Saturday morning) ──────────────────────────────────────────

def run_weekly_recap(config: dict, now_et: datetime):
    """Saturday: run crypto morning picks, then send a compact weekly recap."""
    _log_cron_run("weekly")
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
                        send_inline_keyboard(
                            message,
                            [[
                                {"text": "📈 My Stats",       "callback_data": "cmd|STATS"},
                                {"text": "📊 My Positions",   "callback_data": "cmd|POSITIONS"},
                            ], [
                                {"text": "🏆 Community",      "callback_data": "cmd|COMMUNITY"},
                                {"text": "📋 Full History",   "callback_data": "cmd|HISTORY"},
                            ]],
                            chat_id=uid,
                        )
                except Exception as exc:
                    print(f"[agent] Weekly recap failed for {uid}: {exc}")
        else:
            print("[agent] No weekly picks data — skipping recap.")
    except Exception as exc:
        print(f"[agent] Weekly recap failed (non-critical): {exc}")


# ── Portfolio health nudge helper ────────────────────────────────────────────

def _check_portfolio_health(uid: str, current_prices: dict) -> None:
    """
    Proactive portfolio health check — fires once per ticker per day.
    Checks for concentration risk, positions near stop, and big winners ready to trim.
    """
    log    = load_user_trade_log(uid)
    trades = log.get("open", [])
    if not trades:
        return

    today = date.today()

    # Calculate total portfolio value based on current prices
    total_value = 0.0
    trade_values: list[tuple] = []
    for trade in trades:
        ticker = trade.get("ticker")
        entry  = trade.get("entry_price")
        shares = trade.get("shares") or trade.get("quantity")
        current = current_prices.get(ticker) if ticker else None
        if not (ticker and entry and current):
            continue
        # Estimate current position value: use shares if available, else allocation
        alloc = trade.get("allocation") or trade.get("budget") or 0
        if shares:
            pos_value = float(shares) * float(current)
        elif alloc:
            # approximate shares from original allocation and entry price
            est_shares = float(alloc) / float(entry)
            pos_value  = est_shares * float(current)
        else:
            continue
        trade_values.append((ticker, trade, float(current), float(entry), pos_value))
        total_value += pos_value

    if total_value <= 0:
        return

    for ticker, trade, curr_f, entry_f, pos_value in trade_values:
        pct_of_portfolio = (pos_value / total_value) * 100
        stop = trade.get("stop_loss")
        gain_pct = (curr_f - entry_f) / entry_f * 100

        # ── Concentration risk ────────────────────────────────────────────────
        if pct_of_portfolio > 35:
            key = f"health_{uid}_{ticker}_{today}"
            if not _is_alerted(key):
                _mark_alerted(key)
                send_message(
                    f"⚠️ <b>Concentration risk — {ticker}</b>\n"
                    f"This position is <b>{pct_of_portfolio:.0f}%</b> of your tracked portfolio. "
                    f"Consider trimming to reduce single-stock risk.",
                    chat_id=uid,
                )

        # ── Near stop ─────────────────────────────────────────────────────────
        if stop:
            stop_f = float(stop)
            pct_to_stop = (curr_f - stop_f) / curr_f * 100
            if 0 < pct_to_stop <= 3:
                key = f"health_stop_{uid}_{ticker}_{today}"
                if not _is_alerted(key):
                    _mark_alerted(key)
                    send_message(
                        f"⚠️ <b>{ticker}</b> is approaching your stop at "
                        f"<code>${_p(stop_f)}</code> — consider reviewing",
                        chat_id=uid,
                    )

        # ── Big winner ready to trim ──────────────────────────────────────────
        if gain_pct > 12:
            key = f"health_winner_{uid}_{ticker}_{today}"
            if not _is_alerted(key):
                _mark_alerted(key)
                send_message(
                    f"🎯 <b>{ticker}</b> is up <b>+{gain_pct:.1f}%</b> — "
                    f"consider taking partial profits or moving stop to breakeven",
                    chat_id=uid,
                )


# ── Trailing stop nudge helper ───────────────────────────────────────────────

def _check_trailing_stops(current_prices: dict, uid: str) -> None:
    """
    For each open trade for uid, check if the position has moved far enough
    to warrant adjusting the stop-loss. Fires once per threshold per trade,
    tracked via flags stamped onto the trade dict in the log.

    Thresholds:
      +8%  from entry → suggest moving stop to breakeven (entry price)
      90%  of target  → suggest trailing stop to -5% from current (lock in gains)
    """
    log         = load_user_trade_log(uid)
    open_trades = log.get("open", [])
    dirty       = False   # track whether we modified the log

    for trade in open_trades:
        ticker  = trade.get("ticker")
        entry   = trade.get("entry_price")
        target  = trade.get("target_price")
        current = current_prices.get(ticker) if ticker else None

        if not (ticker and entry and current):
            continue

        entry_f   = float(entry)
        current_f = float(current)
        gain_pct  = (current_f - entry_f) / entry_f * 100

        # ── Threshold 1: +8% — move stop to breakeven ────────────────────────
        if gain_pct >= 8.0 and not trade.get("_notified_breakeven"):
            trade["_notified_breakeven"] = True
            dirty = True
            send_message(
                f"📈 <b>{ticker}</b> is up <b>+{gain_pct:.1f}%</b> from your entry.\n"
                f"💡 Consider moving your stop-loss to <b>breakeven</b> "
                f"(<code>${_p(entry_f)}</code>) — you'd be playing with the house's money.",
                chat_id=uid,
            )

        # ── Threshold 2: 90% of the way to target — trail at -5% from now ────
        if target:
            target_f = float(target)
            if target_f > entry_f:
                progress = (current_f - entry_f) / (target_f - entry_f)
                if progress >= 0.90 and not trade.get("_notified_trail_90"):
                    trade["_notified_trail_90"] = True
                    dirty = True
                    lock_price = round(current_f * 0.95, 2)
                    remaining  = round((target_f - current_f) / current_f * 100, 1)
                    send_message(
                        f"🎯 <b>{ticker}</b> is 90% of the way to target "
                        f"(<code>${_p(target_f)}</code>, {remaining}% left).\n"
                        f"💡 Consider trailing your stop to <code>${_p(lock_price)}</code> "
                        f"(-5% from now) to lock in most of the gain.",
                        chat_id=uid,
                    )

    if dirty:
        log["open"] = open_trades
        save_user_trade_log(uid, log)


# ── Hold/fold nudge helper ───────────────────────────────────────────────────

def _check_hold_or_fold(uid: str, current_prices: dict) -> None:
    """
    For positions between -3% and the stop-loss: send a plain-English
    hold/fold nudge. Once per position per day via cache dedup.
    """
    log = load_user_trade_log(uid)
    for trade in log.get("open", []):
        ticker = trade.get("ticker")
        entry  = trade.get("entry_price")
        stop   = trade.get("stop_loss")
        if not (ticker and entry and stop):
            continue
        current = current_prices.get(ticker)
        if not current:
            continue
        entry_f, stop_f, curr_f = float(entry), float(stop), float(current)
        pct_from_entry = (curr_f - entry_f) / entry_f * 100
        pct_to_stop    = (curr_f - stop_f)  / curr_f  * 100
        # Only nudge when down 2–8% AND within 5% of stop
        if -8.0 <= pct_from_entry <= -2.0 and pct_to_stop <= 5.0:
            key = f"holdfold_{uid}_{ticker}_{date.today()}"
            if _is_alerted(key):
                continue
            _mark_alerted(key)
            send_message(
                f"🤔 <b>Hold or fold? — {ticker}</b>\n"
                f"Down <b>{pct_from_entry:.1f}%</b> from your entry. "
                f"Stop is at <code>${_p(stop_f)}</code> "
                f"({pct_to_stop:.1f}% away).\n\n"
                f"<i>If the thesis still holds, stay in. "
                f"If something has changed, it's okay to cut early and protect capital.</i>\n\n"
                f"<code>/sold {ticker}</code> to close  ·  <code>/updatestop {ticker}</code> to adjust",
                chat_id=uid,
            )


# ── Macro events helper ──────────────────────────────────────────────────────

def _get_macro_events_this_week() -> list[str]:
    """Return a list of notable macro events this week (Mon–Fri)."""
    # We can't reliably fetch a live economic calendar for free,
    # so we return a short note prompting awareness.
    return []  # Future: wire to an economic calendar API


# ── Sunday Week Ahead briefing ───────────────────────────────────────────────

def run_week_ahead(config: dict):
    """
    Sunday run — send a standalone 'Week Ahead' message to all users.
    Includes: upcoming earnings for watchlisted tickers, current market
    regime, and the standard week-ahead commentary from format_week_ahead().
    No new picks are generated — this is a pure briefing.
    """
    _log_cron_run("week_ahead")
    print("[agent] Building Sunday Week Ahead briefing...")

    # Gather tickers from all users' watchlists + any still-open trades
    all_tickers: list[str] = []
    for uid in _all_recipients():
        try:
            wl = get_user_config(uid).get("watchlist", [])
            all_tickers.extend(wl)
        except Exception:
            pass
    # Include this week's last saved picks if available
    try:
        from config_manager import load_weekly_picks
        weekly = load_weekly_picks()
        if weekly:
            latest_picks = list(weekly.values())[-1]
            stocks = latest_picks.get("stocks", latest_picks)
            for s in stocks.get("short_term", []) + stocks.get("long_term", []):
                t = s.get("ticker")
                if t:
                    all_tickers.append(t)
    except Exception as exc:
        print(f"[agent] Week-ahead: weekly picks load failed (non-critical): {exc}")

    all_tickers = list(dict.fromkeys(t for t in all_tickers if t))

    # Fetch earnings + regime
    earnings_week: dict = {}
    try:
        from earnings_checker import get_upcoming_earnings
        earnings_week = get_upcoming_earnings(all_tickers, days_ahead=5) if all_tickers else {}
        print(f"[agent] Week-ahead: {len(earnings_week)} earnings events found.")
    except Exception as exc:
        print(f"[agent] Week-ahead: earnings fetch failed (non-critical): {exc}")

    regime: dict = {}
    try:
        from market_regime import get_market_regime
        regime = get_market_regime()
    except Exception as exc:
        print(f"[agent] Week-ahead: regime fetch failed (non-critical): {exc}")

    week_msg = format_week_ahead(earnings_week, regime)

    if not week_msg:
        print("[agent] Week-ahead message was empty — skipping.")
        return

    macro_events = _get_macro_events_this_week()

    recipients = _all_recipients()
    print(f"[agent] Sending Sunday Week Ahead to {len(recipients)} user(s)...")
    for uid in recipients:
        try:
            user_cfg = {**config, **get_user_config(uid)}
            if user_cfg.get("paused"):
                print(f"[agent] Skipping week-ahead for {uid} — picks paused.")
                continue

            # Build personalised earnings prefix for this user's open positions + watchlist
            user_log = load_user_trade_log(uid)
            open_tickers     = [t["ticker"] for t in user_log.get("open", []) if t.get("asset_type") == "stock"]
            watchlist_tickers = user_log.get("watchlist", [])
            personal_tickers = list(dict.fromkeys(open_tickers + watchlist_tickers))

            personal_earnings: dict = {}
            if personal_tickers:
                try:
                    from earnings_checker import get_upcoming_earnings
                    personal_earnings = get_upcoming_earnings(personal_tickers, days_ahead=7)
                except Exception:
                    pass

            personal_prefix = ""
            if personal_earnings:
                lines = []
                for t, d in list(personal_earnings.items())[:5]:
                    badge = "💼" if t in open_tickers else "👁"
                    lines.append(f"  {badge} <b>{t}</b> — {d}")
                personal_prefix = "📅 <b>Your Earnings This Week</b>\n" + "\n".join(lines) + "\n\n"

            final_msg = personal_prefix + week_msg

            if DRY_RUN:
                print(f"\n{'=' * 60}\nDRY RUN — Week Ahead for {uid}:\n{'=' * 60}\n{final_msg}\n")
            else:
                send_message(final_msg, chat_id=uid)
        except Exception as exc:
            print(f"[agent] Week-ahead send failed for {uid}: {exc}")

    print("[agent] Sunday Week Ahead complete.")


# ── Pre-market pulse (8:45 AM ET Mon–Fri, before the open) ──────────────────

def run_premarket(config: dict):
    """
    8:45 AM run — fires 45 min before market open.
    For each user with open stock positions: fetch pre-market prices via yfinance
    and send a brief 'heading into the open' message.

    Two signals:
      - Big pre-market move (>= 3% either direction) → urgent alert
      - Quiet open (<3% on all positions) → one compact digest message

    Skipped entirely if a user has no open stock positions.
    """
    _log_cron_run("premarket")
    import yfinance as yf
    print("[agent] Running pre-market pulse...")

    for uid in _all_recipients():
        try:
            user_cfg = {**config, **get_user_config(uid)}
            if user_cfg.get("paused") or user_cfg.get("skip_premarket"):
                continue

            log         = load_user_trade_log(uid)
            open_trades = log.get("open", [])
            # Only stocks — crypto is 24/7 and doesn't have a "pre-market"
            stock_trades = [t for t in open_trades if t.get("asset_type") == "stock"]
            if not stock_trades:
                continue

            # Deduplicate tickers
            seen: set = set()
            unique = []
            for t in stock_trades:
                if t["ticker"] not in seen:
                    seen.add(t["ticker"])
                    unique.append(t)

            # Fetch pre-market price for each ticker
            position_lines = []
            big_movers     = []
            sector_by_ticker: dict[str, str] = {}   # for concentration check

            for trade in unique:
                ticker = trade["ticker"]
                try:
                    info        = yf.Ticker(ticker).info
                    pre_price   = info.get("preMarketPrice") or info.get("currentPrice")
                    prev_close  = info.get("regularMarketPreviousClose") or info.get("previousClose")
                    entry       = trade.get("entry_price")
                    # Capture sector for concentration check (stocks only)
                    if trade.get("asset_type") == "stock":
                        sector_by_ticker[ticker] = info.get("sector", "Unknown")

                    if not pre_price:
                        position_lines.append(f"  <b>{ticker}</b>  <i>pre-market data unavailable</i>")
                        continue

                    pre_price  = round(float(pre_price), 2)
                    prev_close = round(float(prev_close), 2) if prev_close else None

                    # % vs previous close
                    if prev_close:
                        vs_prev = (pre_price - prev_close) / prev_close * 100
                        vs_prev_str = f"{'+' if vs_prev >= 0 else ''}{vs_prev:.1f}% vs close"
                    else:
                        vs_prev     = 0
                        vs_prev_str = ""

                    # % vs entry
                    vs_entry_str = ""
                    if entry:
                        vs_entry = (pre_price - float(entry)) / float(entry) * 100
                        vs_entry_str = f"  ·  {'+' if vs_entry >= 0 else ''}{vs_entry:.1f}% vs entry"

                    # Stop proximity
                    stop = trade.get("stop_loss")
                    stop_badge = ""
                    if stop and pre_price <= float(stop) * 1.02:
                        stop_badge = "  ⚠️ <b>NEAR STOP</b>"

                    move_icon = "🔴" if vs_prev <= -3 else ("🟢" if vs_prev >= 3 else "⚪")
                    line = (
                        f"  {move_icon} <b>{ticker}</b>  <code>${_p(pre_price)}</code>  "
                        f"<i>{vs_prev_str}{vs_entry_str}</i>{stop_badge}"
                    )
                    position_lines.append(line)

                    if abs(vs_prev) >= 3:
                        big_movers.append((ticker, pre_price, vs_prev, stop))

                except Exception as exc:
                    print(f"[agent] Pre-market fetch failed for {ticker}: {exc}")
                    position_lines.append(f"  <b>{ticker}</b>  <i>data unavailable</i>")

            if not position_lines:
                continue

            # ── Sector concentration warning ──────────────────────────────────
            concentration_line = ""
            stock_count = len(sector_by_ticker)
            if stock_count >= 2:
                sector_counts: dict[str, int] = {}
                for s in sector_by_ticker.values():
                    sector_counts[s] = sector_counts.get(s, 0) + 1
                top_sector, top_n = max(sector_counts.items(), key=lambda x: x[1])
                if top_sector and top_sector != "Unknown" and top_n / stock_count >= 0.5:
                    pct = int(top_n / stock_count * 100)
                    concentration_line = f"\n⚡ <i>{pct}% of your holdings are {top_sector} — consider diversifying</i>"

            # ── Earnings risk on open positions (2-day window) ─────────────────
            from earnings_checker import get_upcoming_earnings
            earnings_map = get_upcoming_earnings(list(sector_by_ticker.keys()), days_ahead=2)
            earnings_section = ""
            if earnings_map:
                e_lines = [
                    f"  📅 <b>{t}</b> reports {d} — review position size"
                    for t, d in sorted(earnings_map.items())
                ]
                earnings_section = "\n\n⚠️ <b>Earnings this week (open positions)</b>\n" + "\n".join(e_lines)

            # ── Big mover alerts (sent first, individually) ───────────────────
            for ticker, price, pct, stop in big_movers:
                direction = "gapping UP" if pct > 0 else "gapping DOWN"
                action    = ""
                if pct <= -3 and stop and price <= float(stop) * 1.05:
                    action = "\n⚠️ <b>Approaching stop-loss</b> — consider your risk before open."
                elif pct >= 5:
                    action = "\n💡 Strong open expected — consider taking partial profits near target."
                send_message(
                    f"{'🟢' if pct > 0 else '🔴'} <b>{ticker}</b> {direction} <b>{'+' if pct > 0 else ''}{pct:.1f}%</b> "
                    f"pre-market → <code>${_p(price)}</code>{action}",
                    chat_id=uid,
                )

            body = "\n".join(position_lines)
            n    = len(unique)
            send_message(
                f"🌅 <b>Pre-market — your {n} position{'s' if n != 1 else ''}</b>\n\n"
                f"{body}"
                f"{concentration_line}"
                f"{earnings_section}\n\n"
                f"<i>Market opens in ~45 min. 🟢 = +3%+  🔴 = -3%+  ⚪ = quiet</i>",
                chat_id=uid,
            )

        except Exception as exc:
            print(f"[agent] Pre-market pulse failed for {uid} (non-critical): {exc}")

    print("[agent] Pre-market pulse complete.")


# ── Intraday price-alert-only run (every 30 min during market hours) ─────────

def run_price_alerts():
    """
    Lightweight run: check price alerts + trailing stops only.
    No Claude call, no screener — just yfinance price fetch.
    Designed to run every 30 minutes during market hours.
    """
    _log_cron_run("price_alerts")
    print("[agent] Running intraday price alerts check...")

    # Check user-set price alerts (above/below thresholds)
    try:
        fired = check_all_alerts(send_fn=_alert, send_keyboard_fn=send_inline_keyboard)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
        else:
            print("[agent] No price alerts triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed: {exc}")

    # Also check trade targets/stops on open positions using live prices — per user
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

            newly_closed = check_and_close_trades(current_prices, uid)
            for trade in newly_closed:
                ticker = trade["ticker"]
                sign   = "+" if trade["return_pct"] >= 0 else ""
                pct    = f"{sign}{trade['return_pct']:.1f}%"
                usd    = f"${trade['gain_usd']:+.2f}"
                price  = f"${trade['closed_price']}"

                if trade["outcome"] == "target":
                    msg = (
                        f"🎯 <b>{ticker} TARGET HIT!</b>\n"
                        f"Closed @ <code>{price}</code>  ·  <b>{pct}</b>  ({usd})\n"
                        f"<i>Position auto-closed and logged to your history.</i>"
                    )
                    kb = [[
                        {"text": "📊 View Portfolio",  "callback_data": "cmd|POSITIONS"},
                        {"text": "📈 My Stats",         "callback_data": "cmd|STATS"},
                    ]]
                elif trade["outcome"] == "stop":
                    msg = (
                        f"🔴 <b>{ticker} STOP HIT</b>\n"
                        f"Closed @ <code>{price}</code>  ·  <b>{pct}</b>  ({usd})\n"
                        f"<i>Position auto-closed. Risk managed ✓</i>"
                    )
                    kb = [[
                        {"text": "📊 View Portfolio",  "callback_data": "cmd|POSITIONS"},
                        {"text": "📈 Today's Picks",   "callback_data": "cmd|TODAY"},
                    ]]
                else:  # expired
                    msg = (
                        f"⏱ <b>{ticker} position expired</b> (28 days)\n"
                        f"Closed @ <code>{price}</code>  ·  <b>{pct}</b>  ({usd})"
                    )
                    kb = [[{"text": "📊 View Portfolio", "callback_data": "cmd|POSITIONS"}]]

                send_inline_keyboard(msg, kb, chat_id=uid)

                # Target-approach alert: warn when within 5% of target (not yet hit)
            for trade in log.get("open", []):
                ticker  = trade["ticker"]
                current = current_prices.get(ticker)
                target  = trade.get("target_price")
                entry   = trade.get("entry_price")
                if not (current and target and entry and float(target) > float(entry)):
                    continue
                gap_pct = (float(target) - float(current)) / float(current) * 100
                if 0 < gap_pct <= 5:
                    alert_key = f"target_approach_{uid}_{ticker}_{date.today().isoformat()}"
                    from cache_layer import get_cache, set_cache
                    if not get_cache(alert_key):
                        set_cache(alert_key, "1", ttl=86400)
                        send_inline_keyboard(
                            f"⚡ <b>{ticker}</b> is <b>{gap_pct:.1f}%</b> from your target <code>${float(target):.2f}</code>\n"
                            f"Current: <code>${float(current):.2f}</code>  ·  Consider taking partial profit.",
                            [[
                                {"text": f"✅ Close {ticker}", "callback_data": f"sold_pick|{ticker}"},
                                {"text": "Hold 💎",           "callback_data": "noop"},
                            ]],
                            chat_id=uid,
                        )
        except Exception as exc:
            print(f"[agent] Intraday trade check failed for {uid} (non-critical): {exc}")

    # ── Watchlist big-move alerts (>3% intraday) ─────────────────────────────
    try:
        from cache_layer import get_cache, set_cache
        from datetime import date as _date
        for uid in _all_recipients():
            try:
                log = load_user_trade_log(uid)
                watch_tickers = log.get("watchlist", [])
                if not watch_tickers:
                    continue
                raw_w = yf.download(
                    " ".join(watch_tickers), period="2d", interval="1d",
                    progress=False, auto_adjust=True
                )
                if raw_w.empty:
                    continue
                close = raw_w["Close"]
                for t in watch_tickers:
                    try:
                        col = close[t] if t in close.columns else close
                        vals = col.dropna()
                        if len(vals) < 2:
                            continue
                        prev, curr = float(vals.iloc[-2]), float(vals.iloc[-1])
                        pct_chg = (curr - prev) / prev * 100
                        if abs(pct_chg) < 3:
                            continue
                        key = f"watch_move_{uid}_{t}_{_date.today().isoformat()}"
                        if get_cache(key):
                            continue
                        set_cache(key, "1", ttl=86400)
                        emoji = "🚀" if pct_chg > 0 else "🔻"
                        sign  = "+" if pct_chg > 0 else ""
                        send_inline_keyboard(
                            f"{emoji} <b>{t}</b> is moving <b>{sign}{pct_chg:.1f}%</b> today\n"
                            f"Price: <code>${curr:.2f}</code>  (was <code>${prev:.2f}</code>)",
                            [[
                                {"text": f"📊 View {t} Chart",  "callback_data": f"cmd|CHART {t}"},
                                {"text": "🔔 Set Alert",         "callback_data": f"alert|{t}"},
                            ]],
                            chat_id=uid,
                        )
                    except Exception:
                        pass
            except Exception as exc:
                print(f"[agent] Watchlist move check failed for {uid}: {exc}")
    except Exception as exc:
        print(f"[agent] Watchlist move alerts failed (non-critical): {exc}")

    # ── Auto-stop alerts for today's AI picks ────────────────────────────────
    picks = load_picks()
    if picks:
        try:
            picks_prices = get_current_prices(picks)
            for uid in _all_recipients():
                try:
                    _check_picks_stop_loss(picks_prices, picks, uid)
                except Exception as exc:
                    print(f"[agent] Auto-stop (price_alerts) failed for {uid} (non-critical): {exc}")
        except Exception as exc:
            print(f"[agent] Price fetch for auto-stop check failed (non-critical): {exc}")


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
    elif mode == "premarket":
        run_premarket(config)
    elif mode == "morning":
        run_morning(config, now_et)
    elif mode == "weekly":
        run_weekly_recap(config, now_et)
    elif mode == "week_ahead":
        run_week_ahead(config)
    elif mode == "close_check":
        run_close_check()
    elif mode == "eod_summary":
        run_eod_summary()
    elif mode == "price_alerts":
        run_price_alerts()
    else:
        run_confirmation()

    print(f"[agent] Done ({mode}) for {now_et.strftime('%Y-%m-%d')}.")


def compute_pick_streaks(weekly_picks: dict) -> dict:
    """
    Given weekly_picks = {date_str: picks_dict, ...}, return a dict of
    {ticker/symbol: consecutive_day_count} for tickers that appear in today's
    picks AND appeared on each immediately prior trading day in the weekly data.

    Only tickers with a streak ≥ 2 are included.
    Called once per morning run; result passed to format_daily_message().
    """
    from datetime import date, timedelta

    today     = date.today()
    today_str = today.isoformat()

    # Build {date_str: set(ticker)} from weekly picks
    tickers_by_day: dict[str, set] = {}
    for date_str, picks in weekly_picks.items():
        stocks = picks.get("stocks", picks)
        crypto = picks.get("crypto", {})
        day_set: set = set()
        for s in stocks.get("short_term", []) + stocks.get("long_term", []):
            t = s.get("ticker")
            if t:
                day_set.add(t)
        for c in crypto.get("short_term", []) + crypto.get("long_term", []):
            sym = c.get("symbol", "")
            if sym:
                day_set.add(sym)
        tickers_by_day[date_str] = day_set

    today_tickers = tickers_by_day.get(today_str, set())
    if not today_tickers:
        return {}

    streaks: dict[str, int] = {}
    for ticker in today_tickers:
        streak      = 1
        check_date  = today - timedelta(days=1)
        # Walk backwards through calendar days; skip days with no data (weekend/holiday)
        # but stop as soon as a market day has data and the ticker is absent
        checked_days = 0
        while checked_days < 7:   # never look back more than 7 calendar days
            check_str = check_date.isoformat()
            if check_str in tickers_by_day:
                if ticker in tickers_by_day[check_str]:
                    streak += 1
                    checked_days += 1
                    check_date -= timedelta(days=1)
                    continue
                else:
                    break   # market day present but ticker missing — streak ends
            # No data for this calendar day (weekend/holiday) — skip it
            check_date  -= timedelta(days=1)
            checked_days += 1
        if streak >= 2:
            streaks[ticker] = streak

    return streaks


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
        broadcast_all([{"chat_id": uid, "text": message} for uid in recipients])


def _auto_set_pick_alerts(picks: dict, recipients: list[str]) -> None:
    """Auto-set stop-loss + entry alerts for morning picks. Silent — never raises."""
    try:
        all_sections = (
            picks.get("stocks", {}).get("short_term", []) +
            picks.get("stocks", {}).get("long_term",  []) +
            picks.get("crypto", {}).get("short_term", []) +
            picks.get("etfs",   {}).get("short_term", []) +
            picks.get("etfs",   {}).get("long_term",  []) +
            picks.get("commodities", {}).get("short_term", []) +
            picks.get("commodities", {}).get("long_term",  [])
        )
        total_set = 0
        for uid in recipients:
            try:
                log = load_user_trade_log(uid)
                open_tickers = {t.get("ticker", "").upper() for t in log.get("open", [])}
                for pick in all_sections:
                    ticker = (pick.get("ticker") or pick.get("symbol") or "").upper()
                    if not ticker or ticker in open_tickers:
                        continue
                    try:
                        stop = pick.get("stop_loss")
                        if stop:
                            add_alert(uid, ticker, float(stop), direction="below")
                            total_set += 1
                        entry_low = pick.get("entry_low")
                        if entry_low:
                            add_alert(uid, ticker, float(entry_low), direction="below")
                            total_set += 1
                    except Exception as exc:
                        print(f"[agent] _auto_set_pick_alerts: alert set failed for {ticker}/{uid}: {exc}")
            except Exception as exc:
                print(f"[agent] _auto_set_pick_alerts: user {uid} failed (non-critical): {exc}")
        print(f"[agent] _auto_set_pick_alerts: set {total_set} alert(s) across {len(recipients)} user(s).")
    except Exception as exc:
        print(f"[agent] _auto_set_pick_alerts failed (non-critical): {exc}")


def _send_morning_personalised(picks: dict, global_config: dict, label: str = ""):
    """
    Send the morning briefing to all users, personalised per user's config.
    Each user gets their own budget/pick_mode applied, the same underlying picks,
    and a Haiku-generated personal note per pick explaining portfolio fit.
    """
    recipients = _all_recipients()
    print(f"[agent] Sending personalised {label} to {len(recipients)} user(s)...")

    # ── Streak tracking — computed once, shared across all users ─────────────
    pick_streaks: dict = {}
    try:
        from config_manager import load_weekly_picks
        weekly = load_weekly_picks()
        pick_streaks = compute_pick_streaks(weekly)
        if pick_streaks:
            print(f"[agent] Streak tickers: {pick_streaks}")
    except Exception as streak_exc:
        print(f"[agent] Streak computation failed (non-critical): {streak_exc}")

    # ── Pre-compute personalised notes in ONE batch call ─────────────────────
    # Collect only users who have open positions (no positions → no useful note).
    # Batch-call Haiku once for all of them instead of N individual calls.
    users_batch_data: list[dict] = []
    user_positions_cache: dict[str, list] = {}   # uid → open positions

    for uid in recipients:
        try:
            user_cfg_tmp = {**global_config, **get_user_config(uid)}
            if user_cfg_tmp.get("paused"):
                continue
            log            = load_user_trade_log(uid)
            open_positions = log.get("open", [])
            user_positions_cache[uid] = open_positions
            if open_positions:   # only users with positions need personalisation
                users_batch_data.append({
                    "uid":          uid,
                    "positions":    open_positions,
                    "risk_profile": user_cfg_tmp.get("risk_profile", "moderate"),
                })
        except Exception:
            user_positions_cache[uid] = []

    # One batch Haiku call → {uid: {ticker: note}}
    all_personal_notes: dict[str, dict] = {}
    if users_batch_data:
        try:
            all_personal_notes = personalize_picks_batch(picks, users_batch_data)
            print(f"[agent] Batch personalisation done: {len(users_batch_data)} users, "
                  f"{len(all_personal_notes)} notes generated.")
        except Exception as batch_exc:
            print(f"[agent] personalize_picks_batch failed (non-critical): {batch_exc}")

    # Load buy counts once (shared across all users)
    buy_counts: dict = {}
    if global_config.get("show_buy_counts"):
        try:
            from config_manager import load_buy_counts
            buy_counts = load_buy_counts()
        except Exception:
            pass

    # ── Recent performance stats — computed once, shown in morning performance bar ──
    recent_stats = None
    try:
        from performance_tracker import get_recent_stats
        all_logs = [load_user_trade_log(uid) for uid in recipients]
        recent_stats = get_recent_stats(all_logs)
        if recent_stats:
            print(f"[agent] recent_stats: {recent_stats['total']['count']} trades over {recent_stats['days']}d")
    except Exception as exc:
        print(f"[agent] recent_stats failed (non-critical): {exc}")

    # ── Build all per-user payloads first (CPU-bound, fast), then broadcast concurrently ──
    outbox: list[dict] = []
    for uid in recipients:
        try:
            user_cfg = {**global_config, **get_user_config(uid)}
            if user_cfg.get("paused"):
                print(f"[agent] Skipping {uid} — picks paused by user.")
                continue

            # Look up pre-computed notes — no Haiku call here
            personal_notes: dict = all_personal_notes.get(uid, {})

            message = format_daily_message(picks, user_cfg, personal_notes=personal_notes,
                                           pick_streaks=pick_streaks, buy_counts=buy_counts,
                                           recent_stats=recent_stats)

            # Append /bought tip for users who have never logged a trade
            user_log = user_positions_cache.get(uid, None)
            if user_log is None:
                try:
                    user_log = load_user_trade_log(uid).get("open", [])
                except Exception:
                    user_log = []
            log_obj = load_user_trade_log(uid) if uid in user_positions_cache else {"open": user_log, "closed": []}
            if len(log_obj.get("open", [])) == 0 and len(log_obj.get("closed", [])) == 0:
                message += (
                    "\n\n<i>💡 New here? If you place a trade, send <code>/bought TICKER</code> "
                    "— e.g. <code>/bought NVDA</code>. That's how your win rate and P&amp;L get tracked in /stats.</i>"
                )

            if DRY_RUN:
                print(f"\n{'=' * 60}\nDRY RUN — {label} for {uid}:\n{'=' * 60}\n{message}\n")
            else:
                kb = build_picks_keyboard(picks, user_cfg)
                outbox.append({"chat_id": uid, "text": message, "keyboard": kb or None})
        except Exception as exc:
            print(f"[agent] WARNING: Could not prepare morning message for {uid}: {exc}")

    # Fire all messages concurrently — 1000 users in ~3-5 s instead of ~500 s
    if outbox:
        broadcast_all(outbox)


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
        broadcast_all([{"chat_id": uid, "text": text} for uid in _all_recipients()])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print(f"⚠ Unhandled error: {exc}\n{traceback.format_exc()}")
        if not DRY_RUN:
            send_message(f"⚠ Agent crashed: {exc}. Check GitHub Actions logs.")
        sys.exit(1)
