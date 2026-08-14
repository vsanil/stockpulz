"""
backtest_compare.py — does a PROPOSED change actually beat what we ship?

Why this exists
---------------
Live trading is the slowest possible teacher. The synthetic bot produces roughly
six independent observations a weekday and each one takes 30 days to mature, so
"trade, learn, improve" runs at about one usable verdict a year — and every time
the engine changes, the ledger's meaning resets and the count starts over.

This runs the same question against two years of history in minutes:

    proposed change  ->  backtest vs default  ->  adopt / reject / not conclusive

It is the CHEAP, REVERSIBLE test that belongs in front of the expensive,
irreversible one. Survivors go to a live A/B arm (scripts/run_arms.py); losers
never touch a user.


🔴 The head-to-head is scored on DISAGREEMENT ONLY
--------------------------------------------------
This is the part that makes the comparison mean anything.

Two strategies run over the same dates mostly pick the SAME names. Those shared
picks have identical outcomes by construction, so including them drags both win
rates toward each other and manufactures the appearance of "no difference" no
matter how different the strategies really are. The Aug 8 dry run hit exactly
this from the other side: two arms shared 4 of 5 long-term picks, so half the
experiment was measuring nothing.

So the verdict is computed on the picks unique to each side. If A and B agree on
everything, there is no evidence either way and this says so rather than
reporting a confident 0.0 edge.


What it inherits from the walk-forward harness (and why that matters)
--------------------------------------------------------------------
Bars, forward scoring, the stop-before-target rule, the TRAIN/HOLDOUT split and
the levels all come from backtest_walkforward — one definition, so a comparison
cannot silently use different rules than the single-strategy report.

  * TRAIN only. The holdout is not reachable from here at all. Comparing
    candidates IS tuning, and tuning against the holdout is what burns it.
  * Levels are the MEASURED medians users actually get (10.3% / 5.5%), never the
    config defaults — reading levels off config once produced a confident, wrong
    headline about stops being broken.

LIMITS — restate these with any number this prints
--------------------------------------------------
  * Today's universe, so dead names are missing: any edge is an optimistic bound.
  * Screener RANKING only. Claude's final selection is not replayed, and the
    long-term fundamentals leg cannot be (Finnhub serves current data only, so
    scoring a 2024 pick with 2026 fundamentals is look-ahead).
  * A win here is a reason to run a live arm, NOT a reason to ship.
"""
from __future__ import annotations

import argparse
import itertools
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import screener
from screener import STRATEGIES

import backtest_walkforward as wf

# Matches evaluate_picks._MIN_N and performance_context._MIN_DIRECTIVE_N. If 30
# is the bar for calling something conclusive in a report the owner reads, it is
# the bar for changing what real users are recommended.
MIN_N = 30


# ── statistics ───────────────────────────────────────────────────────────────
def _wilson(w: int, n: int) -> tuple[float, float, float]:
    """(point, lo, hi) as fractions. Wide at small n, and honestly so."""
    if not n:
        return (0.0, 0.0, 1.0)
    p, z = w / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (p, max(0.0, c - m), min(1.0, c + m))


def _diff_ci(w1: int, n1: int, w2: int, n2: int) -> tuple[float, float, float]:
    """95% CI for p1 - p2, Newcombe's hybrid-score method.

    Newcombe rather than a Wald interval on the difference because Wald breaks
    down exactly where a small backtest lands it: at proportions near 0 or 1 its
    variance term collapses, so a 12-for-12 arm gets a ZERO-WIDTH interval and
    the harness would report a decisive win from twelve observations. Newcombe
    inherits Wilson's behaviour at the extremes and stays honestly wide.

    (Near p=0.5 the two are comparable — the extremes are the failure mode.)
    """
    p1, l1, u1 = _wilson(w1, n1)
    p2, l2, u2 = _wilson(w2, n2)
    d = p1 - p2
    lo = d - ((p1 - l1) ** 2 + (u2 - p2) ** 2) ** 0.5
    hi = d + ((u1 - p1) ** 2 + (p2 - l2) ** 2) ** 0.5
    return (d, lo, hi)


# ── running one strategy ─────────────────────────────────────────────────────
def picks_by_date(bars: dict, dates: list, top_n: int, strat) -> tuple[dict, dict]:
    """({date: picks}, {date: mid_ranked}) — one scoring pass, both buckets.

    Scoring the universe is the expensive half, so the head-to-head picks and
    the within-strategy baseline come out of the SAME pass. Deriving them
    separately doubled the cost of every comparison for no benefit.

    `mid_ranked` is the control the walk-forward harness uses: same count, same
    date, same eligible pool, but arbitrary. Without it a win rate says nothing
    about whether the SCORE is doing any work.
    """
    picks: dict = {}
    mids: dict = {}
    for d in dates:
        scored = []
        for tkr, hist in bars.items():
            h = hist[hist.index <= d]
            if len(h) < wf.MIN_BARS:
                continue
            try:
                sc, m = screener._short_term_score(h, strat)
            except Exception:
                continue
            if sc <= 0:
                continue
            scored.append((sc, tkr, m, len(h) - 1))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        key = str(d.date())
        mid = len(scored) // 2
        picks[key] = [(t, i, (m.get("atr_pct") or 0)) for _sc, t, m, i in scored[:top_n]]
        mids[key] = [(t, i, (m.get("atr_pct") or 0))
                     for _sc, t, m, i in scored[mid:mid + top_n]]
    return picks, mids


def outcomes(bars: dict, sel: list[tuple]) -> list[dict]:
    rows = []
    for tkr, i, atr in sel:
        r = wf.score_forward(bars[tkr], i, atr)
        if r:
            rows.append({"ticker": tkr, **r})
    return rows


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    w = len([r for r in rows if r["ret"] > 0])
    p, lo, hi = _wilson(w, n)
    return {"n": n, "wins": w, "win_pct": p * 100, "lo": lo * 100, "hi": hi * 100,
            "avg": statistics.mean(r["ret"] for r in rows) if rows else 0.0}


# ── head-to-head ─────────────────────────────────────────────────────────────
def head_to_head(bars: dict, a_picks: dict, b_picks: dict) -> dict:
    """Score A and B on the dates where they DISAGREE.

    Shared picks have identical outcomes and carry zero information about which
    strategy is better — including them only dilutes the difference toward zero.
    """
    a_only, b_only, shared = [], [], 0
    for d in sorted(set(a_picks) & set(b_picks)):
        a_map = {t: (t, i, atr) for t, i, atr in a_picks[d]}
        b_map = {t: (t, i, atr) for t, i, atr in b_picks[d]}
        shared += len(set(a_map) & set(b_map))
        a_only += [a_map[t] for t in set(a_map) - set(b_map)]
        b_only += [b_map[t] for t in set(b_map) - set(a_map)]

    ra, rb = outcomes(bars, a_only), outcomes(bars, b_only)
    sa, sb = _stats(ra), _stats(rb)
    d, lo, hi = _diff_ci(sa["wins"], sa["n"], sb["wins"], sb["n"])
    total = shared + len(a_only)
    return {
        "shared": shared,
        "overlap_pct": (shared / total * 100) if total else 0.0,
        "a": sa, "b": sb,
        "edge_pts": d * 100, "edge_lo": lo * 100, "edge_hi": hi * 100,
    }


def verdict(h: dict, a_name: str, b_name: str) -> tuple[str, str]:
    """(verdict, why). Deliberately hard to get an ADOPT out of."""
    if min(h["a"]["n"], h["b"]["n"]) < MIN_N:
        return ("NOT CONCLUSIVE",
                f"only {h['a']['n']}/{h['b']['n']} disagreeing picks "
                f"(need {MIN_N} each). The strategies agree {h['overlap_pct']:.0f}% "
                f"of the time — there is not enough disagreement to measure.")
    if h["edge_lo"] <= 0 <= h["edge_hi"]:
        return ("NO DETECTABLE DIFFERENCE",
                f"edge {h['edge_pts']:+.1f} pts, CI [{h['edge_lo']:+.1f}, "
                f"{h['edge_hi']:+.1f}] spans zero. Not evidence of equality — "
                f"evidence that this sample cannot tell them apart.")
    better = a_name if h["edge_pts"] > 0 else b_name
    return (f"{better.upper()} WINS",
            f"edge {h['edge_pts']:+.1f} pts, CI [{h['edge_lo']:+.1f}, "
            f"{h['edge_hi']:+.1f}] excludes zero. Survivorship bias still "
            f"flatters this — run a live A/B arm before shipping.")


# ── reporting ────────────────────────────────────────────────────────────────
def report(name: str, s: dict, base: dict | None = None) -> None:
    line = (f"  {name:<12} n={s['n']:<5} win={s['win_pct']:5.1f}%  "
            f"CI[{s['lo']:4.1f}–{s['hi']:4.1f}]  avg={s['avg']:+6.2f}%")
    if base:
        line += f"   vs own baseline: {s['win_pct'] - base['win_pct']:+5.1f} pts"
    print(line)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backtest proposed strategies against the shipped default.")
    ap.add_argument("strategies", nargs="*", default=None,
                    help=f"names to compare (default: all of {sorted(STRATEGIES)})")
    ap.add_argument("--baseline", default="default",
                    help="the shipped strategy every candidate is judged against")
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--step", type=int, default=7)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    names = a.strategies or sorted(STRATEGIES)
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        print(f"unknown strategy: {unknown}. known: {sorted(STRATEGIES)}")
        return 2
    if a.baseline not in names:
        names = [a.baseline] + names

    print(f"[compare] levels: target +{wf.TARGET_PCT}% / stop -{wf.STOP_PCT}% "
          f"({wf.TARGET_PCT / wf.STOP_PCT:.1f}:1)  [measured medians, not config defaults]")

    uni = screener.get_stock_universe()[:a.limit]
    bars = wf.load_bars(uni, a.years, a.refresh)
    bars = {t: h for t, h in bars.items() if h is not None and len(h) >= wf.MIN_BARS}
    if not bars:
        print("no bars — is ALPACA_KEY_ID set?")
        return 2

    all_idx = sorted({d for h in bars.values() for d in h.index})
    start = all_idx[wf.MIN_BARS]
    end = all_idx[-1] - pd.Timedelta(days=wf.HORIZON_DAYS + 3)
    dates = [d for d in all_idx if start <= d <= end][::a.step]

    # 🔴 TRAIN ONLY. Comparing candidates IS tuning, and the holdout is spent the
    # first time it is looked at. It is deliberately unreachable from this script.
    split = dates[int(len(dates) * 0.75)]
    dates = [d for d in dates if d <= split]
    print(f"[compare] {len(bars)} tickers · {len(dates)} rebalances · "
          f"TRAIN ≤ {split.date()} (holdout not reachable from here)")

    picks, summary = {}, {}
    for n in names:
        print(f"[compare] running {n} …")
        picks[n], mids = picks_by_date(bars, dates, a.top, STRATEGIES[n])
        flat = [p for day in picks[n].values() for p in day]
        flat_mid = [p for day in mids.values() for p in day]
        summary[n] = {"picked": _stats(outcomes(bars, flat)),
                      "baseline": _stats(outcomes(bars, flat_mid))}

    print(f"\n{'=' * 74}\nEACH STRATEGY vs ITS OWN MID-RANKED BASELINE\n{'=' * 74}")
    print("  (does the score rank better than arbitrary selection from the same pool?)")
    for n in names:
        report(n, summary[n]["picked"], summary[n]["baseline"])

    print(f"\n{'=' * 74}\nHEAD-TO-HEAD vs {a.baseline}  (disagreeing picks only)\n{'=' * 74}")
    for n in names:
        if n == a.baseline:
            continue
        h = head_to_head(bars, picks[n], picks[a.baseline])
        v, why = verdict(h, n, a.baseline)
        print(f"\n  {n}  vs  {a.baseline}")
        print(f"    overlap: {h['overlap_pct']:.0f}% of picks identical "
              f"({h['shared']} shared)")
        report(f"{n} only", h["a"])
        report(f"{a.baseline} only", h["b"])
        print(f"    -> {v}")
        print(f"       {why}")

    print("\nLIMITS (restate these with any number above):")
    print("  · today's universe → survivorship bias, results biased UP")
    print("  · screener RANKING only — Claude's selection is not replayed")
    print("  · a win here justifies a live A/B arm, never a direct ship")
    return 0


if __name__ == "__main__":
    sys.exit(main())
