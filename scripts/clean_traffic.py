#!/usr/bin/env python3
"""
clean_traffic.py — remove the Full Sweep's hits from traffic_hours.json (one-time).

Until 2026-11-15 03:41 UTC (`8bfc4a5`) the Full Sweep drove the real Flask app
in-process as the owner, so every request it made was counted as the owner
using the app — and, because each run is a fresh process, as a COLD START.
November alone: 1,386 of 1,389 "interactions".

Each polluted hour is attributed by EVIDENCE, not by guess: the GitHub API
says exactly when every Full Sweep run started. Measured from that record, a
run made 33 counted requests, all cold, all under the owner's id. So in each
(month, UTC hour) the script removes at most 33 × the runs that started there
— plus whatever capacity the PREVIOUS hour's runs did not use, because a run
starting at :59 lands its requests in the next hour — and only the owner's
COLD hits. Everything beyond that is LEFT ALONE and printed: warm hits, other
users, and any owner activity the runs cannot account for.

(A first, stricter version removed an hour only when the owner's hits were an
exact multiple of its runs. It left 21 obviously-polluted hours, because of
that spill and of runs that failed partway.)

READ-ONLY by default. `--apply` writes, through the same mutate path the app
uses, and reads back. The store must be the one production uses (Supabase) —
a laptop resolves to the Gist, the rollback copy, and is refused.

    python scripts/clean_traffic.py            # show what would change
    python scripts/clean_traffic.py --apply    # change it
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests  # noqa: E402

FIXED_AT = dt.datetime(2026, 11, 15, 3, 41, 31, tzinfo=dt.timezone.utc)   # 8bfc4a5
TRAFFIC_FILE = "traffic_hours.json"


def sweep_runs_by_hour(repo: str, token: str) -> dict:
    """{("YYYY-MM", "HH"): n} — Full Sweep runs that started before the fix."""
    out: dict = {}
    url = f"https://api.github.com/repos/{repo}/actions/workflows/full_sweep.yml/runs"
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    page = 1
    while True:
        r = requests.get(url, headers=hdr, params={"per_page": 100, "page": page}, timeout=30)
        r.raise_for_status()
        runs = r.json().get("workflow_runs") or []
        if not runs:
            break
        for run in runs:
            ts = run.get("run_started_at") or run.get("created_at")
            t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t >= FIXED_AT:
                continue
            k = (t.strftime("%Y-%m"), t.strftime("%H"))
            out[k] = out.get(k, 0) + 1
        page += 1
    return out


PER_RUN = 33     # counted requests per sweep run, measured Sep-Nov 2026


def plan(data: dict, runs: dict, owner: str) -> tuple[list, dict]:
    """Rows for the report, and {(month, hour): hits_to_remove}.

    Hours are walked in order so a run's unused capacity carries ONE hour
    forward (its requests spill past the hour it started in), then expires.
    A month's buckets sum every day, so 23:00 is followed by 00:00 — the walk
    is CIRCULAR, starting at the first quiet hour so no spill is lost.
    """
    rows, remove = [], {}
    months = sorted(set(data) | {m for m, _ in runs})
    for month in months:
        hours = data.get(month) or {}

        def _quiet(i):
            h = f"{i:02d}"
            return not runs.get((month, h)) and not ((hours.get(h) or {}).get("users") or {}).get(owner)
        start = next((i for i in range(24) if _quiet(i)), 0)
        carry = 0
        for step in range(24):
            h = f"{(start + step) % 24:02d}"
            rec = hours.get(h) or {}
            o = int((rec.get("users") or {}).get(owner, 0))
            n = runs.get((month, h), 0)
            cold = int(rec.get("cold", 0))
            capacity = PER_RUN * n + carry
            take = min(o, cold, capacity)
            carry = min(PER_RUN * n, capacity - take)   # only this hour's runs spill on
            if take:
                remove[(month, h)] = take
            if o or n:
                rows.append((month, h, int(rec.get("hits", 0)), o, cold, n, take))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows, remove


def apply_plan(remove: dict, owner: str) -> int:
    from config_manager import mutate_gist_file

    def _mut(cur):
        cur = cur or {}
        taken = 0
        for (month, h), n in remove.items():
            rec = (cur.get(month) or {}).get(h)
            if not rec:
                continue
            users = rec.setdefault("users", {})
            have = int(users.get(owner, 0))
            k = min(n, have, int(rec.get("hits", 0)), int(rec.get("cold", 0)))
            if k <= 0:
                continue
            rec["hits"] = int(rec["hits"]) - k
            rec["cold"] = int(rec["cold"]) - k
            if have - k > 0:
                users[owner] = have - k
            else:
                users.pop(owner, None)
            if rec["hits"] <= 0 and not users:
                cur[month].pop(h, None)
            taken += k
        return cur, taken

    return mutate_gist_file(TRAFFIC_FILE, _mut, default={})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from storage import get_storage_backend
    be = get_storage_backend()
    print(f"[clean_traffic] store: {be.name()}")
    if be.name() != "supabase":
        print("[clean_traffic] refusing: this is not the store production reads "
              "(a laptop resolves to the Gist, the rollback copy).")
        return 2
    owner = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY") or "vsanil/stockpulz"
    token = os.environ.get("GH_ACTIONS_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not owner or not token:
        print("[clean_traffic] needs TELEGRAM_CHAT_ID and a GitHub token")
        return 2

    data = be.read_strict(TRAFFIC_FILE) or {}
    runs = sweep_runs_by_hour(repo, token)
    rows, remove = plan(data, runs, owner)

    print(f"{'month':8} {'hr':>2} {'hits':>6} {'owner':>6} {'cold':>6} {'runs':>5} {'remove':>7} {'owner left':>10}")
    for month, h, hits, o, cold, n, take in rows:
        print(f"{month:8} {h:>2} {hits:6d} {o:6d} {cold:6d} {n:5d} {take:7d} {o - take:10d}")
    for m in sorted(data):
        tot = sum(int((r or {}).get("hits", 0)) for r in (data[m] or {}).values())
        own = sum(int(((r or {}).get("users") or {}).get(owner, 0)) for r in (data[m] or {}).values())
        rm = sum(n for (mm, _h), n in remove.items() if mm == m)
        print(f"  {m}: {tot} hits stored · owner {own} · sweep {rm} -> {tot - rm} left "
              f"(owner {own - rm})")

    if not args.apply:
        print("[clean_traffic] DRY RUN — nothing written.")
        return 0
    # A copy of exactly what is about to change, checked before anything is
    # touched — this store keeps no history of its own.
    backup = f"traffic_hours.before_cleanup_{dt.date.today().isoformat()}.json"
    be.write(backup, data)
    if (be.read_strict(backup) or {}) != data:
        print(f"[clean_traffic] backup {backup} did not read back — nothing changed.")
        return 1
    print(f"[clean_traffic] backup saved: {backup}")
    planned = sum(remove.values())
    taken = apply_plan(remove, owner)
    after = be.read_strict(TRAFFIC_FILE) or {}

    def _owner_total(d):
        return sum(int(((r or {}).get("users") or {}).get(owner, 0))
                   for m in d for r in (d.get(m) or {}).values())
    before_o, after_o = _owner_total(data), _owner_total(after)
    print(f"[clean_traffic] planned {planned}, removed {taken}; owner hits "
          f"{before_o} -> {after_o} (read back)")
    # After >= before - removed: a real owner tap may land during the run.
    ok = taken == planned and before_o - taken <= after_o <= before_o
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
