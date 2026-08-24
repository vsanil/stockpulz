#!/usr/bin/env python3
"""Approval workflow for engine findings.

    propose <id> --change "..." --files "a.py,b.py"   I describe the fix
    approve <id> [--note "..."]                       you sanction it
    reject  <id> --note "..."                         you decline it
    status                                            what is waiting on whom

🔴 What this IS and IS NOT. It is a durable RECORD and an AUDIT — not a lock.
Nothing can technically stop an agent editing a file. What it can do is make
"implemented without approval" DETECTABLE: a finding that disappears while still
`awaiting_approval` never passed through `approved`, so the daily job flags it
as UNAPPROVED and the guard test fails. An unenforceable promise becomes a
checkable invariant.

Lifecycle:
    open → propose → awaiting_approval → approve → approved → (implement)
                                                              → resolved
    Anything reaching `resolved` from `awaiting_approval` is a VIOLATION.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


AWAITING = "awaiting_approval"
APPROVED = "approved"
VIOLATION = "resolved_UNAPPROVED"


def _load() -> dict:
    from config_manager import get_finding_dispositions
    return get_finding_dispositions()


def _save(state: dict) -> None:
    """Persist through the storage backend so /admin and CI see the same store."""
    from config_manager import set_finding_disposition
    for fid, rec in state.items():
        rec = dict(rec)
        status = rec.pop("status", "open")
        set_finding_disposition(fid, status, rec.pop("note", ""), extra=rec)


def _today() -> str:
    return dt.date.today().isoformat()


def propose(fid: str, change: str, files: str, summary: str = "") -> int:
    """Record WHAT I intend to change, before changing it.

    🔴 `summary` is the PLAIN-ENGLISH sentence the owner actually reads on
    /admin. The technical `change` is written for an engineer — it names
    functions, files and bug classes — and the first proposal rendered that
    text as the whole card. The owner's response was "does this say findings in
    simple wordings?", which is the correct reaction: the entire argument for
    approving on a dashboard rather than a Telegram button was that the change
    could be READ before consenting. An unreadable proposal makes the review
    theatre, which is the failure the dashboard exists to avoid.
    """
    st = _load()
    rec = st.setdefault(fid, {})
    if rec.get("status") == APPROVED:
        print(f"  {fid} is already approved — implement it, do not re-propose.")
        return 1
    rec.update({"status": AWAITING, "proposed_on": _today(),
                "proposed_change": change, "proposed_files": files,
                "proposed_summary": summary})
    rec.pop("approved_on", None)          # a changed proposal needs fresh consent
    rec.pop("approved_note", None)
    _save(st)
    print(f"  ⏳ {fid} → awaiting your approval\n     plain : {summary or '(none — the card will fall back to the technical text)'}\n     change: {change}\n     files : {files}")
    return 0


def approve(fid: str, note: str = "") -> int:
    st = _load()
    rec = st.get(fid)
    if not rec:
        print(f"  unknown finding: {fid}")
        return 1
    if rec.get("status") != AWAITING:
        print(f"  {fid} is '{rec.get('status')}', not {AWAITING} — propose it first.")
        return 1
    rec.update({"status": APPROVED, "approved_on": _today(), "approved_note": note})
    _save(st)
    print(f"  ✅ {fid} approved — cleared to implement and deploy.")
    return 0


def reject(fid: str, note: str) -> int:
    st = _load()
    rec = st.get(fid)
    if not rec:
        print(f"  unknown finding: {fid}")
        return 1
    rec.update({"status": "wont_fix", "note": note, "rejected_on": _today()})
    rec.pop("approved_on", None)
    _save(st)
    print(f"  🚫 {fid} declined — stays visible, off the worklist.")
    return 0


def status() -> int:
    st = _load()
    waiting = {k: v for k, v in st.items() if v.get("status") == AWAITING}
    ready = {k: v for k, v in st.items() if v.get("status") == APPROVED}
    bad = {k: v for k, v in st.items() if v.get("status") == VIOLATION}

    print(f"\n  ⏳ Awaiting YOUR approval ({len(waiting)})")
    for k, v in sorted(waiting.items()):
        print(f"     {k}\n       change: {v.get('proposed_change', '?')}"
              f"\n       files : {v.get('proposed_files', '?')}")
    print(f"\n  ✅ Approved, cleared to implement ({len(ready)})")
    for k, v in sorted(ready.items()):
        print(f"     {k}  (approved {v.get('approved_on')})")
    if bad:
        print(f"\n  🚨 IMPLEMENTED WITHOUT APPROVAL ({len(bad)})")
        for k, v in sorted(bad.items()):
            print(f"     {k}  — resolved on {v.get('resolved_on')} while still awaiting consent")
    print()
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose"); p.add_argument("id")
    p.add_argument("--change", required=True); p.add_argument("--files", default="")
    p.add_argument("--summary", default="",
                   help="ONE plain-English sentence the owner reads on /admin. "
                        "No function names, no file paths, no jargon.")
    a = sub.add_parser("approve"); a.add_argument("id"); a.add_argument("--note", default="")
    r = sub.add_parser("reject");  r.add_argument("id"); r.add_argument("--note", required=True)
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "propose":
        return propose(args.id, args.change, args.files, args.summary)
    if args.cmd == "approve":
        return approve(args.id, args.note)
    if args.cmd == "reject":
        return reject(args.id, args.note)
    return status()


if __name__ == "__main__":
    sys.exit(main())
