#!/usr/bin/env python3
"""Walk-forward backtest of the LONG-TERM score, on point-in-time fundamentals.

Answers the one question `backtest_walkforward` cannot: does the long-term
rubric — P/E vs sector 30, revenue growth 25, net margin 20, debt/equity 15,
price > 200MA 10 — rank better than arbitrary selection from the same eligible
pool? That is ~90 of ~100 points that have never been validated.

    python3 scripts/backtest_longterm.py --limit 120

🔴 WHAT MAKES IT HONEST
  • Fundamentals come from SEC EDGAR filtered to `filed <= decision date`, so
    only what was public is used. See sec_fundamentals.py.
  • The 200MA leg is scored on a point-in-time BAR SLICE. `_long_term_score`
    fetches bars itself, so the fetch is patched to the slice — otherwise that
    leg reads today's price and reintroduces look-ahead through the back door.
  • The REAL `screener._long_term_score` is called. Re-implementing the rubric
    here would fork it, and the numbers would drift from production silently.
  • Sector comes from the filer's SIC code, not from today's classification.
  • Bars, MIN_BARS and `wilson` are INHERITED from backtest_walkforward.
  • Baseline is MID-RANKED candidates from the same pool. A win rate alone is
    unreadable; the question is "do top-ranked beat mid-ranked".

🔴 WHAT IT CANNOT TEST — restate these with any number it prints
  • ANNUAL figures, not TTM. Production scores on Finnhub TTM; assembling TTM
    from XBRL means inferring Q4 from FY minus Q1-Q3. So this measures the
    RUBRIC, not the exact live pipeline.
  • Claude's final selection is not replayed — expensive, and the model's
    training contains the outcome. This grades the SCREENER's LT ranking.
  • Survivorship: today's universe, so dead names are missing. Any edge is an
    optimistic upper bound.
  • NO STOPS. Long-term picks carry no stop_loss by design, so outcome is
    mark-to-market at the horizon plus alpha vs SPY. It is deliberately NOT
    `score_forward`, which applies short-term stops and targets.

TRAIN ONLY. There is no --holdout here on purpose: the holdout is spent the
first time it is looked at, and it belongs to the short-term harness that
already owns that decision.
"""
from __future__ import annotations

import argparse
import os
import pickle
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import screener
import sec_fundamentals as sf
from backtest_walkforward import MIN_BARS, load_bars, wilson

FACTS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".sec_facts.pkl")
LT_HORIZON_DAYS = 180     # a multi-year thesis judged at 30 days measures noise
MIN_N = 30                # matches evaluate_picks._MIN_N — the honesty gate

# SIC ranges -> the sector names SECTOR_MEDIAN_PE uses. Approximate by
# construction, but taken from the FILING rather than from today's
# classification, which would be a small look-ahead.
_SIC = [
    (2833, 2836, "Health Care"), (3570, 3579, "Technology"),
    (3600, 3699, "Technology"), (7370, 7379, "Technology"),
    (3841, 3851, "Health Care"), (8000, 8099, "Health Care"),
    (1200, 1399, "Energy"), (2900, 2999, "Energy"),
    (4900, 4999, "Utilities"), (4800, 4899, "Communication Services"),
    (2700, 2799, "Communication Services"), (7800, 7999, "Communication Services"),
    (6000, 6499, "Financials"), (6500, 6599, "Real Estate"),
    (6798, 6798, "Real Estate"),
    (1000, 1099, "Materials"), (1400, 1499, "Materials"),
    (2600, 2699, "Materials"), (2800, 2832, "Materials"),
    (3000, 3099, "Materials"), (3200, 3399, "Materials"),
    (2000, 2199, "Consumer Staples"), (5400, 5499, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"), (3711, 3716, "Consumer Discretionary"),
    (5000, 5399, "Consumer Discretionary"), (5500, 5999, "Consumer Discretionary"),
    (7000, 7299, "Consumer Discretionary"), (3900, 3999, "Consumer Discretionary"),
    (1500, 1799, "Industrials"), (2400, 2599, "Industrials"),
    (3400, 3569, "Industrials"), (3700, 3799, "Industrials"),
    (4000, 4799, "Industrials"), (8700, 8799, "Industrials"),
]


def _sector(sic) -> str:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return "Unknown"
    for lo, hi, name in _SIC:
        if lo <= code <= hi:
            return name
    return "Unknown"


def load_facts(tickers: list[str], refresh: bool) -> dict:
    """companyfacts + SIC per ticker, cached. One fetch per TICKER, not per
    date — the same filings serve every decision date."""
    cache: dict = {}
    if os.path.exists(FACTS_CACHE) and not refresh:
        with open(FACTS_CACHE, "rb") as fh:
            cache = pickle.load(fh)
        print(f"[lt] facts cache: {len(cache)} tickers")
    todo = [t for t in tickers if t not in cache]
    for i, t in enumerate(todo, 1):
        try:
            facts = sf.company_facts(t)
            sic = None
            if facts:
                cik = sf.ticker_to_cik(t)
                sub = sf._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
                sic = (sub or {}).get("sic")
            # Keep ONLY the concepts the four legs need. Full companyfacts is
            # megabytes per filer; caching all of it would be gigabytes.
            if facts:
                keep = set(sf._EPS + sf._REVENUE + sf._NET_INCOME +
                           sf._EQUITY + sf._DEBT_LONG + sf._DEBT_SHORT)
                g = facts.get("facts", {}).get("us-gaap", {})
                facts = {"facts": {"us-gaap": {k: v for k, v in g.items()
                                               if k in keep}}}
            cache[t] = {"facts": facts, "sector": _sector(sic)}
        except Exception as exc:
            print(f"[lt] {t}: {type(exc).__name__} {str(exc)[:60]}")
            cache[t] = {"facts": None, "sector": "Unknown"}
        if i % 20 == 0 or i == len(todo):
            print(f"[lt] fetched {i}/{len(todo)} filings")
            with open(FACTS_CACHE, "wb") as fh:
                pickle.dump(cache, fh)
    with open(FACTS_CACHE, "wb") as fh:
        pickle.dump(cache, fh)
    return cache


def lt_score_at(ticker: str, hist: pd.DataFrame, i: int, rec: dict,
                strat) -> tuple[int, dict] | None:
    """The REAL rubric, fed only what was knowable at bar i."""
    date = str(hist.index[i].date())
    price = float(hist["Close"].iloc[i])
    f = sf.fundamentals_as_of(ticker, date, price=price, facts=rec["facts"])
    if not f:
        return None
    inferred = bool(f.get("debt_inferred_zero"))
    # Shaped like Finnhub's response so the rubric needs no modification.
    fh = {
        "peBasicExclExtraTTM": f["pe_ratio"],
        "revenueGrowthTTMYoy": f["revenue_growth"],
        "netMarginTTM": f["net_margin"],
        "totalDebt/totalEquityAnnual": f["debt_to_equity"],
        "marketCapitalization": None,
    }
    info = {"sector": rec["sector"], "symbol": ticker}

    # 🔴 The 200MA leg fetches bars itself. Without this patch it reads TODAY's
    # price against a rolling mean of today's history — look-ahead straight
    # through the back door, in the one leg that looks like price data and so
    # feels safe.
    sliced = hist.iloc[max(0, i - 400):i + 1]
    real_bars = screener._alpaca_single_bars
    screener._alpaca_single_bars = lambda sym, days=365: sliced
    try:
        score, m = screener._long_term_score(info, fh, ticker=ticker, strat=strat)
        m["debt_inferred_zero"] = inferred
        return score, m
    finally:
        screener._alpaca_single_bars = real_bars


def forward(hist: pd.DataFrame, i: int, spy: pd.Series | None) -> dict | None:
    """Mark-to-market at the horizon, plus alpha vs SPY.

    No stops or targets: a long-term pick carries no stop_loss by design, so
    imposing one would measure a trade the app never published.
    """
    entry = float(hist["Close"].iloc[i])
    if entry <= 0:
        return None
    fwd = hist.iloc[i + 1:]
    fwd = fwd[fwd.index <= hist.index[i] + pd.Timedelta(days=LT_HORIZON_DAYS)]
    if len(fwd) < 20:
        return None
    exit_px = float(fwd["Close"].iloc[-1])
    ret = (exit_px - entry) / entry * 100
    alpha = None
    if spy is not None:
        try:
            s0 = float(spy.asof(hist.index[i]))
            s1 = float(spy.asof(fwd.index[-1]))
            if s0 > 0:
                alpha = ret - (s1 - s0) / s0 * 100
        except Exception:
            alpha = None
    return {"ret_pct": round(ret, 2), "win": ret > 0,
            "alpha_pct": round(alpha, 2) if alpha is not None else None}


def run(bars: dict, facts: dict, dates: list, top_n: int, strat,
        spy: pd.Series | None) -> list[dict]:
    rows: list[dict] = []
    scored_any = 0
    for d in dates:
        ranked = []
        for t, hist in bars.items():
            if d not in hist.index:
                continue
            i = hist.index.get_loc(d)
            if i < MIN_BARS:
                continue
            rec = facts.get(t)
            if not rec or not rec.get("facts"):
                continue
            got = lt_score_at(t, hist, i, rec, strat)
            if got is None:
                continue
            score, m = got
            ranked.append((score, t, hist, i, m))
        if len(ranked) < top_n * 3:
            continue
        scored_any += len(ranked)
        ranked.sort(key=lambda r: -r[0])
        mid = len(ranked) // 2
        buckets = [("picked", ranked[:top_n]),
                   ("baseline", ranked[mid:mid + top_n])]
        for label, group in buckets:
            for score, t, hist, i, m in group:
                out = forward(hist, i, spy)
                if not out:
                    continue
                rows.append({"date": str(d.date()), "ticker": t, "bucket": label,
                             "score": score, **out,
                             "inferred_debt": bool(m.get("debt_inferred_zero"))})
    print(f"[lt] {scored_any} ticker-dates scored")
    return rows


def summarise(rows: list[dict], label: str) -> dict:
    if not rows:
        print(f"  {label}: no rows")
        return {}
    n = len(rows)
    w = sum(1 for r in rows if r["win"])
    lo, hi = wilson(w, n)
    med = statistics.median(r["ret_pct"] for r in rows)
    al = [r["alpha_pct"] for r in rows if r["alpha_pct"] is not None]
    # 🔴 wilson() already returns PERCENTAGES (it multiplies internally).
    # Multiplying again printed CI[4530.0-6540.0] — and a garbled confidence
    # interval is precisely the number that decides whether to believe the
    # result, so it is the worst one to get wrong.
    line = (f"  {label:<10} n={n:<4} win={w/n*100:5.1f}%  "
            f"CI[{lo:.1f}-{hi:.1f}]  median={med:+6.2f}%")
    if al:
        line += f"  alpha={statistics.median(al):+6.2f}%"
    print(line)
    return {"n": n, "win": w / n, "lo": lo, "hi": hi, "median": med,
            "alpha": statistics.median(al) if al else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.5)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--step", type=int, default=21, help="days between rebalances")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--refresh-facts", action="store_true")
    a = ap.parse_args()

    uni = screener.get_stock_universe()[:a.limit]
    bars = load_bars(uni, a.years, a.refresh)
    bars = {t: h for t, h in bars.items()
            if h is not None and len(h) >= MIN_BARS and t in uni}
    if not bars:
        print("no bars — is ALPACA_KEY_ID set?")
        return 2

    facts = load_facts(sorted(bars), a.refresh_facts)
    have = sum(1 for r in facts.values() if r.get("facts"))
    print(f"[lt] SEC filings for {have}/{len(bars)} tickers "
          f"(ETFs and non-filers cannot be scored — that is correct, not a gap)")

    # load_bars returns the whole cache regardless of the list it is given, so
    # SPY has to be fetched directly. Without it there is no alpha, and a raw
    # win rate cannot distinguish a good rubric from a rising market.
    spy = None
    try:
        got = bars.get("SPY")
        if got is None:
            got = screener._alpaca_bulk_bars(["SPY"],
                                             days=int(a.years * 365) + 60).get("SPY")
        if got is not None and not got.empty:
            spy = got["Close"]
    except Exception as exc:
        print(f"[lt] SPY unavailable ({type(exc).__name__}) — alpha will be omitted")
    if spy is None:
        print("[lt] ⚠️  no SPY benchmark: win rates below are ABSOLUTE, so they "
              "cannot separate a good rubric from a rising market")

    all_idx = sorted({d for h in bars.values() for d in h.index})
    start = all_idx[MIN_BARS]
    end = all_idx[-1] - pd.Timedelta(days=LT_HORIZON_DAYS + 5)
    dates = [d for d in all_idx if start <= d <= end][::a.step]
    print(f"[lt] {len(bars)} tickers · {len(dates)} rebalances · "
          f"horizon {LT_HORIZON_DAYS}d · TRAIN only")

    rows = run(bars, facts, dates, a.top, screener.DEFAULT_STRATEGY, spy)
    picked = [r for r in rows if r["bucket"] == "picked"]
    base = [r for r in rows if r["bucket"] == "baseline"]

    print("\n  LONG-TERM RUBRIC — point-in-time fundamentals")
    print("  " + "─" * 68)
    p = summarise(picked, "top-ranked")
    b = summarise(base, "mid-ranked")

    if p and b:
        edge = (p["win"] - b["win"]) * 100
        print(f"\n  EDGE {edge:+.1f} points (top-ranked minus mid-ranked)")
        if min(p["n"], b["n"]) < MIN_N:
            print(f"  🔴 NOT CONCLUSIVE — under {MIN_N} observations per side. "
                  f"Do not tune the LT rubric on this.")
        elif abs(edge) < 3:
            print("  within noise — the rubric does not rank better than "
                  "arbitrary selection from the same pool")
        else:
            print("  a real difference at this sample size. Still an optimistic "
                  "upper bound: survivorship, annual-not-TTM figures, and "
                  "Claude's selection step is not replayed.")
    print("\n  Caveats that travel with every number above: ANNUAL figures not "
          "TTM, so this measures the RUBRIC not the live pipeline; today's "
          "universe, so survivorship flatters it; no stops, because long-term "
          "picks carry none.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
