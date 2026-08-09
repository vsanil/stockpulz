"""
actionability.py — is a pick *tradeable*, separate from whether it is *right*?

Why this is worth having before Sep 9
-------------------------------------
Win rate answers "were the picks right", and needs ~1,500 observations to
separate a 55% engine from a 60% one. These metrics answer a different and much
more tractable question — "could a user have acted on this pick at the price we
told them?" — and they are DESCRIPTIVE, so ~30 observations is genuinely
informative. They also separate two failures a win rate conflates:

    a losing pick that was WRONG          → the engine chose badly
    a losing pick STOPPED OUT BY NOISE    → the stop was too tight

Those need opposite fixes, and no aggregate return distinguishes them.

Everything here is computed from the synthetic bot's real fills joined to the
ledger's stated pick levels. Nothing is inferred; where the data cannot support
a metric this module says so instead of producing a number.
"""
from __future__ import annotations

import statistics

# Derived from formatters, which OWNS the promise — never re-hardcode 2 or 3.
# Three independent copies of this number is what let a short-term pick breach
# its own published window while the gap check stayed silent.
from formatters import entry_window_pct

ENTRY_WINDOW_PCT = {"short_term": entry_window_pct(),
                    "long_term":  entry_window_pct(is_long_term=True)}
_DEFAULT_WINDOW = entry_window_pct()

# A stop closer than this to entry is inside ordinary daily noise for most
# equities — it will be taken out by random movement regardless of direction.
TIGHT_STOP_PCT = 3.0


def _num(x):
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def entry_slippage(ledger: dict, positions: list) -> list[dict]:
    """Fill price vs the entry price the user was shown.

    This is the check that matters most for trust: the message states a hard
    rule ("skip if above $X"). If our own bot routinely fills outside that
    window, a user who followed the instruction literally would have skipped
    the trade — so the pick was not actionable as published.
    """
    # ONE observation per PICK. The bot commonly holds the same ticker as both a
    # real and a paper position, and both fills join to the same pick — counting
    # both would weight whichever picks it happened to buy twice, skewing the
    # "was this reachable" rate. Reachability is a property of the pick, not of
    # how many times we bought it.
    out, seen = [], set()
    for pos in positions or []:
        tkr = (pos.get("ticker") or "").upper()
        day = pos.get("opened_date") or pos.get("bought_date") or ""
        if (day, tkr) in seen:
            continue
        pick = (ledger or {}).get((day, tkr))
        if not pick:
            continue
        seen.add((day, tkr))
        want, got = _num(pick.get("entry")), _num(pos.get("entry_price") or pos.get("avg_price"))
        if not want or not got or want <= 0:
            continue
        slip = (got - want) / want * 100.0
        window = ENTRY_WINDOW_PCT.get(pick.get("timeframe") or "", _DEFAULT_WINDOW)
        out.append({
            "ticker": tkr, "date": day,
            "pick_entry": round(want, 4), "fill": round(got, 4),
            "slippage_pct": round(slip, 2),
            "window_pct": window,
            # Only ABOVE the window breaks the promise — filling cheaper is fine.
            "outside_window": slip > window,
        })
    return out


def stop_distances(positions: list) -> list[dict]:
    out = []
    for pos in positions or []:
        e = _num(pos.get("entry_price") or pos.get("avg_price"))
        s = _num(pos.get("stop_loss"))
        if not e or not s or e <= 0 or s >= e:
            continue
        d = (e - s) / e * 100.0
        out.append({"ticker": (pos.get("ticker") or "").upper(),
                    "stop_pct": round(d, 2), "tight": d < TIGHT_STOP_PCT})
    return out


def outcome_mix(closed: list) -> dict:
    """Why did positions close?

    NOTE the known gap: the synthetic bot sells through `close_trade`, which
    stamps outcome='manual' regardless of whether it hit target or stop. So the
    bot KNOWS why it sold and does not record it. Until that is fixed this
    returns `usable=False` rather than inventing a stop/target split — a
    degenerate field must not be dressed up as a finding.
    """
    counts: dict[str, int] = {}
    for t in closed or []:
        counts[str(t.get("outcome") or "unknown")] = counts.get(
            str(t.get("outcome") or "unknown"), 0) + 1
    informative = {k for k in counts if k in ("target", "stop", "expired")}
    return {
        "counts": counts,
        "usable": bool(informative),
        "note": ("" if informative else
                 "all closes are tagged 'manual' — close_trade does not record "
                 "whether the exit was a target or a stop, so stop-vs-target "
                 "cannot be measured yet"),
    }


def analyse(ledger_rows: list, positions: list, closed: list) -> dict:
    ledger = {(r["date"], (r.get("ticker") or "").upper()): r
              for r in (ledger_rows or []) if not r.get("control") and r.get("date")}
    slip = entry_slippage(ledger, positions)
    stops = stop_distances(positions)
    outs = outcome_mix(closed)

    slips = [s["slippage_pct"] for s in slip]
    outside = [s for s in slip if s["outside_window"]]
    tight = [s for s in stops if s["tight"]]

    return {
        "entry": {
            "n": len(slip),
            "median_slippage_pct": round(statistics.median(slips), 2) if slips else None,
            "worst_pct": round(max(slips), 2) if slips else None,
            "outside_window": len(outside),
            "outside_pct": round(len(outside) / len(slip) * 100, 1) if slip else None,
            "examples": sorted(outside, key=lambda s: -s["slippage_pct"])[:5],
        },
        "stops": {
            "n": len(stops),
            "median_stop_pct": round(statistics.median([s["stop_pct"] for s in stops]), 2) if stops else None,
            "tight": len(tight),
            "tight_examples": sorted(tight, key=lambda s: s["stop_pct"])[:5],
            "threshold_pct": TIGHT_STOP_PCT,
        },
        "outcomes": outs,
        # Everything above is descriptive, but small samples still mislead when
        # rendered as a percentage — the UI states n next to every figure.
        "sample_warning": (None if len(slip) >= 30 else
                           f"only {len(slip)} matched fills — directional, not conclusive"),
    }
