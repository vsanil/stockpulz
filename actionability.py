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
# 🔴 A FLAT threshold is the wrong test for "inside the noise", and it produced
# a false positive: KMI was flagged at 2.99% against this 3.0%. But the engine's
# stop is `1.5 x ATR%` (screener.suggested_stop_pct) — already volatility-scaled
# — so a 2.99% stop implies ATR ~2% and is correctly OUTSIDE that ticker's noise.
# Acting on the flag would have widened stops on exactly the low-volatility
# names where a tight stop is right.
#
# Noise is per-ticker, so the test must be too: a stop is tight only when it is
# inside the ticker's OWN recent range. The flat value is kept solely as a
# last-resort floor for positions carrying no ATR, and such positions are
# reported as `unknown` rather than flagged — an unmeasurable case is not a
# violation.
TIGHT_STOP_PCT = 3.0
# A stop below this multiple of the ticker's ATR% is inside its own noise.
# 1.0 x ATR is a single average day's move; the engine targets 1.5x.
TIGHT_STOP_ATR_MULT = 1.0


def _num(x):
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _open_day(pos: dict) -> str:
    """The day the position was OPENED — the ledger join key.

    Closed paper trades keep `bought_date` only because paper_sell now copies it
    into the history record; before that it recorded `closed_date` alone, which
    cannot be joined to a pick at all. `closed_date` is deliberately NOT a
    fallback here: joining a sale date to a pick date would silently match the
    wrong pick whenever a ticker is picked more than once.
    """
    return pos.get("opened_date") or pos.get("bought_date") or ""


def _fill_price(pos: dict):
    # `buy_price` is the paper-history spelling of the same number.
    return _num(pos.get("entry_price") or pos.get("avg_price") or pos.get("buy_price"))


def entry_slippage(ledger: dict, positions: list) -> tuple[list[dict], int]:
    """Fill price vs the entry price the user was shown.

    This is the check that matters most for trust: the message states a hard
    rule ("skip if above $X"). If our own bot routinely fills outside that
    window, a user who followed the instruction literally would have skipped
    the trade — so the pick was not actionable as published.

    Returns (observations, undated) — `undated` counts fills that could not be
    joined to a pick because they carry no open date. Those are silently missing
    evidence, so the caller reports the number rather than hiding it: a breach
    that drops out of the denominator makes the promise look better kept than
    it was, which is the one direction this metric must never fail in.
    """
    # ONE observation per PICK. The bot commonly holds the same ticker as both a
    # real and a paper position, and both fills join to the same pick — counting
    # both would weight whichever picks it happened to buy twice, skewing the
    # "was this reachable" rate. Reachability is a property of the pick, not of
    # how many times we bought it.
    out, seen, undated = [], set(), 0
    for pos in positions or []:
        tkr = (pos.get("ticker") or "").upper()
        day = _open_day(pos)
        if not day:
            # Only counts as missing evidence if we actually paid for something.
            undated += 1 if tkr and _fill_price(pos) else 0
            continue
        if (day, tkr) in seen:
            continue
        pick = (ledger or {}).get((day, tkr))
        if not pick:
            continue
        seen.add((day, tkr))
        want, got = _num(pick.get("entry")), _fill_price(pos)
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
            "closed": bool(pos.get("closed_date") or pos.get("sell_price")),
        })
    return out, undated


def stop_distances(positions: list) -> list[dict]:
    out, seen = [], set()
    for pos in positions or []:
        e = _fill_price(pos)
        s = _num(pos.get("stop_loss"))
        if not e or not s or e <= 0 or s >= e:
            continue
        # A closed paper trade and its former open record describe the SAME
        # position, so key on what identifies the position rather than counting
        # the same stop twice once history starts feeding in.
        key = ((pos.get("ticker") or "").upper(), _open_day(pos), round(e, 4), round(s, 4))
        if key in seen:
            continue
        seen.add(key)
        d = (e - s) / e * 100.0
        tk = (pos.get("ticker") or "").upper()
        atr = _num(pos.get("atr_pct"))
        if atr and atr > 0:
            tight = d < atr * TIGHT_STOP_ATR_MULT      # inside its OWN noise
            basis = f"{atr:.2f}% ATR"
        else:
            tight = False                               # unmeasurable != violation
            basis = "no ATR recorded — not assessed"
        out.append({"ticker": tk, "stop_pct": round(d, 2),
                    "tight": tight, "basis": basis})
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
    slip, undated = entry_slippage(ledger, positions)
    stops = stop_distances(positions)
    outs = outcome_mix(closed)

    slips = [s["slippage_pct"] for s in slip]
    outside = [s for s in slip if s["outside_window"]]
    tight = [s for s in stops if s["tight"]]

    return {
        "entry": {
            "n": len(slip),
            "closed_n": sum(1 for s in slip if s.get("closed")),
            "median_slippage_pct": round(statistics.median(slips), 2) if slips else None,
            "worst_pct": round(max(slips), 2) if slips else None,
            "outside_window": len(outside),
            "outside_pct": round(len(outside) / len(slip) * 100, 1) if slip else None,
            "examples": sorted(outside, key=lambda s: -s["slippage_pct"])[:5],
            # Fills with no open date cannot be joined to a pick. Surfacing the
            # count keeps the rate honest — an unjoinable breach would otherwise
            # just vanish and flatter the number.
            "undated": undated,
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
