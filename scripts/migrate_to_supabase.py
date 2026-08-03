#!/usr/bin/env python3
"""
migrate_to_supabase.py — copy every Gist file into Supabase, then VERIFY.

Why: the Gist can only rewrite a whole file, so saving one user rewrites the blob
every other user shares. That caused two real data-loss classes (cross-user lost
update via read-after-write lag; same-user cross-process clobber) and the Gist API
has no compare-and-swap to fix it — verified: PATCH rejects If-Match with 400.
Supabase gives one row per user plus a version token, which removes the class.

Layout written:
  • user-keyed files (user_trades, user_paper, user_configs, price_alerts,
    backtest_trades, pending_users) → `user_records`, ONE ROW PER USER
  • everything else (picks, caches, global config)                → `documents`

Safety:
  • READ-ONLY on the Gist — nothing is deleted or modified there, so rollback is
    just unsetting the env vars.
  • Verifies every file/row by reading it back and comparing content.
  • Refuses to run unless SUPABASE_URL + SUPABASE_KEY are set AND the schema
    (supabase_schema.sql) has been applied.

Usage:
    python3 scripts/migrate_to_supabase.py --dry-run   # show the plan, write nothing
    python3 scripts/migrate_to_supabase.py             # migrate + verify
"""
from __future__ import annotations

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests


def _gist_files() -> dict:
    gid = os.environ.get("GIST_ID")
    tok = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not (gid and tok):
        raise SystemExit("GIST_ID and GH_GIST_TOKEN are required to read the source data.")
    r = requests.get(f"https://api.github.com/gists/{gid}",
                     headers={"Authorization": f"token {tok}"}, timeout=30)
    r.raise_for_status()
    out = {}
    for name, meta in r.json().get("files", {}).items():
        raw = meta.get("content") or ""
        try:
            out[name] = json.loads(raw) if raw.strip() else None
        except Exception:
            print(f"  ! {name}: not valid JSON — skipped")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")):
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set to migrate. "
                         "Create the project, run supabase_schema.sql, then re-run.")

    from config_manager import USER_KEYED_FILES
    files = _gist_files()
    print(f"source: {len(files)} Gist files\n")

    plan_rows, plan_docs = [], []
    for name, data in sorted(files.items()):
        if data is None:
            continue
        if name in USER_KEYED_FILES and isinstance(data, dict):
            for uid, content in data.items():
                plan_rows.append((name, uid, content))
        else:
            plan_docs.append((name, data))

    print(f"→ user_records : {len(plan_rows)} rows "
          f"({len({r[0] for r in plan_rows})} files, one row per user)")
    for f in sorted({r[0] for r in plan_rows}):
        ids = [r[1] for r in plan_rows if r[0] == f]
        print(f"    {f:24} {len(ids)} users: {ids}")
    print(f"→ documents    : {len(plan_docs)} blobs")
    for n, _ in plan_docs:
        print(f"    {n}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    from storage import SupabaseBackend
    be = SupabaseBackend()

    print("\nwriting…")
    for name, data in plan_docs:
        be.write(name, data)
    for name, uid, content in plan_rows:
        # expected_version=None → insert-if-absent; re-running is therefore safe
        # for fresh rows and simply reports an existing one as already migrated.
        be.write_user(name, uid, content, None)

    print("verifying…")
    bad = []
    for name, data in plan_docs:
        if be.read(name) != data:
            bad.append(f"documents/{name}")
    for name, uid, content in plan_rows:
        got, _ver = be.read_user(name, uid)
        if got != content:
            bad.append(f"user_records/{name}[{uid}]")

    if bad:
        print(f"\n🚨 VERIFY FAILED for {len(bad)}:")
        for b in bad[:20]:
            print("   ", b)
        print("\nDo NOT switch over. The Gist is untouched — leave the env vars unset.")
        return 1

    print(f"\n✅ migrated + verified: {len(plan_docs)} documents, {len(plan_rows)} user rows")
    print("The Gist is untouched. To switch over, set SUPABASE_URL + SUPABASE_KEY "
          "on Render and in GitHub secrets. To roll back, unset them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
