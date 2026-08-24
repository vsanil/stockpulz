#!/usr/bin/env python3
"""Prune legacy junk keys from price_alerts.json.

Approved by the owner on 2026-08-23 as finding `storage/price_alerts_junk_keys`.

WHAT IT REMOVES, and only this:
  1. Recursion artifacts — keys where `_history_` appears more than once
     (`_history__history_123`, `_history__history__history_123`). These came
     from an old recursion bug whose iteration site is already guarded, so they
     are carried-over garbage, not a live defect.
  2. Named synthetic accounts that are NOT real users — currently just
     `999000999`, canary residue from before the restore was made row-aware.

WHAT IT WILL NEVER TOUCH:
  • any chat_id returned by `get_allowed_users()` — checked at runtime, and the
    script REFUSES to run at all if that list cannot be read. Guessing "this id
    looks synthetic" is exactly how a real user's alerts get deleted.
  • `_history_<real_uid>` — that is a legitimate per-user history key.
  • the canonical test account (`DEFAULT_TEST_CHAT_ID`), which is real storage
    for the synthetic bot.

⚠️ Not wired to any workflow (it has run; the row-aware canary restore now
prevents the residue that made it necessary). If it is ever needed again, run
it WHERE SUPABASE_* IS SET — from a local shell get_storage_backend() returns
the Gist, which is the rollback copy, not what the app reads.

Dry run by default. `--apply` writes.

    python3 scripts/prune_alert_junk.py            # show what would go
    python3 scripts/prune_alert_junk.py --apply    # actually delete
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FILE = "price_alerts.json"

# Deliberately an explicit list, not a heuristic. An id that merely "looks
# synthetic" may be a real user who has not traded yet.
KNOWN_SYNTHETIC = {"999000999"}


def _is_recursion_artifact(key: str) -> bool:
    """`_history_` appearing more than once means the history key was itself
    re-wrapped — the signature of the old recursion bug."""
    return key.count("_history_") > 1


def classify(keys, protected: set) -> tuple[list, list]:
    """Return (junk, kept). Protected keys are never junk, whatever they look
    like — that check comes LAST so no rule can override it."""
    junk = []
    for k in keys:
        k = str(k)
        if k in protected:
            continue
        if _is_recursion_artifact(k) or k in KNOWN_SYNTHETIC:
            junk.append(k)
    return sorted(junk), sorted(set(map(str, keys)) - set(junk))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    args = ap.parse_args()

    from storage import get_storage_backend
    from config_manager import get_allowed_users, DEFAULT_TEST_CHAT_ID

    backend = get_storage_backend()
    print(f"  backend: {backend.name()}")

    # Fail CLOSED. If we cannot establish who the real users are we must not
    # delete anything — the whole safety of this script rests on that list.
    try:
        real = set(map(str, get_allowed_users() or []))
    except Exception as exc:
        print(f"  🚨 REFUSING TO RUN: cannot read the allowed-user list ({exc}).")
        return 2
    if not real:
        print("  🚨 REFUSING TO RUN: the allowed-user list came back EMPTY. "
              "Every key would look unprotected.")
        return 2

    protected = set(real) | {str(DEFAULT_TEST_CHAT_ID)}
    protected |= {f"_history_{u}" for u in protected}
    print(f"  protected: {len(protected)} key(s) — real users, the test account, "
          f"and their _history_ keys")

    rows = backend.supports_rows()
    data = backend.read_all_users(FILE) if rows else (backend.read(FILE) or {})
    junk, kept = classify(list(data or {}), protected)

    print(f"\n  {FILE}: {len(data or {})} key(s) present")
    for k in kept:
        n = len(data.get(k) or []) if isinstance(data.get(k), list) else "—"
        print(f"    keep   {k:<40} {n} entr(y/ies)")
    for k in junk:
        v = data.get(k)
        n = len(v) if isinstance(v, list) else "—"
        why = "recursion artifact" if _is_recursion_artifact(k) else "known synthetic account"
        print(f"    DELETE {k:<40} {n} entr(y/ies)  ({why})")

    if not junk:
        print("\n  nothing to prune.")
        return 0

    if not args.apply:
        print(f"\n  (dry run — {len(junk)} key(s) would be deleted; nothing written)")
        print("  re-run with --apply to write.")
        return 0

    print("\n  deleting…")
    failed = []
    if rows:
        for k in junk:
            try:
                if not backend.delete_user(FILE, k):
                    failed.append(f"{k} (backend cannot delete rows)")
                else:
                    print(f"    ✓ {k}")
            except Exception as exc:
                failed.append(f"{k} ({exc})")
    else:
        backend.write(FILE, {k: v for k, v in (data or {}).items()
                             if str(k) not in set(junk)})
        print(f"    ✓ rewrote {FILE} without {len(junk)} key(s)")

    # Read back. A "deleted N" line is not evidence of deletion.
    after = backend.read_all_users(FILE) if rows else (backend.read(FILE) or {})
    still = [k for k in junk if k in (after or {})]
    for k in protected:
        if k in (data or {}) and k not in (after or {}):
            failed.append(f"🚨 PROTECTED KEY {k} DISAPPEARED")
    if still:
        failed.append(f"still present after delete: {', '.join(still)}")
    if failed:
        print("\n  🚨 FAILED: " + "; ".join(failed))
        return 1
    print(f"\n  ✅ pruned {len(junk)} key(s); {len(after or {})} remain, "
          f"all protected keys intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
