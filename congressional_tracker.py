"""
congressional_tracker.py — Track congressional stock purchases via House Stock Watcher.

Congressional stock disclosures (STOCK Act) are public law. Members of Congress
must report trades within 45 days. House Stock Watcher aggregates these filings
into a free public API — no key required.

Why this matters: congressional members have historically outperformed the market
significantly, with studies showing ~6-12% annualised alpha. Cluster buys
(multiple members buying the same stock) are the strongest signal.

Disclosure lag: 30-45 days by law. Use as a medium-term (LT) signal only.
Cached 24h — filings are batch-released, not real-time.
"""

import requests
from datetime import date, datetime, timedelta

from config_manager import load_signal_cache, save_signal_cache

HOUSE_API     = "https://housestockwatcher.com/api"
CACHE_KEY     = "__congressional__"
CACHE_TTL_HRS = 24

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Look back this many days for purchases (covers the disclosure window)
LOOKBACK_DAYS = 60


def _is_cache_fresh(cache: dict) -> bool:
    """Return True if the congressional cache was written within TTL hours."""
    entry = cache.get(CACHE_KEY, {})
    ts = entry.get("cached_at", "")
    if not ts:
        return False
    try:
        cached_dt = datetime.fromisoformat(ts)
        age_hrs = (datetime.utcnow() - cached_dt).total_seconds() / 3600
        return age_hrs < CACHE_TTL_HRS
    except Exception:
        return False


def get_all_congressional_trades() -> dict[str, dict]:
    """
    Fetch all recent congressional purchases from House Stock Watcher.
    Returns {ticker: {count, members, latest_date, is_cluster}} — cached 24h.
    Falls back to {} silently on any failure.
    """
    cache = load_signal_cache()
    if _is_cache_fresh(cache):
        trades = cache.get(CACHE_KEY, {}).get("trades", {})
        print(f"[congressional] Loaded {len(trades)} tickers from cache.")
        return trades

    print("[congressional] Cache miss — fetching live from House Stock Watcher...")
    trades: dict[str, dict] = {}
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)

    try:
        resp = requests.get(HOUSE_API, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"[congressional] API returned {resp.status_code}")
            return {}

        data = resp.json()
        if not isinstance(data, list):
            return {}

        for item in data:
            # Only purchases, not sales
            tx_type = str(item.get("type", "") or item.get("transaction_type", "")).lower()
            if not any(kw in tx_type for kw in ("purchase", "buy", "p -")):
                continue

            ticker = str(item.get("ticker", "") or "").upper().strip()
            if not ticker or not ticker.replace("-", "").isalpha() or len(ticker) > 5:
                continue

            # Parse date — skip if too old
            raw_date = item.get("transaction_date") or item.get("disclosure_date") or ""
            try:
                tx_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if tx_date < cutoff:
                continue

            member = str(item.get("representative") or item.get("senator") or "Unknown")
            amount = str(item.get("amount") or "")

            if ticker not in trades:
                trades[ticker] = {
                    "count":       0,
                    "members":     [],
                    "latest_date": str(tx_date),
                    "amounts":     [],
                }

            trades[ticker]["count"] += 1
            if member not in trades[ticker]["members"]:
                trades[ticker]["members"].append(member)
            trades[ticker]["amounts"].append(amount)
            if str(tx_date) > trades[ticker]["latest_date"]:
                trades[ticker]["latest_date"] = str(tx_date)

        # Add cluster flag
        for t in trades:
            trades[t]["is_cluster"] = len(trades[t]["members"]) >= 2

        # Save to signal cache
        cache[CACHE_KEY] = {
            "cached_at": datetime.utcnow().isoformat(),
            "trades":    trades,
        }
        save_signal_cache(cache)
        print(f"[congressional] Fetched {len(trades)} tickers with recent buys.")
        return trades

    except Exception as exc:
        print(f"[congressional] Fetch failed: {exc}")
        return {}


def get_congressional_signal(ticker: str,
                              all_trades: dict | None = None) -> dict:
    """
    Return congressional signal for a ticker.
    {congress_buys, congress_members, congress_score (0-10), is_cluster, note}
    """
    if all_trades is None:
        all_trades = get_all_congressional_trades()

    if ticker not in all_trades:
        return {
            "congress_buys":    0,
            "congress_members": 0,
            "congress_score":   0,
            "is_cluster":       False,
            "note":             "no recent congressional buys",
        }

    d        = all_trades[ticker]
    count    = d["count"]
    members  = len(d["members"])
    cluster  = d["is_cluster"]
    latest   = d.get("latest_date", "")

    # Score: single buy = 3, two members = 6, cluster (3+) = 10
    if members >= 3:
        score = 10
    elif members == 2:
        score = 6
    elif count >= 2:
        score = 4
    else:
        score = 3

    member_str = ", ".join(d["members"][:3])
    if len(d["members"]) > 3:
        member_str += f" +{len(d['members']) - 3} more"

    note = (
        f"{'CLUSTER BUY: ' if cluster else ''}"
        f"{count} congressional purchase(s) by {members} member(s) "
        f"({member_str}), latest {latest}"
    )

    return {
        "congress_buys":    count,
        "congress_members": members,
        "congress_score":   score,
        "is_cluster":       cluster,
        "note":             note,
    }


def batch_congressional_signals(tickers: list[str]) -> dict[str, dict]:
    """Fetch signals for multiple tickers — one API call for all."""
    all_trades = get_all_congressional_trades()
    return {t: get_congressional_signal(t, all_trades) for t in tickers}


if __name__ == "__main__":
    import pprint
    print("\n=== All congressional trades (last 60 days) ===")
    trades = get_all_congressional_trades()
    # Show top 10 most-bought tickers
    top = sorted(trades.items(), key=lambda x: x[1]["count"], reverse=True)[:10]
    for ticker, data in top:
        print(f"  {ticker}: {data['count']} buy(s) by {data['members'][:2]}")
    print(f"\n=== Signal for NVDA ===")
    pprint.pprint(get_congressional_signal("NVDA"))
