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



# What still DEMANDS a decision. `acknowledged`/`wont_fix` are decisions
# already made — keeping them in the worklist is the cry-wolf failure that
# trains you to skim it.
# `awaiting_approval` stays in the worklist: it is waiting on the OWNER, so it
# is exactly what should be surfaced at sign-in. `approved` leaves it — the
# decision is made and it is mine to implement.
WORKLIST_STATUSES = ("open", "awaiting_approval")
# What is still PRESENT in some form, and so should be auto-resolved the day
# its condition disappears — including things you chose to live with.
ACTIVE_STATUSES = ("open", "acknowledged", "wont_fix",
                   "awaiting_approval", "approved")


class Finding:
    """One output of the analysis.

    kind="finding"  addressable — it can be fixed and marked complete
    kind="metric"   an ongoing measurement — never "done", so never dispositioned

    That split matters: "address every finding" is impossible if a standing
    measurement like reward:risk sits in the same list as a specific breach.

    `fix` must name the FILE and the CHANGE. "Consider reviewing" is not a fix
    and cannot be actioned at sign-in.
    """

    CATEGORIES = ("bug", "engine")

    def __init__(self, fid, tier, title, evidence, fix, n=None,
                 kind="finding", blocked_until=None, where="",
                 category=None, plain=""):
        """`category` splits the two things the owner is being asked to judge:

          "bug"     a technical defect. One instance is enough to act on; the
                    fix restores intended behaviour and changes no strategy.
          "engine"  a change to HOW picks are chosen or levelled. It alters
                    what real users are told to buy, so it needs OUTCOME
                    evidence accumulated over time — the n>=30 honesty gate —
                    and never the synthetic bot's win rate.

        🔴 The default is "engine" on purpose. A bug mislabelled as an engine
        change costs a moment's extra scrutiny; an engine change mislabelled as
        a bug gets waved through and silently alters everyone's picks. Fail
        toward the answer that demands a closer look.
        """
        # 🔴 REQUIRED for an addressable finding — there is no default.
        # A default does not demand a closer look, it INVENTS a classification
        # the author never made, and the /admin card then states it as fact.
        # Metrics are not classified: they are measurements, not decisions.
        if kind == "finding" and category not in self.CATEGORIES:
            raise ValueError(
                f"finding {fid!r} needs an explicit category "
                f"{self.CATEGORIES} — a bug and a change to how picks are "
                f"chosen carry different evidence bars")
        self.id = fid
        self.category = category
        self.plain = plain
        self.kind = kind
        self.tier = tier
        self.title = title
        self.evidence = evidence
        self.fix = fix
        self.where = where
        self.n = n
        self.blocked_until = blocked_until
        self.status = "open"
        self.note = ""
        self.first_seen = None
        self.reopened = False

    @property
    def age_days(self) -> int:
        if not self.first_seen:
            return 0
        try:
            return (dt.date.today() - dt.date.fromisoformat(self.first_seen)).days
        except Exception:
            return 0

    CATEGORY_LABEL = {"bug": "TECHNICAL BUG",
                      "engine": "DECISION-ENGINE CHANGE"}

    def render(self) -> str:
        # Tier stays FIRST: an existing guard parses it from this header to
        # assert ACT findings are never buried below a HOLD. The category
        # reads just as clearly in second position, and it must be there — a
        # technical bug and a change to how picks are chosen are different
        # decisions with different evidence bars.
        tag = (self.CATEGORY_LABEL[self.category] if self.kind == "finding"
               else "METRIC")
        head = f"### [{self.tier}] [{tag}] {self.title}"
        if self.n is not None:
            head += f"  *(n={self.n})*"
        L = [head, ""]
        if self.kind == "finding":
            age = f", open {self.age_days}d" if self.age_days else ""
            flag = "  \U0001F501 **REOPENED**" if self.reopened else ""
            L += [f"`{self.id}` · **{self.status}**{age}{flag}", ""]
            if self.note:
                L += [f"> {self.note}", ""]
        # 🔴 The PLAIN sentence leads. The /admin card learned this the hard
        # way: leading with engineer-facing text made the proposal unreadable,
        # and a change you cannot read is one you can only rubber-stamp. The
        # same file is the agenda read at session start, so the technical text
        # is DEMOTED, never dropped — raw markdown still carries all of it.
        if self.plain:
            L += [self.plain, ""]
        # The evidence bar stays ABOVE the fold: it is a caution, not detail.
        if self.kind == "finding" and self.category == "engine":
            gate = (f"n={self.n}" if self.n is not None else "no outcome sample")
            L += [f"**This changes what users are recommended.** Judge it on "
                  f"outcomes over time ({gate}; {MIN_N} needed to be "
                  f"conclusive), never on the synthetic bot's win rate.", ""]
        elif self.kind == "finding" and self.category == "bug":
            L += ["**Technical bug** — fixes broken behaviour; changes nothing "
                  "about how picks are chosen.", ""]
        if self.plain:
            L += ["<details><summary>Technical detail</summary>", ""]
        L += [f"**Evidence:** {self.evidence}", "", f"**Fix:** {self.fix}"]
        if self.blocked_until:
            L += ["", f"**Held until:** {self.blocked_until} — below n={MIN_N} "
                      f"any conclusion is noise."]
        if self.reopened:
            L += ["", "\U0001F501 Marked fixed previously but the condition is "
                      "STILL PRESENT, so it is reopened. 'Fixed' must never "
                      "mean 'hidden'."]
        if self.plain:
            L += ["", "</details>"]
        return "\n".join(L)


def _load_state() -> dict:
    """🔴 From STORAGE, not the repo. Render's filesystem is ephemeral, so an
    approval made on /admin would be lost on the next deploy and this job would
    never see it. The generated REPORT stays in the repo; the DECISIONS do not."""
    try:
        from config_manager import get_finding_dispositions
        return get_finding_dispositions()
    except Exception as exc:
        print(f"[analyze] disposition store unreadable ({exc}) — treating as empty")
        return {}


def _save_state(state: dict) -> None:
    try:
        from config_manager import set_finding_disposition
        for fid, rec in state.items():
            rec = dict(rec)
            status = rec.pop("status", "open")
            set_finding_disposition(fid, status, rec.pop("note", ""), extra=rec)
    except Exception as exc:
        print(f"[analyze] could not persist dispositions ({exc})")


def _apply_state(findings: list, state: dict, today: str) -> dict:
    """Overlay saved dispositions; reopen anything marked fixed that is back.

    Borrowed wholesale from position_audit.apply_dispositions, which learned
    this the hard way: a finding marked resolved and still present means the
    defect is live and the mark is hiding it.
    """
    seen = set()
    for f in findings:
        if f.kind != "finding":
            continue
        seen.add(f.id)
        rec = state.get(f.id) or {}
        f.first_seen = rec.get("first_seen") or today
        f.note = rec.get("note", "")
        prior = rec.get("status", "open")
        if prior == "fixed":
            f.status, f.reopened = "open", True      # still here => not fixed
        else:
            f.status = prior
        # 🔴 UPDATE, never replace. A replace dropped proposed_change /
        # proposed_summary / proposed_files, so the night after I proposed a fix
        # the card would render "(no description recorded)" and the owner could
        # not see what they were being asked to approve.
        rec.update({"status": f.status, "note": f.note,
                    "first_seen": f.first_seen, "last_seen": today,
                    "title": f.title, "category": f.category, "n": f.n})
        state[f.id] = rec
    # A finding that has DISAPPEARED is genuinely resolved — record it once.
    for fid, rec in state.items():
        if fid in seen or rec.get("status") not in ACTIVE_STATUSES:
            continue
        # 🚨 The invariant. A finding whose condition DISAPPEARED while it was
        # still awaiting the owner's consent was implemented without it. This
        # cannot prevent that — nothing can stop an agent editing a file — but
        # it makes it DETECTABLE, which turns an unenforceable promise into a
        # checkable one. `resolved_UNAPPROVED` is deliberately never cleared
        # automatically; it stays until a human looks at it.
        if rec.get("status") == "awaiting_approval":
            rec["status"] = "resolved_UNAPPROVED"
        else:
            rec["status"] = "resolved"
        rec["resolved_on"] = today
    return state


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


def _levels_geometry(closed) -> list:
    geo = []
    for t in closed:
        try:
            e, st, g = (float(t["entry_price"]), float(t["stop_loss"]),
                        float(t["target_price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if e > 0:
            geo.append(((e - st) / e * 100, (g - e) / e * 100))
    if len(geo) < 3:
        return []
    ms = statistics.median([a for a, _ in geo])
    mt = statistics.median([b for _, b in geo])
    rr = mt / ms if ms else 0
    return [Finding(
        "metric:levels_geometry", "MEASURE",
        "Stop/target geometry on filled positions",
        f"median stop {ms:.1f}% below entry, median target {mt:.1f}% above, "
        f"reward:risk {rr:.2f}:1 across {len(geo)} filled positions. The "
        f"walk-forward backtest measured real ledger picks at 10.3%/5.5% = "
        f"1.9:1 — compare against that, never config defaults.",
        "No action while R:R stays near 1.9:1. If it drifts materially below, "
        "the stops are tightening relative to targets and will manufacture "
        "stop-outs — route any change through scripts/backtest_compare.py "
        "first.",
        n=len(geo), kind="metric")]


def _integrity(uid, log, paper) -> list:
    try:
        import position_audit
        raw = position_audit.audit_account(uid, log, paper)
    except Exception as exc:
        return [Finding("metric:audit_broken", "MEASURE",
                        "Position audit could not run",
                        f"{type(exc).__name__}: {exc}",
                        "Check position_audit imports — this is the integrity net.",
                        kind="metric")]
    out = []
    for f in raw:
        tk = f.get("ticker", "?")
        chk = f.get("check", "?")
        live = f.get("live")
        # 🔴 Use position_audit's OWN id. It hashes (check, account, ticker,
        # date, entry, target, stop, shares) precisely because the bot scales
        # into the same ticker twice in a day — a coarser id collapses two
        # distinct broken positions into one row, and resolving one silently
        # resolves the other. Building a second id scheme here reintroduces the
        # exact bug that file already fixed.
        out.append(Finding(
            f"integrity/{f.get('id') or (chk + '/' + tk)}",
            "ACT" if live else "MEASURE",
            f"{chk} on {tk}" + ("" if live else " (historical)"),
            f.get("detail") or f"{chk} violation on {tk}"
            + (" — LIVE position" if live else " — closed trade, not fixable retroactively"),
            ("Fix the level on the live position, then check whether the "
             "GENERATOR can still emit it — ai_analyzer._validate_and_clean_picks "
             "is where a target below entry should be rejected before delivery."
             if live else
             "Historical: acknowledge it. It cannot be fixed retroactively. "
             "Worth confirming ai_analyzer._validate_and_clean_picks now rejects "
             "the shape so it cannot recur."),
            where="ai_analyzer._validate_and_clean_picks", category="bug",
            plain=(f"A {tk} position has levels that cannot work: "
                   + ("it is live, so it will behave wrongly until the levels "
                      "are corrected." if live else
                      "the trade is already closed, so this is a record of "
                      "what shipped, not something fixable now."))))
    return out


def _reachability(rows, log, paper) -> list:
    try:
        import actionability
        positions = ((log.get("open") or []) + (log.get("closed") or [])
                     + (paper.get("positions") or []) + (paper.get("history") or []))
        res = actionability.analyse(rows, positions, log.get("closed") or [])
    except Exception as exc:
        return [Finding("metric:actionability_broken", "MEASURE",
                        "Actionability could not run", f"{type(exc).__name__}: {exc}",
                        "Check actionability inputs.", kind="metric")]
    out = []

    entry = (res or {}).get("entry") or {}
    for ex in (entry.get("examples") or []):
        tk, slip = ex.get("ticker", "?"), ex.get("slippage_pct")
        out.append(Finding(
            f"entry_window/{tk}/{ex.get('date', '?')}",
            "ACT", f"{tk} filled {slip}% outside the published entry window",
            f"The morning message promises \"enter within X% — skip if above\". "
            f"{tk} filled {slip}% above, so a user who OBEYED the instruction "
            f"would have skipped a pick the bot bought. "
            f"{entry.get('outside_window', 0)} of {entry.get('n', 0)} "
            f"observations breach ({entry.get('outside_pct', 0)}%).",
            "This is a TRUST defect, not a performance one. Either widen the "
            "published window in formatters.entry_window_pct to match measured "
            "reality, or make agent._build_premarket_gap_warnings warn on the "
            "gap. Do NOT re-hardcode 2 or 3 — that constant is the ONE "
            "definition and it has drifted before.",
            n=entry.get("n"),
            where="formatters.entry_window_pct / agent._build_premarket_gap_warnings",
            category="bug",
            plain=(f"{tk} was bought {slip}% above the price the morning "
                   f"message told people not to go past, so anyone who "
                   f"followed that instruction would have skipped a pick the "
                   f"bot itself took.")))

    stops = (res or {}).get("stops") or {}
    for ex in (stops.get("tight_examples") or []):
        tk, pct = ex.get("ticker", "?"), ex.get("stop_pct")
        out.append(Finding(
            f"stop_tight/{tk}/{pct}", "ACT",
            f"{tk} stop at {pct}% is inside the noise threshold",
            f"Threshold is {stops.get('threshold_pct')}%; median across "
            f"{stops.get('n')} positions is {stops.get('median_stop_pct')}%. A "
            f"stop inside ordinary daily movement converts a sound thesis into "
            f"a stop-out.",
            "screener.suggested_stop_pct is 1.5x ATR%, which collapses for a "
            "low-volatility name. Add a floor there (or in "
            "ai_analyzer._ST_SECTIONS' 5% fallback) so no published stop sits "
            "below the noise threshold.",
            n=stops.get("n"),
            where="screener.suggested_stop_pct (1.5x ATR%) — add a floor",
            category="engine",
            plain=(f"{tk}'s sell-stop sits only {pct}% below the buy price — "
                   f"inside the range this stock moves on an ordinary day — so "
                   f"a normal wobble would sell a position that was fine.")))
    if stops.get("n"):
        out.append(Finding(
            "metric:stop_distance", "MEASURE", "Stop distance distribution",
            f"median {stops.get('median_stop_pct')}% across {stops['n']} "
            f"positions; {stops.get('tight', 0)} below the "
            f"{stops.get('threshold_pct')}% threshold.",
            "Context for the geometry metric — no action on its own.",
            n=stops["n"], kind="metric"))

    mix = ((res or {}).get("outcomes") or {}).get("counts") or {}
    if sum(mix.values()) >= 5:
        st, tg = mix.get("stop", 0), mix.get("target", 0)
        seg = _exit_mix_by_levels_source(log, paper)
        segtxt = ("  By levels source — "
                  + " · ".join(f"{k}: {v}" for k, v in sorted(seg.items()))
                  + ". Only `pick` speaks to the ENGINE's levels."
                  if seg else "")
        out.append(Finding(
            "metric:exit_mix", "MEASURE", "Exit-reason mix",
            f"{mix} — " + (f"stops hit {st/tg:.1f}x as often as targets."
                           if tg else "no target exits yet.") + segtxt,
            "Judge the published levels on the `pick` slice ALONE. A high "
            "stop:target ratio there means stops are too tight; the same ratio "
            "in the fallback slice means the pick's levels did not bracket the "
            "fill — levels drifting from the live price by delivery time, which "
            "is a different fix.",
            n=sum(mix.values()), kind="metric"))
    return out


def _exit_mix_by_levels_source(log, paper) -> dict:
    """Closed trades grouped by where their levels came from.

    Trades opened before 2026-08-23 carry no `levels_source` and report as
    `unrecorded` — never folded into `pick`, which would overstate what the
    engine's own levels have actually been measured on.
    """
    seg: dict = {}
    for t in (log.get("closed") or []) + (paper.get("history") or []):
        k = t.get("levels_source") or "unrecorded"
        seg[k] = seg.get(k, 0) + 1
    return seg


def _maturity(rows) -> list:
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
        return [Finding(
            "metric:maturity", "MEASURE",
            "Pick ledger has cleared the honesty gate",
            f"{matured} matured picks (gate {MIN_N}).",
            "Run scripts/evaluate_picks.py and read the picked-vs-control edge "
            "— the first evidence that can speak to SELECTION quality.",
            n=matured, kind="metric")]
    return [Finding(
        "metric:maturity", "HOLD",
        "Engine win-rate questions are not yet answerable",
        f"{matured} matured picks against a gate of {MIN_N}. The synthetic "
        f"bot's own record is deliberately excluded — mechanical fills, and "
        f"feeding them back steers real recommendations.",
        "Do not tune selection weights below the gate. The findings above are "
        "safe to act on; win rate is not.",
        n=matured, kind="metric",
        blocked_until="the ledger reaches 30 matured picks (~Sep 10)")]


def _notify_new_act(findings: list, state: dict, today: str) -> int:
    """DM the owner about ACT findings they have never been told about.

    🔴 Deliberately narrow. NOT a daily digest, NOT MEASURE/HOLD, NOT a re-send
    of anything already notified or already ruled on. Two monitors cried wolf
    on 2026-08-22 (`weekly.on_github` every run, `data.completeness` every
    weekend) and the lesson was the same both times: an alert that fires when
    nothing is wrong trains you to ignore the one time it matters.

    Exactly-once is tracked by `notified_on` in the state file rather than by
    "first seen today", so a second run on the same day cannot re-send.
    """
    fresh = [f for f in findings
             if f.kind == "finding" and f.tier == "ACT" and f.status == "open"
             and not (state.get(f.id) or {}).get("notified_on")]
    if not fresh:
        return 0
    lines = [f"🔎 *{len(fresh)} new engine finding(s) need a decision*", ""]
    for f in fresh:
        lines += [f"*{f.title}*", f"  {f.evidence[:220]}",
                  f"  → fix in `{f.where or 'see the report'}`", ""]
    lines.append("Full detail in `analysis/ENGINE_FINDINGS.md`. "
                 "Nothing will be changed until you say so.")
    try:
        from telegram_api import send_message
        send_message("\n".join(lines), chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""))
    except Exception as exc:
        print(f"[analyze] notify failed (non-critical): {exc}")
        return 0
    for f in fresh:
        state.setdefault(f.id, {})["notified_on"] = today
    print(f"[analyze] notified owner of {len(fresh)} new ACT finding(s).")
    return len(fresh)


def _notify_awaiting_approval(findings: list, state: dict, today: str) -> int:
    """DM the owner when a finding is PROPOSED and now needs their decision.

    🔴 The hole this closes: `_notify_new_act` filters on status == "open", so
    the moment I proposed a fix the finding became `awaiting_approval` and
    dropped out of the notification path entirely. Proposing a change actively
    REMOVED it from the only channel that reaches the owner — the one state
    that exists to ask them something was the one state that never asked.
    That is why the Approve/Decline buttons went unseen until 2026-08-23.

    Tracked by its OWN key (`proposed_notified_on`), not `notified_on`: a
    finding may legitimately be announced twice — once as a new ACT finding,
    once when a concrete fix is proposed for it. Sharing the key would silence
    the second, which is the one carrying a decision.
    """
    fresh = [f for f in findings
             if f.kind == "finding" and f.status == "awaiting_approval"
             and not (state.get(f.id) or {}).get("proposed_notified_on")]
    if not fresh:
        return 0
    lines = [f"🔔 *{len(fresh)} change(s) awaiting your approval*", ""]
    for f in fresh:
        rec = state.get(f.id) or {}
        lines += [f"*{rec.get('proposed_summary') or f.title}*",
                  f"  files: `{rec.get('proposed_files', '?')}`", ""]
    lines.append("Approve or decline on /admin — Engine findings. "
                 "Approving lets me write the code; it does NOT deploy anything.")
    try:
        from telegram_api import send_message
        send_message("\n".join(lines), chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""))
    except Exception as exc:
        # A failed send must NOT mark them notified, or the ask vanishes.
        print(f"[analyze] approval notify failed (non-critical): {exc}")
        return 0
    for f in fresh:
        state.setdefault(f.id, {})["proposed_notified_on"] = today
    print(f"[analyze] notified owner of {len(fresh)} finding(s) awaiting approval.")
    return len(fresh)


def build(dry: bool = False, notify: bool = False) -> str:
    d = _load()
    closed = d["log"].get("closed") or []
    items = (_integrity(d["uid"], d["log"], d["paper"])
             + _reachability(d["rows"], d["log"], d["paper"])
             + _levels_geometry(closed)
             + _maturity(d["rows"]))

    today = dt.date.today().isoformat()
    state = _load_state()
    state = _apply_state(items, state, today)

    rank = {"ACT": 0, "MEASURE": 1, "HOLD": 2}
    findings = [f for f in items if f.kind == "finding"]
    metrics = [f for f in items if f.kind == "metric"]
    findings.sort(key=lambda f: (f.status != "open", rank.get(f.tier, 9), -f.age_days))
    metrics.sort(key=lambda f: rank.get(f.tier, 9))

    todo = [f for f in findings if f.status in WORKLIST_STATUSES]
    decided = [f for f in findings if f.status in ("acknowledged", "wont_fix")]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    L = [
        "# Engine findings — the standing agenda",
        "",
        f"*Regenerated {stamp} by `scripts/analyze_engine.py`. "
        "Read this at the START of a session and work the open findings.*",
        "",
        f"## Open: {len(todo)} finding(s) needing a decision",
        "",
    ]
    if todo:
        L.append("| id | what | age | fix in |")
        L.append("|---|---|---|---|")
        for f in todo:
            L.append(f"| `{f.id}` | {f.title} | {f.age_days}d | {f.where or '—'} |")
    else:
        L.append("_Nothing open. Every addressable finding has been resolved._")
    violations = [fid for fid, r in state.items()
                  if r.get("status") == "resolved_UNAPPROVED"]
    if violations:
        L += ["", "## 🚨 IMPLEMENTED WITHOUT APPROVAL", "",
              "*These conditions disappeared while still awaiting your consent — "
              "the change was made without it. This is a record, not a rollback.*", ""]
        L += [f"- `{v}` — resolved {state[v].get('resolved_on')}" for v in violations]
        L.append("")

    awaiting = [f for f in findings if f.status == "awaiting_approval"]
    if awaiting:
        L += ["", f"### ⏳ Awaiting YOUR approval ({len(awaiting)})", ""]
        for f in awaiting:
            rec = state.get(f.id) or {}
            L.append(f"- `{f.id}` — {f.title}\n"
                     f"  - **proposed change:** {rec.get('proposed_change', '?')}\n"
                     f"  - **files:** `{rec.get('proposed_files', '?')}`\n"
                     f"  - approve with: `python3 scripts/findings.py approve {f.id}`")
        L.append("")

    if decided:
        L += ["", f"### Decided, still present ({len(decided)})", "",
              "*You have already ruled on these — they are not in the worklist. "
              "They will clear themselves the day the condition disappears.*", ""]
        for f in decided:
            L.append(f"- `{f.id}` — **{f.status}** — {f.title}"
                     + (f" · _{f.note}_" if f.note else ""))
    L += [
        "",
        "**To close one:** implement the fix, or record a decision on the "
        "**/admin** dashboard (Engine findings card) or via "
        "`scripts/findings.py` "
        "(`status`: `acknowledged` | `wont_fix`, plus a `note` saying why). "
        "A finding whose condition DISAPPEARS is marked `resolved` "
        "automatically — that is the intended path.",
        "",
        "🔁 **A finding marked `fixed` that is still present is REOPENED.** "
        "Otherwise 'fixed' silently means 'hidden' while the defect is live.",
        "",
        "🔴 **Engine changes are never recommended from the bot's win rate.** "
        "Mechanical fills; in July they were steering real picks and that loop "
        f"was cut. Anything needing n≥{MIN_N} is HELD with its clearing date.",
        "",
        "---",
        "",
        "## Findings",
        "",
    ]
    L.append("\n\n".join(f.render() for f in findings) or "_None._")
    L += ["", "---", "", "## Metrics (ongoing — never 'complete')", ""]
    L.append("\n\n".join(f.render() for f in metrics) or "_None._")

    doc = "\n".join(L) + "\n"
    if not dry and notify:
        _notify_new_act(findings, state, today)
        # Separate call, separate state key: a finding can legitimately be
        # announced as a new ACT finding AND later as a proposal awaiting a
        # decision. Folding these together would silence the second.
        _notify_awaiting_approval(findings, state, today)
    if not dry:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as fh:
            fh.write(doc)
        _save_state(state)
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify", action="store_true",
                    help="DM the owner about NEW ACT findings (CI uses this)")
    args = ap.parse_args()
    doc = build(dry=args.dry_run, notify=args.notify)
    print(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
