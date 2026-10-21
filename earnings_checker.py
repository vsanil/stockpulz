"""
earnings_checker.py — Fetch upcoming earnings dates for S&P 500 candidates.

One Finnhub API call covers all tickers for the next N days.
Returns a dict so the screener and analyzer can flag high-risk picks.
"""

import os
import requests
from datetime import date, timedelta
from datefmt import strftime_portable

FINNHUB_BASE = "https://finnhub.io/api/v1"


def get_upcoming_earnings(tickers: list[str], days_ahead: int = 5) -> dict[str, str]:
    """
    Returns {ticker: "Thu May 1"} for stocks in `tickers` that report earnings
    within the next `days_ahead` calendar days.

    Uses Finnhub calendar/earnings (one API call — free tier supports it).
    Returns {} gracefully if the API key is missing or the call fails.
    """
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        print("[earnings_checker] No FINNHUB_API_KEY — skipping earnings check.")
        return {}

    today    = date.today()
    end_date = today + timedelta(days=days_ahead)

    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={
                "from":  today.isoformat(),
                "to":    end_date.isoformat(),
                "token": api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("earningsCalendar", [])
    except Exception as exc:
        print(f"[earnings_checker] Finnhub call failed ({exc}). Skipping earnings check.")
        return {}

    ticker_set = set(tickers)
    result: dict[str, str] = {}

    for event in events:
        sym = event.get("symbol", "")
        dt  = event.get("date", "")
        if sym not in ticker_set or not dt:
            continue
        try:
            d = date.fromisoformat(dt)
            result[sym] = strftime_portable(d, "%a %b %-d")   # e.g. "Thu May 1"
        except ValueError:
            pass

    print(f"[earnings_checker] {len(result)} ticker(s) report earnings in next {days_ahead} days: "
          f"{list(result.keys())[:10]}{'...' if len(result) > 10 else ''}")
    return result
