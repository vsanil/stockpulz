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
# inside the ticker's OWN recent range. A position with no ATR on record is
# reported as not assessed rather than flagged — an unmeasurable case is not a
# violation. (The flat 3.0 constant lingered here unused until 2026-11-15, and
# the admin card kept printing it as if it were the test.)
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


def entry_slippage(ledger: dict, positions: list,
                   skipped: list | None = None) -> tuple[list[dict], int]:
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
    # 🔴 Picks the bot DECLINED because they breached the window are
    # observations, not absences. Without them this metric gets quieter every
    # time the bot correctly obeys the rule, and the breach rate falls toward
    # zero while nothing has improved — the same flattering-direction failure
    # as a closed position erasing its own breach. A skip is also the BETTER
    # evidence: it is what an obedient user actually experienced.
    # `fill` is None because nothing was bought; `would_pay` is what it cost.
    for r in skipped or []:
        day = str(r.get("date") or "")
        tkr = str(r.get("t") or r.get("ticker") or "").upper()
        if not day or not tkr or (day, tkr) in seen:
            continue
        want, slip = _num(r.get("entry")), _num(r.get("slippage_pct"))
        if not want or slip is None or want <= 0:
            continue
        seen.add((day, tkr))
        pick = (ledger or {}).get((day, tkr)) or {}
        out.append({
            "ticker": tkr, "date": day,
            "pick_entry": round(want, 4), "fill": None,
            "would_pay": _num(r.get("would_pay")),
            "slippage_pct": round(slip, 2),
            "window_pct": ENTRY_WINDOW_PCT.get(pick.get("timeframe") or "",
                                               _DEFAULT_WINDOW),
            # Recorded only when it breached, so it is a breach by construction.
            "outside_window": True, "skipped": True, "closed": False,
        })
    return out, undated


# A stop leg the ENGINE published. `levels_source` records which leg the bot had
# to substitute when the pick's level no longer bracketed the fill: "pick" and
# "target" leave the stop as published; "stop"/"both" replaced it with a flat
# fallback, and "invalidation*" is the long-term thesis level, not a stop.
_ENGINE_STOP_SOURCES = ("pick", "target")


def atr_by_pick(ledger_rows: list) -> dict:
    """{(date, TICKER): atr_pct} from every ledger row that recorded one.

    🔴 Positions never store `atr_pct` — nothing in add_holding or paper_buy
    writes it — so `stop_distances` judged every stop "not assessed" and the
    card's "0 tighter than …" had never measured anything (found 2026-11-15).
    The pick's own ledger row has carried the figure all along, in `screen`.
    Volatility is a property of the ticker on that day, so ANY row for the pair
    will do — production, control or tournament arm."""
    out = {}
    for r in ledger_rows or []:
        if not isinstance(r, dict):
            continue
        atr = _num((r.get("screen") or {}).get("atr_pct"))
        day, tkr = r.get("date"), (r.get("ticker") or "").upper()
        if atr and atr > 0 and day and tkr:
            out.setdefault((day, tkr), atr)
    return out


def stop_distances(positions: list, atr_by: dict | None = None,
                   ledger: dict | None = None) -> list[dict]:
    """How far each stop sits below the FILL, and — separately — whether the
    stop the ENGINE published was itself too tight.

    Two different questions, and the first run against live data (2026-11-15)
    showed why they must not be merged: ETR's stop was 1.12% below the bot's
    fill against a 1.63% daily range, but only because the bot bought after the
    price had already fallen toward the stop. The engine's stop was a normal
    distance below the entry it PUBLISHED. `tight` answers "what did the holder
    actually risk"; `engine_tight` answers "did the engine publish a bad stop",
    and only that one is the engine's to fix."""
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
        atr = _num(pos.get("atr_pct")) or (atr_by or {}).get((_open_day(pos), tk))
        assessed = bool(atr and atr > 0)
        if assessed:
            tight = d < atr * TIGHT_STOP_ATR_MULT      # inside its OWN noise
            basis = f"{atr:.2f}% ATR"
        else:
            tight = False                               # unmeasurable != violation
            basis = "no ATR recorded — not assessed"
        engine_stop = pos.get("levels_source") in _ENGINE_STOP_SOURCES
        # The engine's own geometry: its stop against the entry it published.
        # None when there is no production pick to compare with — unmeasurable.
        published = None
        pick = (ledger or {}).get((_open_day(pos), tk)) if engine_stop else None
        want = _num((pick or {}).get("entry"))
        if want and want > 0 and s < want:
            published = (want - s) / want * 100.0
        out.append({"ticker": tk, "stop_pct": round(d, 2),
                    "tight": tight, "basis": basis, "assessed": assessed,
                    "atr_pct": round(atr, 2) if assessed else None,
                    "date": _open_day(pos),
                    "open": not (pos.get("closed_date") or pos.get("sell_price")),
                    "engine_stop": engine_stop,
                    "published_stop_pct": round(published, 2) if published is not None else None,
                    # Could the engine's own stop be judged at all?
                    "engine_assessed": bool(assessed and published is not None),
                    "engine_tight": bool(assessed and published is not None
                                         and published < atr * TIGHT_STOP_ATR_MULT)})
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


def analyse(ledger_rows: list, positions: list, closed: list,
            skipped: list | None = None) -> dict:
    # PRODUCTION picks only. A tournament arm's row for the same ticker and day
    # carries the ARM's entry price, and these positions are fills of the
    # published pick — joining to an arm row would measure slippage against a
    # price no user was shown. (This mattered from 2026-11-15, when the card
    # began reading the live ledger, arm rows included.)
    ledger = {(r["date"], (r.get("ticker") or "").upper()): r
              for r in (ledger_rows or [])
              if not r.get("control") and not r.get("arm") and r.get("date")}
    slip, undated = entry_slippage(ledger, positions, skipped)
    stops = stop_distances(positions, atr_by_pick(ledger_rows), ledger)
    outs = outcome_mix(closed)

    slips = [s["slippage_pct"] for s in slip]
    outside = [s for s in slip if s["outside_window"]]
    tight = [s for s in stops if s["tight"]]
    # What can still be ACTED on: a stop the engine published TOO TIGHT against
    # its own entry, on a position that is still open. A tight stop on a closed
    # trade is history; one the bot substituted is not the engine's; and one
    # that is tight only from a late fill is the fill's doing, not the stop's.
    actionable = [s for s in stops if s["open"] and s["engine_tight"]]

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
            # How many could be judged at all. Without this, "0 tight" reads the
            # same whether every stop passed or none was ever checked.
            "assessed": sum(1 for s in stops if s["assessed"]),
            "tight": len(tight),
            # Same rule as `assessed`: a zero below must say out of how many.
            "engine_assessed_open": sum(1 for s in stops if s["open"] and s["engine_assessed"]),
            "tight_open_engine": len(actionable),
            "tight_examples": sorted(actionable, key=lambda s: s["published_stop_pct"])[:5],
            "atr_mult": TIGHT_STOP_ATR_MULT,
        },
        "outcomes": outs,
        # Everything above is descriptive, but small samples still mislead when
        # rendered as a percentage — the UI states n next to every figure.
        "sample_warning": (None if len(slip) >= 30 else
                           f"only {len(slip)} matched fills — directional, not conclusive"),
    }
