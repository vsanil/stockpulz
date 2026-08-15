#!/usr/bin/env python3
"""
input_audit.py — does every external input actually return data, and does
anything depend on it?

Why this exists
---------------
On 2026-08-11 ten bugs were found in one day, nearly all the same shape: an
input is wired, returns nothing, the exception is swallowed, every monitor stays
green. Patching them individually loses. This answers the three questions that
decide keep-vs-cut for each input, in one place:

    LIVE?     does it return data right now
    FEEDS?    score / display / storage / delivery — what breaks if it dies
    TESTED?   is there any test that touches it

READ-ONLY. Probes are single small requests; nothing is written anywhere.

⚠️ LOCAL TLS FALSE ALARM: this dev machine has a TLS-intercepting proxy, so
Telegram (and anything via curl_cffi/yfinance) can report DEAD here while being
perfectly healthy in production. CI is the authority — re-run there before
believing a transport failure.

⚠️ A "DEAD" row may be a BAD PROBE, not a dead input — that happened twice while
writing this (wrong function name, wrong response key). Before acting on a
failure, confirm the probe calls the real function and reads a real field.

    python3 scripts/input_audit.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tested(*needles: str) -> str:
    """Any test file mentioning this input?"""
    try:
        out = subprocess.run(
            ["grep", "-rl", "-e", needles[0], *sum((["-e", n] for n in needles[1:]), []),
             os.path.join(ROOT, "tests")],
            capture_output=True, text=True, timeout=30).stdout.strip()
        n = len([l for l in out.splitlines() if l])
        return f"{n} file(s)" if n else "NONE"
    except Exception:
        return "?"


def probe(fn, timeout_note=""):
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:60]}", time.time() - t0
    return ok, detail, time.time() - t0


# ── probes ───────────────────────────────────────────────────────────────────
def p_alpaca_bulk():
    import screener
    r = screener._alpaca_bulk_bars(["AAPL", "MSFT"], days=20)
    return bool(r.get("AAPL") is not None), f"{len(r)}/2 tickers"


def p_alpaca_single():
    import screener
    d = screener._alpaca_single_bars("SPY", days=65)
    return d is not None and len(d) > 20, f"{0 if d is None else len(d)} bars"


def p_finnhub_profile():
    import screener
    d = screener._get_finnhub_profile("AAPL")
    return bool(d and d.get("sector")), f"sector={d.get('sector')}" if d else "empty"


def p_finnhub_metrics():
    import screener
    d = screener._get_finnhub_metrics("AAPL")
    return bool(d), f"{len(d or {})} fields"


def p_coingecko():
    from price_checker import cg_prices
    d = cg_prices(["bitcoin"])
    v = (d or {}).get("bitcoin")
    return bool(v and v > 0), f"BTC=${v:,.0f}" if v else "no price"


def p_yfinance():
    from market_data import get_live_price
    v = get_live_price("AAPL")
    return bool(v and v > 0), f"AAPL=${v:,.2f}" if v else "no price"


def p_universe_sp500():
    import requests, pandas as pd
    from io import StringIO
    r = requests.get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
                     "main/data/constituents.csv", timeout=20)
    n = len(pd.read_csv(StringIO(r.text)))
    return n > 400, f"{n} tickers"


def p_universe_nasdaq():
    import screener
    s = screener._nasdaq_100_symbols()
    return len(s) > 50, f"{len(s)} tickers"


def p_universe_midcap():
    import screener
    s = screener._wiki_symbols("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies")
    return len(s) > 200, f"{len(s)} tickers"


def p_congress():
    import screener
    cands = [n for n in dir(screener) if "congress" in n.lower() and callable(getattr(screener, n))]
    if not cands:
        return False, "no fetcher in screener"
    fn = getattr(screener, cands[0])
    try:
        r = fn(["AAPL"]) if fn.__code__.co_argcount else fn()
    except TypeError:
        r = fn()
    return bool(r), f"{cands[0]} -> {len(r or [])} rows"


def p_insider():
    import requests
    r = requests.get("http://openinsider.com/latest-insider-trading", timeout=20)
    return r.status_code == 200 and "insider" in r.text.lower(), f"HTTP {r.status_code}"


def p_anthropic():
    from llm_client import _get_client
    m = _get_client().messages.create(model="claude-haiku-4-5-20251001", max_tokens=8,
                                      messages=[{"role": "user", "content": "say ok"}])
    return bool(m.content), f"{m.content[0].text.strip()[:12]!r}"


def p_gist():
    import config_manager as cm
    d = cm._load_gist_file("picks.json")
    return bool(d), f"picks._saved_date={(d or {}).get('_saved_date')}"


def p_telegram():
    import os, requests
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        return False, "no token in env"
    r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=15)
    return r.status_code == 200, f"HTTP {r.status_code}"


def p_polygon():
    import os, requests
    k = os.environ.get("POLYGON_API_KEY", "")
    if not k:
        return False, "no POLYGON_API_KEY set"
    r = requests.get("https://api.polygon.io/v3/reference/tickers",
                     params={"limit": 1, "apiKey": k}, timeout=15)
    return r.status_code == 200, f"HTTP {r.status_code}"


def p_polygon_options():
    """Separate from p_polygon on purpose — the reference endpoint is free while
    the OPTIONS SNAPSHOT is not. Probing the free one and calling Polygon 'ok'
    is what let the options chain look healthy while it 403'd on every call."""
    import os, requests
    from datetime import date, timedelta
    k = os.environ.get("POLYGON_API_KEY", "")
    if not k:
        return False, "no POLYGON_API_KEY set"
    t = date.today()
    r = requests.get("https://api.polygon.io/v3/snapshot/options/NVDA",
                     params={"expiration_date.gte": t.isoformat(),
                             "expiration_date.lte": (t + timedelta(days=45)).isoformat(),
                             "limit": 5, "apiKey": k}, timeout=15)
    if r.status_code == 403:
        return False, "HTTP 403 — not entitled (paid tier); falls back to yfinance"
    return r.status_code == 200 and bool((r.json() or {}).get("results")), f"HTTP {r.status_code}"


def p_commodity_candidates():
    """🔴 The Aug 14 bug: this returned 0 on every run for ≥10 days and printed
    nothing, so the commodities section was silently empty."""
    import ai_analyzer
    c = ai_analyzer._build_commodity_candidates()
    n_signals = sum(1 for x in c
                    if x.get("ret_1m") is not None and x.get("rsi") is not None)
    return bool(c), f"{len(c)} candidates, {n_signals} with full signals"


def p_options_strike():
    """The field that decides whether an options play ships with a real strike.
    When it is None the prompt's documented fallback fires and the play goes out
    as 'CALL, strike: null' — actionable by nobody."""
    from options_flow import get_options_signal
    s = get_options_signal("NVDA")
    strike = s.get("nearest_otm_call")
    return strike is not None, (f"NVDA strike={strike} exp={s.get('nearest_call_expiry')} "
                                f"src={s.get('source')}")


# name, probe, what it feeds, test-grep needles
INPUTS = [
    ("Alpaca bulk bars",    p_alpaca_bulk,      "SCORE  short-term technicals (600 tickers)", ("_alpaca_bulk_bars",)),
    ("Alpaca single bars",  p_alpaca_single,    "SCORE  SPY RS baseline + 200MA leg",         ("_alpaca_single_bars",)),
    ("Finnhub profile",     p_finnhub_profile,  "SCORE  sector / name for LT",                ("_get_finnhub_profile", "finnhub_profile")),
    ("Finnhub metrics",     p_finnhub_metrics,  "SCORE  ~90% of the long-term score",         ("_get_finnhub_metrics", "finnhub_metrics")),
    ("yfinance",            p_yfinance,         "SCORE  price/bar fallback + all live prices",("get_live_price", "yfinance")),
    ("CoinGecko",           p_coingecko,        "SCORE  crypto prices + crypto screen",       ("cg_prices",)),
    ("Universe: S&P 500",   p_universe_sp500,   "SCORE  600-ticker universe",                 ("get_stock_universe",)),
    ("Universe: Nasdaq-100",p_universe_nasdaq,  "SCORE  universe (11 names)",                 ("_nasdaq_100_symbols",)),
    ("Universe: MidCap 400",p_universe_midcap,  "SCORE  universe",                            ("_wiki_symbols",)),
    # StockTwits + Reddit removed 2026-08-12 — both dead, both "weight LEAST",
    # and each cost a failing HTTP call per cache miss. Congressional stays:
    # dormant (no QUIVER_API_KEY), not broken, and zero cost to keep.
    ("Congressional",       p_congress,         "SCORE  +8/+4 on LT (dormant)",               ("congress",)),
    ("Insider (openinsider)",p_insider,         "context  LLM",                               ("insider",)),
    ("Polygon",             p_polygon,          "context  options flow + bar fallback",       ("polygon", "options_flow")),
    ("Polygon options snap", p_polygon_options, "context  options chain (PAID tier)",         ("_get_polygon_options",)),
    ("Commodity candidates", p_commodity_candidates, "CORE  the entire commodities section",   ("_build_commodity_candidates", "commodity")),
    ("Options strike",      p_options_strike,   "CORE  strike/expiry on every options play",  ("nearest_otm_call",)),
    ("Anthropic",           p_anthropic,        "CORE   pick selection + all NL",             ("anthropic", "llm_client")),
    ("GitHub Gist",         p_gist,             "CORE   all storage",                         ("gist", "storage")),
    ("Telegram",            p_telegram,         "CORE   all delivery",                        ("telegram",)),
]


def main() -> int:
    print(f"{'INPUT':<24}{'LIVE':<6}{'DETAIL':<30}{'FEEDS':<44}{'TESTED'}")
    print("-" * 118)
    dead = []
    for name, fn, feeds, needles in INPUTS:
        ok, detail, dt = probe(fn)
        if not ok:
            # Flag the known local-proxy signature rather than reporting it as
            # a real outage — it has cost investigation time before.
            if "SSL" in detail or "certificate" in detail.lower():
                detail += "  [likely LOCAL TLS proxy — verify on CI]"
            dead.append((name, feeds, detail))
        print(f"{name:<24}{'ok' if ok else 'DEAD':<6}{detail[:29]:<30}{feeds:<44}{_tested(*needles)}")
    print("-" * 118)
    if dead:
        print(f"\n🔴 {len(dead)} DEAD input(s):")
        for n, f, d in dead:
            sev = "SCORE-AFFECTING" if f.startswith("SCORE") or f.startswith("CORE") else "cosmetic"
            print(f"   {n:<24} {sev:<16} {d}")
    else:
        print("\nAll inputs returning data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())