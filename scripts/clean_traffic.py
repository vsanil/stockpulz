#!/usr/bin/env python3
"""
clean_traffic.py — remove the Full Sweep's hits from traffic_hours.json (one-time).

Until 2026-11-15 03:41 UTC (`8bfc4a5`) the Full Sweep drove the real Flask app
in-process as the owner, so every request it made was counted as the owner
using the app — and, because each run is a fresh process, as a COLD START.
November alone: 1,386 of 1,389 "interactions".

Each polluted hour is attributed by EVIDENCE, not by guess: the GitHub API
says exactly when every Full Sweep run started, so for each (month, UTC hour)
we know how many runs fell in it. The script removes the owner's hits in an
hour only when they are a clean multiple of the runs there; anything that does
not fit is LEFT ALONE and printed, so a real interaction is never deleted to
make the numbers tidy.

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


def plan(data: dict, runs: dict, owner: str) -> tuple[list, dict]:
    """Rows for the report, and {(month, hour): hits_to_remove}."""
    rows, remove = [], {}
    months = sorted(set(data) | {m for m, _ in runs})
    for month in months:
        hours = data.get(month) or {}
        for h in sorted(set(hours) | {hh for m, hh in runs if m == month}):
            rec = hours.get(h) or {}
            o = int((rec.get("users") or {}).get(owner, 0))
            n = runs.get((month, h), 0)
            cold = int(rec.get("cold", 0))
            if not o and not n:
                continue
            per = (o / n) if n else None
            # Remove only what the runs fully explain: the owner's hits are an
            # exact multiple of the runs in that hour, every one of them cold.
            fits = bool(n) and o > 0 and o % n == 0 and o <= cold
            rows.append((month, h, int(rec.get("hits", 0)), o, cold, n, per, fits))
            if fits:
                remove[(month, h)] = o
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

    print(f"{'month':8} {'hr':>2} {'hits':>6} {'owner':>6} {'cold':>6} {'runs':>5} {'per run':>8}  verdict")
    for month, h, hits, o, cold, n, per, fits in rows:
        per_s = f"{per:.1f}" if per is not None else "-"
        print(f"{month:8} {h:>2} {hits:6d} {o:6d} {cold:6d} {n:5d} {per_s:>8}  "
              f"{'REMOVE ' + str(o) if fits else 'keep'}")
    by_month: dict = {}
    for (m, _h), n in remove.items():
        by_month[m] = by_month.get(m, 0) + n
    for m in sorted(data):
        tot = sum(int((r or {}).get("hits", 0)) for r in (data[m] or {}).values())
        print(f"  {m}: {tot} hits stored, {by_month.get(m, 0)} attributable to the sweep "
              f"-> {tot - by_month.get(m, 0)} left")
    kept = [(m, h, o) for m, h, _x, o, _c, n, _p, fits in rows if o and not fits]
    if kept:
        print(f"  ⚠️ {len(kept)} owner hour(s) NOT explained by sweep runs — left as they are: {kept[:12]}")

    if not args.apply:
        print("[clean_traffic] DRY RUN — nothing written.")
        return 0
    taken = apply_plan(remove, owner)
    after = be.read_strict(TRAFFIC_FILE) or {}
    left = sum(int((after.get(m) or {}).get(h, {}).get("users", {}).get(owner, 0))
               for (m, h) in remove)
    print(f"[clean_traffic] removed {taken} hit(s); owner hits left in those hours: {left}")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
