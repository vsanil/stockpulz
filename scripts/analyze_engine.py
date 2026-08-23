#!/usr/bin/env python3
"""Daily engine analysis — turn the synthetic bot's activity into ranked,
implementable improvements to the recommendation engine.

WHY THIS EXISTS
    The synthetic bot trades every weekday. Its value has been as a bug
    detector, but nothing synthesised its output into "here is what to change,
    with the evidence". This does, and writes the result where the next session
    reads it: analysis/ENGINE_FINDINGS.md.

🔴 THE ONE RULE THAT MAKES THIS SAFE
    Engine changes are NEVER recommended from the bot's win rate. Its trades are
    a robot's mechanical fills; in July, 22 of them were being shown to users as
    the community record AND injected into the pick prompt, so crude stop-outs
    were steering real recommendations. That loop was cut deliberately. Anything
    needing n>=30 is HELD with the date it will clear.

    What IS actionable at small n is DESCRIPTIVE:
      • integrity violations  — binary, one instance is enough
      • levels geometry       — the bot fills mechanically at published levels
      • reachability          — could a user have acted at the published price
      • coverage              — an asset class producing nothing
      • exit-reason mix       — stop vs target vs expiry

Usage:  python3 scripts/analyze_engine.py [--dry-run]
"""
from __future__ import annotations   # `X | None` on Python 3.9 (CI is 3.11)

import argparse
import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "analysis", "ENGINE_FINDINGS.md")
MIN_N = 30            # matches evaluate_picks._MIN_N and _MIN_DIRECTIVE_N


class Finding:
    """One observation, with the evidence that justifies acting on it."""

    def __init__(self, tier, title, evidence, action, n=None, blocked_until=None):
        self.tier = tier                 # ACT | MEASURE | HOLD
        self.title = title
        self.evidence = evidence
        self.action = action
        self.n = n
        self.blocked_until = blocked_until

    def render(self) -> str:
        head = f"### [{self.tier}] {self.title}"
        if self.n is not None:
            head += f"  *(n={self.n})*"
        body = [head, "", f"**Evidence:** {self.evidence}", "",
                f"**Proposed action:** {self.action}"]
        if self.blocked_until:
            body += ["", f"**Held until:** {self.blocked_until} — "
                         f"below n={MIN_N} any conclusion is noise."]
        return "\n".join(body)


def _load():
    """Everything the analysis reads. Missing sources degrade to empty."""
    import config_manager as cm
    uid = cm.DEFAULT_TEST_CHAT_ID
    out = {"uid": uid}
    try:
        out["log"] = cm.load_user_trade_log(uid) or {}
    except Exception:
        out["log"] = {}
    try:
        out["paper"] = cm.load_user_paper(uid) or {}
    except Exception:
        out["paper"] = {}
    # 🔴 The ledger is YEAR-SHARDED (pick_ledger_2026.json) and lives on the
    # Gist regardless of the storage backend — evaluate_picks manages its own
    # store. Reading "pick_ledger.json" alone sees only the legacy shard and
    # reports zero picks. Reuse the evaluator's loader rather than reimplement
    # it: a forked reader would silently diverge from what the report scores.
    out["rows"] = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "evaluate_picks",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "evaluate_picks.py"))
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        rows, _shard, _name = ev._load_ledger()
        out["rows"] = rows or []
    except Exception as exc:
        print(f"[analyze] ledger read failed ({exc}) — pick-based findings skipped")
    return out


def _levels_geometry(closed) -> Finding | None:
    """The bot fills mechanically at published levels, so its closed trades are
    the only direct read on whether the stop/target geometry is sane."""
    geo = []
    for t in closed:
        try:
            e, s, g = (float(t["entry_price"]), float(t["stop_loss"]),
                       float(t["target_price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if e > 0:
            geo.append(((e - s) / e * 100, (g - e) / e * 100))
    if len(geo) < 3:
        return None
    stops = [a for a, _ in geo]
    targs = [b for _, b in geo]
    ms, mt = statistics.median(stops), statistics.median(targs)
    rr = mt / ms if ms else 0
    return Finding(
        "MEASURE", "Stop/target geometry on filled positions",
        f"median stop {ms:.1f}% below entry, median target {mt:.1f}% above, "
        f"reward:risk {rr:.2f}:1 across {len(geo)} filled positions. "
        f"The walk-forward backtest measured real ledger picks at 10.3%/5.5% "
        f"= 1.9:1, so compare against that rather than config defaults.",
        "If R:R drifts materially below ~1.9:1, the stops are tightening "
        "relative to targets and will manufacture stop-outs. Route any change "
        "through scripts/backtest_compare.py before shipping — a backtest win "
        "is not permission to ship, but a backtest loss is a reason not to.",
        n=len(geo))


def _integrity(uid, log, paper) -> Finding | None:
    """Arithmetic that must hold for ANY long position. Binary — one is enough."""
    try:
        import position_audit
        findings = position_audit.audit_account(uid, log, paper)
    except Exception as exc:
        return Finding("MEASURE", "Position audit could not run",
                       f"{type(exc).__name__}: {exc}",
                       "Check position_audit imports; this is the integrity net.")
    live = [f for f in findings if f.get("live")]
    if not findings:
        return None
    kinds = {}
    for f in findings:
        kinds[f.get("check", "?")] = kinds.get(f.get("check", "?"), 0) + 1
    return Finding(
        "ACT" if live else "MEASURE",
        "Position integrity violations",
        f"{len(findings)} finding(s) ({len(live)} on LIVE positions): "
        + ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())),
        "A live violation means a position that cannot win or was born "
        "stopped-out. Fix the level; then check whether the generator can still "
        "emit it — a historical-only finding needs no code change.",
        n=len(findings))


def _reachability(rows, log, paper) -> list:
    """Could a user have acted at the price we published? Descriptive, so a few
    dozen fills already inform — unlike a win rate."""
    try:
        import actionability
        positions = ((log.get("open") or []) + (log.get("closed") or [])
                     + (paper.get("positions") or []) + (paper.get("history") or []))
        res = actionability.analyse(rows, positions, log.get("closed") or [])
    except Exception as exc:
        return [Finding("MEASURE", "Actionability could not run",
                        f"{type(exc).__name__}: {exc}", "Check actionability inputs.")]
    out = []
    entry = (res or {}).get("entry") or {}
    if entry.get("n"):
        breached, n = entry.get("outside_window", 0), entry["n"]
        ex = ", ".join(f"{e.get('ticker')} +{e.get('slippage_pct')}%"
                       for e in (entry.get("examples") or [])[:3])
        out.append(Finding(
            "ACT" if breached else "MEASURE",
            "Picks filling outside the published entry window",
            f"{breached} of {n} observations ({entry.get('outside_pct', 0)}%) "
            f"filled beyond the window the morning message promises; worst "
            f"{entry.get('worst_pct')}%." + (f" Examples: {ex}." if ex else "") +
            " The message says \"enter within X% — skip if above\", so a breach "
            "means a user who obeyed would have skipped a pick the bot bought.",
            "A TRUST defect, not a performance one — no win rate shows it. "
            "Either widen the published window to match reality, or warn on the "
            "gap. formatters.entry_window_pct is the ONE definition; do not "
            "re-hardcode 2 or 3.",
            n=n))

    stops = (res or {}).get("stops") or {}
    if stops.get("n"):
        tight = stops.get("tight", 0)
        ex = ", ".join(f"{e.get('ticker')} {e.get('stop_pct')}%"
                       for e in (stops.get("tight_examples") or [])[:3])
        out.append(Finding(
            "ACT" if tight else "MEASURE",
            "Stops tighter than the noise threshold",
            f"median stop {stops.get('median_stop_pct')}% below entry across "
            f"{stops['n']} positions; {tight} tighter than the "
            f"{stops.get('threshold_pct')}% threshold."
            + (f" {ex}." if ex else ""),
            "A stop inside ordinary daily noise converts a good thesis into a "
            "stop-out. Check whether the ATR-based level "
            "(screener.suggested_stop_pct = 1.5x ATR%) collapsed for a "
            "low-volatility name, and floor it if so.",
            n=stops["n"]))

    mix = ((res or {}).get("outcomes") or {}).get("counts") or {}
    if sum(mix.values()) >= 5:
        st, tg = mix.get("stop", 0), mix.get("target", 0)
        ratio = (f"stops hit {st/tg:.1f}x as often as targets" if tg
                 else "no target exits yet")
        # Segment by whether the levels came from the PICK or the fallback —
        # only the "pick" slice says anything about the engine's own levels.
        seg = _exit_mix_by_levels_source(log, paper)
        seg_txt = ""
        if seg:
            parts = [f"{k}: {v}" for k, v in sorted(seg.items())]
            seg_txt = ("  Segmented by levels source — " + " · ".join(parts) +
                       ". Only the `pick` slice speaks to the ENGINE's levels; "
                       "`stop`/`target`/`both` are the ±5%/8% fallback.")
        out.append(Finding(
            "MEASURE", "Exit-reason mix",
            f"{mix} — {ratio}.{seg_txt}",
            "Read the `pick` slice alone when judging the published levels. A "
            "high stop:target ratio THERE means the stops are too tight "
            "relative to targets; the same ratio in the fallback slice means "
            "the pick's levels did not bracket the fill, which is a different "
            "problem (levels drifting from the live price by delivery time).",
            n=sum(mix.values())))
    return out


def _exit_mix_by_levels_source(log, paper) -> dict:
    """Closed trades grouped by where their levels came from.

    Positions opened before 2026-08-23 carry no `levels_source`; they are
    reported as `unrecorded` rather than silently folded into `pick`, which
    would overstate what the engine's own levels have been measured on.
    """
    seg: dict = {}
    rows = (log.get("closed") or []) + (paper.get("history") or [])
    for t in rows:
        src = t.get("levels_source") or "unrecorded"
        seg[src] = seg.get(src, 0) + 1
    return seg


def _maturity(rows) -> Finding:
    """How close the real evidence base is to saying anything."""
    today = dt.date.today()
    matured = 0
    for r in rows:
        try:
            d = dt.date.fromisoformat(str(r.get("date"))[:10])
            if (today - d).days >= 30 and not r.get("control"):
                matured += 1
        except Exception:
            continue
    if matured >= MIN_N:
        return Finding(
            "MEASURE", "Pick ledger has cleared the honesty gate",
            f"{matured} matured picks (gate is {MIN_N}).",
            "Run scripts/evaluate_picks.py and read the picked-vs-control edge. "
            "This is the first evidence that can speak to SELECTION quality.",
            n=matured)
    return Finding(
        "HOLD", "Engine win-rate questions are not yet answerable",
        f"{matured} matured picks against a gate of {MIN_N}. The synthetic "
        f"bot's own record is deliberately excluded — it is a robot's "
        f"mechanical fills, and feeding it back steers real recommendations.",
        "Do not tune selection weights on anything below the gate. The "
        "descriptive findings above are safe to act on; win rate is not.",
        n=matured, blocked_until="the ledger reaches 30 matured picks")


def build(dry: bool = False) -> str:
    d = _load()
    closed = (d["log"].get("closed") or [])
    findings = [f for f in (
        _integrity(d["uid"], d["log"], d["paper"]),
        _levels_geometry(closed),
        _maturity(d["rows"]),
    ) if f]
    findings += _reachability(d["rows"], d["log"], d["paper"])
    order = {"ACT": 0, "MEASURE": 1, "HOLD": 2}
    findings.sort(key=lambda f: order.get(f.tier, 9))

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = [
        "# Engine findings — what to improve, and what the evidence supports",
        "",
        f"*Regenerated {stamp} by `scripts/analyze_engine.py`. "
        "Read this at the start of a session; it is the standing agenda.*",
        "",
        "**How to read the tiers**",
        "",
        "- **ACT** — actionable now. Binary or integrity findings where a single "
        "instance is enough to justify a change.",
        "- **MEASURE** — descriptive and informative at small n. Safe to reason "
        "about; confirm direction before changing weights.",
        f"- **HOLD** — needs n≥{MIN_N}. Acting earlier is tuning on noise.",
        "",
        "🔴 **The bot's win rate is never an input to engine changes.** Its "
        "trades are mechanical fills; in July they were steering real picks and "
        "that loop was cut. What it measures well is levels, reachability and "
        "integrity — not selection quality.",
        "",
        "---",
        "",
    ]
    body = "\n\n".join(f.render() for f in findings) or "_No findings._"
    doc = "\n".join(head) + body + "\n"

    if not dry:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as fh:
            fh.write(doc)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    doc = build(dry=args.dry_run)
    print(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
