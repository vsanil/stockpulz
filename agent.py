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
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, date, timedelta, timezone

import pytz

# ── Sentry error tracking (subprocess — separate from Flask's Sentry) ─────────
try:
    import sentry_sdk
    _sentry_dsn = os.environ.get("SENTRY_DSN", "")
    if _sentry_dsn:
        sentry_sdk.init(dsn=_sentry_dsn, send_default_pii=False, traces_sample_rate=0.0)
except Exception:
    pass  # Sentry is non-critical — never block the agent

# ── Mini App deep-link button helper ─────────────────────────────────────────
# Tab names mirror the Mini App nav: picks | portfolio | performance | watchlist | settings
def _miniapp_btn(label: str, tab: str, fallback_cmd: str) -> dict:
    """Return a web_app button that opens the Mini App at the given tab.
    Falls back to callback_data if APP_URL env var is not configured."""
    _app_url = os.environ.get("APP_URL", "").rstrip("/")
    if _app_url:
        return {"text": label, "web_app": {"url": f"{_app_url}/miniapp?tab={tab}"}}
    return {"text": label, "callback_data": f"cmd|{fallback_cmd}"}


def _miniapp_url_btn(label: str, path: str, fallback_cb: str) -> dict:
    """web_app button with an arbitrary miniapp path/query string.
    Falls back to callback_data if APP_URL is not set.
    path should start with '?' or '/' e.g. '?chart=AAPL' or '?tab=watchlist&alert=AAPL'
    """
    _app_url = os.environ.get("APP_URL", "").rstrip("/")
    if _app_url:
        sep = "" if path.startswith("?") else "/"
        return {"text": label, "web_app": {"url": f"{_app_url}/miniapp{sep}{path}"}}
    return {"text": label, "callback_data": fallback_cb}

from config_manager import (
    get_config, update_config, save_picks, load_picks, save_weekly_pick,
    get_dynamic_pick_counts, get_user_config, update_user_config,
    load_user_trade_log,
    save_screener_cache, load_screener_cache, et_today,
)
# 🔴 Both of these were CALLED but never imported — a NameError swallowed by the
# surrounding catch-all, so the feature silently did nothing. update_user_config
# meant trade reminders were never cleared (so one re-fired every day, forever)
# and alerts_sent_count never incremented; _get_client meant run_friday_wrap's
# AI weekly lesson was never generated. Found by converting the delivery loops.
from llm_client import _get_client
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
    format_eod_full_summary, format_week_ahead, build_picks_keyboard, _p,
)
from telegram_api import send_message, send_inline_keyboard, broadcast_all, send_photo
from cache_layer import cache_get, cache_set, cache_delete

ET        = pytz.timezone("America/New_York")
DRY_RUN   = os.environ.get("DRY_RUN",   "false").lower() == "true"


# "Near stop" proximity — single definition for the NEAR STOP badge and the EOD
# health insight (was 2% on the badge vs 3% in health). A position is "near stop"
# when price is within this % above its stop.
STOP_PROXIMITY_PCT = 3.0


def _stop_badge(price, stop) -> str:
    """
    Position badge for stop proximity. Distinguishes ALREADY past the stop (loud)
    from merely near it — a position 13% BELOW its stop must not read 'NEAR STOP'
    (that was the BNB bug: $560 shown 'near' a $645 stop it had blown through).
      price <= stop            → 🔴 STOP HIT
      stop < price <= stop*1.03 → ⚠️ NEAR STOP
    """
    try:
        price, stop = float(price), float(stop)
    except (TypeError, ValueError):
        return ""
    if not (stop > 0 and price > 0):
        return ""
    if price <= stop:
        return "  🔴 <b>STOP HIT</b>"
    if price <= stop * (1 + STOP_PROXIMITY_PCT / 100):
        return "  ⚠️ <b>NEAR STOP</b>"
    return ""


def _is_pos(x) -> bool:
    """
    True only for a real, positive number. Rejects None, 0, negatives, and nan.
    `float(nan) > 0` is False and `float(None)` raises — so both are excluded.
    Use at every nudge/alert guard: a non-positive/nan price is a failed fetch,
    not a real quote, and `if not x` lets nan through (nan is truthy).
    """
    try:
        return float(x) > 0
    except (TypeError, ValueError):
        return False


def _yf_symbol_map(tickers: list[str]) -> dict[str, str]:
    """
    Map original tickers → {yfinance_symbol: original_ticker}.
    Crypto symbols get the -USD suffix (BTC → BTC-USD); without it yfinance
    resolves a bare "BTC" to an unrelated instrument (the ~$28 BTC bug).
    """
    from price_checker import _SYMBOL_TO_CG_ID
    return {(f"{t}-USD" if t.upper() in _SYMBOL_TO_CG_ID else t): t for t in tickers}


def _download_prices(tickers: list[str], **yf_kwargs) -> dict[str, float]:
    """
    Fetch latest close prices for a list of tickers.
    Handles crypto symbols: BNB → BNB-USD, ETH → ETH-USD, etc.
    Returns {orig_ticker: price} — keys always use the original symbol.
    """
    if not tickers:
        return {}
    from price_checker import _SYMBOL_TO_CG_ID
    # Build yfinance symbol → original symbol mapping
    yf_map: dict[str, str] = {}
    for t in tickers:
        yf_t = f"{t}-USD" if t.upper() in _SYMBOL_TO_CG_ID else t
        yf_map[yf_t] = t
    yf_tickers = list(yf_map.keys())
    try:
        raw = yf.download(" ".join(yf_tickers), progress=False, **yf_kwargs)
        if raw.empty:
            return {}
        close = raw["Close"] if "Close" in raw else raw
        prices: dict[str, float] = {}
        if hasattr(close, "columns"):
            for yf_t, orig_t in yf_map.items():
                if yf_t in close.columns:
                    try:
                        prices[orig_t] = float(close[yf_t].dropna().iloc[-1])
                    except Exception:
                        pass
        elif len(yf_tickers) == 1:
            try:
                orig = list(yf_map.values())[0]
                prices[orig] = float(close.dropna().iloc[-1])
            except Exception:
                pass
        return prices
    except Exception:
        return {}
MOCK_DATA = os.environ.get("MOCK_DATA", "false").lower() == "true"

# Default conviction threshold — picks below this are filtered before broadcast.
# 3 = allow ★★★ (2 confirming signals); 4 = require ★★★★ (3 signals).
# Set to 3 so moderate-days still surface picks; conservative users can raise in /settings.
DEFAULT_MIN_CONVICTION = 3


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
            "near_misses": s.get("near_misses", {}),  # pass through, no filtering
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
        update_config(f"cron_last_{mode}", datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        print(f"[agent] cron log failed for {mode}: {exc}")

CRYPTO_RETRY_DELAYS = [15, 30, 60, 120]   # seconds between retries (4 attempts after first)

VIX_ALERT_THRESHOLD = 25   # warn when VIX exceeds this level


# ── US Market holiday helpers (module-level, shared by detector + name lookup) ─

def _holiday_observed(fixed: date) -> date:
    """Shift a fixed-date holiday to its observed date when it falls on a weekend."""
    if fixed.weekday() == 5:   # Saturday → Friday
        return fixed - timedelta(days=1)
    if fixed.weekday() == 6:   # Sunday → Monday
        return fixed + timedelta(days=1)
    return fixed


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of weekday (0=Mon…6=Sun) in the given month."""
    first  = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday in the given month."""
    import calendar
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Computus algorithm — returns Easter Sunday for the given year."""
    a       = year % 19
    b, c    = divmod(year, 100)
    d2, e   = divmod(b, 4)
    f       = (b + 8) // 25
    g       = (b - f + 1) // 3
    h       = (19 * a + b - d2 - g + 15) % 30
    i, k    = divmod(c, 4)
    l       = (32 + 2 * e + 2 * i - h - k) % 7
    m       = (a + 11 * h + 22 * l) // 451
    mo, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, mo, day + 1)


def _us_market_holidays(year: int) -> dict:
    """Return {date: name} for all US stock-market holidays in the given year."""
    return {
        _holiday_observed(date(year, 1, 1)):           "New Year's Day",
        _nth_weekday_of_month(year, 1, 0, 3):          "MLK Day",
        _nth_weekday_of_month(year, 2, 0, 3):          "Presidents' Day",
        _easter_sunday(year) - timedelta(days=2):      "Good Friday",
        _last_weekday_of_month(year, 5, 0):            "Memorial Day",
        _holiday_observed(date(year, 6, 19)):          "Juneteenth",
        _holiday_observed(date(year, 7, 4)):           "Independence Day",
        _nth_weekday_of_month(year, 9, 0, 1):          "Labor Day",
        _nth_weekday_of_month(year, 11, 3, 4):         "Thanksgiving",
        _holiday_observed(date(year, 12, 25)):         "Christmas",
    }


# ── US Market holiday detector ────────────────────────────────────────────────

def is_market_holiday(d: date) -> bool:
    """Return True if d is a US stock market holiday (NYSE/NASDAQ)."""
    return d in _us_market_holidays(d.year)


def get_holiday_name(d: date) -> str:
    """Return the holiday name for d if it is a US market holiday, else empty string."""
    return _us_market_holidays(d.year).get(d, "")


def next_trading_day(d: date) -> date:
    """Return the next US market trading day after d (skip weekends + holidays)."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5 or is_market_holiday(nxt):
        nxt += timedelta(days=1)
    return nxt


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
    "macro_context": {"spy_pct": 0.4, "vix": 18.2, "tnx_yield": 4.32},
}


# ── Mode detection ────────────────────────────────────────────────────────────

def detect_run_mode(now_et: datetime) -> str:
    """Auto-detect run mode by ET hour/weekday. Override with RUN_MODE env var."""
    forced = os.environ.get("RUN_MODE", "").lower()
    if forced in ("morning", "confirmation", "weekly", "close_check", "eod_summary", "prescreener", "price_alerts", "week_ahead", "premarket", "digest", "friday_wrap", "macro_alert", "midday_check", "vix_check", "news_check", "pre_earnings", "monthly_commentary", "recap", "watchdog"):
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
            # Screener ran without error but found no qualifying candidates — don't retry
            print(f"[agent] Crypto screener: no qualifying candidates today (attempt {attempt}).")
            return empty
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

def _screener_cache_with_retry(attempts: int = 3, delay_s: int = 4) -> dict | None:
    """
    Load the midnight screener cache, retrying transient Gist failures.
    A single flaky GitHub API call must never cost users their morning picks.
    """
    for i in range(attempts):
        try:
            cache = load_screener_cache()
            if cache:
                return cache
        except Exception as exc:
            print(f"[agent] Screener cache load attempt {i + 1}/{attempts} failed: {exc}")
        if i < attempts - 1:
            time.sleep(delay_s)
    return None


def _can_run_live_screener() -> bool:
    """
    The full 600-ticker screener needs more RAM than Render's 512MB instance
    (proven OOM). Render sets RENDER=true automatically — never run the live
    screener there; GitHub Actions (7GB) is the only environment that can.
    """
    return not os.environ.get("RENDER")


def run_morning(config: dict, now_et: datetime):
    """Full screener + Claude analysis + save picks + send morning message."""
    _log_cron_run("morning")
    is_weekend = now_et.weekday() >= 5
    is_holiday = (not is_weekend) and is_market_holiday(now_et.date())

    # Human-readable reason + next open day used in the morning picks message
    if is_weekend:
        _day_name = now_et.strftime("%A")   # "Saturday" or "Sunday"
        market_closed_reason = _day_name
    elif is_holiday:
        market_closed_reason = get_holiday_name(now_et.date()) or "US holiday"
    else:
        market_closed_reason = ""

    if is_weekend or is_holiday:
        _next_open = next_trading_day(now_et.date())
        next_open_label = _next_open.strftime("%A, %b %-d")   # e.g. "Tuesday, May 27"
    else:
        next_open_label = ""

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
            # ── Macro context (SPY%, 10Y yield, VIX) — one bulk download ────
            try:
                import yfinance as yf
                macro_hist = yf.download(
                    "SPY ^TNX ^VIX", period="2d", interval="1d",
                    progress=False, auto_adjust=True,
                )
                # Handle both single-ticker (Series) and multi-ticker (DataFrame)
                if not macro_hist.empty:
                    close = macro_hist["Close"] if "Close" in macro_hist.columns else macro_hist

                    def _get_col(col: str):
                        """Safely extract a column from a multi-ticker Close DataFrame."""
                        if hasattr(close, "columns"):
                            return close[col].dropna() if col in close.columns else None
                        return None

                    spy_c = _get_col("SPY")
                    tnx_c = _get_col("^TNX")
                    vix_c = _get_col("^VIX")

                    if spy_c is not None and len(spy_c) >= 2:
                        spy_prev = float(spy_c.iloc[-2])
                        spy_curr = float(spy_c.iloc[-1])
                        macro_context["spy_pct"]   = round((spy_curr - spy_prev) / spy_prev * 100, 2)
                        macro_context["spy_price"] = round(spy_curr, 2)
                    if tnx_c is not None and len(tnx_c) >= 1:
                        macro_context["tnx_yield"] = round(float(tnx_c.iloc[-1]), 2)
                    if vix_c is not None and len(vix_c) >= 1:
                        vix = float(vix_c.iloc[-1])
                        macro_context["vix"] = round(vix, 1)
                        print(f"[agent] VIX = {vix:.1f}")
                        if vix > VIX_ALERT_THRESHOLD:
                            _alert(
                                f"⚠️ <b>High Volatility Alert</b> — VIX = <code>{vix:.1f}</code>\n"
                                f"Market fear is elevated. Consider tightening stop-losses "
                                f"and reducing short-term position sizes today.",
                                tab="portfolio",
                            )
            except Exception as exc:
                print(f"[agent] Macro context fetch failed (non-critical): {exc}")

            # ── Stock screener: use midnight cache if fresh, else run live ────
            cache = _screener_cache_with_retry()

            if cache:
                print("[agent] Using midnight screener cache — skipping live screener.")
                stock_candidates = cache["stocks"]
                # Re-apply LT quality floor — guards against cached results from
                # before the quality gate was added (task #102) or stale midnight
                # runs where some candidates have been scored well below threshold.
                _LT_FLOOR = 15   # floor raised from 25→15 to keep more LT candidates for Claude
                _before = len(stock_candidates.get("long_term", []))
                stock_candidates["long_term"] = [
                    c for c in stock_candidates.get("long_term", [])
                    if c.get("lt_score", 0) >= _LT_FLOOR
                ]
                _after = len(stock_candidates["long_term"])
                if _before != _after:
                    print(f"[agent] LT quality floor: dropped {_before - _after} "
                          f"cached candidates with lt_score < {_LT_FLOOR}.")
            elif not _can_run_live_screener():
                # 512MB OOM guard: degrade gracefully — users get crypto/ETF
                # picks with the normal "no stock setups today" rendering;
                # only the admin learns something went wrong.
                print("[agent] No screener cache and live screener disabled on "
                      "this host (512MB OOM guard) — stock picks skipped.")
                _alert(
                    "⚠️ <b>Screener cache missing</b> — stock picks omitted from "
                    "this morning's brief (live screener disabled on Render). "
                    "Re-run the GitHub Actions prescreener, then trigger "
                    "/trigger/morning?force=true to recover.",
                    admin_only=True,
                )
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

        # Preserve near-misses from screener — Claude doesn't see these,
        # attach them directly so the formatter can show "almost qualified" picks
        _nm = stock_candidates.get("near_misses")
        if _nm:
            if "stocks" not in picks:
                picks["stocks"] = {}
            picks["stocks"]["near_misses"] = _nm

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

    n_sent = _send_morning_personalised(picks, config, label="8:00 AM Morning Briefing",
                                        market_closed=bool(is_weekend or is_holiday),
                                        closed_reason=market_closed_reason,
                                        next_open_label=next_open_label)

    # ── Auto-set entry + stop alerts for all picks ────────────────────────────
    _auto_set_pick_alerts(picks, _all_recipients())

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
                def _week_msg(uid):
                    user_cfg = {**config, **get_user_config(uid)}
                    return None if user_cfg.get("paused") else week_msg
                _fanout(_week_msg, tag="week_ahead_block")
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
        _alert(
            f"✅ <b>Morning run complete</b>\n"
            f"Sent to {n_sent} user(s)  ·  "
            f"📈 {n_st} ST + {n_lt} LT stocks  ·  "
            f"🪙 {n_crypto} crypto  ·  "
            f"📦 {n_etf} ETFs  ·  "
            f"🛢 {n_comm} commodities  ·  "
            f"🎯 {n_opts} options plays",
            admin_only=True,
            tab="picks",
        )
        # Save timestamp for /dashboard
        update_config("last_morning_run", datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        print(f"[agent] Admin run summary failed (non-critical): {exc}")


# ── Trade-close broadcast helper (shared by confirmation + close-check runs) ──

def _fanout(msg_for, recipients=None, tag: str = "broadcast") -> int:
    """Build every user's message first, then send them ALL concurrently.

    🔴 Why this exists. Delivery was 34 serial `for uid … send_message()` loops.
    At 2 users that is invisible; at 100 it is ~35s per loop against Telegram's
    ~30 msg/s cap, and a single 429 mid-loop stalls everyone behind it.
    broadcast_all() fans out with a 30-way semaphore, so the same 100 users take
    a few seconds.

    `msg_for(uid)` returns:
        None / ""  → skip this user (paused, quiet hours, nothing to say)
        str        → message text
        dict       → {"text": …, "keyboard": …} for an inline keyboard
        list       → several of the above, for a user who gets N messages

    🔴 Returns the number of USERS reached, never the number of messages sent.
    broadcast_all() returns {chat_id: ok}, so a user receiving N messages
    collapses to ONE key — counting messages off that dict would silently
    under-report. The sends themselves all happen; only the result mapping
    collapses. Ordering within one user's batch is NOT guaranteed, so only pass
    a list where the messages are independent (different tickers, say) rather
    than a sequence the user is meant to read in order.

    The per-user try/except is preserved deliberately: one user's build failure
    must never abort delivery for the rest — that rule predates this helper and
    is why every converted loop keeps its own error line.
    """
    outbox: list[dict] = []
    for uid in (_all_recipients() if recipients is None else recipients):
        try:
            m = msg_for(uid)
            if not m:
                continue
            for one in (m if isinstance(m, list) else [m]):
                if not one:
                    continue
                outbox.append({"chat_id": uid, "text": one} if isinstance(one, str)
                              else {"chat_id": uid, **one})
        except Exception as exc:
            print(f"[agent] {tag}: build failed for {uid} (non-critical): {exc}")
    if not outbox:
        return 0
    results = broadcast_all(outbox)
    failed = [c for c, ok in results.items() if not ok]
    if failed:
        print(f"[agent] {tag}: {len(failed)} user(s) had a failed send: {failed[:5]}")
    return sum(1 for ok in results.values() if ok)


def _broadcast_trade_closes(current_prices: dict) -> None:
    """Auto-close disabled — positions must be closed manually by the user."""
    return  # noqa: auto-close removed per user preference
    """Check and close trades for all recipients, sending close alerts + debriefs."""
    _app_url = (os.environ.get("APP_URL") or "").rstrip("/")
    def _close_msgs(uid):
            out = []
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
                        "\n\n💬 <i>The target was set when the pick was made. Hitting it means the thesis played out exactly as planned — that's the system working.</i>"
                    )
                elif outcome == "stop":
                    close_msg = (
                        f"🛡 Your stop-loss triggered on {ticker} — this protection saved you "
                        f"from bigger losses. Sell now to close the position.\n"
                        f"Closed @ {price_str}  {pct_str}  {usd_str}"
                        "\n\n💬 <i>Stop-losses exist for this exact moment. By exiting here, you avoided any further downside. A small controlled loss is far better than a large uncontrolled one.</i>"
                    )
                else:
                    close_msg = (
                        f"⏱ {ticker} position expired after 28 days. "
                        f"Review and decide: hold or close?\n"
                        f"Closed @ {price_str}  {pct_str}  {usd_str}"
                        "\n\n💬 <i>Not every trade hits its target within the window — that's normal. Review the position and decide if the thesis still holds before re-entering.</i>"
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
                if _app_url:
                    kb_row.append({"text": "📊 Dashboard", "web_app": {"url": f"{_app_url}/miniapp"}})
                out.append({"text": close_msg, "keyboard": [kb_row]})
            return out

    _fanout(_close_msgs, tag="trade_closes")


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
    today = et_today().isoformat()
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

    # ── Trailing stop nudges — batched per user to reduce notification noise ───
    def _trail_msg(uid):
        if get_user_config(uid).get("skip_trail_nudge"):
            return None
        if _is_quiet_hours(uid):
            return None
        buf: list = []
        _check_trailing_stops(current_prices, uid, msg_buffer=buf)
        if not buf:
            return None
        combined = "\n\n──────────────\n\n".join(buf)
        header = f"📋 <b>Position alerts ({len(buf)})</b>\n\n" if len(buf) > 1 else ""
        return f"{header}{combined}"

    _fanout(_trail_msg, tag="trailing_stops")

    # ── Price alerts ──────────────────────────────────────────────────────────
    try:
        fired = check_all_alerts(send_fn=_alert, send_keyboard_fn=send_inline_keyboard)
        if fired:
            print(f"[agent] {fired} price alert(s) triggered.")
    except Exception as exc:
        print(f"[agent] Price alert check failed (non-critical): {exc}")

    # ── Per-user earnings warnings ────────────────────────────────────────────
    def _earnings_warn_msgs(uid):
        if get_user_config(uid).get("skip_earnings"):
            return None
        from earnings_checker import get_upcoming_earnings
        log = load_user_trade_log(uid)
        open_stock_tickers = [
            t["ticker"] for t in log.get("open", [])
            if t.get("asset_type") == "stock"
        ]
        if not open_stock_tickers:
            return None
        upcoming = get_upcoming_earnings(open_stock_tickers, days_ahead=3)
        # One message per ticker — independent, so fan-out order does not matter.
        return [
            f"🗓️ <b>Earnings Warning</b> — <b>{ticker}</b> reports <b>{earnings_date}</b>\n"
            f"You have an open position. Earnings can cause sharp moves — "
            f"consider closing before the announcement."
            for ticker, earnings_date in upcoming.items()
        ]

    _fanout(_earnings_warn_msgs, tag="earnings_warning")

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

        def _watch_signal_msgs(uid):
                out = []
                u_cfg     = get_user_config(uid)
                if u_cfg.get("paused") or u_cfg.get("skip_watchlist_alerts"):
                    return None
                watchlist = u_cfg.get("watchlist", [])
                # Only scan tickers NOT already in today's picks
                scan = [t for t in watchlist if t and t not in pick_symbols]
                if not scan:
                    return None
                from price_checker import _SYMBOL_TO_CG_ID as _CG_IDS_WL
                yf_scan_map = {(f"{t}-USD" if t.upper() in _CG_IDS_WL else t): t for t in scan}
                hist = yf.download(" ".join(yf_scan_map.keys()), period="60d", interval="1d",
                                   progress=False, auto_adjust=True)
                _close_raw = hist["Close"]
                if not hasattr(_close_raw, "columns"):
                    _close_raw = _close_raw.to_frame(name=list(yf_scan_map.keys())[0])
                # Remap columns back to original tickers
                closes = _close_raw.rename(columns=yf_scan_map)
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
                            out.append({
                                "text": (
                                    f"👀 <b>Watchlist Signal — {ticker}</b>  <code>${current_price:,.2f}</code>\n"
                                    f"<i>{' · '.join(alerts)}</i>\n"
                                    f"Not in today's picks — but worth a look."
                                ),
                                "keyboard": [[
                                    _miniapp_url_btn(f"📊 {ticker} Chart", f"?chart={ticker}", f"cmd|CHART {ticker}"),
                                    _miniapp_btn("👁 Watchlist", "watchlist", f"watch|{ticker}"),
                                ]],
                            })
                    except Exception:
                        continue
                return out

        _fanout(_watch_signal_msgs, tag="watchlist_signal")
    except Exception as exc:
        print(f"[agent] Watchlist signal check failed (non-critical): {exc}")

    message = format_confirmation_message(picks, current_prices)
    # Broadcast to all users who haven't opted out
    if DRY_RUN:
        print(f"\n{'=' * 60}\nDRY RUN — 10:30 AM Confirmation (not sent):\n{'=' * 60}\n{message}")
    else:
        def _conf_msg(uid):
            ucfg = get_user_config(uid)
            if ucfg.get("paused") or ucfg.get("skip_confirmation"):
                print(f"[agent] Skipping confirmation for {uid} (paused or opted out).")
                return None
            return {"text": message, "keyboard": [
                [_miniapp_btn("📊 Open Dashboard  ↗", "picks", "TODAY")],
                [
                    _miniapp_btn("📋 Help",     "picks",       "HELP"),
                    _miniapp_btn("📲 Share",    "picks",       "SHARE"),
                    _miniapp_btn("💬 Feedback", "picks",       "FEEDBACK"),
                    _miniapp_btn("📊 Positions","portfolio",   "POSITIONS"),
                ],
            ]}
        _fanout(_conf_msg, tag="confirmation")

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
    today_str = et_today().isoformat()
    cfg = get_user_config(uid)
    if cfg.get("paused"):
        return

    # picks is nested: picks["stocks"]["short_term"], picks["etfs"]["short_term"], etc.
    _st  = picks.get("stocks",      {}) if isinstance(picks.get("stocks"),      dict) else {}
    _et  = picks.get("etfs",        {}) if isinstance(picks.get("etfs"),        dict) else {}
    _co  = picks.get("commodities", {}) if isinstance(picks.get("commodities"), dict) else {}
    _all_equity = (
        (_st.get("short_term", []) or []) +
        (_st.get("long_term",  []) or []) +
        (_et.get("short_term", []) or []) +
        (_et.get("long_term",  []) or []) +
        (_co.get("short_term", []) or []) +
        (_co.get("long_term",  []) or [])
    )
    for pick in _all_equity:
        if not isinstance(pick, dict):
            continue
        ticker = (pick.get("ticker") or "").upper()
        if not ticker:
            continue

        # Determine stop price
        sizing     = pick.get("sizing") or {}
        stop_price = sizing.get("stop_price") or pick.get("stop_loss")
        if not stop_price or stop_price <= 0:
            continue

        # 🔴 `current <= 0` rejects zero but NOT holiday garbage: a $0.01 tick
        # is positive and trivially satisfies `current < stop_price`, which fired
        # "Current price $0.01 has breached today's suggested stop (↓100.0%)".
        # Validate against the pick's own entry (fallback: the stop itself).
        from market_data import plausible_price
        current = current_prices.get(ticker)
        if not plausible_price(current, pick.get("entry_price") or stop_price):
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
# Each key gets its own cache entry so per-key TTLs are independent.
# Old approach stored all keys in a single list — any _mark_alerted(ttl=72h)
# call silently extended the TTL for every other key in that list.

_ALERTED_KEY_PREFIX = "alerted:"
_ALERTED_STOPS_TTL  = 28 * 3600  # 28 hours default


def _is_alerted(key: str) -> bool:
    """Return True if we have already fired an alert for this key today."""
    return cache_get(f"{_ALERTED_KEY_PREFIX}{key}") is not None


def _mark_alerted(key: str, ttl_hours: int | None = None) -> None:
    """Persist alert dedup key so restarts don't resend the same alert.

    ttl_hours overrides the default 28-hour window — use for longer cooldowns
    (e.g. stop-coverage nudge uses 72 h so it doesn't fire every day).
    Each key has an independent TTL — setting a 72-hour TTL for one key
    no longer affects the expiry of other alerted keys.
    """
    ttl = (ttl_hours * 3600) if ttl_hours else _ALERTED_STOPS_TTL
    cache_set(f"{_ALERTED_KEY_PREFIX}{key}", True, ttl_seconds=ttl)


def _clear_alerted(key: str) -> None:
    """Re-arm a dedup key so its alert can fire again on a fresh event — e.g. a
    stopped-out position that recovers back above its stop, so the next breach
    alerts instead of staying suppressed by the fire-once flag."""
    cache_delete(f"{_ALERTED_KEY_PREFIX}{key}")


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

    # ── Trailing stop nudges — batched per user to reduce notification noise ───
    def _trail_msg(uid):
        if get_user_config(uid).get("skip_trail_nudge"):
            return None
        if _is_quiet_hours(uid):
            return None
        buf: list = []
        _check_trailing_stops(current_prices, uid, msg_buffer=buf)
        if not buf:
            return None
        combined = "\n\n──────────────\n\n".join(buf)
        header = f"📋 <b>Position alerts ({len(buf)})</b>\n\n" if len(buf) > 1 else ""
        return f"{header}{combined}"

    _fanout(_trail_msg, tag="trailing_stops")

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

    def _eod_msg(uid):
            cfg = get_user_config(uid)
            if cfg.get("paused") or cfg.get("skip_eod"):
                return None
            log            = load_user_trade_log(uid)
            watchlist      = cfg.get("watchlist", [])
            open_positions = log.get("open", [])

            # Price the user's open positions too — get_current_prices only
            # covers pick tickers, which silently dropped every non-pick
            # position from the "Open positions vs entry" block.
            user_prices = dict(current_prices)
            missing = [p["ticker"] for p in open_positions
                       if p.get("ticker") and p["ticker"] not in user_prices]
            if missing:
                # get_live_prices is partial-tolerant (crypto-aware, per-ticker):
                # one failed/rate-limited fetch returns the rest, whereas the old
                # _download_prices threw on any failure and the except then dropped
                # the ENTIRE positions block from the EOD (the crypto-429 case).
                try:
                    from market_data import get_live_prices as _glp
                    user_prices.update(_glp(missing) or {})
                except Exception as _px_exc:
                    print(f"[agent] EOD position price fetch failed for {uid} (non-critical): {_px_exc}")

            # ── EOD full summary (picks performance) ─────────────────────────
            msg = format_eod_full_summary(picks, user_prices, open_positions, watchlist=watchlist)

            # ── Hold/fold nudges ──────────────────────────────────────────────
            try:
                _check_hold_or_fold(uid, user_prices)
            except Exception as _hf_exc:
                print(f"[agent] Hold/fold nudge failed for {uid} (non-critical): {_hf_exc}")

            # ── Take-profit nudges ────────────────────────────────────────────
            try:
                _check_take_profit_nudge(uid, user_prices)
            except Exception as _tp_exc:
                print(f"[agent] Take-profit nudge failed for {uid} (non-critical): {_tp_exc}")

            # ── Informational insights: health + cash + correlation + stop coverage
            # Appended to the EOD message so it's one send instead of two.
            if not _is_quiet_hours(uid):
                eod_insights: list[str] = []

                try:
                    health_blocks = _check_portfolio_health(uid, user_prices)
                    eod_insights.extend(health_blocks or [])
                except Exception as _h_exc:
                    print(f"[agent] health check failed for {uid}: {_h_exc}")

                try:
                    cash_note = _check_cash_position(uid, cfg, open_positions, user_prices)
                    if cash_note:
                        eod_insights.append(cash_note)
                except Exception as _c_exc:
                    print(f"[agent] cash check failed for {uid}: {_c_exc}")

                # Correlation warning — Mondays only
                if et_today().weekday() == 0:
                    try:
                        corr_note = _check_portfolio_correlation(uid, open_positions)
                        if corr_note:
                            eod_insights.append(corr_note)
                    except Exception as _corr_exc:
                        print(f"[agent] correlation check failed for {uid}: {_corr_exc}")

                # Stop coverage nudge — 3-day cooldown so it doesn't fire every single day
                if not cfg.get("skip_stop_coverage") and open_positions:
                    total     = len(open_positions)
                    with_stop = sum(1 for t in open_positions if t.get("stop_loss"))
                    no_stop   = [t["ticker"] for t in open_positions if not t.get("stop_loss")]
                    if no_stop:
                        stop_cov_key = f"stop_coverage_{uid}"
                        if not _is_alerted(stop_cov_key):
                            _mark_alerted(stop_cov_key, ttl_hours=72)  # 3-day cooldown
                            coverage_pct = int(with_stop / total * 100)
                            tickers_str  = ", ".join(f"<b>{t}</b>" for t in no_stop[:4])
                            if len(no_stop) > 4:
                                tickers_str += f" +{len(no_stop) - 4} more"
                            eod_insights.append(
                                f"🛡 <b>Stop coverage: {coverage_pct}%</b> — "
                                f"no stop on {tickers_str}. "
                                f"<code>/updatestop TICKER PRICE</code> to protect."
                            )

                # Options flow alert — fire if unusual activity on any held position
                if open_positions:
                    try:
                        opts_note = _check_options_flow_alert(uid, open_positions)
                        if opts_note:
                            eod_insights.append(opts_note)
                    except Exception as _opts_exc:
                        print(f"[agent] options flow check failed for {uid}: {_opts_exc}")

                # Congressional cluster alert — fire when 3+ members buy a held ticker
                if open_positions:
                    try:
                        congress_note = _check_congressional_alert(uid, open_positions)
                        if congress_note:
                            eod_insights.append(congress_note)
                    except Exception as _cong_exc:
                        print(f"[agent] congressional check failed for {uid}: {_cong_exc}")

                if eod_insights:
                    insights_header = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n📋 <b>Portfolio Insights</b>\n\n"
                    insights_body   = "\n\n──────────────\n\n".join(eod_insights)
                    if msg:
                        msg = msg + insights_header + insights_body
                    else:
                        msg = insights_header.lstrip() + insights_body

            if not msg:
                return None
            if DRY_RUN:
                print(f"\nDRY RUN — EOD Summary + Insights for {uid}:\n{msg}")
                return None
            return {"text": msg, "keyboard": [
                [
                    _miniapp_btn("📊 Portfolio",     "portfolio",   "POSITIONS"),
                    _miniapp_btn("📈 Today's Picks", "picks",       "TODAY"),
                    _miniapp_btn("📉 Performance",   "performance", "STATS"),
                ],
                [
                    _miniapp_btn("📋 Help",     "picks",     "HELP"),
                    _miniapp_btn("📲 Share",    "picks",     "SHARE"),
                    _miniapp_btn("💬 Feedback", "picks",     "FEEDBACK"),
                    _miniapp_btn("📜 History",  "portfolio", "HISTORY"),
                ],
            ]}

    _fanout(_eod_msg, tag="eod_summary")



# ── Friday Evening Wrap-Up ────────────────────────────────────────────────────

def run_friday_wrap():
    """
    Friday ~6 PM ET — send each user a personalised weekly summary.
    Covers: closed trades this week (winners/losers/P&L) + open positions
    with unrealised P&L if live prices are available.
    """
    _log_cron_run("friday_wrap")
    print("[agent] Running Friday evening wrap-up...")

    now_et = datetime.now(ET)
    # Week window: Monday 00:00 ET through today
    today = now_et.date()
    week_start = today - timedelta(days=today.weekday())  # Monday

    import yfinance as yf

    # uid -> {"main": str, "nudge": str|None, "lesson": str|None}
    _wrap: dict = {}

    for uid in _all_recipients():
        try:
            cfg = get_user_config(uid)
            if cfg.get("paused"):
                continue

            log = load_user_trade_log(uid)
            closed_trades = log.get("closed", [])
            open_trades   = log.get("open", [])

            # Filter trades closed THIS week
            week_closed = []
            for trade in closed_trades:
                closed_date_str = trade.get("closed_date") or trade.get("closed_at", "")
                if not closed_date_str:
                    continue
                try:
                    closed_d = date.fromisoformat(str(closed_date_str)[:10])
                    if week_start <= closed_d <= today:
                        week_closed.append(trade)
                except Exception:
                    continue

            if not week_closed:
                _wrap[uid] = {"main": (
                    "📭 No closed trades this week — your positions are still working. "
                    "Check /positions for live P&amp;L.")}
                continue

            # Stats on closed trades
            winners = [t for t in week_closed if float(t.get("gain_usd") or t.get("return_pct") or 0) > 0]
            losers  = [t for t in week_closed if t not in winners]
            total_pnl = sum(float(t.get("gain_usd") or 0) for t in week_closed)
            accuracy  = round(len(winners) / len(week_closed) * 100) if week_closed else 0

            pnl_sign  = "+" if total_pnl >= 0 else ""
            pnl_str   = f"{pnl_sign}${total_pnl:,.2f}"

            # Unrealised P&L for open positions
            unrealised_pnl = 0.0
            n_open = len(open_trades)
            if open_trades:
                try:
                    open_tickers = list({t["ticker"] for t in open_trades if t.get("ticker")})
                    prices = _download_prices(open_tickers, period="1d", interval="1d", auto_adjust=True)
                    for trade in open_trades:
                        ticker = trade.get("ticker")
                        entry  = trade.get("entry_price")
                        curr   = prices.get(ticker) if ticker else None
                        if not (ticker and entry and curr):
                            continue
                        alloc  = trade.get("allocation") or trade.get("budget") or 0
                        shares = trade.get("shares") or trade.get("quantity")
                        if shares:
                            unrealised_pnl += (float(curr) - float(entry)) * float(shares)
                        elif alloc:
                            est_shares = float(alloc) / float(entry)
                            unrealised_pnl += (float(curr) - float(entry)) * est_shares
                except Exception as exc:
                    print(f"[agent] Friday wrap — unrealised P&L fetch failed for {uid}: {exc}")

            unr_sign = "+" if unrealised_pnl >= 0 else ""
            unr_str  = f"{unr_sign}${unrealised_pnl:,.2f}"

            # Motivational closing line
            if accuracy >= 60:
                closer = "Keep letting the stops and targets do their job — that's how the edge compounds. 🎯"
            elif len(week_closed) == 1:
                closer = "One trade is still a data point. Stay consistent and let the process work. 🎯"
            else:
                closer = "Every loss is tuition. The system is working — keep trusting it. 💪"

            open_section = ""
            if n_open:
                open_section = (
                    f"\n<b>Still open:</b> {n_open} position{'s' if n_open != 1 else ''}\n"
                    f"📈 Unrealised P&amp;L: {unr_str}\n"
                )

            # SPY weekly return
            spy_week_str = ""
            try:
                spy_data = yf.download("SPY", period="5d", progress=False)["Close"]
                if not spy_data.empty and len(spy_data) >= 2:
                    spy_week = float((spy_data.iloc[-1] / spy_data.iloc[0] - 1) * 100)
                    spy_week_str = f"\n<b>vs Market:</b> SPY this week: {spy_week:+.1f}%"
            except Exception as _spy_exc:
                print(f"[agent] Friday wrap — SPY fetch failed for {uid}: {_spy_exc}")

            msg = (
                f"📊 <b>Your Week in Review — StockPulz</b>\n\n"
                f"<b>Closed this week:</b> {len(week_closed)} trade{'s' if len(week_closed) != 1 else ''}\n"
                f"✅ Winners: {len(winners)}  |  ❌ Stopped out: {len(losers)}\n"
                f"💰 Realised P&amp;L: {pnl_str}\n"
                f"{open_section}\n"
                f"<b>Bot accuracy this week:</b> {accuracy}% ({len(winners)} of {len(week_closed)})\n\n"
                f"{closer}"
                f"{spy_week_str}"
            )
            _entry: dict = {"main": msg}
            _wrap[uid] = _entry

            # ── Auto trade review nudge (≥5 total closed trades) ────────────
            # Send a /review prompt so users know the feature exists.
            # Only if they have meaningful history to analyse.
            try:
                if len(closed_trades) >= 5:
                    _entry["nudge"] = (
                        "🧠 <b>Weekly Trade Review available</b>\n\n"
                        "You have enough trade history for an AI pattern analysis.\n"
                        "Send <code>/review</code> for a candid coach's take on your trading habits.")
            except Exception:
                pass

            # ── Week-in-review Haiku lesson ──────────────────────────────────
            # If user closed ≥1 trade this week, ask Haiku for a one-sentence
            # lesson derived from the week's actual results.
            try:
                if week_closed:
                    dedup_key = f"friday_lesson_{uid}_{week_start.isoformat()}"
                    if not _is_alerted(dedup_key):
                        _mark_alerted(dedup_key)
                        trade_summary_lines = []
                        for t in week_closed:
                            r = t.get("return_pct") or 0
                            g = t.get("gain_usd") or 0
                            outcome = "win" if float(g) > 0 else "loss"
                            trade_summary_lines.append(
                                f"{t.get('ticker','?')}: {outcome}, {float(r):+.1f}%"
                            )
                        trade_summary = "\n".join(trade_summary_lines)
                        prompt = (
                            f"A trader closed {len(week_closed)} trade(s) this week with "
                            f"{accuracy}% win rate ({len(winners)} wins, {len(losers)} losses). "
                            f"Results:\n{trade_summary}\n\n"
                            f"Write ONE concise lesson (max 25 words) this trader should take from "
                            f"their week. Be specific to the data, not generic. No preamble."
                        )
                        client = _get_client()
                        resp = client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=80,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        lesson = resp.content[0].text.strip().strip('"')
                        _entry["lesson"] = f"💡 <b>This week's lesson:</b>\n{lesson}"
            except Exception as exc:
                print(f"[agent] Friday wrap — lesson generation failed for {uid}: {exc}")

        except Exception as exc:
            print(f"[agent] Friday wrap failed for {uid} (non-critical): {exc}")

    # 🔴 THREE ORDERED PASSES, not one fan-out of all three. A user must read the
    # wrap, then the review nudge, then the lesson — and ordering WITHIN one
    # user's batch is not guaranteed by broadcast_all. Each pass completes before
    # the next begins, so the sequence holds while each pass still fans out.
    _uids = list(_wrap)
    for _key, _tag in (("main", "friday_wrap"),
                       ("nudge", "friday_review_nudge"),
                       ("lesson", "friday_lesson")):
        _fanout(lambda uid, _k=_key: _wrap.get(uid, {}).get(_k),
                recipients=_uids, tag=_tag)

    print("[agent] Friday evening wrap-up complete.")



def run_tax_loss_harvest_check():
    """
    Run on the 1st and 15th of November and December.
    Alert users with open positions sitting at an unrealised loss ≥ $50,
    nudging them to consider year-end tax-loss harvesting.
    """
    _log_cron_run("tax_harvest")
    today = et_today()
    if today.month not in (11, 12):
        print("[agent] Tax harvest check — not November or December, skipping.")
        return

    print("[agent] Running tax-loss harvest check...")
    import yfinance as yf

    dedup_month = today.strftime("%Y-%m")

    # Marked only AFTER the sends land — marking during the build would suppress
    # next month's nudge for a user whose send then failed.
    _harvest_keys: dict = {}

    def _harvest_msg(uid):
            cfg = get_user_config(uid)
            if cfg.get("paused"):
                return None

            dedup_key = f"taxharvest_{uid}_{dedup_month}"
            if _is_alerted(dedup_key):
                return None

            log = load_user_trade_log(uid)
            open_trades = log.get("open", [])
            if not open_trades:
                return None

            open_tickers = list({t["ticker"] for t in open_trades if t.get("ticker")})
            try:
                prices = _download_prices(open_tickers, period="1d", interval="1d", auto_adjust=True)
                if not prices:
                    return None
            except Exception as exc:
                print(f"[agent] Tax harvest — price fetch failed for {uid}: {exc}")
                return None

            loss_positions = []
            for trade in open_trades:
                ticker = trade.get("ticker")
                entry  = trade.get("entry_price")
                curr   = prices.get(ticker) if ticker else None
                if not (ticker and entry and curr):
                    continue
                shares = trade.get("shares") or trade.get("quantity")
                alloc  = trade.get("allocation") or trade.get("budget")
                if shares:
                    unrealised = (float(curr) - float(entry)) * float(shares)
                elif alloc:
                    est_shares = float(alloc) / float(entry)
                    unrealised = (float(curr) - float(entry)) * est_shares
                else:
                    continue
                if unrealised < -50:
                    loss_pct = (float(curr) - float(entry)) / float(entry) * 100
                    loss_positions.append({
                        "ticker": ticker,
                        "abs_loss": abs(unrealised),
                        "loss_pct": loss_pct,
                    })

            if not loss_positions:
                return None

            positions_lines = "\n".join(
                f"• {p['ticker']} — down ${p['abs_loss']:.0f} "
                f"(unrealised loss of {p['loss_pct']:.1f}%)"
                for p in loss_positions
            )

            msg = (
                f"🍂 <b>Year-End Tax Tip</b>\n\n"
                f"You have positions sitting at a loss that could be useful before December 31:\n\n"
                f"{positions_lines}\n\n"
                f"Selling these before year-end lets you <b>offset capital gains</b> you've "
                f"realised this year, potentially reducing your tax bill.\n\n"
                f"⚠️ <b>Watch the wash-sale rule:</b> If you sell at a loss, don't buy the same "
                f"stock back within 30 days or the deduction is disallowed.\n\n"
                f"<i>This is general information, not tax advice. Consult your tax advisor for "
                f"your specific situation.</i>"
            )
            _harvest_keys[uid] = dedup_key
            return {"text": msg, "keyboard": [
                [_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]]}

    _fanout(_harvest_msg, tag="tax_harvest")

    for uid, key in _harvest_keys.items():
        _mark_alerted(key)

    print("[agent] Tax-loss harvest check complete.")


# ── Tomorrow's macro events (ForexFactory JSON, high-impact only) ─────────────

def _get_tomorrows_macro_events() -> list[dict]:
    """
    Fetch tomorrow's high-impact economic events from ForexFactory public JSON.
    Returns list of dicts like [{"name": "CPI Release", "impact": "high"}, ...].
    Falls back to empty list on any error (graceful degradation).
    """
    import urllib.request
    import json

    tomorrow = (et_today() + timedelta(days=1)).strftime("%m-%d-%Y")
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        req = urllib.request.Request(url, headers={"User-Agent": "StockPulz/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        events = []
        for item in data:
            if item.get("date", "").startswith(tomorrow) and item.get("impact", "").lower() == "high":
                events.append({"name": item.get("title", item.get("event", "")), "impact": "high"})
        return events
    except Exception as exc:
        print(f"[agent] _get_tomorrows_macro_events: fetch failed (non-critical): {exc}")
        return []


# ── Mid-week macro event alerts ───────────────────────────────────────────────

def run_macro_alert_check():
    """
    Mon–Thu ~5 PM ET — check if high-impact macro events are scheduled tomorrow.
    If yes, alert all users with a personalised view of potentially affected positions.
    Deduplicated: only one alert per calendar day.
    """
    _log_cron_run("macro_alert")
    print("[agent] Running macro alert check...")

    # Only fire Mon–Thu
    now_et = datetime.now(ET)
    if now_et.weekday() >= 4:  # 4 = Friday, 5 = Sat, 6 = Sun
        print("[agent] macro_alert: skipping — not Mon–Thu.")
        return

    dedup_key = f"macroalert_{et_today().isoformat()}"
    if _is_alerted(dedup_key):
        print("[agent] macro_alert: already sent today — skipping.")
        return

    events = _get_tomorrows_macro_events()
    if not events:
        print("[agent] macro_alert: no high-impact events tomorrow.")
        return

    _mark_alerted(dedup_key)
    event_names = ", ".join(e["name"] for e in events[:5])

    def _macro_msg(uid):
        cfg = get_user_config(uid)
        if cfg.get("paused"):
            return None

        log           = load_user_trade_log(uid)
        open_tickers  = [t["ticker"] for t in log.get("open", []) if t.get("ticker")]
        watch_tickers = cfg.get("watchlist", [])

        personal_section = ""
        if open_tickers or watch_tickers:
            lines = []
            if open_tickers:
                lines.append(f"💼 {', '.join(open_tickers[:6])} (open positions)")
            if watch_tickers:
                lines.append(f"👁 {', '.join(watch_tickers[:6])} (watchlist)")
            personal_section = (
                "\n\nYour positions that may be affected:\n"
                + "\n".join(lines)
                + "\n\nNo action needed — your stops are in place. Just be aware."
            )

        return (
            f"⚠️ <b>Market Alert — Big Event Tomorrow</b>\n\n"
            f"<b>Tomorrow:</b> {event_names}\n\n"
            f"These events can cause sharp moves in equities, crypto, and rate-sensitive assets."
            f"{personal_section}"
        )

    _fanout(_macro_msg, tag="macro_alert")

    print(f"[agent] macro_alert: sent to {len(_all_recipients())} users for events: {event_names}")


# ── Intraday Mid-Day Check ────────────────────────────────────────────────────

def run_midday_check():
    """12:30 PM ET intraday check — catch big moves during market hours."""
    _log_cron_run("midday_check")
    print("[agent] run_midday_check: starting")
    try:
        import yfinance as yf

        users = _all_recipients()

        # Collect all tracked tickers across all users
        all_tickers: set = set()
        for uid in users:
            try:
                log = load_user_trade_log(uid)
                for trade in log.get("open", []):
                    if trade.get("ticker"):
                        all_tickers.add(trade["ticker"])
            except Exception:
                pass

        if not all_tickers:
            print("[agent] run_midday_check: no open positions — nothing to check.")
            return

        # Fetch current prices — route through _download_prices so crypto gets
        # the -USD suffix (BTC→BTC-USD). A raw yf.download(" ".join(...)) here
        # priced BTC at ~$28 and fired false "down 99%" hold/fold + drawdown
        # alerts for any crypto holder at midday.
        ticker_list = list(all_tickers)
        current_prices = _download_prices(
            ticker_list, period="1d", interval="1m", auto_adjust=True
        )
        if not current_prices:
            print("[agent] run_midday_check: price fetch returned empty.")
            return

        for uid in users:
            try:
                if _is_quiet_hours(uid):
                    continue   # non-urgent nudges suppressed during user's quiet window
            except Exception as _q_exc:
                print(f"[agent] quiet-hours check failed for {uid} (non-critical): {_q_exc}")
                continue

            # Timely alerts — fire immediately (per-user guarded so one user's
            # failure can't abort the rest of the midday loop).
            try:
                _check_hold_or_fold(uid, current_prices)
            except Exception as _hf_exc:
                print(f"[agent] hold/fold check failed for {uid} (non-critical): {_hf_exc}")
            try:
                _check_portfolio_drawdown(uid, current_prices)
            except Exception as _dd_exc:
                print(f"[agent] drawdown check failed for {uid} (non-critical): {_dd_exc}")

            # Informational nudges — collect and send as one digest
            insight_blocks: list[str] = []
            try:
                health_blocks = _check_portfolio_health(uid, current_prices)
                insight_blocks.extend(health_blocks or [])
            except Exception as _health_exc:
                print(f"[agent] health check failed for {uid} (non-critical): {_health_exc}")
            try:
                stale = _check_stale_positions(uid, current_prices)
                insight_blocks.extend(stale or [])
            except Exception as _stale_exc:
                print(f"[agent] stale check failed for {uid} (non-critical): {_stale_exc}")
            try:
                cfg          = get_user_config(uid)
                log          = load_user_trade_log(uid)
                open_trades  = log.get("open", [])
                cash_note    = _check_cash_position(uid, cfg, open_trades, current_prices)
                if cash_note:
                    insight_blocks.append(cash_note)
            except Exception as _cash_exc:
                print(f"[agent] cash check failed for {uid} (non-critical): {_cash_exc}")
            try:
                log         = load_user_trade_log(uid)
                open_trades = log.get("open", [])
                corr_note   = _check_portfolio_correlation(uid, open_trades)
                if corr_note:
                    insight_blocks.append(corr_note)
            except Exception as _corr_exc:
                print(f"[agent] correlation check failed for {uid} (non-critical): {_corr_exc}")

            if insight_blocks:
                divider = "\n\n──────────────\n\n"
                body    = divider.join(insight_blocks)
                header  = f"📋 <b>Portfolio Insights</b>\n\n" if len(insight_blocks) > 1 else ""
                try:
                    send_message(f"{header}{body}", chat_id=uid, parse_mode="HTML")
                except Exception as _send_exc:
                    print(f"[agent] insight digest send failed for {uid}: {_send_exc}")

    except Exception as e:
        print(f"[agent] run_midday_check error: {e}")

    # ── Trade reminders check ────────────────────────────────────────────────
    _check_trade_reminders()

    print("[agent] run_midday_check: complete.")


# ── Stale position nudge helper ──────────────────────────────────────────────

def _check_stale_positions(uid: str, current_prices: dict) -> list:
    """
    For each open trade older than 21 days with less than 10% progress
    toward target, return text blocks (caller batches into one message).
    """
    from datetime import date as _date
    log          = load_user_trade_log(uid)
    open_trades  = log.get("open", [])
    dirty        = False
    stale_blocks: list = []

    for trade in open_trades:
        if trade.get("_paused"):
            continue  # user muted nudges for this position via /pause
        if trade.get("_notified_stale"):
            continue
        ticker  = trade.get("ticker")
        entry   = trade.get("entry_price")
        target  = trade.get("target_price")
        opened  = trade.get("opened_date") or trade.get("entry_date") or trade.get("bought_at")
        current = current_prices.get(ticker) if ticker else None

        if not (ticker and _is_pos(entry) and _is_pos(current) and opened):
            continue

        try:
            open_date = _date.fromisoformat(str(opened)[:10])
            age_days  = (et_today() - open_date).days
        except Exception:
            continue

        if age_days < 21:
            continue

        entry_f   = float(entry)
        current_f = float(current)
        gain_pct  = (current_f - entry_f) / entry_f * 100

        # Check progress toward target
        progress_pct = gain_pct   # if no target, use raw gain
        if target:
            target_f = float(target)
            span     = abs(target_f - entry_f)
            if span > 0:
                progress_pct = (current_f - entry_f) / span * 100

        if progress_pct < 10:
            trade["_notified_stale"] = True
            dirty = True
            gain_str = f"{gain_pct:+.1f}%"
            target_str = f" (target <code>${_p(float(target))}</code>)" if target else ""
            stale_blocks.append(
                f"🕐 <b>{ticker}</b> — open {age_days} days, only {gain_str} from entry{target_str}. "
                f"Still convicted? <code>/sold {ticker}</code>"
            )

    if dirty:
        _persist_notify_flags(uid, open_trades, ("_notified_stale",))

    return stale_blocks


# ── VIX Volatility Spike Alerts ──────────────────────────────────────────────

def run_vix_check():
    """
    10:00 AM ET weekdays — alert all users when VIX spikes above 20.
    Three tiers: mild (20-25), high (25-35), extreme (35+).
    Deduplicated once per tier per day.
    """
    _log_cron_run("vix_check")
    print("[agent] Running VIX check...")
    try:
        import yfinance as yf
        vix_data = yf.download("^VIX", period="1d", progress=False)["Close"]
        if vix_data.empty:
            print("[agent] vix_check: no VIX data returned.")
            return
        vix = float(vix_data.iloc[-1])
        print(f"[agent] vix_check: VIX = {vix:.1f}")
    except Exception as exc:
        print(f"[agent] vix_check: VIX fetch failed: {exc}")
        return

    if vix < 20:
        print("[agent] vix_check: VIX below 20 — no alert needed.")
        return

    # Determine tier and dedup key — distinct keys per tier so an escalation
    # (e.g. VIX 26 high → 40 extreme same day) isn't suppressed by the lower tier.
    if vix >= 35:
        dedup_key = f"vix_extreme_{et_today()}"
        msg = (
            f"🚨 <b>Extreme Volatility — VIX {vix:.1f}</b>\n\n"
            f"Markets are in fear mode. Conditions like this are rare and usually short-lived.\n\n"
            f"✅ Your stops are protecting you. Trust the system.\n"
            f"⚠️ Avoid adding new positions until volatility settles (VIX back below 25).\n"
            f"📊 Check /regime for current market conditions."
        )
    elif vix >= 25:
        dedup_key = f"vix_high_{et_today()}"
        msg = (
            f"⚠️ <b>High Volatility Alert — VIX {vix:.1f}</b>\n\n"
            f"Markets are getting nervous. The VIX fear index is above 25 — expect sharp intraday moves.\n\n"
            f"✅ Your stop-losses are your protection. Don't panic-sell manually above your stops.\n"
            f"💡 This is often when the best buying opportunities appear — stay disciplined.\n\n"
            f"Check /positions to see your current exposure."
        )
    else:
        # 20 <= vix < 25
        dedup_key = f"vix_mild_{et_today()}"
        msg = (
            f"📊 <b>Market Volatility Update</b>\n\n"
            f"The VIX fear index is at {vix:.1f} — slightly elevated. Markets may be choppier than usual.\n\n"
            f"Your stops are in place. No action needed — just be aware that daily swings could be larger."
        )

    if _is_alerted(dedup_key):
        print(f"[agent] vix_check: already alerted today ({dedup_key}) — skipping.")
        return
    _mark_alerted(dedup_key)

    def _vix_msg(uid):
        return None if get_user_config(uid).get("paused") else msg
    n = _fanout(_vix_msg, tag="vix_check")

    print(f"[agent] vix_check: sent VIX={vix:.1f} alert to {n} user(s).")


# ── Position News Alerts ──────────────────────────────────────────────────────

def _news_context_note(title: str) -> str:
    """Return a 1-sentence contextual note based on keywords in the headline."""
    t = title.lower()
    if any(w in t for w in ("partnership", "deal", "contract")):
        return "Major partnerships often signal institutional confidence."
    if any(w in t for w in ("earnings", "beat", "revenue")):
        return "Earnings beats typically cause a gap up at open."
    if any(w in t for w in ("miss", "below expectations", "cut")):
        return "Earnings misses can cause sharp drops — check your stop."
    if any(w in t for w in ("fda", "approval", "cleared")):
        return "Regulatory approvals are strong catalysts for the sector."
    if any(w in t for w in ("lawsuit", "sec", "investigation", "fine")):
        return "Regulatory/legal issues can weigh on the stock short-term."
    if any(w in t for w in ("layoffs", "restructur")):
        return "Restructuring news is mixed — often positive long-term, volatile short-term."
    if any(w in t for w in ("upgrade", "buy rating", "price target raised")):
        return "Analyst upgrades often bring institutional buying."
    if any(w in t for w in ("downgrade", "sell rating", "price target cut")):
        return "Analyst downgrades can trigger selling pressure."
    return "Monitor price action at open and check if your thesis still holds."


def run_news_check():
    """
    8:30 AM and 1:00 PM ET weekdays — scan news for tickers users hold or watch.
    Sends batched per-user messages for significant stories in the last 24 hours.
    """
    _log_cron_run("news_check")
    print("[agent] Running position news check...")
    import yfinance as yf
    import time as _time

    # Gather tickers per user (open positions + watchlist)
    user_tickers: dict[str, set] = {}
    for uid in _all_recipients():
        try:
            cfg = get_user_config(uid)
            if cfg.get("paused"):
                continue
            log = load_user_trade_log(uid)
            tickers: set = set()
            for t in log.get("open", []):
                if t.get("ticker"):
                    tickers.add(t["ticker"].upper())
            for t in cfg.get("watchlist", []):
                if t:
                    tickers.add(t.upper())
            if tickers:
                user_tickers[uid] = tickers
        except Exception as exc:
            print(f"[agent] news_check: user ticker gather failed for {uid}: {exc}")

    # Unique tickers across all users
    all_tickers: set = set()
    for tickers in user_tickers.values():
        all_tickers.update(tickers)

    if not all_tickers:
        print("[agent] news_check: no tickers to check.")
        return

    cutoff = _time.time() - 86400
    # {sym: [(title, publisher, link, published_ts), ...]}
    ticker_news: dict[str, list] = {}

    for sym in all_tickers:
        try:
            ticker_obj = yf.Ticker(sym)
            news = ticker_obj.news or []
            relevant = []
            for item in news:
                published = item.get("providerPublishTime", 0)
                title = item.get("title", "")
                if published < cutoff:
                    continue
                if sym.lower() not in title.lower():
                    continue
                relevant.append((
                    title,
                    item.get("publisher", ""),
                    item.get("link", ""),
                    published,
                ))
            if relevant:
                ticker_news[sym] = relevant
        except Exception as exc:
            print(f"[agent] news_check: news fetch failed for {sym}: {exc}")

    if not ticker_news:
        print("[agent] news_check: no relevant news found.")
        return

    # Send per-user — all stories in one batched message
    for uid, tickers in user_tickers.items():
        try:
            if _is_quiet_hours(uid):
                continue
            story_blocks: list[str] = []
            for sym in sorted(tickers):
                stories = ticker_news.get(sym)
                if not stories:
                    continue
                # Take at most 2 stories per ticker to avoid wall-of-text
                for title, publisher, link, published_ts in stories[:2]:
                    dedup_key = f"news_{sym}_{hash(title) % 100000}_{et_today()}"
                    if _is_alerted(dedup_key):
                        continue
                    _mark_alerted(dedup_key)

                    age_secs = _time.time() - published_ts
                    if age_secs < 3600:
                        age_str = f"{int(age_secs / 60)}m ago"
                    elif age_secs < 7200:
                        age_str = "1h ago"
                    else:
                        age_str = f"{int(age_secs / 3600)}h ago"

                    context_note = _news_context_note(title)
                    link_part    = f' <a href="{link}">→</a>' if link else ""
                    story_blocks.append(
                        f"<b>{sym}</b> · {age_str}{link_part}\n"
                        f"{title}\n"
                        f"<i>💡 {context_note}</i>"
                    )

            if story_blocks:
                header = f"📰 <b>Position News ({len(story_blocks)} stor{'y' if len(story_blocks)==1 else 'ies'})</b>\n\n"
                body   = "\n\n──────────────\n\n".join(story_blocks)
                send_message(header + body, chat_id=uid, parse_mode="HTML")
        except Exception as exc:
            print(f"[agent] news_check: send failed for {uid} (non-critical): {exc}")

    print("[agent] news_check: complete.")


# ── Pre-Earnings Guidance ─────────────────────────────────────────────────────

def run_pre_earnings_guidance():
    """
    4:00 PM ET weekdays — alert users when a position or watchlist ticker
    has earnings tomorrow (within 24-48 hours).
    """
    _log_cron_run("pre_earnings")
    print("[agent] Running pre-earnings guidance check...")
    import yfinance as yf

    now_ts = datetime.now(ET).timestamp()
    window_start = now_ts + 86400   # 24 hours from now
    window_end   = now_ts + 172800  # 48 hours from now

    def _pre_earnings_msgs(uid):
            out = []
            cfg = get_user_config(uid)
            if cfg.get("paused"):
                return None

            log = load_user_trade_log(uid)
            open_tickers  = [t["ticker"] for t in log.get("open", []) if t.get("ticker")]
            watch_tickers = cfg.get("watchlist", [])
            all_tickers   = list(dict.fromkeys(open_tickers + watch_tickers))

            for sym in all_tickers:
                dedup_key = f"preearnings_{sym}_{et_today()}"
                if _is_alerted(dedup_key):
                    continue
                try:
                    info = yf.Ticker(sym).info
                    earnings_ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
                    if not earnings_ts:
                        continue
                    earnings_ts = float(earnings_ts)
                    if not (window_start <= earnings_ts <= window_end):
                        continue

                    # Data for message
                    target_price     = info.get("targetMeanPrice")
                    current_price    = info.get("regularMarketPrice") or info.get("currentPrice")
                    rec_key          = info.get("recommendationKey", "hold")
                    n_analysts       = info.get("numberOfAnalystOpinions") or 0

                    # Analyst consensus text
                    if rec_key in ("strong_buy", "buy"):
                        rec_text = f"Analysts lean bullish ({n_analysts} analysts, avg target ${target_price:.0f})" if target_price else f"Analysts lean bullish ({n_analysts} analysts)"
                    elif rec_key in ("sell", "strong_sell"):
                        rec_text = f"Analysts lean cautious ({n_analysts} analysts, avg target ${target_price:.0f})" if target_price else f"Analysts lean cautious ({n_analysts} analysts)"
                    else:
                        rec_text = f"Analysts are mixed ({n_analysts} analysts, avg target ${target_price:.0f})" if target_price else f"Analysts are mixed ({n_analysts} analysts)"

                    # Upside
                    upside_str = ""
                    if target_price and current_price and float(current_price) > 0:
                        upside = (float(target_price) - float(current_price)) / float(current_price) * 100
                        sign   = "+" if upside >= 0 else ""
                        upside_str = f"\n📈 Upside to target: {sign}{upside:.1f}% from current price"

                    # Stop info for open positions
                    stop_str = ""
                    for trade in log.get("open", []):
                        if trade.get("ticker") == sym and trade.get("stop_loss"):
                            stop_str = f" Your stop at ${_p(float(trade['stop_loss']))} is your safety net either way."
                            break

                    _mark_alerted(dedup_key)
                    msg = (
                        f"📅 <b>Earnings Tomorrow — {sym}</b>\n\n"
                        f"{sym} reports earnings <b>tomorrow</b>.\n\n"
                        f"📊 Analyst consensus: {rec_text}"
                        f"{upside_str}\n\n"
                        f"<b>What should you do?</b>\n"
                        f"Earnings are unpredictable — even good companies can drop on good results "
                        f"if expectations were too high.\n\n"
                        f"<b>Two common approaches:</b>\n"
                        f"• <b>Hold through</b> — if you believe in the long-term thesis and your stop is in place\n"
                        f"• <b>Trim before</b> — sell half now to lock in some gains, let the rest ride\n\n"
                        f"There's no universally right answer.{stop_str}"
                    )
                    out.append({"text": msg, "keyboard": [
                        [_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]]})
                except Exception as exc:
                    print(f"[agent] pre_earnings: data fetch failed for {sym}: {exc}")
            return out

    _fanout(_pre_earnings_msgs, tag="pre_earnings")

    print("[agent] pre_earnings guidance complete.")


# ── Monthly Market Commentary ─────────────────────────────────────────────────

def run_monthly_commentary():
    """
    1st of each month — send a market commentary with last month's returns
    and a forward-looking view for the new month.
    """
    _log_cron_run("monthly_commentary")
    now_et = datetime.now(ET)
    year   = now_et.year
    month  = now_et.strftime("%B")

    dedup_key = f"monthly_commentary_{year}_{month}"
    if _is_alerted(dedup_key):
        print(f"[agent] monthly_commentary: already sent for {month} {year} — skipping.")
        return

    print(f"[agent] Running monthly commentary for {month} {year}...")
    import yfinance as yf

    try:
        # Fetch 1-month returns for broad market + sectors
        symbols = {
            "SPY":  "S&P 500",
            "QQQ":  "Nasdaq 100",
            "XLK":  "Technology",
            "XLF":  "Financials",
            "XLE":  "Energy",
            "XLV":  "Healthcare",
            "XLI":  "Industrials",
            "^VIX": "VIX",
        }
        raw = yf.download(list(symbols.keys()), period="1mo", progress=False)["Close"]
        returns: dict[str, float] = {}
        current_vix = None
        for sym in symbols:
            try:
                col = raw[sym] if sym in raw.columns else None
                if col is None or col.dropna().empty:
                    continue
                vals = col.dropna()
                if sym == "^VIX":
                    current_vix = float(vals.iloc[-1])
                else:
                    ret = (float(vals.iloc[-1]) - float(vals.iloc[0])) / float(vals.iloc[0]) * 100
                    returns[sym] = round(ret, 2)
            except Exception:
                pass

        spy_ret  = returns.get("SPY",  0.0)
        qqq_ret  = returns.get("QQQ",  0.0)
        sector_returns = {k: returns[k] for k in ("XLK", "XLF", "XLE", "XLV", "XLI") if k in returns}
        sorted_sectors = sorted(sector_returns.items(), key=lambda x: x[1], reverse=True)
        sector_names   = {"XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Healthcare", "XLI": "Industrials"}

        best_sector  = sector_names.get(sorted_sectors[0][0], sorted_sectors[0][0]) if sorted_sectors else "N/A"
        worst_sector = sector_names.get(sorted_sectors[-1][0], sorted_sectors[-1][0]) if sorted_sectors else "N/A"

    except Exception as exc:
        print(f"[agent] monthly_commentary: market data fetch failed: {exc}")
        spy_ret = qqq_ret = 0.0
        sorted_sectors = []
        best_sector = worst_sector = "N/A"
        current_vix = None

    # Try Claude for commentary; fall back to template
    commentary = None
    try:
        sector_lines = "\n".join(
            f"  {sector_names.get(sym, sym)}: {ret:+.1f}%"
            for sym, ret in sorted_sectors
        )
        vix_line = f"VIX: {current_vix:.1f}" if current_vix else ""
        prompt = (
            f"Write a 3-paragraph monthly market commentary for {month} {year} based on this data:\n"
            f"SPY: {spy_ret:+.1f}%\nQQQ: {qqq_ret:+.1f}%\n{sector_lines}\n{vix_line}\n\n"
            f"Paragraph 1: What happened last month (SPY/QQQ returns, dominant theme). "
            f"Paragraph 2: Sector leadership/laggards and why. "
            f"Paragraph 3: What to watch this month (forward-looking). "
            f"Use plain concise language suitable for retail investors. No bullet points, just prose. "
            f"Each paragraph 2-3 sentences max."
        )
        client = _get_client()
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        commentary = resp.content[0].text.strip()
    except Exception as exc:
        print(f"[agent] monthly_commentary: Claude call failed, using template: {exc}")

    if not commentary:
        sign_spy = "+" if spy_ret >= 0 else ""
        sign_qqq = "+" if qqq_ret >= 0 else ""
        commentary = (
            f"Last month, the S&P 500 returned {sign_spy}{spy_ret:.1f}% while the Nasdaq 100 returned "
            f"{sign_qqq}{qqq_ret:.1f}%. Markets were driven by a mix of macro data and earnings results.\n\n"
            f"{best_sector} was the standout sector, while {worst_sector} lagged. "
            f"Sector rotation remained a key theme as investors repositioned around economic data.\n\n"
            f"This month, watch for central bank policy signals and upcoming earnings releases "
            f"which could set the tone for equities. Keep your stops in place and let the market tell its story."
        )

    _mark_alerted(dedup_key)

    msg = (
        f"📋 <b>Monthly Market Update — {month} {year}</b>\n\n"
        f"{commentary}\n\n"
        f"<b>Your portfolio:</b> Check /summary for your current positions and exposure."
    )

    def _commentary_msg(uid):
        return None if get_user_config(uid).get("paused") else msg
    n = _fanout(_commentary_msg, tag="monthly_commentary")

    print(f"[agent] monthly_commentary: sent for {month} {year} to {n} user(s).")


# ── Monthly Personal P&L Digest ───────────────────────────────────────────────

def run_monthly_pnl_digest():
    """
    1st of each month — send each user a personal P&L recap for the prior month:
    trades closed last month, win rate, total gain/loss, best/worst trade,
    and a brief goal progress nudge if the user has an annual return goal set.
    Skipped for users with no closed trades last month.
    """
    _log_cron_run("monthly_pnl_digest")
    now_et     = datetime.now(ET)
    # Target: the previous calendar month
    first_this = now_et.replace(day=1)
    last_month = first_this - timedelta(days=1)
    target_m   = last_month.month
    target_y   = last_month.year
    month_name = last_month.strftime("%B %Y")

    dedup_key = f"monthly_pnl_{target_y}_{target_m}"
    if _is_alerted(dedup_key):
        print(f"[agent] monthly_pnl_digest: already sent for {month_name} — skipping.")
        return

    print(f"[agent] Running monthly P&L digest for {month_name}...")
    def _digest_msg(uid):
            log    = load_user_trade_log(uid)
            closed = log.get("closed", [])

            # Filter to trades closed last month
            month_trades = []
            for t in closed:
                d_str = t.get("closed_date") or t.get("exit_date") or ""
                if not d_str:
                    continue
                try:
                    from datetime import date as _date
                    import re as _re
                    m = _re.match(r"(\d{4})-(\d{2})", d_str)
                    if m and int(m.group(1)) == target_y and int(m.group(2)) == target_m:
                        month_trades.append(t)
                except Exception:
                    pass

            if not month_trades:
                return None  # nothing closed last month — skip this user

            # Compute stats
            wins    = [t for t in month_trades if float(t.get("return_pct", 0)) > 0]
            losses  = [t for t in month_trades if float(t.get("return_pct", 0)) <= 0]
            win_rate = len(wins) / len(month_trades) * 100
            total_gain = sum(float(t.get("gain_usd", 0)) for t in month_trades)
            best  = max(month_trades, key=lambda t: float(t.get("return_pct", 0)))
            worst = min(month_trades, key=lambda t: float(t.get("return_pct", 0)))

            sign   = "+" if total_gain >= 0 else ""
            w_icon = "🟢" if total_gain >= 0 else "🔴"
            lines  = [
                f"📆 <b>Your {month_name} P&amp;L recap</b>",
                "",
                f"  Trades closed: <b>{len(month_trades)}</b>  ({len(wins)}W / {len(losses)}L)",
                f"  Win rate:      <b>{win_rate:.0f}%</b>",
                f"  Total P&amp;L:     {w_icon} <b>{sign}${abs(total_gain):,.2f}</b>",
                "",
                f"  🏆 Best:  <b>{best['ticker']}</b>  {'+' if float(best.get('return_pct', 0)) >= 0 else ''}{best.get('return_pct', 0):.1f}%",
                f"  💀 Worst: <b>{worst['ticker']}</b>  {'+' if float(worst.get('return_pct', 0)) >= 0 else ''}{worst.get('return_pct', 0):.1f}%",
            ]

            # Goal progress nudge
            try:
                u_cfg       = get_user_config(uid)
                goal_pct    = u_cfg.get("annual_return_goal")
                port_cfg    = u_cfg.get("portfolio", {}) if isinstance(u_cfg.get("portfolio"), dict) else {}
                cap         = float(port_cfg.get("portfolio_size", 0))
                start_bal   = u_cfg.get("year_start_balance") or cap
                if goal_pct and start_bal and float(start_bal) > 0:
                    all_closed  = log.get("closed", [])
                    ytd_gain    = sum(float(t.get("gain_usd", 0)) for t in all_closed
                                      if (t.get("closed_date") or "").startswith(str(target_y)))
                    ytd_pct     = ytd_gain / float(start_bal) * 100
                    goal_f      = float(goal_pct)
                    progress    = min(ytd_pct / goal_f * 100, 100) if goal_f else 0
                    bar_len     = 15
                    filled      = int(progress / 100 * bar_len)
                    bar         = "█" * filled + "░" * (bar_len - filled)
                    lines += [
                        "",
                        f"  🎯 YTD vs goal ({goal_f:.0f}%):  {ytd_pct:+.1f}%",
                        f"  [{bar}] {progress:.0f}%",
                    ]
            except Exception:
                pass

            lines += ["", "<i>Run /stats for full history · /review for AI coaching</i>"]
            return "\n".join(lines)

    sent = _fanout(_digest_msg, tag="monthly_pnl_digest")

    _mark_alerted(dedup_key)
    print(f"[agent] monthly_pnl_digest: sent to {sent} user(s) for {month_name}.")


# ── Cash Position Guidance ────────────────────────────────────────────────────

def _check_cash_position(uid: str, user_settings: dict, open_positions: list, current_prices: dict) -> str | None:
    """
    Warn user if they are over 85% invested based on total_portfolio_size setting.
    Fires at most once per week per user. Returns text block or None (caller batches).
    """
    total_portfolio = user_settings.get("total_portfolio_size")
    if not total_portfolio:
        # Also check inside the nested settings structure
        total_portfolio = user_settings.get("settings", {}).get("total_portfolio_size")
    if not total_portfolio:
        # Fall back to the /settings capital key (portfolio.portfolio_size) so the
        # warning fires for users who set capital there instead of in onboarding.
        total_portfolio = user_settings.get("portfolio", {}).get("portfolio_size")
    if not total_portfolio:
        return

    try:
        total_portfolio = float(total_portfolio)
    except (TypeError, ValueError):
        return

    if total_portfolio <= 0:
        return

    # Calculate total invested
    total_invested = 0.0
    for pos in open_positions:
        ticker = pos.get("ticker")
        current = current_prices.get(ticker) if ticker else None
        if not current:
            continue
        shares = pos.get("shares") or pos.get("quantity")
        alloc  = pos.get("allocation") or pos.get("budget") or 0
        entry  = pos.get("entry_price")
        if shares:
            total_invested += float(shares) * float(current)
        elif alloc and entry and float(entry) > 0:
            est_shares = float(alloc) / float(entry)
            total_invested += est_shares * float(current)

    if total_invested <= 0:
        return

    invested_pct = total_invested / total_portfolio * 100
    if invested_pct <= 85:
        return

    # Dedup: once per week
    week_num  = et_today().isocalendar()[1]
    dedup_key = f"cash_warning_{uid}_{week_num}"
    if _is_alerted(dedup_key):
        return
    _mark_alerted(dedup_key)

    return (
        f"💰 <b>Allocation check:</b> You're {invested_pct:.0f}% invested "
        f"(${total_invested:,.0f} of ${total_portfolio:,.0f}). "
        f"Consider keeping 15-20% cash for dips and new opportunities."
    )


# ── Portfolio Correlation Warning ─────────────────────────────────────────────

def _check_portfolio_correlation(uid: str, open_positions: list) -> str | None:
    """
    Warn user if any two open stock positions have moved together > 85% of the time
    over the last 30 days. Runs on Mondays only (called from run_eod_summary).
    Deduped once per week per user. Returns text block or None (caller batches).
    """
    import yfinance as yf

    # Filter to stock positions only. For legacy positions with no asset_type,
    # classify via the canonical crypto set (not an ad-hoc last-char-digit rule).
    from price_checker import CRYPTO_SYMBOLS
    stock_positions = [p for p in open_positions if p.get("asset_type") == "stock" or
                       (p.get("asset_type") is None and p.get("ticker")
                        and p.get("ticker", "").upper() not in CRYPTO_SYMBOLS)]
    tickers = list(dict.fromkeys(p["ticker"] for p in stock_positions if p.get("ticker")))

    if len(tickers) < 3:
        return

    week_num  = et_today().isocalendar()[1]
    dedup_key = f"correlation_{uid}_{week_num}"
    if _is_alerted(dedup_key):
        return

    try:
        data = yf.download(tickers, period="30d", progress=False)["Close"]
        if data.empty:
            return
        # Ensure DataFrame with columns
        if not hasattr(data, "columns") or len(data.columns) < 2:
            return
        corr = data.pct_change().corr()
    except Exception as exc:
        print(f"[agent] _check_portfolio_correlation: data fetch failed for {uid}: {exc}")
        return

    # Find highly correlated pairs
    high_corr_pairs: list[tuple[str, str, float]] = []
    checked = set()
    for t1 in tickers:
        for t2 in tickers:
            if t1 == t2 or (t1, t2) in checked or (t2, t1) in checked:
                continue
            checked.add((t1, t2))
            try:
                if t1 not in corr.columns or t2 not in corr.columns:
                    continue
                c = float(corr.loc[t1, t2])
                if c > 0.85:
                    high_corr_pairs.append((t1, t2, c))
            except Exception:
                pass

    if not high_corr_pairs:
        return

    _mark_alerted(dedup_key)

    # Build pairs text
    pairs_lines = []
    for t1, t2, c in high_corr_pairs:
        pct = int(c * 100)
        pairs_lines.append(f"Your positions <b>{t1}</b> and <b>{t2}</b> have moved together <b>{pct}%</b> of the time over the last 30 days.")

    pairs_text = "\n".join(pairs_lines)

    return (
        f"🔗 <b>Correlation:</b> {pairs_text} "
        f"A sector move could hit both — your risk is more concentrated than it looks. "
        f"<code>/positions</code> for full view."
    )


# ── Options Flow Alert ────────────────────────────────────────────────────────

def _check_options_flow_alert(uid: str, open_positions: list) -> str | None:
    """
    Check held tickers for unusual options activity. Returns a text block if any
    position shows BOTH unusual vol/OI ratio AND signal_score >= 3, with enough
    volume to rule out thin chains (call+put volume >= 200). Deduped 24h per ticker.
    """
    from options_flow import batch_options_signals

    tickers = list(dict.fromkeys(
        p["ticker"].upper() for p in open_positions if p.get("ticker")
    ))
    if not tickers:
        return None

    signals = batch_options_signals(tickers)
    hits: list[str] = []
    for ticker, sig in signals.items():
        # Require BOTH unusual flag AND meaningful conviction score — OR/score-only
        # fired too many false positives on thin options chains
        unusual = sig.get("unusual", False)
        score   = sig.get("signal_score", 0)
        total_vol = (sig.get("call_volume") or 0) + (sig.get("put_volume") or 0)
        if not (unusual and score >= 3 and total_vol >= 200):
            continue
        dedup_key = f"options_flow_{uid}_{ticker}"
        if _is_alerted(dedup_key):
            continue
        _mark_alerted(dedup_key, ttl_hours=24)

        iv_label   = sig.get("iv_label", "")
        bullish    = sig.get("bullish_flow", False)
        bearish    = sig.get("bearish_flow", False)
        sweep      = sig.get("sweep_detected", False)

        direction  = "📈 bullish" if bullish else ("📉 bearish" if bearish else "⚡ mixed")
        sweep_str  = " Sweep detected." if sweep else ""
        iv_str     = f" IV: {iv_label}." if iv_label and iv_label != "NORMAL" else ""
        hits.append(
            f"<b>{ticker}</b> — unusual options flow ({direction}, score {score:+d}).{sweep_str}{iv_str}"
        )

    if not hits:
        return None

    body = "\n".join(hits)
    return (
        f"🔥 <b>Unusual Options Activity</b> on your held positions:\n\n{body}\n\n"
        f"Smart money may be positioning ahead of a move — worth a look."
    )


# ── Congressional Cluster Alert ───────────────────────────────────────────────

def _check_congressional_alert(uid: str, open_positions: list) -> str | None:
    """
    Fire when 3+ Congress members have recently bought a ticker you hold (cluster buy).
    Deduped once per week per ticker per user.
    """
    from congressional_tracker import batch_congressional_signals

    tickers = list(dict.fromkeys(
        p["ticker"].upper() for p in open_positions if p.get("ticker")
    ))
    if not tickers:
        return None

    signals = batch_congressional_signals(tickers)
    hits: list[str] = []
    week_num = et_today().isocalendar()[1]
    for ticker, sig in signals.items():
        if not sig.get("is_cluster"):
            continue
        dedup_key = f"congressional_{uid}_{ticker}_{week_num}"
        if _is_alerted(dedup_key):
            continue
        _mark_alerted(dedup_key, ttl_hours=168)  # 7-day cooldown

        members = sig.get("congress_members", 0)
        buys    = sig.get("congress_buys", 0)
        note    = sig.get("note", "")
        hits.append(f"<b>{ticker}</b> — {members} members, {buys} purchase(s). {note}")

    if not hits:
        return None

    body = "\n".join(hits)
    return (
        f"🏛 <b>Congressional Cluster Buy</b> on your held positions:\n\n{body}\n\n"
        f"3+ members buying the same stock you hold is historically significant. "
        f"<i>Note: STOCK Act filings typically lag 30–45 days — this reflects recent reported activity, not today's trades.</i>"
    )


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
            _carded: list = []

            def _recap_msg(uid):
                user_cfg = {**config, **get_user_config(uid)}
                if user_cfg.get("paused"):
                    print(f"[agent] Skipping weekly recap for {uid} — picks paused.")
                    return None
                message = format_weekly_recap_message(recap, config=user_cfg)
                if DRY_RUN:
                    print(f"\n{'='*60}\nDRY RUN — Weekly Recap for {uid}:\n{'='*60}\n{message}")
                    return None
                _carded.append(uid)
                return {"text": message, "keyboard": [[
                    _miniapp_btn("📈 My Stats",     "performance", "STATS"),
                    _miniapp_btn("📊 My Positions", "portfolio",   "POSITIONS"),
                ], [
                    _miniapp_btn("🏆 Community",    "performance", "COMMUNITY"),
                    _miniapp_btn("📋 Full History", "portfolio",   "HISTORY"),
                ]]}

            _fanout(_recap_msg, recipients=recipients, tag="weekly_recap")

            # 🔴 The alpha card stays SERIAL: broadcast_all speaks sendMessage
            # only, and faking a photo through it would send a broken message.
            # It runs after the recap so the card never arrives before the text
            # it illustrates. Card generation dominates the cost here anyway.
            for uid in _carded:
                try:
                    from card_generator import generate_performance_card
                    card = generate_performance_card(uid)
                    if card:
                        send_photo(card, caption="📊 Your weekly alpha card", chat_id=uid)
                except Exception as _card_exc:
                    print(f"[agent] Weekly card failed for {uid} (non-critical): {_card_exc}")
        else:
            print("[agent] No weekly picks data — skipping recap.")
    except Exception as exc:
        print(f"[agent] Weekly recap failed (non-critical): {exc}")

    # Step 3: Auto-push missed picks digest — "picks you skipped and their outcome"
    print("[agent] Building auto missed-picks digest...")
    try:
        from cmd_trades import get_missed_picks_message
        def _missed_msg(uid):
            user_cfg = {**config, **get_user_config(uid)}
            if user_cfg.get("paused"):
                return None
            msg, kb = get_missed_picks_message(uid)
            if not msg:
                return None
            if DRY_RUN:
                print(f"\n[DRY RUN] Missed picks for {uid}:\n{msg}")
                return None
            return {"text": msg, "keyboard": kb}

        _fanout(_missed_msg, tag="missed_picks")
    except Exception as exc:
        print(f"[agent] Missed picks auto-push failed (non-critical): {exc}")


# ── Portfolio health nudge helper ────────────────────────────────────────────

def _check_portfolio_health(uid: str, current_prices: dict) -> list:
    """
    Proactive portfolio health check — fires once per ticker per day.
    Checks for concentration risk, positions near stop, and big winners ready to trim.
    Returns list of text blocks (caller batches into one digest message).
    """
    log    = load_user_trade_log(uid)
    trades = log.get("open", [])
    if not trades:
        return []

    today   = et_today()
    blocks: list[str] = []

    # Calculate total portfolio value based on current prices
    from market_data import plausible_price
    total_value = 0.0
    trade_values: list[tuple] = []
    for trade in trades:
        ticker  = trade.get("ticker")
        entry   = trade.get("entry_price")
        shares  = trade.get("shares") or trade.get("quantity")
        if not (ticker and _is_pos(entry)):
            continue
        raw = current_prices.get(ticker) if ticker else None
        # An implausible/missing live price (holiday garbage: $0.01 that collapses
        # the portfolio total and fires a false 49% concentration / stop) falls
        # back to entry (cost basis) → position still counts at a 0% move, so no
        # false nudge fires off a $0 quote.
        current = float(raw) if plausible_price(raw, entry) else float(entry)
        alloc = trade.get("allocation") or trade.get("budget") or 0
        if shares:
            pos_value = float(shares) * current
        elif alloc:
            est_shares = float(alloc) / float(entry)
            pos_value  = est_shares * current
        else:
            continue
        trade_values.append((ticker, trade, current, float(entry), pos_value))
        total_value += pos_value

    if total_value <= 0:
        return []

    for ticker, trade, curr_f, entry_f, pos_value in trade_values:
        pct_of_portfolio = (pos_value / total_value) * 100
        stop     = trade.get("stop_loss")
        gain_pct = (curr_f - entry_f) / entry_f * 100

        # ── Concentration risk ────────────────────────────────────────────────
        if pct_of_portfolio > 35:
            key = f"health_{uid}_{ticker}_{today}"
            if not _is_alerted(key):
                _mark_alerted(key)
                blocks.append(
                    f"⚠️ <b>{ticker}</b> is {pct_of_portfolio:.0f}% of your portfolio — "
                    f"consider trimming to reduce single-stock risk."
                )

        # ── Near stop ─────────────────────────────────────────────────────────
        if stop:
            stop_f      = float(stop)
            pct_to_stop = (curr_f - stop_f) / curr_f * 100
            if 0 < pct_to_stop <= STOP_PROXIMITY_PCT:
                key = f"health_stop_{uid}_{ticker}_{today}"
                if not _is_alerted(key):
                    _mark_alerted(key)
                    blocks.append(
                        f"⚠️ <b>{ticker}</b> is within 3% of your stop "
                        f"(<code>${_p(stop_f)}</code>) — consider reviewing."
                    )

        # ── Big winner ready to trim ──────────────────────────────────────────
        if gain_pct > 12:
            key = f"health_winner_{uid}_{ticker}_{today}"
            if not _is_alerted(key):
                _mark_alerted(key)
                blocks.append(
                    f"🎯 <b>{ticker}</b> is up +{gain_pct:.1f}% — "
                    f"consider partial profits or moving stop to breakeven."
                )

    return blocks


# ── Portfolio daily drawdown alert ───────────────────────────────────────────

def _check_portfolio_drawdown(uid: str, current_prices: dict) -> None:
    """
    If the user's open portfolio has dropped more than `max_daily_drawdown_pct`
    (default 3%) on the day, send a single urgent alert.
    Requires both current price and previous close — fetched via yfinance history.
    Deduped once per calendar day per user.
    """
    import yfinance as yf
    from datetime import date as _date

    today_str = et_today().isoformat()
    dedup_key = f"drawdown_{uid}_{today_str}"
    if _is_alerted(dedup_key):
        return

    try:
        cfg       = get_user_config(uid)
        threshold = float(cfg.get("max_daily_drawdown_pct") or 3.0)

        log         = load_user_trade_log(uid)
        open_trades = log.get("open", [])
        if not open_trades:
            return

        tickers = list({t["ticker"] for t in open_trades if t.get("ticker")})
        if not tickers:
            return

        # Fetch 2 days of data to get prev close + current (handles crypto via _download_prices)
        from price_checker import _SYMBOL_TO_CG_ID as _CG_IDS
        yf_map = {(f"{t}-USD" if t.upper() in _CG_IDS else t): t for t in tickers}
        yf_tickers = list(yf_map.keys())
        raw = yf.download(" ".join(yf_tickers), period="2d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw.empty or len(raw) < 2:
            return

        close_data = raw["Close"]
        if not hasattr(close_data, "columns"):
            close_data = close_data.to_frame(name=yf_tickers[0])

        prev_closes: dict = {}
        curr_closes: dict = {}
        for yf_t, orig_t in yf_map.items():
            if yf_t not in close_data.columns:
                continue
            col = close_data[yf_t].dropna()
            if len(col) >= 2:
                prev_closes[orig_t] = float(col.iloc[-2])
                curr_closes[orig_t] = float(col.iloc[-1])
            elif len(col) == 1:
                curr_closes[orig_t] = float(col.iloc[-1])

        # Use intraday current_prices if more recent than daily close — but only
        # when the tick is plausible against the price we already know for real.
        # This overwrite had NO validation, so one bad tick fired a
        # "🚨 Portfolio Drawdown Alert ... down 99.9% today".
        from market_data import plausible_price
        for t in tickers:
            if t not in current_prices:
                continue
            reference = prev_closes.get(t) or curr_closes.get(t)
            if plausible_price(current_prices[t], reference):
                curr_closes[t] = current_prices[t]

        # Calculate today's portfolio P&L
        total_prev_value  = 0.0
        total_curr_value  = 0.0
        position_lines    = []

        for trade in open_trades:
            ticker = trade.get("ticker")
            if not ticker or ticker not in prev_closes:
                continue
            shares = trade.get("shares") or trade.get("quantity")
            entry  = trade.get("entry_price")
            if not shares:
                # Estimate shares from allocation if available
                alloc = trade.get("allocation") or trade.get("budget")
                if alloc and _is_pos(entry):
                    shares = float(alloc) / float(entry)
            if not shares:
                continue

            prev_v = prev_closes[ticker] * float(shares)
            curr_v = curr_closes.get(ticker, prev_closes[ticker]) * float(shares)
            day_pnl = curr_v - prev_v
            total_prev_value += prev_v
            total_curr_value += curr_v

            if abs(day_pnl) >= 0.01:
                sign = "+" if day_pnl >= 0 else ""
                position_lines.append(
                    f"  {'🔴' if day_pnl < 0 else '🟢'} <b>{ticker}</b>  {sign}${abs(day_pnl):,.0f}"
                )

        if total_prev_value <= 0:
            return

        drawdown_pct = (total_curr_value - total_prev_value) / total_prev_value * 100
        if drawdown_pct >= -threshold:
            return   # Within acceptable range, no alert needed

        _mark_alerted(dedup_key)

        total_loss = total_curr_value - total_prev_value
        breakdown  = "\n".join(position_lines) if position_lines else "  <i>No per-position detail available</i>"

        send_inline_keyboard(
            f"🚨 <b>Portfolio Drawdown Alert</b>\n\n"
            f"Your open positions are down <b>{drawdown_pct:.1f}%</b> today "
            f"(${abs(total_loss):,.0f} on the day).\n\n"
            f"<b>By position:</b>\n{breakdown}\n\n"
            f"<i>Threshold: -{threshold:.0f}% · Adjust in /settings</i>",
            [[_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]],
            chat_id=uid,
        )

    except Exception as exc:
        print(f"[agent] drawdown check failed for {uid} (non-critical): {exc}")


# ── Trade reminders helper ───────────────────────────────────────────────────

def _check_trade_reminders() -> None:
    """
    Check each user's pending reminders. For any reminder whose date is today
    (or overdue), send a Telegram push and remove it from their config.
    Called daily from run_midday_check (12:30 PM ET).
    """
    from datetime import date as _date
    today_str = et_today().isoformat()

    # The reminder list is cleared only AFTER the sends land — clearing it during
    # the build would drop a reminder whose send then failed.
    _fired: dict = {}

    def _reminder_msgs(uid):
        cfg = get_user_config(uid)
        reminders = list(cfg.get("reminders") or [])
        if not reminders:
            return None

        due    = [r for r in reminders if r.get("date", "9999") <= today_str]
        future = [r for r in reminders if r.get("date", "9999") > today_str]
        if not due:
            return None

        _fired[uid] = future
        out = []
        for r in due:
            ticker = r.get("ticker", "?")
            note   = r.get("note", "")
            note_part = f"\n📝 <i>{note}</i>" if note else ""
            out.append({
                "text": (f"⏰ <b>Trade Reminder: {ticker}</b>{note_part}\n\n"
                         f"You asked to be reminded about this position today."),
                "keyboard": [[_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]],
            })
        return out

    _fanout(_reminder_msgs, tag="reminders")

    for uid, future in _fired.items():
        try:
            update_user_config(uid, "reminders", future)
        except Exception as exc:
            print(f"[agent] reminders: could not clear for {uid} (non-critical): {exc}")


# ── Trailing stop nudge helper ───────────────────────────────────────────────

def _check_trailing_stops(current_prices: dict, uid: str,
                           msg_buffer: list | None = None) -> None:
    """
    For each open trade for uid, check if the position has moved far enough
    to warrant adjusting the stop-loss. Fires once per threshold per trade,
    tracked via flags stamped onto the trade dict in the log.

    If msg_buffer is provided, text alerts are appended to it instead of being
    sent immediately — callers can flush the buffer as a single combined message
    to reduce notification noise (alert batching). Inline-keyboard alerts (like
    the break-even one-tap button) are always sent immediately.

    Thresholds:
      +8%  from entry → suggest moving stop to breakeven (entry price)
      90%  of target  → suggest trailing stop to -5% from current (lock in gains)
    """
    def _emit(text: str) -> None:
        """Send or buffer a plain text alert."""
        if msg_buffer is not None:
            msg_buffer.append(text)
        else:
            send_message(text, chat_id=uid)
    log         = load_user_trade_log(uid)
    open_trades = log.get("open", [])
    dirty       = False   # track whether we modified the log

    for trade in open_trades:
        if trade.get("_paused"):
            continue  # user muted nudges for this position via /pause
        ticker  = trade.get("ticker")
        entry   = trade.get("entry_price")
        target  = trade.get("target_price")
        current = current_prices.get(ticker) if ticker else None

        # _is_pos rejects NaN but accepts $0.01 and a 10x spike, which fired
        # "crossed below your entry" and "up +900.0%" off a bad quote.
        from market_data import plausible_price
        if not (ticker and _is_pos(entry) and plausible_price(current, entry)):
            continue

        entry_f   = float(entry)
        current_f = float(current)
        gain_pct  = (current_f - entry_f) / entry_f * 100

        # ── Threshold -1: position crossed below entry ────────────────────────
        # Fires each time the stock dips below entry after recovering above it.
        # The flag is reset whenever price is back above entry so a second dip
        # (the dangerous double-dip pattern) always generates a new alert.
        if current_f >= entry_f and trade.get("_notified_below_entry"):
            # Price recovered above entry — reset so the next dip fires again
            trade["_notified_below_entry"] = False
            dirty = True

        if current_f < entry_f and not trade.get("_notified_below_entry"):
            trade["_notified_below_entry"] = True
            dirty = True
            pct_below = (entry_f - current_f) / entry_f * 100
            stop = trade.get("stop_loss")
            stop_note = (
                f"\n  Stop-loss: <code>${_p(float(stop))}</code> "
                f"({((current_f - float(stop)) / float(stop) * 100):+.1f}% away)"
            ) if stop else "\n  ⚠️ No stop-loss set — consider protecting this position."
            _emit(
                f"📉 <b>{ticker}</b> has crossed <b>below your entry</b> "
                f"(<code>${_p(entry_f)}</code>) and is now {pct_below:.1f}% underwater.{stop_note}\n\n"
                f"<i>Is the thesis still intact? If not, cutting early limits the damage.</i>\n"
                f"<code>/sold {ticker}</code> · <code>/updatestop {ticker}</code>"
            )

        # ── Threshold 1: +8% — move stop to breakeven (one-tap confirm) ────────
        if gain_pct >= 8.0 and not trade.get("_notified_breakeven"):
            trade["_notified_breakeven"] = True
            dirty = True
            entry_str = _p(entry_f)
            send_inline_keyboard(
                f"📈 <b>{ticker}</b> is up <b>+{gain_pct:.1f}%</b> from your entry "
                f"(<code>${entry_str}</code>).\n\n"
                f"💡 <b>Move stop to break-even?</b> You'd lock out a loss entirely "
                f"— playing with the house's money from here.",
                buttons=[[
                    {"text": f"✅ Set stop to ${entry_str}", "callback_data": f"be_stop|{ticker}|{entry_str}"},
                    {"text": "❌ Not yet", "callback_data": "be_stop_dismiss"},
                ], [
                    _miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS"),
                ]],
                chat_id=uid,
            )

        # ── Threshold 2: +15% gain — trail stop to lock in significant profit ──
        if gain_pct >= 15.0 and not trade.get("_notified_trail_15"):
            trade["_notified_trail_15"] = True
            dirty = True
            stop_f      = float(trade["stop_loss"]) if trade.get("stop_loss") else None
            trail_price = round(current_f * 0.92, 2)   # trail to -8% from now
            stop_note   = ""
            if stop_f and stop_f < entry_f:
                stop_note = (
                    f"  Your current stop (<code>${_p(stop_f)}</code>) is still below "
                    f"entry — you're exposed to a full reversal."
                )
            _trail_msg = (
                f"🚀 <b>{ticker}</b> is up <b>+{gain_pct:.1f}%</b> — a strong move.{stop_note}\n\n"
                f"💡 <b>Trail your stop</b> to <code>${_p(trail_price)}</code> "
                f"(-8% from current) to protect most of your gain if the stock reverses.\n"
                f"Use <code>/updatestop {ticker} {trail_price}</code> to set it now."
            )
            if msg_buffer is not None:
                msg_buffer.append(_trail_msg)
            else:
                send_inline_keyboard(
                    _trail_msg,
                    [[_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]],
                    chat_id=uid,
                )

        # ── Threshold 3: 90% of the way to target — trail at -5% from now ────
        if target:
            target_f = float(target)
            if target_f > entry_f:
                progress = (current_f - entry_f) / (target_f - entry_f)
                if progress >= 0.90 and not trade.get("_notified_trail_90"):
                    trade["_notified_trail_90"] = True
                    dirty = True
                    lock_price = round(current_f * 0.95, 2)
                    remaining  = round((target_f - current_f) / current_f * 100, 1)
                    _emit(
                        f"🎯 <b>{ticker}</b> is 90% of the way to target "
                        f"(<code>${_p(target_f)}</code>, {remaining}% left).\n"
                        f"💡 Consider trailing your stop to <code>${_p(lock_price)}</code> "
                        f"(-5% from now) to lock in most of the gain."
                    )

        # ── Threshold 0: target hit — proactive close suggestion ────────────
        if target and not trade.get("_notified_target_hit"):
            target_f = float(target)
            if current_f >= target_f and target_f > entry_f:
                trade["_notified_target_hit"] = True
                dirty = True
                gain_at_target = (target_f - entry_f) / entry_f * 100
                _emit(
                    f"🎯 <b>{ticker}</b> has hit your target of "
                    f"<code>${_p(target_f)}</code>!\n\n"
                    f"You're up <b>+{gain_pct:.1f}%</b> from entry — "
                    f"that's the full <b>+{gain_at_target:.1f}%</b> you planned for.\n"
                    f"💡 Consider closing now or trailing your stop to lock in gains.\n"
                    f"Use <code>/sold {ticker}</code> to close the position."
                )

        # ── Threshold 4: 2:1 reward/risk — partial profit nudge ─────────────
        # Fires when the position has gained 2× the original risk distance,
        # meaning the reward-to-risk ratio has been fully realised on half the pos.
        stop = trade.get("stop_loss")
        if target and stop and not trade.get("_notified_partial_profit"):
            target_f = float(target)
            stop_f   = float(stop)
            risk     = abs(entry_f - stop_f)
            if risk > 0 and target_f > entry_f:
                two_r_level = entry_f + 2.0 * risk
                if current_f >= two_r_level and two_r_level < target_f:
                    trade["_notified_partial_profit"] = True
                    dirty = True
                    gained_r = (current_f - entry_f) / risk
                    # Calculate specific share/dollar amounts if shares known
                    shares_held = trade.get("shares") or trade.get("quantity")
                    if shares_held:
                        sh = float(shares_held)
                        # Fractional-safe: a crypto position (0.0008 BTC) must not
                        # round to "1 of your 0 shares". Sell half, keep precision.
                        half = round(sh / 2, 6)
                        rem  = round(sh - half, 6)
                        profit_on_half = round(half * (current_f - entry_f), 2)
                        def _q(x):
                            return str(int(x)) if float(x).is_integer() else ("%g" % x)
                        specific = (
                            f"Sell <b>{_q(half)} of your {_q(sh)} shares</b> at "
                            f"<code>${_p(current_f)}</code> → locks in "
                            f"<b>${profit_on_half:,.2f}</b> profit. "
                            f"The remaining {_q(rem)} ride to target "
                            f"(<code>${_p(target_f)}</code>) risk-free."
                        )
                    else:
                        specific = (
                            f"Consider selling half your position at "
                            f"<code>${_p(current_f)}</code> to lock in profit — "
                            f"the rest rides to target (<code>${_p(target_f)}</code>) risk-free."
                        )
                    _emit(
                        f"💰 <b>{ticker}</b> is up <b>{gained_r:.1f}R</b> "
                        f"(<code>${_p(current_f)}</code>, +{gain_pct:.1f}% from entry).\n\n"
                        f"💡 <b>Partial profit opportunity:</b> {specific}\n"
                        f"<i>Use <code>/trim {ticker} {half if shares_held else ''}</code> to log a partial exit.</i>"
                    )

    if dirty:
        _persist_notify_flags(uid, open_trades,
                              ("_notified_target_hit", "_notified_partial_profit"))


# ── Quiet hours guard ────────────────────────────────────────────────────────

def _persist_notify_flags(uid: str, trades: list, flag_names: tuple) -> None:
    """Persist notification-dedup flags via a FRESH read of the trade log.

    These loops read the log, spend seconds fetching prices / building messages,
    then saved the WHOLE stale snapshot back — so a scheduled job and a user's
    mini-app action on the same account raced, and whoever wrote last erased the
    other. Only the flags matter here, so re-apply just those to the current
    record instead of writing back a snapshot taken seconds ago."""
    marks = {t.get("ticker"): [f for f in flag_names if t.get(f)]
             for t in trades if any(t.get(f) for f in flag_names)}
    if not marks:
        return
    from config_manager import mutate_user_trade_log

    def _mut(log):
        log = log or {"open": [], "closed": [], "watchlist": []}
        for tr in log.get("open", []):
            for f in marks.get(tr.get("ticker"), []):
                tr[f] = True
        return log, None

    mutate_user_trade_log(uid, _mut)


def _is_quiet_hours(uid: str) -> bool:
    """
    Return True if the current ET time falls within the user's quiet window.
    Config keys: quiet_from (e.g. "22:00"), quiet_to (e.g. "07:00").
    Defaults: 10 PM – 7 AM ET.  Quiet hours are OFF unless the user has
    explicitly enabled them (quiet_hours_enabled = True in their config).

    Urgent alerts (stop blown, drawdown, circuit breaker) bypass this check
    by NOT calling _is_quiet_hours at their call sites.
    """
    try:
        cfg = get_user_config(uid)
        if not cfg.get("quiet_hours_enabled"):
            return False

        from_str = cfg.get("quiet_from", "22:00")
        to_str   = cfg.get("quiet_to",   "07:00")

        now_et = datetime.now(ET)
        now_hm = now_et.hour * 60 + now_et.minute

        def _hm(s: str) -> int:
            parts = str(s).split(":")
            return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else 0

        from_hm = _hm(from_str)
        to_hm   = _hm(to_str)

        if from_hm > to_hm:
            # Spans midnight, e.g. 22:00 → 07:00
            return now_hm >= from_hm or now_hm < to_hm
        else:
            return from_hm <= now_hm < to_hm
    except Exception:
        return False   # never block alerts on an error


# ── Hold/fold nudge helper ───────────────────────────────────────────────────

def _check_hold_or_fold(uid: str, current_prices: dict) -> None:
    """
    For positions down 2–8% from entry AND within 5% of their stop: send a
    plain-English hold/fold nudge. Once per position per day via cache dedup.
    """
    from market_data import plausible_price
    log = load_user_trade_log(uid)
    for trade in log.get("open", []):
        ticker = trade.get("ticker")
        entry  = trade.get("entry_price")
        stop   = trade.get("stop_loss")
        if not (ticker and _is_pos(entry) and _is_pos(stop)):
            continue
        current = current_prices.get(ticker)
        # A garbage feed value ($0.01 on a holiday) is _is_pos-positive but would
        # fire a FALSE "🔴 STOP HIT — sell" alert (curr 0.01 <= any stop). Reject
        # anything implausible vs entry — never signal a sell off a bad quote.
        if not plausible_price(current, entry):
            continue
        entry_f, stop_f, curr_f = float(entry), float(stop), float(current)
        pct_from_entry = (curr_f - entry_f) / entry_f * 100
        pct_to_stop    = (curr_f - stop_f)  / curr_f  * 100

        # Hard stop breach — price at/below the stop. Fires regardless of how far
        # underwater, so a position that blew PAST its stop (e.g. BNB at -13.9%,
        # well beyond the -8% hold/fold floor below) still gets a loud alert. The
        # old code only nudged in the -2% to -8% band, so a blown-through stop
        # went silent and the pre-market card mislabelled it "NEAR STOP".
        # Fire ONCE per breach episode, then stay quiet while it remains below the
        # stop (a position sitting past its stop for days used to re-alert every
        # morning). The date-less key + long cooldown = one loud alert; recovery
        # above the stop re-arms it (below) so a fresh breach alerts again.
        stophit_key = f"stophit_{uid}_{ticker}"
        if curr_f <= stop_f:
            if not _is_alerted(stophit_key):
                _mark_alerted(stophit_key, ttl_hours=720)   # ~30d fire-once window
                send_inline_keyboard(
                    f"🔴 <b>STOP HIT — {ticker}</b>\n"
                    f"Now <code>${_p(curr_f)}</code>, below your "
                    f"<code>${_p(stop_f)}</code> stop  ·  <b>{pct_from_entry:.1f}%</b> from entry.\n\n"
                    f"<i>Your stop has triggered. Consider closing to protect capital, "
                    f"or move the stop if the thesis still holds.</i>\n\n"
                    f"<code>/sold {ticker}</code> to close  ·  <code>/updatestop {ticker}</code> to adjust",
                    [[_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]],
                    chat_id=uid,
                )
            continue   # past-stop: don't ALSO send the softer hold/fold nudge

        # Above the stop → re-arm, so if it breaches again later it alerts afresh.
        _clear_alerted(stophit_key)

        # Only nudge when down 2–8% AND within 5% of stop
        if -8.0 <= pct_from_entry <= -2.0 and pct_to_stop <= 5.0:
            key = f"holdfold_{uid}_{ticker}_{et_today()}"
            if _is_alerted(key):
                continue
            _mark_alerted(key)
            # Dollar risk: how much the user would lose if stop hits
            risk_str = ""
            try:
                shares = trade.get("shares") or trade.get("quantity")
                alloc  = trade.get("allocation") or trade.get("position_size")
                if shares:
                    risk_usd = (curr_f - stop_f) * float(shares)
                elif alloc:
                    est_shares = float(alloc) / entry_f
                    risk_usd = (curr_f - stop_f) * est_shares
                else:
                    risk_usd = None
                if risk_usd is not None:
                    risk_str = f"  Your risk to stop: <b>~${risk_usd:,.0f}</b>."
            except Exception:
                pass
            send_inline_keyboard(
                f"🤔 <b>Hold or fold? — {ticker}</b>\n"
                f"Down <b>{pct_from_entry:.1f}%</b> from entry · stop "
                f"<code>${_p(stop_f)}</code> ({pct_to_stop:.1f}% away).{risk_str}\n\n"
                f"<i>If the thesis still holds, stay in. "
                f"If something has changed, it's okay to cut early and protect capital.</i>\n\n"
                f"<code>/sold {ticker}</code> to close  ·  <code>/updatestop {ticker}</code> to adjust",
                [[_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]],
                chat_id=uid,
            )


# ── RSI helper (cached, shared across all per-user nudge calls in one run) ────

_rsi_cache: dict[str, tuple[float, float]] = {}  # ticker → (rsi, timestamp)

def _get_rsi(ticker: str, period: int = 14) -> float | None:
    """
    Compute 14-period RSI for a ticker. Cached 30 minutes in-process so
    multiple users holding the same ticker pay only one yfinance call.
    """
    import time as _time
    cached = _rsi_cache.get(ticker)
    if cached and (_time.time() - cached[1]) < 1800:
        return cached[0]
    try:
        import yfinance as yf
        from price_checker import _SYMBOL_TO_CG_ID
        # Crypto needs the -USD suffix or yf resolves to an unrelated instrument
        # (e.g. bare "BTC" ≠ BTC-USD) → garbage RSI in the take-profit nudge.
        yf_t = f"{ticker}-USD" if ticker.upper() in _SYMBOL_TO_CG_ID else ticker
        hist = yf.Ticker(yf_t).history(period=f"{period + 6}d", interval="1d", auto_adjust=True)
        if len(hist) < period + 1:
            return None
        closes = hist["Close"].tolist()
        deltas  = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains   = [max(0.0, d) for d in deltas]
        losses  = [max(0.0, -d) for d in deltas]
        avg_g   = sum(gains[-period:]) / period
        avg_l   = sum(losses[-period:]) / period
        if avg_l == 0:
            rsi = 100.0
        else:
            rs  = avg_g / avg_l
            rsi = round(100.0 - 100.0 / (1.0 + rs), 1)
        _rsi_cache[ticker] = (rsi, _time.time())
        return rsi
    except Exception:
        return None


# ── Take-profit nudge ────────────────────────────────────────────────────────

def _check_take_profit_nudge(uid: str, current_prices: dict) -> None:
    """
    Fire when a position is >= 85% of the way from entry to target.
    Suggests considering a partial exit or tightening the stop.
    Once per position per day via cache dedup.
    """
    log = load_user_trade_log(uid)
    for trade in log.get("open", []):
        ticker = trade.get("ticker")
        entry  = trade.get("entry_price")
        target = trade.get("target_price")
        stop   = trade.get("stop_loss")
        if not (ticker and entry and target):
            continue
        current = current_prices.get(ticker)
        # 🔴 `if not current` lets NaN through (nan is truthy) and lets holiday
        # garbage / a 10x spike through. Both reached users: NaN RAISED at
        # int(progress * 100), killing the nudge for every LATER position of
        # this user with the dedup key already marked, and a spike fired
        # "Up 900.0% ... unrealised gain ~$9,000" — a take-profit
        # recommendation off a phantom gain. Validate against the entry.
        from market_data import plausible_price
        if not plausible_price(current, entry):
            continue
        entry_f, target_f, curr_f = float(entry), float(target), float(current)
        if target_f <= entry_f:
            continue
        progress = (curr_f - entry_f) / (target_f - entry_f)
        if progress < 0.85:
            continue

        key = f"tp_nudge_{uid}_{ticker}_{et_today()}"
        if _is_alerted(key):
            continue
        _mark_alerted(key)

        pct_gain    = (curr_f - entry_f) / entry_f * 100
        pct_to_tgt  = (target_f - curr_f) / target_f * 100
        progress_pct = int(progress * 100)

        # Dollar gain
        gain_str = ""
        try:
            shares = trade.get("shares") or trade.get("quantity")
            alloc  = trade.get("allocation") or trade.get("position_size")
            if shares:
                gain_usd = (curr_f - entry_f) * float(shares)
            elif alloc:
                est_shares = float(alloc) / entry_f
                gain_usd = (curr_f - entry_f) * est_shares
            else:
                gain_usd = None
            if gain_usd is not None:
                gain_str = f"  Unrealised gain: <b>~${gain_usd:,.0f}</b>."
        except Exception:
            pass

        # RSI context — fetch once per ticker (cached 30 min, shared across users)
        rsi = _get_rsi(ticker)
        if rsi is not None:
            if rsi >= 70:
                rsi_str = f"RSI <b>{rsi:.0f}</b> — overbought. Strong case for taking partial profits now."
            elif rsi >= 60:
                rsi_str = f"RSI <b>{rsi:.0f}</b> — elevated. Consider trimming or tightening stop."
            else:
                rsi_str = f"RSI <b>{rsi:.0f}</b> — not extended. Momentum still intact."
        else:
            rsi_str = None

        rsi_line = f"\n{rsi_str}" if rsi_str else ""

        send_inline_keyboard(
            f"🎯 <b>{ticker} near target</b> — {progress_pct}% of the way there\n"
            f"Up <b>{pct_gain:.1f}%</b> · target <code>${_p(target_f)}</code> "
            f"({pct_to_tgt:.1f}% away).{gain_str}{rsi_line}\n\n"
            f"<i>Consider taking partial profits or tightening your stop to lock in gains.</i>\n\n"
            f"<code>/sold {ticker}</code> to close  ·  <code>/updatestop {ticker}</code> to tighten",
            [[_miniapp_btn("📊 View Portfolio", "portfolio", "POSITIONS")]],
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
    def _week_ahead_msg(uid):
            user_cfg = {**config, **get_user_config(uid)}
            if user_cfg.get("paused"):
                print(f"[agent] Skipping week-ahead for {uid} — picks paused.")
                return None

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
                return None
            return final_msg

    _fanout(_week_ahead_msg, recipients=recipients, tag="week_ahead")

    print("[agent] Sunday Week Ahead complete.")


# ── Pre-market pulse (8:45 AM ET Mon–Fri, before the open) ──────────────────

def run_premarket(config: dict):
    """
    8:45 AM run — fires 45 min before market open.
    For each user with open positions: fetch pre-market prices via yfinance (stocks)
    or live prices via get_live_price (crypto, and as stock fallback).

    Two signals:
      - Big pre-market move (>= 3% either direction) → urgent alert
      - Quiet open (<3% on all positions) → one compact digest message

    Skipped entirely if a user has no open positions.
    """
    _log_cron_run("premarket")
    import yfinance as yf
    from market_data import get_live_price, _CRYPTO_SYMBOLS, plausible_price
    print("[agent] Running pre-market pulse...")

    # uid -> {"movers": [str, …], "summary": str}
    _pre: dict = {}

    for uid in _all_recipients():
        try:
            user_cfg = {**config, **get_user_config(uid)}
            if user_cfg.get("paused") or user_cfg.get("skip_premarket"):
                continue

            log         = load_user_trade_log(uid)
            open_trades = log.get("open", [])
            if not open_trades:
                continue

            # Deduplicate tickers (all asset types)
            seen: set = set()
            unique = []
            for t in open_trades:
                if t["ticker"] not in seen:
                    seen.add(t["ticker"])
                    unique.append(t)

            # Fetch pre-market price for each ticker
            position_lines = []
            big_movers     = []
            sector_by_ticker: dict[str, str] = {}   # for concentration check

            for trade in unique:
                ticker    = trade["ticker"]
                entry     = trade.get("entry_price")
                stop      = trade.get("stop_loss")
                is_crypto = (
                    trade.get("asset_type") == "crypto"
                    or ticker.upper() in _CRYPTO_SYMBOLS
                )

                # ── Crypto: 24/7 market — use live price ──────────────────────
                if is_crypto:
                    try:
                        price = get_live_price(ticker)
                        # gate on plausibility vs entry/stop so a garbage feed value
                        # ($0.00) never shows a false −100% row / STOP HIT badge here
                        if plausible_price(price, entry or stop):
                            price = round(float(price), 6)
                            vs_entry_str = ""
                            if entry:
                                ve = (price - float(entry)) / float(entry) * 100
                                vs_entry_str = f"  ·  {'+' if ve >= 0 else ''}{ve:.1f}% vs entry"
                            stop_badge = _stop_badge(price, stop) if stop else ""
                            position_lines.append(
                                f"  ⚪ <b>{ticker}</b>  <code>${_p(price)}</code>"
                                f"  <i>24/7 · live{vs_entry_str}</i>{stop_badge}"
                            )
                        else:
                            position_lines.append(f"  <b>{ticker}</b>  <i>live price unavailable</i>")
                    except Exception as exc:
                        print(f"[agent] Pre-market crypto fetch failed for {ticker}: {exc}")
                        position_lines.append(f"  <b>{ticker}</b>  <i>live price unavailable</i>")
                    continue

                # ── Stock: fast_info + prepost history (no .info — 401s on cloud IPs) ──
                try:
                    tkr_obj    = yf.Ticker(ticker)
                    fi         = tkr_obj.fast_info          # never 401s
                    prev_close = getattr(fi, "previous_close", None)
                    pre_price  = None

                    # Primary: prepost history for actual pre-market price
                    try:
                        hist = tkr_obj.history(period="1d", interval="1m", prepost=True)
                        if not hist.empty:
                            pre_price = float(hist["Close"].iloc[-1])
                            if not prev_close and len(hist) > 1:
                                # last regular-hours close = last row before 16:00
                                rh = hist.between_time("09:30", "16:00")
                                if not rh.empty:
                                    prev_close = float(rh["Close"].iloc[-1])
                    except Exception:
                        pass

                    # Sector: leave as Unknown (fast_info has no sector field)
                    sector_by_ticker[ticker] = "Unknown"

                    # Last-resort fallback: Alpaca/yfinance live price
                    price_label = "pre-market"
                    if not pre_price:
                        pre_price = get_live_price(ticker)
                        price_label = "last price"

                    if not pre_price:
                        position_lines.append(f"  <b>{ticker}</b>  <i>pre-market data unavailable</i>")
                        continue

                    pre_price  = round(float(pre_price), 2)
                    prev_close = round(float(prev_close), 2) if prev_close else None

                    # Reject a garbage feed value (holiday $0.01) vs prev close / entry
                    # so the card never shows a false −100% / STOP HIT for a stock.
                    if not plausible_price(pre_price, prev_close or entry):
                        position_lines.append(f"  <b>{ticker}</b>  <i>pre-market data unavailable</i>")
                        continue

                    # % vs previous close (only meaningful when we have real pre-market data)
                    vs_prev = 0
                    if prev_close and price_label == "pre-market":
                        vs_prev = (pre_price - prev_close) / prev_close * 100
                        vs_prev_str = f"{'+' if vs_prev >= 0 else ''}{vs_prev:.1f}% vs close"
                    elif price_label != "pre-market":
                        vs_prev_str = price_label   # "last price"
                    else:
                        vs_prev_str = ""

                    # % vs entry
                    vs_entry_str = ""
                    if entry:
                        vs_entry = (pre_price - float(entry)) / float(entry) * 100
                        vs_entry_str = f"  ·  {'+' if vs_entry >= 0 else ''}{vs_entry:.1f}% vs entry"

                    # Stop proximity (STOP HIT if already at/below the stop)
                    stop_badge = _stop_badge(pre_price, stop) if stop else ""

                    move_icon = "🔴" if vs_prev <= -3 else ("🟢" if vs_prev >= 3 else "⚪")
                    line = (
                        f"  {move_icon} <b>{ticker}</b>  <code>${_p(pre_price)}</code>  "
                        f"<i>{vs_prev_str}{vs_entry_str}</i>{stop_badge}"
                    )
                    position_lines.append(line)

                    if price_label == "pre-market" and abs(vs_prev) >= 3:
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

            # ── Big mover alerts (delivered first, individually) ──────────────
            _mover_msgs: list = []
            for ticker, price, pct, stop in big_movers:
                direction = "gapping UP" if pct > 0 else "gapping DOWN"
                action    = ""
                stop_blown = stop and price < float(stop)
                if stop_blown:
                    action = (
                        f"\n🚨 <b>Pre-market price is BELOW your stop</b> (<code>${_p(stop)}</code>).\n"
                        f"Consider a limit sell before open to control your exit price."
                    )
                elif pct <= -3 and stop and price <= float(stop) * 1.05:
                    action = "\n⚠️ <b>Approaching stop-loss</b> — consider your risk before open."
                elif pct >= 5:
                    action = "\n💡 Strong open expected — consider taking partial profits near target."
                _mover_msgs.append(
                    f"{'🟢' if pct > 0 else '🔴'} <b>{ticker}</b> {direction} <b>{'+' if pct > 0 else ''}{pct:.1f}%</b> "
                    f"pre-market → <code>${_p(price)}</code>{action}"
                )

            body = "\n".join(position_lines)
            n    = len(unique)
            _pre[uid] = {
                "movers": _mover_msgs,
                "summary": (
                    f"🌅 <b>Pre-market — your {n} position{'s' if n != 1 else ''}</b>\n\n"
                    f"{body}"
                    f"{concentration_line}"
                    f"{earnings_section}\n\n"
                    f"<i>Market opens in ~45 min. 🟢 = +3%+  🔴 = -3%+  ⚪ = quiet</i>"
                ),
            }

        except Exception as exc:
            print(f"[agent] Pre-market pulse failed for {uid} (non-critical): {exc}")

    # 🔴 TWO ORDERED PASSES: the per-ticker gap alerts must land before the
    # summary card that contextualises them. The expensive per-ticker yfinance
    # work above runs ONCE — both passes read the prepared dict, never rebuild.
    _uids = list(_pre)
    _fanout(lambda uid: _pre.get(uid, {}).get("movers") or None,
            recipients=_uids, tag="premarket_movers")
    _fanout(lambda uid: _pre.get(uid, {}).get("summary"),
            recipients=_uids, tag="premarket_summary")

    print("[agent] Pre-market pulse complete.")


# ── Intraday price-alert-only run (every 30 min during market hours) ─────────

def _watch_move_quote(ticker: str) -> tuple[float | None, float | None]:
    """
    (live_price, previous_close) for a watchlist "moving X% today" check.

    Uses the live price (prepost 1-minute bar → get_live_price fallback) plus
    fast_info.previous_close — the SAME method as the pre-market positions card —
    instead of lagging daily bars. The old `yf.download(period="2d", interval="1d")`
    read whole-day close bars: pre-market / at the open the "today" bar isn't
    formed, so it showed a stale close as the price and could report yesterday's
    move as today's (e.g. the same "was $X" two days running). Crypto routes
    through the -USD yfinance symbol.
    """
    import yfinance as yf   # agent.py imports yfinance only inside functions
    from market_data import get_live_price
    yf_t = next(iter(_yf_symbol_map([ticker])))   # BTC → BTC-USD; equities unchanged
    live = None
    prev = None
    try:
        tkr = yf.Ticker(yf_t)
        try:
            prev = getattr(tkr.fast_info, "previous_close", None)
        except Exception:
            prev = None
        try:
            hist = tkr.history(period="1d", interval="1m", prepost=True)
            if not hist.empty:
                live = float(hist["Close"].iloc[-1])
        except Exception:
            live = None
    except Exception:
        pass
    if not (live and live > 0):
        live = get_live_price(ticker)   # handles crypto + Alpaca/yfinance fallback
    return (float(live) if live and live > 0 else None,
            float(prev) if prev and prev > 0 else None)


def _move_band(pct_chg: float) -> str:
    """
    Escalation band for a watchlist "% today" move, in ±3% steps.
    Same band → same day → already alerted (no spam). Crossing into the NEXT band
    re-alerts, so a ticker that keeps running past its first small alert isn't
    silenced for the rest of the day. Direction-aware ("+"/"-") so a reversal to
    the other side alerts separately from the original move.
      +3.2% → "+1"   +6.0% → "+2"   +8.4% → "+2"   -3.2% → "-1"   -9.0% → "-3"
    """
    return f"{'+' if pct_chg > 0 else '-'}{int(abs(pct_chg) // 3)}"


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
    def _target_approach_msgs(uid):
            out = []
            log = load_user_trade_log(uid)
            open_trades = log.get("open", [])
            if not open_trades:
                return None

            tickers = list({t["ticker"] for t in open_trades})
            current_prices = _download_prices(tickers, period="1d", interval="1m", auto_adjust=True)

            # Auto-close disabled — price_alert_manager sends the notification;
            # user closes the position manually via the portfolio tab.

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
                    alert_key = f"target_approach_{uid}_{ticker}_{et_today().isoformat()}"
                    from cache_layer import cache_get, cache_set
                    if not cache_get(alert_key):
                        cache_set(alert_key, "1", ttl_seconds=86400)
                        out.append({
                            "text": (
                                f"⚡ <b>{ticker}</b> is <b>{gap_pct:.1f}%</b> from your target <code>${float(target):.2f}</code>\n"
                                f"Current: <code>${float(current):.2f}</code>  ·  Consider taking partial profit."
                            ),
                            "keyboard": [[
                                _miniapp_btn(f"✅ Close {ticker}", "portfolio", f"sold_pick|{ticker}"),
                                {"text": "Hold 💎", "callback_data": "noop"},
                            ]],
                        })
            return out

    _fanout(_target_approach_msgs, tag="target_approach")

    # ── Watchlist big-move alerts (>3% intraday) ─────────────────────────────
    try:
        from cache_layer import cache_get, cache_set
        from datetime import date as _date
        def _watch_move_msgs(uid):
                out = []
                log = load_user_trade_log(uid)
                watch_tickers = log.get("watchlist", [])
                if not watch_tickers:
                    return None
                # Live price vs true previous close (same method as the positions
                # card) — not lagging daily bars, which showed stale pre-market
                # closes and could report yesterday's move as today's.
                for t in watch_tickers:
                    try:
                        curr, prev = _watch_move_quote(t)
                        # plausibility vs previous close: a garbage live value ($0.01)
                        # would otherwise report a false "moving −100% today".
                        from market_data import plausible_price
                        if not (curr and prev) or not plausible_price(curr, prev):
                            continue
                        pct_chg = (curr - prev) / prev * 100
                        if abs(pct_chg) < 3:
                            continue
                        # Re-alert when the move escalates into the next ±3% band,
                        # instead of once-flat-per-day (which silenced a ticker that
                        # kept running well past its first small alert).
                        key = f"watch_move_{uid}_{t}_{et_today().isoformat()}_{_move_band(pct_chg)}"
                        if cache_get(key):
                            continue
                        cache_set(key, "1", ttl_seconds=86400)
                        emoji = "🚀" if pct_chg > 0 else "🔻"
                        sign  = "+" if pct_chg > 0 else ""
                        out.append({
                            "text": (
                                f"{emoji} <b>{t}</b> is moving <b>{sign}{pct_chg:.1f}%</b> today\n"
                                f"Price: <code>${_p(curr)}</code>  (was <code>${_p(prev)}</code>)"
                            ),
                            "keyboard": [[
                                _miniapp_url_btn(f"📊 {t} Chart", f"?chart={t}", f"cmd|CHART {t}"),
                                _miniapp_btn("🔔 Set Alert", "watchlist", f"alert|{t}"),
                            ]],
                        })
                    except Exception:
                        pass
                return out

        _fanout(_watch_move_msgs, tag="watch_move")
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

def run_watchdog():
    """
    Watchdog — runs at 9:00 AM ET on weekdays.
    Checks whether the morning run was logged today. If it wasn't, and today is
    a trading day, sends an admin-only alert so the owner knows picks were missed.
    """
    now_et = datetime.now(ET)
    today  = now_et.date()

    # No alert needed on non-trading days
    if today.weekday() >= 5 or is_market_holiday(today):
        print(f"[watchdog] Non-trading day ({today}) — nothing to check.")
        return

    try:
        last_run_str = get_config().get("cron_last_morning", "")
        if last_run_str:
            last_run_utc = datetime.fromisoformat(last_run_str).replace(tzinfo=pytz.utc)
            last_run_et  = last_run_utc.astimezone(ET).date()
            if last_run_et >= today:
                print(f"[watchdog] Morning run already logged today ({last_run_et}). All good.")
                return
        else:
            last_run_et = None

        # Morning run was not logged for today → alert admin
        since = f"Last logged run: {last_run_et}" if last_run_et else "No run ever logged"
        print(f"[watchdog] ⚠️ Morning run NOT detected for {today}. {since}.")
        _alert(
            f"⚠️ <b>Missed morning run</b> — picks may not have been sent today ({today.strftime('%b %-d')}).\n"
            f"{since}\n\n"
            f"<i>Trigger manually: cron-job.org → StockPulz-morning → Run now\n"
            f"Or: https://stock-agent-enqx.onrender.com/trigger/morning?secret=CRON_SECRET&force=true</i>",
            admin_only=True,
            tab="picks",
        )
    except Exception as exc:
        print(f"[watchdog] Error during watchdog check: {exc}")


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
    elif mode == "digest":
        run_digest()
    elif mode == "morning":
        # Duplicate-run guard — cron-job.org is the primary trigger; GitHub Actions
        # is the backup. If cron-job.org already ran this morning, skip to avoid
        # sending picks twice.
        _last_morning = config.get("cron_last_morning", "")
        _today_et     = now_et.date().isoformat()
        _force        = os.environ.get("FORCE_MORNING", "").lower() in ("1", "true")
        if _last_morning and _last_morning[:10] >= _today_et and not MOCK_DATA and not _force:
            print(f"[agent] Morning already ran today ({_last_morning[:16]}) — skipping duplicate (GitHub Actions backup).")
        else:
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
    elif mode == "friday_wrap":
        run_friday_wrap()
    elif mode == "tax_harvest":
        run_tax_loss_harvest_check()
    elif mode == "macro_alert":
        run_macro_alert_check()
    elif mode == "midday_check":
        run_midday_check()
    elif mode == "vix_check":
        run_vix_check()
    elif mode == "news_check":
        run_news_check()
    elif mode == "pre_earnings":
        run_pre_earnings_guidance()
    elif mode == "monthly_commentary":
        run_monthly_commentary()
        run_monthly_pnl_digest()
    elif mode == "recap":
        run_recap()
    elif mode == "watchdog":
        run_watchdog()
    else:
        run_confirmation()

    print(f"[agent] Done ({mode}) for {now_et.strftime('%Y-%m-%d')}.")


def run_digest():
    """
    9:30 AM ET weekday — personalized position digest at market open.
    Sends each user a brief check-in on their open positions: live prices,
    vs-entry move, stop/target proximity, and any earnings this week.
    Skipped for users with no open positions or skip_digest flag set.
    """
    _log_cron_run("digest")
    print("[agent] run_digest: starting")
    try:
        from config_manager import get_allowed_users, load_user_trade_log, get_user_config
        from telegram_api import send_message
        from bot_commands import _parse_and_execute

        users = get_allowed_users()
        if not users:
            print("[agent] run_digest: no allowed users")
            return

        sent = 0
        for chat_id in users:
            try:
                cfg = get_user_config(chat_id)
                if cfg.get("paused") or cfg.get("skip_digest"):
                    continue
                log = load_user_trade_log(chat_id)
                if not log.get("open"):
                    continue
                reply = _parse_and_execute("DIGEST", original="/digest", chat_id=chat_id)
                # Background job: never surface failures to users. The command
                # layer's catch-all returns a "Something went wrong" reply —
                # swallow it here (admin sees the log), users see nothing.
                if reply and "Something went wrong" in reply:
                    print(f"[agent] run_digest: handler errored for {chat_id} — suppressed.")
                    continue
                if reply:
                    send_message(reply, chat_id=chat_id)
                    sent += 1
            except Exception as exc:
                print(f"[agent] run_digest: failed for {chat_id}: {exc}")

        print(f"[agent] run_digest: sent to {sent} user(s).")
    except Exception as e:
        print(f"[agent] run_digest error: {e}")


def run_recap():
    """
    4:30 PM ET weekday — send each user a /recap-style position check-in via Telegram.
    Broadcasts to all allowed users who have open positions.
    """
    _log_cron_run("recap")
    print("[agent] run_recap: starting")
    try:
        from config_manager import get_allowed_users, load_user_trade_log
        from telegram_api import send_message
        from bot_commands import _parse_and_execute

        users = get_allowed_users()
        if not users:
            print("[agent] run_recap: no allowed users")
            return

        sent = 0
        for chat_id in users:
            try:
                log = load_user_trade_log(chat_id)
                if not log.get("open"):
                    continue
                reply = _parse_and_execute("RECAP", original="/recap", chat_id=chat_id)
                if reply:
                    send_message(reply, chat_id=chat_id)
                    sent += 1
            except Exception as exc:
                print(f"[agent] run_recap: failed for {chat_id}: {exc}")

        print(f"[agent] run_recap: sent to {sent} user(s).")
    except Exception as e:
        print(f"[agent] run_recap error: {e}")


def compute_pick_streaks(weekly_picks: dict) -> dict:
    """
    Given weekly_picks = {date_str: picks_dict, ...}, return a dict of
    {ticker/symbol: consecutive_day_count} for tickers that appear in today's
    picks AND appeared on each immediately prior trading day in the weekly data.

    Only tickers with a streak ≥ 2 are included.
    Called once per morning run; result passed to format_daily_message().
    """
    from datetime import date, timedelta

    today     = et_today()
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
    """Return all allowed chat_ids (always includes owner).
    If OWNER_ONLY=1 env var is set, returns only the owner (for manual test triggers)."""
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    if os.environ.get("OWNER_ONLY") == "1":
        return [owner] if owner else []
    try:
        from config_manager import get_allowed_users
        return get_allowed_users()
    except Exception:
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


# How far below entry a long-term pick's thesis is treated as questionable.
# Deliberately WIDE — this must never read as a stop-loss. A long-term thesis
# should survive ordinary drawdowns; 15% says "something may have changed",
# which is the signal an LT holder actually wants and currently gets none of.
LT_INVALIDATION_PCT = 15.0

# How many days back to keep retrying a long-term entry-zone alert that could
# not arm on its publication day. One trading week — past that the entry level
# is stale enough that "it pulled back to entry" is no longer the same trade.
LT_REARM_DAYS = 5


def _lt_rearm_picks(today_picks: dict) -> list[dict]:
    """Long-term picks from earlier this week whose entry alert never armed.

    🔴 Why this exists. The entry-zone alert requires price >= 1% above entry so
    it cannot insta-fire — but it is evaluated ONCE, during the morning run that
    publishes the pick, at the exact moment price is by definition within 2-3% of
    entry. So for a long-term pick it almost never arms, and it is never
    re-checked. Combined with LT picks having no stop, that left MRK and TLT with
    zero alerts for real users on 2026-08-14.

    Re-offering them each morning costs nothing: add_alert collapses duplicates
    of the same ticker+direction+kind, so an already-armed alert is a no-op, and
    a pick that has since risen above entry finally gets its alert.

    Silent on failure — this is an enhancement to alerting, and it must never be
    able to break the morning run.
    """
    try:
        from config_manager import load_weekly_picks
        today = et_today().isoformat()
        seen: set = set()
        for sec in ("stocks", "etfs", "commodities"):
            for p in (today_picks.get(sec, {}) or {}).get("long_term", []) or []:
                seen.add((p.get("ticker") or p.get("symbol") or "").upper())

        out: list[dict] = []
        for day, past in sorted((load_weekly_picks() or {}).items(), reverse=True):
            if day == today:
                continue
            try:
                age = (date.fromisoformat(today) - date.fromisoformat(day)).days
            except ValueError:
                continue
            if not (0 < age <= LT_REARM_DAYS):
                continue
            for sec in ("stocks", "etfs", "commodities"):
                for p in (past.get(sec, {}) or {}).get("long_term", []) or []:
                    t = (p.get("ticker") or p.get("symbol") or "").upper()
                    # Today's pick of the same ticker wins — its entry is fresher.
                    if not t or t in seen or not _is_pos(p.get("entry_price")):
                        continue
                    seen.add(t)
                    out.append(p)
        if out:
            print(f"[agent] _lt_rearm_picks: re-offering entry alerts for "
                  f"{len(out)} long-term pick(s) from earlier this week: "
                  f"{[ (p.get('ticker') or p.get('symbol')) for p in out ]}")
        return out
    except Exception as exc:
        print(f"[agent] _lt_rearm_picks failed (non-critical): {exc}")
        return []


def _auto_set_pick_alerts(picks: dict, recipients: list[str]) -> None:
    """Auto-set stop-loss + entry alerts for morning picks. Silent — never raises.

    SCOPE — deliberately excluded, do not "helpfully" add:
      * crypto long_term — hard-coded [] upstream (ai_analyzer); crypto LT picks
        are not used, so there is never anything to alert on.
      * options_plays — an alert here would fire on the UNDERLYING's price, not
        the contract the user actually holds, whose value is driven by IV and
        time decay as much as by spot. A "stop" on the underlying would be
        actively misleading about an option position.
    Everything else that reaches a user MUST be covered by this function.
    """
    try:
        _lt_picks = (
            picks.get("stocks", {}).get("long_term",  []) +
            picks.get("etfs",   {}).get("long_term",  []) +
            picks.get("commodities", {}).get("long_term",  [])
        )
        all_sections = (
            picks.get("stocks", {}).get("short_term", []) +
            picks.get("crypto", {}).get("short_term", []) +
            picks.get("etfs",   {}).get("short_term", []) +
            picks.get("commodities", {}).get("short_term", []) +
            _lt_picks
        )
        # Short-term picks are processed FIRST so a ticker that is both an ST and
        # an LT pick (KMI on 2026-08-14) gets its real stop, and the wide LT
        # invalidation level is then skipped for it as redundant noise.
        # Re-arm: long-term entry alerts from earlier this week that could not arm
        # on their publication day. See _lt_rearm_picks().
        all_sections = all_sections + _lt_rearm_picks(picks)
        _lt_ids = {id(p) for p in _lt_picks}   # identity, not dict equality
        # Fetch live prices once so entry alerts are only armed when the pick is
        # trading ABOVE its entry. Picks are issued at ~entry (within 2%), so a
        # "below entry" alert set at issue time would insta-fire the same morning
        # on normal noise — no real pullback. Require ≥1% of room so firing means
        # the price genuinely dipped back into the buy zone.
        ENTRY_ALERT_MIN_ABOVE = 1.01
        pick_tickers = list({(p.get("ticker") or p.get("symbol") or "").upper()
                             for p in all_sections if (p.get("ticker") or p.get("symbol"))})
        try:
            live_prices = _download_prices(pick_tickers, period="1d", interval="1m", auto_adjust=True)
        except Exception:
            live_prices = {}

        total_set = 0
        for uid in recipients:
            try:
                log = load_user_trade_log(uid)
                open_tickers = {t.get("ticker", "").upper() for t in log.get("open", [])}
                stopped: set = set()     # tickers that got a real stop this run
                for pick in all_sections:
                    ticker = (pick.get("ticker") or pick.get("symbol") or "").upper()
                    if not ticker or ticker in open_tickers:
                        continue
                    try:
                        # Stop-loss alert — fires if pick drops to stop. Stop sits
                        # well below current, so no insta-fire risk; always armed.
                        stop = pick.get("stop_loss")
                        if stop:
                            add_alert(uid, ticker, float(stop), direction="below",
                                      auto=True, kind="stop")
                            stopped.add(ticker)
                            total_set += 1
                        # Entry zone alert — only when price is ≥1% above entry, so
                        # "below entry" firing means a genuine pullback into the zone.
                        entry = pick.get("entry_price")
                        cur   = live_prices.get(ticker)
                        if entry and cur and cur >= float(entry) * ENTRY_ALERT_MIN_ABOVE:
                            add_alert(uid, ticker, float(entry), direction="below",
                                      auto=True, kind="entry")
                            total_set += 1
                        # 🔴 Long-term invalidation level (Aug 15). LT picks carry NO
                        # stop by design — a multi-year thesis should not be stopped
                        # out on a 5% wobble — and the entry alert above is checked
                        # ONCE, at publication, when price is by definition within
                        # 2-3% of entry, so it almost never arms. Net effect measured
                        # on 2026-08-14: MRK and TLT reached real users with ZERO
                        # alerts of any kind. The only LT pick that had one (KMI) was
                        # covered by coincidence, because it was also a short-term
                        # pick. "No stop" was never meant to mean "no alert".
                        #
                        # This is NOT a stop: it sits far below any sensible one, and
                        # it says "the thesis may be broken", not "sell now".
                        if (id(pick) in _lt_ids and ticker not in stopped
                                and _is_pos(pick.get("entry_price"))):
                            lvl = round(float(pick["entry_price"])
                                        * (1 - LT_INVALIDATION_PCT / 100), 2)
                            add_alert(uid, ticker, lvl, direction="below",
                                      auto=True, kind="invalidation")
                            total_set += 1
                    except Exception as exc:
                        print(f"[agent] _auto_set_pick_alerts: alert set failed for {ticker}/{uid}: {exc}")
            except Exception as exc:
                print(f"[agent] _auto_set_pick_alerts: user {uid} failed (non-critical): {exc}")
        print(f"[agent] _auto_set_pick_alerts: set {total_set} alert(s) across {len(recipients)} user(s).")
    except Exception as exc:
        print(f"[agent] _auto_set_pick_alerts failed (non-critical): {exc}")


def _build_premarket_gap_warnings(picks: dict) -> dict[str, str]:
    """
    For each stock/ETF pick, check if the current pre-market price is > 3% above entry.
    Only runs before 9:30 AM ET — stale data after market opens is not useful.
    Uses yfinance history(prepost=True) for actual pre-market ticks, not fast_info
    which often returns the previous session's close instead of a real pre-market quote.
    """
    import pytz
    import yfinance as yf
    import concurrent.futures as _cf

    # Skip entirely once market opens — pre-market data is stale after 9:30 AM ET
    _et = pytz.timezone("America/New_York")
    _now_et = datetime.now(_et)
    if _now_et.hour > 9 or (_now_et.hour == 9 and _now_et.minute >= 30):
        return {}

    # Tag each pick with the window the MESSAGE promised it, rather than
    # applying one flat threshold. A short-term pick promises 2%; warning only
    # above 3% meant a pick could breach its own published rule in silence —
    # measured live: ANET +2.43% and NWSA +2.15% produced no warning at all.
    from formatters import entry_window_pct
    all_picks: list[dict] = []
    for section, is_lt in (("stocks", False), ("etfs", False)):
        sec = picks.get(section, picks if section == "stocks" else {}) or {}
        for tf, lt in (("short_term", False), ("long_term", True)):
            for p in sec.get(tf, []) or []:
                if p.get("ticker") and p.get("entry_price"):
                    all_picks.append({**p, "_window_pct": entry_window_pct(is_long_term=lt)})

    if not all_picks:
        return {}

    _market_open_et = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)

    def _fetch(pick: dict):
        ticker = pick["ticker"].upper()
        try:
            hist = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
            if hist.empty:
                return ticker, None
            # Filter to confirmed pre-market bars only (before 9:30 AM ET today)
            hist.index = hist.index.tz_convert(_et)
            today = _now_et.date()
            pre = hist[(hist.index.date == today) & (hist.index < _market_open_et)]
            if pre.empty:
                return ticker, None
            price = float(pre["Close"].iloc[-1])
            entry = float(pick["entry_price"])
            if entry <= 0:
                return ticker, None
            gap_pct = (price - entry) / entry * 100
            window  = float(pick.get("_window_pct") or 3.0)
            if gap_pct > window:
                price_fmt = f"${price:,.2f}" if price < 100 else f"${price:,.0f}"
                entry_fmt = f"${entry:,.2f}" if entry < 100 else f"${entry:,.0f}"
                return ticker, (
                    f"<b>{ticker}</b> gapping +{gap_pct:.1f}% pre-market ({price_fmt} vs entry {entry_fmt}) "
                    f"— above its {window:g}% entry window. Wait for pullback or skip."
                )
        except Exception:
            pass
        return ticker, None

    warnings: dict[str, str] = {}
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        for ticker, warn in pool.map(_fetch, all_picks):
            if warn:
                warnings[ticker] = warn
    return warnings


def _send_morning_personalised(picks: dict, global_config: dict, label: str = "",
                               market_closed: bool = False,
                               closed_reason: str = "",
                               next_open_label: str = ""):
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

    # ── Earnings this week — across full screened universe ───────────────────
    morning_earnings: dict = {}
    try:
        from earnings_checker import get_upcoming_earnings
        _all_pick_tickers = []
        for _sec in (picks.get("stocks", picks).get("short_term", []) +
                     picks.get("stocks", picks).get("long_term",  []) +
                     picks.get("etfs",   {}).get("short_term", [])    +
                     picks.get("etfs",   {}).get("long_term",  [])):
            t = (_sec.get("ticker") or _sec.get("symbol", "")).upper()
            if t:
                _all_pick_tickers.append(t)
        if _all_pick_tickers:
            morning_earnings = get_upcoming_earnings(_all_pick_tickers, days_ahead=5)
            print(f"[agent] Morning earnings digest: {len(morning_earnings)} events this week.")
    except Exception as _me:
        print(f"[agent] Morning earnings fetch failed (non-critical): {_me}")

    # ── Watchlist prices — batch-fetch across all users once ─────────────────
    # Collect every unique watchlist ticker across all recipients, fetch price +
    # 24h change once via yfinance, then distribute per-user in the loop below.
    all_wl_tickers: set = set()
    for _uid in recipients:
        try:
            _wl = get_user_config(_uid).get("watchlist") or []
            all_wl_tickers.update(t.upper() for t in _wl if t)
        except Exception:
            pass

    watchlist_prices_global: dict = {}
    if all_wl_tickers:
        try:
            import yfinance as _yf
            import concurrent.futures as _cf

            def _fetch_wl_price(ticker: str):
                try:
                    from price_checker import _SYMBOL_TO_CG_ID as _CG_IDS
                    yf_sym = f"{ticker}-USD" if ticker in _CG_IDS else ticker
                    info  = _yf.Ticker(yf_sym).fast_info
                    price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
                    prev  = getattr(info, "previous_close", None)
                    if price:
                        chg = round((float(price) - float(prev)) / float(prev) * 100, 2) if prev else None
                        return ticker, {"price": round(float(price), 2), "change_pct": chg}
                except Exception:
                    pass
                return ticker, None

            with _cf.ThreadPoolExecutor(max_workers=8) as _pool:
                for _t, _data in _pool.map(_fetch_wl_price, all_wl_tickers):
                    if _data:
                        watchlist_prices_global[_t] = _data
            print(f"[agent] Watchlist prices: {len(watchlist_prices_global)}/{len(all_wl_tickers)} fetched.")
        except Exception as _wl_exc:
            print(f"[agent] Watchlist price fetch failed (non-critical): {_wl_exc}")

    # ── Pre-fetch company names for all pick tickers (shared across all users) ──
    _picks_company_names: dict = {}
    try:
        from market_data import get_company_names as _gcn_agent
        def _picks_tickers_agent(p: dict) -> list[str]:
            tks = []
            stocks = p.get("stocks", p)
            for _p in stocks.get("short_term", []) + stocks.get("long_term", []):
                t = _p.get("ticker", "")
                if t: tks.append(t)
            for _p in p.get("crypto", {}).get("short_term", []) + p.get("crypto", {}).get("long_term", []):
                t = _p.get("symbol", "") or _p.get("ticker", "")
                if t: tks.append(t)
            for _sec in (p.get("etfs", {}), p.get("commodities", {})):
                for _p in _sec.get("short_term", []) + _sec.get("long_term", []):
                    t = _p.get("ticker", "") or _p.get("symbol", "")
                    if t: tks.append(t)
            return list(dict.fromkeys(t.upper() for t in tks if t))
        _picks_company_names = _gcn_agent(_picks_tickers_agent(picks))
        print(f"[agent] Company names fetched for {len(_picks_company_names)} tickers.")
    except Exception as _cn_exc:
        print(f"[agent] Company name fetch failed (non-critical): {_cn_exc}")

    # ── Pre-market gap check — flag picks that have already gapped above entry ──
    premarket_gap_warnings: dict[str, str] = {}
    if not market_closed:
        try:
            premarket_gap_warnings = _build_premarket_gap_warnings(picks)
            if premarket_gap_warnings:
                print(f"[agent] Pre-market gap warnings for: {list(premarket_gap_warnings.keys())}")
        except Exception as _gap_exc:
            print(f"[agent] Pre-market gap check failed (non-critical): {_gap_exc}")

    # ── Build all per-user payloads first (CPU-bound, fast), then broadcast concurrently ──
    outbox: list[dict] = []
    n_paused = 0
    for uid in recipients:
        try:
            user_cfg = {**global_config, **get_user_config(uid)}
            if user_cfg.get("paused"):
                print(f"[agent] Skipping {uid} — picks paused by user.")
                n_paused += 1
                continue

            # Look up pre-computed notes — no Haiku call here
            personal_notes: dict = all_personal_notes.get(uid, {})

            # ── "Already holding" badge set ───────────────────────────────────
            # Pass the set of tickers the user currently holds so formatters.py
            # can badge picks the user already owns (📌 In your portfolio).
            held_tickers = {
                t["ticker"].upper()
                for t in user_positions_cache.get(uid, [])
                if t.get("ticker")
            }

            # Per-user watchlist prices — only keys the user actually watches
            user_wl = set(t.upper() for t in (user_cfg.get("watchlist") or []) if t)
            user_wl_prices = {t: watchlist_prices_global[t]
                              for t in user_wl if t in watchlist_prices_global}

            # Per-user active price alerts map: {TICKER: [{target, direction}]}
            user_alerts_map: dict = {}
            if user_wl:
                try:
                    from price_alert_manager import get_user_alerts_map
                    user_alerts_map = get_user_alerts_map(uid)
                except Exception:
                    pass

            message = format_daily_message(picks, user_cfg, personal_notes=personal_notes,
                                           pick_streaks=pick_streaks, buy_counts=buy_counts,
                                           recent_stats=recent_stats,
                                           earnings_this_week=morning_earnings,
                                           held_tickers=held_tickers,
                                           watchlist_prices=user_wl_prices or None,
                                           user_alerts_map=user_alerts_map or None,
                                           market_closed=market_closed,
                                           closed_reason=closed_reason,
                                           next_open_label=next_open_label,
                                           company_names=_picks_company_names or None)

            # Prepend pre-market gap warnings if any picks are gapping above entry
            if premarket_gap_warnings:
                gap_lines = "\n".join(premarket_gap_warnings.values())
                message = (
                    f"⚡ <b>Pre-market gap alert</b>\n{gap_lines}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n\n" + message
                )

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
                # Track how many alerts this user has received (for discovery tips)
                try:
                    update_user_config(uid, "alerts_sent_count",
                                       user_cfg.get("alerts_sent_count", 0) + 1)
                except Exception:
                    pass
        except Exception as exc:
            import traceback
            err_detail = traceback.format_exc()
            print(f"[agent] WARNING: Could not prepare morning message for {uid}: {exc}\n{err_detail}")
            # Surface the error to admin Telegram so it's visible without checking GH Actions logs
            try:
                owner = os.environ.get("TELEGRAM_CHAT_ID", "")
                if owner and not DRY_RUN:
                    send_message(
                        f"⚠️ <b>Morning run: failed to prepare message for user {uid}</b>\n"
                        f"<code>{type(exc).__name__}: {exc}</code>",
                        chat_id=owner,
                    )
            except Exception:
                pass

    # Fire all messages concurrently — 1000 users in ~3-5 s instead of ~500 s
    if outbox:
        broadcast_all(outbox)
    else:
        # Warn admin if we had recipients but nobody ended up in the outbox
        if recipients and not DRY_RUN:
            try:
                owner = os.environ.get("TELEGRAM_CHAT_ID", "")
                if owner:
                    n_errors = len(recipients) - n_paused
                    reason_parts = []
                    if n_paused:
                        reason_parts.append(f"{n_paused} paused picks (send /resume to fix)")
                    if n_errors:
                        reason_parts.append(f"{n_errors} skipped due to errors (see Actions log)")
                    reason_str = "  ·  ".join(reason_parts) or "unknown reason"
                    send_message(
                        f"⚠️ <b>Morning run: picks generated but 0 users received them</b>\n"
                        f"<i>{len(recipients)} recipient(s) in list — {reason_str}.</i>",
                        chat_id=owner,
                    )
            except Exception:
                pass
    return len(outbox)


def _alert(text: str, admin_only: bool = False, tab: str = "picks"):
    """
    Send an operational alert with an Open Dashboard button.
    admin_only=True  → owner only (errors, warnings, system messages)
    admin_only=False → all users (trade closes, earnings warnings, market info)
    tab              → mini-app tab the button opens (default: picks)
    """
    print(f"[agent] ALERT: {text}")
    if DRY_RUN:
        return

    _app_url = (os.environ.get("APP_URL") or "").rstrip("/")
    keyboard = [[_miniapp_btn("📊 Open Dashboard  ↗", tab, "TODAY")]] if _app_url else None

    if admin_only:
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        if owner:
            if keyboard:
                send_inline_keyboard(text, keyboard, chat_id=owner)
            else:
                send_message(text, chat_id=owner)
    else:
        payloads = [{"chat_id": uid, "text": text} for uid in _all_recipients()]
        if keyboard:
            for p in payloads:
                p["keyboard"] = keyboard
        broadcast_all(payloads)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print(f"⚠ Unhandled error: {exc}\n{traceback.format_exc()}")
        if not DRY_RUN:
            send_message(f"⚠ Agent crashed: {exc}. Check GitHub Actions logs.")
        sys.exit(1)
