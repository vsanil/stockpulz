#!/usr/bin/env python3
"""
remove_legacy_alerts.py — one-time removal of 15 system-made alerts from May 2026.

They were created by the morning run on 2026-05-15..19, BEFORE automatic alerts
carried the `auto` marker, so the one-week expiry (2026-11-15) reads them as
hand-set and will never remove them. They are plainly system-made — pairs set
in the same second, and five near-identical CVX levels within ten hours — and
the user holds no positions, so none protects anything.

Owner's decision, 2026-11-15. Three other alerts from the same days (AAPL, GLD,
MSFT at round numbers) look hand-set and are deliberately NOT listed.

An alert is removed only if ALL of these hold: it belongs to the given user, it
matches a listed (ticker, direction, target), it has no `auto` marker, and it
was set in May 2026. Anything else is left alone. Dry run by default; `--apply`
saves a backup first and reads the result back. Supabase only.

    python scripts/remove_legacy_alerts.py --chat-id <id>            # show
    python scripts/remove_legacy_alerts.py --chat-id <id> --apply    # remove
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEGACY = [
    ("CHD", "below", 90.94), ("CHD", "below", 90.95),
    ("DXCM", "below", 58.05), ("DXCM", "below", 61.01),
    ("MMM", "below", 140.84), ("MMM", "below", 144.8),
    ("CAH", "below", 186.61), ("CAH", "below", 193.25),
    ("NEAR", "below", 1.46), ("NEAR", "below", 1.6),
    ("CVX", "below", 179.63), ("CVX", "below", 179.65), ("CVX", "below", 179.87),
    ("CVX", "below", 179.9), ("CVX", "below", 179.91),
]
SET_IN = "2026-05"


def is_legacy(a: dict) -> bool:
    """One of the 15, and nothing else."""
    if not isinstance(a, dict) or a.get("auto"):
        return False
    if not str(a.get("set_at") or "").startswith(SET_IN):
        return False
    try:
        target = float(a.get("target"))
    except (TypeError, ValueError):
        return False
    return any(a.get("ticker") == t and a.get("direction") == d and abs(target - x) < 0.005
               for t, d, x in LEGACY)


def split(alerts: list) -> tuple[list, list]:
    """(kept, removed) for one user's alert list."""
    kept, removed = [], []
    for a in alerts or []:
        (removed if is_legacy(a) else kept).append(a)
    return kept, removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    uid = str(args.chat_id).strip()

    from storage import get_storage_backend
    be = get_storage_backend()
    print(f"[legacy_alerts] store: {be.name()}")
    if be.name() != "supabase":
        print("[legacy_alerts] refusing: not the store production reads.")
        return 2

    import price_alert_manager as pam
    before = list((pam._load_alerts() or {}).get(uid) or [])
    kept, removed = split(before)
    print(f"[legacy_alerts] user has {len(before)} alert(s); {len(removed)} match, {len(kept)} stay")
    for a in removed:
        print(f"   remove: {a.get('ticker')} {a.get('direction')} {a.get('target')} "
              f"set {str(a.get('set_at'))[:16]}")
    if len(removed) != len(LEGACY):
        print(f"[legacy_alerts] expected {len(LEGACY)} — found {len(removed)}. "
              f"Not what was reviewed; nothing changed.")
        return 1
    if not args.apply:
        print("[legacy_alerts] DRY RUN — nothing written.")
        return 0

    backup = f"price_alerts.{uid}.before_cleanup_{dt.date.today().isoformat()}.json"
    be.write(backup, before)
    if (be.read_strict(backup) or []) != before:
        print(f"[legacy_alerts] backup {backup} did not read back — nothing changed.")
        return 1
    print(f"[legacy_alerts] backup saved: {backup}")

    from config_manager import mutate_gist_file

    def _mut(alerts):
        alerts = alerts or {}
        k, r = split(alerts.get(uid) or [])
        alerts[uid] = k
        return alerts, len(r)

    n = mutate_gist_file(pam.ALERTS_FILENAME, _mut, default={})
    import config_manager
    config_manager._cache_invalidate(pam.ALERTS_FILENAME)
    after = list((pam._load_alerts() or {}).get(uid) or [])
    still = [a for a in after if is_legacy(a)]
    print(f"[legacy_alerts] removed {n}; user now has {len(after)} alert(s); "
          f"{len(still)} of the 15 remain (read back)")
    ok = n == len(LEGACY) and not still and len(after) == len(before) - len(LEGACY)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
