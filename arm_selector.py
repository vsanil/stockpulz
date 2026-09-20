"""
arm_selector.py — deterministic pick selection for a tournament ARM.

Owner's Option B (2026-09-19): **only the LIVE arm calls Claude.** Every other
arm is selected from the screener's own ranking by the rules below — about $60
a month instead of ~$200, and, more importantly, it isolates the one thing that
has never been measured: what Claude's SELECTION adds over simply taking the
screener's top N. With the live arm on the model and the others off it, that
difference is the experiment rather than an assumption.

🔴 MEASUREMENT, NEVER INPUT. Nothing here may be imported by the pick engine.
An arm exists to be compared against production; wiring it back into production
is the contamination `evaluate_picks` was built to avoid.

WHAT AN ARM TRADES, stated plainly so no number is read for more than it is:
  * STOCKS ONLY. Crypto, ETFs and commodities come from separate screeners that
    arms do not run, so the tournament compares the STOCK engine. A standing
    against the live arm is a statement about stocks.
  * LEVELS ARE RULE-BASED, not model-chosen. Every non-live arm uses the
    identical rules below, so arm-vs-arm is clean. Arm-vs-LIVE carries one
    confound worth naming: live targets are chosen per pick by Claude, these
    are derived from measured medians, so part of any difference is
    level-setting rather than selection. The standings say so.
"""
from __future__ import annotations

# ── Levels ───────────────────────────────────────────────────────────────────
# Measured medians of what REAL short-term picks actually carried — NOT config
# defaults. Reading levels off config defaults once produced a confident, wrong
# headline ("the levels are broken") because a wide target with a tight stop
# manufactures stop-outs. These mirror `backtest_walkforward.TARGET_PCT/STOP_PCT`
# and a test pins them to it; they are restated rather than imported because
# that module pulls pandas and the whole screener for two floats.
TARGET_PCT = 10.3
STOP_PCT = 5.5
# A stop outside this band is not a stop: tighter than 3% sits inside ordinary
# daily noise, wider than 20% is not a risk control. Same clamp the backtest uses.
MIN_STOP_PCT, MAX_STOP_PCT = 3.0, 20.0

# A long-term pick carries NO stop by design — the app's answer to "the thesis
# may be broken" is agent.LT_INVALIDATION_PCT, applied by the trader, and a test
# pins this to it. The long-term target is that distance times the measured
# reward:risk of real short-term picks (10.3/5.5 ≈ 1.87), so it is DERIVED from
# what users actually got rather than chosen. Leaving it unset would hand the
# trader its +8% short-term fallback and exit a multi-quarter thesis in weeks.
LT_INVALIDATION_PCT = 15.0
LT_TARGET_PCT = round(LT_INVALIDATION_PCT * (TARGET_PCT / STOP_PCT), 1)   # 28.1

# Conviction scales the position size, so it must mean something. The screener's
# own score bands are the only ranking signal an arm has; the bands match
# `evaluate_picks._score_band` so a slice of the report lines up with the sizing.
_CONVICTION_BY_BAND = ((95, 5), (85, 4), (70, 3))
_MIN_CONVICTION = 2

MAX_PICKS_PER_SECTION = 5


def _num(x):
    """A real, finite number or None. NaN passes `isinstance(x, float)` and has
    made a whole score NaN in this project before."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def conviction_for(score) -> int:
    s = _num(score)
    if s is None:
        return _MIN_CONVICTION
    for floor, conv in _CONVICTION_BY_BAND:
        if s >= floor:
            return conv
    return _MIN_CONVICTION


def stop_pct_for(cand: dict) -> float:
    """Percent below entry, from the candidate's own volatility.

    `suggested_stop_pct` is 1.5 x ATR% and the screener writes it as a PERCENT
    (7.5 means 7.5%), which has been misread as a fraction before and clamped
    every ATR stop to the maximum. Falls back to the measured median.
    """
    v = _num(cand.get("suggested_stop_pct"))
    if v is None:
        atr = _num(cand.get("atr_pct"))
        v = atr * 1.5 if atr is not None else None
    if v is None or v <= 0:
        return STOP_PCT
    return max(MIN_STOP_PCT, min(MAX_STOP_PCT, v))


def _pick_from(cand: dict, long_term: bool) -> dict | None:
    entry = _num(cand.get("current_price"))
    ticker = (cand.get("ticker") or "").upper()
    if not ticker or entry is None or entry <= 0:
        return None                      # unpriceable is never a pick

    if long_term:
        stop = None                      # by design — the trader applies invalidation
        target = round(entry * (1 + LT_TARGET_PCT / 100), 4)
    else:
        sp = stop_pct_for(cand)
        stop = round(entry * (1 - sp / 100), 4)
        target = round(entry * (1 + TARGET_PCT / 100), 4)

    pick = {
        "ticker": ticker,
        "entry_price": round(entry, 4),
        "stop_loss": stop,
        "target_price": target,
        "conviction": conviction_for(cand.get("score")),
        "asset_type": "stock",
        "sector": cand.get("sector"),
        "atr_pct": _num(cand.get("atr_pct")),
        "suggested_stop_pct": _num(cand.get("suggested_stop_pct")),
        # Said in the pick itself, because these reach a report the owner reads
        # and "why do I own this" must not require reading the source.
        "plain_english": (
            f"{cand.get('company') or ticker} — selected by the screener's "
            f"{'fundamental' if long_term else 'technical'} ranking "
            f"(score {cand.get('score')}). No model judgement was used."),
    }
    # Provenance, exactly as the live path records it, so every slice of the
    # evaluator's report works for arms too.
    screen = {k: v for k, v in cand.items()
              if k not in ("company", "sector", "current_price")}
    if screen:
        pick["_screen"] = screen
    return pick


def select(screen: dict, strategy=None, max_per_section: int = MAX_PICKS_PER_SECTION) -> dict:
    """Screener output -> a picks dict of the shape the rest of the app expects.

    Takes the screener's own top-N, which is the point: an arm measures the
    RANKING. The live arm takes the same pool and hands it to Claude, so the
    difference between them is the selection step and nothing else.
    """
    if not isinstance(screen, dict):
        # A failed screen must never read as "this arm sat out today". A
        # starved arm and a quiet market are indistinguishable unless the code
        # says which, and the caller turns an exception into a loud CRASHED
        # line rather than a silent zero.
        raise TypeError(
            f"arm_selector.select needs the screener's output dict, got "
            f"{type(screen).__name__} — the screen failed; an arm must not "
            f"report that as having no candidates")
    out = {sec: {"short_term": [], "long_term": []}
           for sec in ("stocks", "crypto", "etfs", "commodities")}
    for key, lt in (("short_term", False), ("long_term", True)):
        seen = set()
        for cand in (screen.get(key) or [])[:max_per_section]:
            p = _pick_from(cand, long_term=lt)
            if p and p["ticker"] not in seen:
                seen.add(p["ticker"])
                out["stocks"][key].append(p)
    return out
