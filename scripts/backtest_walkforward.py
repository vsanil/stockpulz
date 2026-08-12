#!/usr/bin/env python3
"""
backtest_walkforward.py — does the short-term score actually rank future
winners above losers, measured on data the tuner never saw?

WHY THIS EXISTS
---------------
The live evaluator needs ~1,500 picks to separate a 55% engine from a 60% one —
about a year at 6 picks/day. This replays history instead, which is the only
honest way to get thousands of samples quickly, and it is also the single most
common way retail systems fool themselves. So the discipline matters more than
the code:

  * TRAIN window   — tune here, look as much as you like.
  * HOLDOUT window — evaluated ONCE, with --holdout. Every time you tune after
                     looking at it, it stops being out-of-sample and the number
                     it produced becomes a lie.

WHAT IT CAN AND CANNOT TEST — read before believing any output
--------------------------------------------------------------
  ✓ The SHORT-TERM technical score, computed strictly from bars up to each
    decision date (no look-ahead within the price series).
  ✓ Ranking quality vs two baselines. A win rate alone is unreadable; the
    question is whether the score beats random selection from the same universe.

  ✗ The LONG-TERM score. Finnhub serves CURRENT fundamentals only — scoring a
    2024 pick with 2026 P/E is look-ahead, so the LT leg is untestable here and
    is excluded rather than faked.
  ✗ Claude's final selection. Replaying it is both expensive and contaminated:
    the model's training includes the outcome. This measures the SCREENER
    RANKING, which is the part with tunable weights anyway.
  ✗ Survivorship. The universe is TODAY's constituents, so dead and delisted
    names are missing and results are biased upward. Treat any edge found here
    as an optimistic bound, not an estimate.

Usage:
    python3 scripts/backtest_walkforward.py --years 2 --limit 150
    python3 scripts/backtest_walkforward.py --holdout        # one shot. Mean it.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import screener
from screener import DEFAULT_STRATEGY, STRATEGIES, _short_term_score

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backtest_bars.pkl")
HOLDOUT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".holdout_views.json")

HORIZON_DAYS   = 30      # calendar days a pick has to work, matching the evaluator
MIN_BARS       = 60      # bars required before a ticker can be scored at all

# 🔴 Levels must match what users are ACTUALLY given, not the config defaults.
# Measured from 24 real short-term picks in the ledger: median target +10.3%,
# median stop -5.5% (reward:risk 1.9:1). The first run of this harness used the
# raw config defaults (15% target, 1.5xATR ~= 3.9% stop = 3.8:1), which is a far
# more aggressive trade than the app publishes — it mechanically inflates the
# stop-out rate and made the levels look broken when the discrepancy was mine.
# Claude picks per-pick targets that cannot be replayed, so the empirical median
# is the honest stand-in.
TARGET_PCT     = 10.3    # measured median of real ST picks
STOP_PCT       = 5.5     # measured median of real ST picks
STOP_ATR_MULT  = 1.5     # used only with --atr-stop


# ── data ─────────────────────────────────────────────────────────────────────
def load_bars(tickers: list[str], years: float, refresh: bool) -> dict:
    """Daily bars per ticker. Cached — the fetch is the slow part, not the maths."""
    if os.path.exists(CACHE) and not refresh:
        bars = pd.read_pickle(CACHE)
        print(f"[backtest] cache: {len(bars)} tickers")
        return bars
    days = int(years * 365) + 60
    out: dict = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        got = screener._alpaca_bulk_bars(chunk, days=days)
        out.update(got)
        print(f"[backtest] fetched {len(out)}/{len(tickers)}")
    pd.to_pickle(out, CACHE)
    return out


# ── forward scoring — identical rule to the live evaluator ───────────────────
USE_ATR_STOP = False     # set by --atr-stop


def score_forward(hist: pd.DataFrame, i: int, atr_pct: float) -> dict | None:
    """Outcome of buying at bar i's close, held HORIZON_DAYS.

    Stop is checked BEFORE target on any given bar — the conservative
    assumption, so the engine is never flattered by an ambiguous day.
    """
    entry = float(hist["Close"].iloc[i])
    if entry <= 0:
        return None
    fwd = hist.iloc[i + 1:]
    fwd = fwd[fwd.index <= hist.index[i] + pd.Timedelta(days=HORIZON_DAYS)]
    if len(fwd) < 5:
        return None
    if USE_ATR_STOP:
        stop_pct = max(3.0, min(20.0, atr_pct * STOP_ATR_MULT)) if atr_pct else STOP_PCT
    else:
        stop_pct = STOP_PCT
    stop, target = entry * (1 - stop_pct / 100), entry * (1 + TARGET_PCT / 100)
    for _, row in fwd.iterrows():
        if float(row["Low"]) <= stop:
            return {"outcome": "stop",   "ret": -stop_pct}
        if float(row["High"]) >= target:
            return {"outcome": "target", "ret": TARGET_PCT}
    last = float(fwd["Close"].iloc[-1])
    return {"outcome": "expired", "ret": (last - entry) / entry * 100}


def run(bars: dict, dates: list, top_n: int, strat) -> list[dict]:
    """Walk the calendar. At each date score every ticker on bars up to THAT DATE."""
    rows = []
    for di, d in enumerate(dates):
        scored = []
        for tkr, hist in bars.items():
            h = hist[hist.index <= d]
            if len(h) < MIN_BARS:
                continue
            try:
                sc, m = _short_term_score(h, strat)
            except Exception:
                continue
            if sc <= 0:
                continue
            scored.append((sc, tkr, m, len(h) - 1))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        chosen = scored[:top_n]
        # Baseline: same count, same date, same eligible pool — but arbitrary.
        # Without this a win rate says nothing about the SCORE.
        mid = scored[len(scored) // 2: len(scored) // 2 + top_n]
        for bucket, sel in (("picked", chosen), ("baseline", mid)):
            for sc, tkr, m, i in sel:
                r = score_forward(bars[tkr], i, m.get("atr_pct") or 0)
                if r:
                    rows.append({"date": str(d.date()), "ticker": tkr, "score": sc,
                                 "bucket": bucket, **r})
        if di % 10 == 0:
            print(f"[backtest] {d.date()}  pool={len(scored)}  rows={len(rows)}")
    return rows


# ── reporting ────────────────────────────────────────────────────────────────
def wilson(w: int, n: int) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    p, z = w / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round((c - m) * 100, 1), round((c + m) * 100, 1))


def summarise(rows: list[dict], label: str) -> None:
    print(f"\n{'='*74}\n{label}   n={len(rows)}\n{'='*74}")
    if not rows:
        print("  no data")
        return
    for bucket in ("picked", "baseline"):
        b = [r for r in rows if r["bucket"] == bucket]
        if not b:
            continue
        wins = [r for r in b if r["ret"] > 0]
        wr = len(wins) / len(b) * 100
        lo, hi = wilson(len(wins), len(b))
        avg = statistics.mean(r["ret"] for r in b)
        med = statistics.median(r["ret"] for r in b)
        mix = {}
        for r in b:
            mix[r["outcome"]] = mix.get(r["outcome"], 0) + 1
        print(f"  {bucket:<9} n={len(b):<6} win={wr:5.1f}%  CI[{lo}–{hi}]  "
              f"avg={avg:+6.2f}%  med={med:+6.2f}%  {mix}")
    p = [r for r in rows if r["bucket"] == "picked"]
    q = [r for r in rows if r["bucket"] == "baseline"]
    if p and q:
        edge = (len([r for r in p if r['ret'] > 0]) / len(p)
                - len([r for r in q if r['ret'] > 0]) / len(q)) * 100
        print(f"\n  EDGE over baseline: {edge:+.1f} points of win rate")
        if abs(edge) < 3:
            print("  -> within noise. The score is not ranking better than arbitrary selection.")


def main() -> int:
    global TARGET_PCT, STOP_PCT, USE_ATR_STOP
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=150, help="universe size (compute bound)")
    ap.add_argument("--top", type=int, default=5, help="picks per rebalance date")
    ap.add_argument("--step", type=int, default=7, help="days between rebalances")
    ap.add_argument("--strategy", default="default", choices=sorted(STRATEGIES))
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--target-pct", type=float, default=TARGET_PCT)
    ap.add_argument("--stop-pct", type=float, default=STOP_PCT)
    ap.add_argument("--atr-stop", action="store_true",
                    help="use 1.5xATR stops instead of the measured median")
    ap.add_argument("--holdout", action="store_true",
                    help="reveal the out-of-sample window. ONE SHOT — see the module docstring.")
    a = ap.parse_args()

    TARGET_PCT, STOP_PCT, USE_ATR_STOP = a.target_pct, a.stop_pct, a.atr_stop
    print(f"[backtest] levels: target +{TARGET_PCT}% / stop -{STOP_PCT}% "
          f"({TARGET_PCT/STOP_PCT:.1f}:1)" + ("  [ATR stops]" if USE_ATR_STOP else "  [measured medians]"))

    uni = screener.get_stock_universe()[:a.limit]
    bars = load_bars(uni, a.years, a.refresh)
    bars = {t: h for t, h in bars.items() if h is not None and len(h) >= MIN_BARS}
    if not bars:
        print("no bars — is ALPACA_KEY_ID set?")
        return 2

    all_idx = sorted({d for h in bars.values() for d in h.index})
    start, end = all_idx[MIN_BARS], all_idx[-1] - pd.Timedelta(days=HORIZON_DAYS + 3)
    dates = [d for d in all_idx if start <= d <= end][::a.step]
    split = dates[int(len(dates) * 0.75)]
    print(f"[backtest] {len(bars)} tickers · {len(dates)} rebalances · "
          f"TRAIN ≤ {split.date()} · HOLDOUT > {split.date()}")

    rows = run(bars, dates, a.top, STRATEGIES[a.strategy])
    train = [r for r in rows if r["date"] <= str(split.date())]
    hold  = [r for r in rows if r["date"] >  str(split.date())]

    summarise(train, f"TRAIN  (tune freely)   strategy={a.strategy}")

    if a.holdout:
        views = []
        if os.path.exists(HOLDOUT_LOG):
            views = json.load(open(HOLDOUT_LOG))
        views.append({"when": str(date.today()), "strategy": a.strategy, "n": len(hold)})
        json.dump(views, open(HOLDOUT_LOG, "w"), indent=1)
        print(f"\n⚠️  HOLDOUT VIEWED {len(views)} time(s). After the first, this window is no "
              f"longer out-of-sample — every later tune has seen it.")
        summarise(hold, f"HOLDOUT  (one shot)   strategy={a.strategy}")
    else:
        print(f"\n[holdout withheld — {len(hold)} rows. Re-run with --holdout when you are "
              f"done tuning, and only then.]")

    print("\nLIMITS (restate these with any number above):")
    print("  · today's universe → survivorship bias, results biased UP")
    print("  · short-term technical score only — LT fundamentals are not point-in-time")
    print("  · screener ranking only — Claude's final selection is not replayed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
