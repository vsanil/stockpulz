"""
insider_tracker.py — Detect recent insider buying via OpenInsider.com (free).

OpenInsider tracks SEC Form 4 filings. We look for open-market purchases (P)
by C-suite executives and directors in the last 14 days.

Cluster buys (multiple insiders buying at once) are the strongest signal.
"""

import time
import requests
import pandas as pd
from io import StringIO

OPENINSIDER_TICKER_URL = (
    "http://openinsider.com/screener?"
    "s={ticker}&o=&pl=&ph=&ll=&lh=&fd=14&fdr=&td=0&tdr=&"
    "fdlyl=&fdlyh=&daysago=&xp=1&xs=1&"          # P=purchase only
    "vl=10000&vh=&ocl=&och=&"                     # min $10k trade value
    "sic1=-1&sicl=100&sich=9999&"
    "iscob=0&isceo=1&ispres=1&iscoo=1&iscfo=1&isgc=1&"   # CEO, CFO, COO, Pres, GC, Director
    "isvp=0&isdirector=1&istenpercent=0&isother=0&"
    "sortcol=0&cnt=20&Action=signin"
)

# Cluster buys (multiple insiders, any ticker, last 7 days) — strong signal
OPENINSIDER_CLUSTER_URL = (
    "http://openinsider.com/screener?"
    "s=&o=&pl=&ph=&ll=&lh=&fd=7&"
    "xp=1&xs=1&vl=50000&vh=&"
    "iscob=0&isceo=1&ispres=1&iscoo=1&iscfo=1&isgc=1&isdirector=1&"
    "sortcol=0&cnt=100&Action=signin"
)

# Browser-like User-Agent reduces risk of OpenInsider blocking automated requests
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _parse_value(val_str: str) -> float:
    """Parse value strings like '$1,234,567' or '+$500K' to float."""
    try:
        cleaned = str(val_str).replace("$", "").replace(",", "").replace("+", "").strip()
        if cleaned.endswith("K"):
            return float(cleaned[:-1]) * 1_000
        if cleaned.endswith("M"):
            return float(cleaned[:-1]) * 1_000_000
        return float(cleaned)
    except Exception:
        return 0.0


def get_insider_signal(ticker: str) -> dict:
    """
    Check OpenInsider for recent buys of this specific ticker (last 14 days).

    Returns:
        {
            "recent_buys":   int,
            "unique_insiders": int,
            "total_value":   float,
            "insider_score": int,    # 0–10
            "is_cluster":    bool,   # 3+ insiders buying
            "note":          str,
        }
    """
    try:
        url  = OPENINSIDER_TICKER_URL.format(ticker=ticker)
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return _empty_signal("fetch failed")

        tables = pd.read_html(StringIO(resp.text))
        if not tables:
            return _empty_signal("no table")

        # OpenInsider's main table has a recognizable structure — find the data table
        df = None
        for t in tables:
            if len(t.columns) >= 8 and len(t) > 0:
                df = t
                break

        if df is None or df.empty:
            return _empty_signal("no data")

        # Filter for purchase transactions
        for col in df.columns:
            col_str = str(col).lower()
            if "trade" in col_str or "type" in col_str:
                df = df[df[col].astype(str).str.contains("P|Purchase|Buy", na=False, case=False)]
                break

        buy_count = len(df)
        if buy_count == 0:
            return _empty_signal("no buys in last 14 days")

        # Unique insiders
        unique_insiders = buy_count   # conservative: assume each row = different insider
        for col in df.columns:
            if "insider" in str(col).lower() or "name" in str(col).lower():
                unique_insiders = df[col].nunique()
                break

        # Total value
        total_value = 0.0
        for col in df.columns:
            if "value" in str(col).lower() or "$" in str(col):
                total_value = df[col].apply(_parse_value).sum()
                break

        is_cluster = unique_insiders >= 3

        # Score
        if is_cluster and total_value >= 1_000_000:
            score = 10
        elif is_cluster:
            score = 8
        elif buy_count >= 2 and total_value >= 500_000:
            score = 7
        elif buy_count >= 2:
            score = 5
        elif total_value >= 500_000:
            score = 4
        elif buy_count == 1:
            score = 2
        else:
            score = 0

        note = (
            f"{'CLUSTER: ' if is_cluster else ''}"
            f"{buy_count} insider buy(s), {unique_insiders} insider(s), "
            f"${total_value:,.0f} total"
        )

        return {
            "recent_buys":     buy_count,
            "unique_insiders": unique_insiders,
            "total_value":     total_value,
            "insider_score":   score,
            "is_cluster":      is_cluster,
            "note":            note,
        }

    except Exception as exc:
        print(f"[insider] Error for {ticker}: {exc}")
        return _empty_signal("unavailable")


def get_cluster_buys() -> list[str]:
    """
    Fetch tickers with cluster buying activity in the last 7 days (any ticker).
    Returns a list of tickers — these are high-conviction signals.
    """
    tickers = []
    try:
        resp = requests.get(OPENINSIDER_CLUSTER_URL, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return []
        tables = pd.read_html(StringIO(resp.text))
        for t in tables:
            for col in t.columns:
                if "ticker" in str(col).lower() or "symbol" in str(col).lower():
                    tickers = t[col].dropna().astype(str).str.upper().tolist()
                    break
            if tickers:
                break
    except Exception as exc:
        print(f"[insider] Cluster buy fetch failed: {exc}")

    # Deduplicate
    seen = set()
    result = []
    for t in tickers:
        if t and t not in seen and t.isalpha():
            seen.add(t)
            result.append(t)
    return result


def batch_insider_signals(tickers: list[str], delay: float = 0.5) -> dict[str, dict]:
    """Fetch insider signals for multiple tickers. Returns {ticker: signal_dict}."""
    results = {}
    for t in tickers:
        results[t] = get_insider_signal(t)
        time.sleep(delay)
    return results


def _empty_signal(reason: str) -> dict:
    return {
        "recent_buys":     0,
        "unique_insiders": 0,
        "total_value":     0.0,
        "insider_score":   0,
        "is_cluster":      False,
        "note":            reason,
    }


if __name__ == "__main__":
    import pprint
    for ticker in ["NVDA", "AAPL"]:
        print(f"\n=== {ticker} ===")
        pprint.pprint(get_insider_signal(ticker))
    print("\n=== Cluster buys ===")
    print(get_cluster_buys())
