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

STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "analysis", "findings_state.json")

AWAITING = "awaiting_approval"
APPROVED = "approved"
VIOLATION = "resolved_UNAPPROVED"


def _load() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def _today() -> str:
    return dt.date.today().isoformat()


def propose(fid: str, change: str, files: str) -> int:
    """Record WHAT I intend to change, before changing it."""
    st = _load()
    rec = st.setdefault(fid, {})
    if rec.get("status") == APPROVED:
        print(f"  {fid} is already approved — implement it, do not re-propose.")
        return 1
    rec.update({"status": AWAITING, "proposed_on": _today(),
                "proposed_change": change, "proposed_files": files})
    rec.pop("approved_on", None)          # a changed proposal needs fresh consent
    rec.pop("approved_note", None)
    _save(st)
    print(f"  ⏳ {fid} → awaiting your approval\n     change: {change}\n     files : {files}")
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
    a = sub.add_parser("approve"); a.add_argument("id"); a.add_argument("--note", default="")
    r = sub.add_parser("reject");  r.add_argument("id"); r.add_argument("--note", required=True)
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "propose":
        return propose(args.id, args.change, args.files)
    if args.cmd == "approve":
        return approve(args.id, args.note)
    if args.cmd == "reject":
        return reject(args.id, args.note)
    return status()


if __name__ == "__main__":
    sys.exit(main())
