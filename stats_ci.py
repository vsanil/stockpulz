"""
stats_ci.py — THE definition of the confidence intervals this project decides on.

There were three copies of Wilson's interval (`evaluate_picks._wilson`,
`backtest_compare._wilson`, `backtest_walkforward.wilson`) differing only in
whether they returned fractions or rounded percentages. Three copies of the
arithmetic that decides whether a result is real is how they drift, and the
tournament's standings rest entirely on it: an interval that excludes zero is
what promotes a strategy.

🔴 WILSON, NOT WALD — and the reason is a real failure mode, not taste. Wald's
variance term collapses at proportions near 0 or 1, so a 12-for-12 arm gets a
ZERO-WIDTH interval and a harness would certify a decisive win from twelve
observations. Wilson stays honestly wide at the extremes. Near p=0.5 the two are
comparable; the extremes are the failure mode.

🔴 NEWCOMBE for the DIFFERENCE between two proportions, for the same reason —
it inherits Wilson's behaviour at the extremes instead of collapsing.

Both are deliberately dependency-free (stdlib `math` only) so every caller can
import them: the evaluator runs on a CI box with yfinance and pandas already
loaded, but the backtests must not pull the evaluator's Gist code, and nothing
here should ever be a reason not to reuse the one definition.
"""
from __future__ import annotations

import math

Z95 = 1.96


def wilson(wins: int, n: int) -> tuple[float, float, float]:
    """(point, lo, hi) as FRACTIONS in [0, 1]. Wide at small n, honestly so.

    n=0 returns (0.0, 0.0, 1.0) — no observations means the whole interval is
    possible, never a confident zero.
    """
    if not n:
        return (0.0, 0.0, 1.0)
    p = wins / n
    d = 1 + Z95 * Z95 / n
    c = (p + Z95 * Z95 / (2 * n)) / d
    m = Z95 * math.sqrt(p * (1 - p) / n + Z95 * Z95 / (4 * n * n)) / d
    return (p, max(0.0, c - m), min(1.0, c + m))


def wilson_pct(wins: int, n: int, places: int | None = None) -> tuple[float, float]:
    """(lo, hi) as PERCENTAGES. `places` rounds; None leaves them raw.

    ⚠️ These are already percentages. Multiplying by 100 again is a real bug
    this project has shipped — a run once printed `CI[4530.0-6540.0]`, and a
    garbled interval is the single worst number to get wrong, because it is the
    one that decides whether to believe the rest.
    """
    _, lo, hi = wilson(wins, n)
    lo, hi = lo * 100.0, hi * 100.0
    return (round(lo, places), round(hi, places)) if places is not None else (lo, hi)


def diff_ci(w1: int, n1: int, w2: int, n2: int) -> tuple[float, float, float]:
    """95% CI for p1 - p2 as FRACTIONS, by Newcombe's hybrid-score method.

    Returns (difference, lo, hi). An interval that does NOT contain zero is the
    bar for calling one arm better than another — and it is deliberately hard to
    clear at the sample sizes this project can reach.
    """
    p1, l1, u1 = wilson(w1, n1)
    p2, l2, u2 = wilson(w2, n2)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (d, lo, hi)


def excludes_zero(lo: float, hi: float) -> bool:
    """Whether an interval supports a directional claim at all."""
    return (lo > 0 and hi > 0) or (lo < 0 and hi < 0)
