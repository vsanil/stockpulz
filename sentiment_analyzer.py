"""
sentiment_analyzer.py — Social + news sentiment scoring (all free, no API keys).

Sources:
  1. StockTwits public API  — sentiment tagged by users (Bullish/Bearish)
  2. Reddit r/wallstreetbets — mention count via public search JSON
  3. yfinance news           — headline count (already used in ai_analyzer)

Score range: -10 (very bearish) to +10 (very bullish)
"""
from __future__ import annotations

import time
import requests

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
REDDIT_URL     = "https://www.reddit.com/r/{sub}/search.json"
HEADERS        = {"User-Agent": "PersonalStockBot/1.0 (research only, not commercial)"}

# Only search wallstreetbets — the highest-signal subreddit for short-term moves.
# Searching 3 subs added ~30s per morning run with minimal extra signal.
REDDIT_SUBS = ["wallstreetbets"]


# ── StockTwits ─────────────────────────────────────────────────────────────────

def _stocktwits_sentiment(ticker: str) -> dict | None:
    """
    Fetch StockTwits message stream for a ticker.
    Returns bullish_pct, bearish_pct, message_count or None on failure.
    """
    try:
        resp = requests.get(
            STOCKTWITS_URL.format(symbol=ticker),
            headers=HEADERS,
            timeout=8,
        )
        if resp.status_code == 429:
            print(f"[sentiment] StockTwits rate-limited for {ticker}.")
            return None
        if resp.status_code != 200:
            return None

        messages = resp.json().get("messages", [])
        if not messages:
            return None

        bull  = sum(1 for m in messages
                    if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bear  = sum(1 for m in messages
                    if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total = bull + bear

        return {
            "bullish_pct":   round(bull / total * 100) if total else None,
            "bearish_pct":   round(bear / total * 100) if total else None,
            "message_count": len(messages),
            "tagged_count":  total,
        }
    except Exception as exc:
        print(f"[sentiment] StockTwits error for {ticker}: {exc}")
        return None


# ── Reddit ─────────────────────────────────────────────────────────────────────

def _reddit_mentions(ticker: str) -> int:
    """
    Count recent hot mentions of ticker across key stock subreddits.
    Uses Reddit's public JSON API — no API key needed.
    """
    total = 0
    for sub in REDDIT_SUBS:
        try:
            resp = requests.get(
                REDDIT_URL.format(sub=sub),
                params={"q": ticker, "sort": "hot", "limit": 25, "restrict_sr": "on", "t": "week"},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                posts  = resp.json().get("data", {}).get("children", [])
                total += len(posts)
        except Exception:
            pass
        time.sleep(0.2)   # be polite — Reddit throttles aggressively

    return total


# ── Combined sentiment ─────────────────────────────────────────────────────────

def get_sentiment(ticker: str) -> dict:
    """
    Compute combined social sentiment score for a ticker.

    Returns:
        {
            "score":           int,   # -10 to +10
            "stocktwits":      dict | None,
            "reddit_mentions": int,
            "label":           str,   # "bullish" | "bearish" | "neutral" | "hot"
            "summary":         str,
        }
    """
    st    = _stocktwits_sentiment(ticker)
    rd    = _reddit_mentions(ticker)
    score = 0
    parts = []

    # StockTwits scoring
    if st and st.get("tagged_count", 0) >= 3:
        bull = st.get("bullish_pct") or 0
        bear = st.get("bearish_pct") or 0
        if bull >= 65:
            score += 5
            parts.append(f"StockTwits {bull}% bullish ({st['message_count']} msgs)")
        elif bull >= 55:
            score += 2
            parts.append(f"StockTwits leaning bullish ({bull}%)")
        elif bear >= 65:
            score -= 5
            parts.append(f"StockTwits {bear}% bearish ({st['message_count']} msgs)")
        elif bear >= 55:
            score -= 2
            parts.append(f"StockTwits leaning bearish ({bear}%)")
        else:
            parts.append(f"StockTwits mixed ({bull}% bull / {bear}% bear)")
    elif st:
        parts.append(f"StockTwits: {st['message_count']} msgs, untagged")

    # Reddit scoring
    if rd >= 10:
        score += 4
        parts.append(f"Reddit: {rd} hot mentions (trending)")
    elif rd >= 5:
        score += 2
        parts.append(f"Reddit: {rd} mentions")
    elif rd >= 2:
        score += 1
        parts.append(f"Reddit: {rd} mentions")

    # Label
    if score >= 6:
        label = "bullish"
    elif score <= -4:
        label = "bearish"
    elif rd >= 10:
        label = "hot"
    else:
        label = "neutral"

    return {
        "score":           max(-10, min(10, score)),
        "stocktwits":      st,
        "reddit_mentions": rd,
        "label":           label,
        "summary":         ", ".join(parts) if parts else "no social signal",
    }


def batch_sentiment(tickers: list[str], delay: float = 0.8) -> dict[str, dict]:
    """
    Fetch sentiment for a list of tickers. Adds delay between calls to avoid throttling.
    Returns {ticker: sentiment_dict}.
    """
    results = {}
    for t in tickers:
        results[t] = get_sentiment(t)
        time.sleep(delay)
    return results


if __name__ == "__main__":
    import pprint
    for ticker in ["NVDA", "AAPL", "TSLA"]:
        print(f"\n=== {ticker} ===")
        pprint.pprint(get_sentiment(ticker))
