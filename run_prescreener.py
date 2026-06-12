"""
run_prescreener.py — Lightweight midnight pre-screener (MANUAL/LOCAL USE).

Scores 600 tickers and saves top candidates to Gist for the 7 AM morning run.
Thin imports only (screener + config_manager + crypto_screener) — but even
this proved too heavy for Render's 512MB instance (OOMed Jun 11 2026).

Production prescreening runs on GitHub Actions (daily_run.yml, 03:00 +
07:00 UTC schedules, 7GB runners). webhook.py /trigger/prescreener relays
there via workflow dispatch. Keep this script for generating the cache
from a laptop when GitHub is unavailable: python3 run_prescreener.py
"""
from __future__ import annotations

import calendar
import os
import time
from datetime import datetime, date, timedelta, timezone

import pytz

from config_manager import get_config, update_config, save_screener_cache
from screener import run_screener
from crypto_screener import run_crypto_screener

ET = pytz.timezone("America/New_York")

CRYPTO_RETRY_DELAYS = [15, 30, 60, 120]   # seconds between retries


# ── Holiday helpers (pure stdlib — copied from agent.py, no extra imports) ───

def _holiday_observed(fixed: date) -> date:
    if fixed.weekday() == 5:
        return fixed - timedelta(days=1)
    if fixed.weekday() == 6:
        return fixed + timedelta(days=1)
    return fixed


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    first  = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
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


def is_market_holiday(d: date) -> bool:
    return d in _us_market_holidays(d.year)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_cron_run(mode: str) -> None:
    try:
        update_config(f"cron_last_{mode}", datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        print(f"[prescreener] cron log failed for {mode}: {exc}")


def _run_crypto_with_retry() -> dict:
    """Retry crypto screener up to 5 times. No Telegram alerts (thin script)."""
    empty = {"short_term": [], "long_term": []}
    for attempt, delay in enumerate([0] + CRYPTO_RETRY_DELAYS, start=1):
        if delay:
            print(f"[prescreener] Crypto retry {attempt}/5 — waiting {delay}s...")
            time.sleep(delay)
        try:
            result = run_crypto_screener()
            if result.get("short_term") or result.get("long_term"):
                return result
            print(f"[prescreener] Crypto screener: no qualifying candidates (attempt {attempt}).")
            return empty
        except Exception as exc:
            print(f"[prescreener] Crypto screener attempt {attempt}/5 failed: {exc}")
    return empty


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _log_cron_run("prescreener")
    print("[prescreener] Running midnight pre-screener...")

    if is_market_holiday(datetime.now(ET).date()):
        print("[prescreener] Market holiday tomorrow — skipping.")
        return

    config = {}
    try:
        config = get_config()
    except Exception as exc:
        print(f"[prescreener] Could not load config ({exc}) — using defaults.")

    # ── Stock screener ────────────────────────────────────────────────────────
    stock_results = {"short_term": [], "long_term": []}
    try:
        stock_results = run_screener(
            watchlist=config.get("watchlist", []),
            excluded_sectors=config.get("excluded_sectors", []),
        )
        print(f"[prescreener] {len(stock_results['short_term'])} ST, "
              f"{len(stock_results['long_term'])} LT candidates.")
    except Exception as exc:
        print(f"[prescreener] Stock screener failed: {exc}")

    # ── Crypto screener ───────────────────────────────────────────────────────
    crypto_results = {"short_term": [], "long_term": []}
    if config.get("crypto_enabled", True):
        try:
            crypto_results = _run_crypto_with_retry()
            print(f"[prescreener] {len(crypto_results['short_term'])} crypto ST, "
                  f"{len(crypto_results['long_term'])} crypto LT candidates.")
        except Exception as exc:
            print(f"[prescreener] Crypto screener failed: {exc}")

    # ── Save cache (only if stock results are usable) ─────────────────────────
    has_stocks = bool(stock_results.get("short_term") or stock_results.get("long_term"))
    if has_stocks:
        try:
            save_screener_cache(stock_results, crypto_results)
            print("[prescreener] Cache saved to Gist. Morning run will load from cache.")
        except Exception as exc:
            print(f"[prescreener] Cache save failed: {exc}")
    else:
        print("[prescreener] Stock results empty — cache NOT saved. "
              "Morning run will fall back to live screener.")


if __name__ == "__main__":
    main()
