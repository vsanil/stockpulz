#!/usr/bin/env python3
"""Reconcile the Gist and Supabase after the stalled Aug-19 migration.

🔴 Both stores hold unique data, so a blind copy either way LOSES some. Measured
2026-08-22:
  Gist-only     user_configs, user_paper, backtest_trades, pending_users
  Gist newer    user_trades, weekly_picks, picks (_saved_date 08-21 vs 08-19)
  Supabase-only usage_counts  (the Gist copy is empty)
  BOTH unique   price_alerts  (3 chat_ids only in Supabase, 2 only in the Gist)
                traffic_hours (same hours, different counts — disjoint streams)

Destination is SUPABASE. The Gist is READ-ONLY here and stays intact as the
rollback copy.

    python3 scripts/reconcile_storage.py --dry-run   # plan only, writes nothing
    python3 scripts/reconcile_storage.py             # write + verify
"""
import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Per-file strategy, derived from the measured comparison above.
GIST_WINS = ("user_configs.json", "user_paper.json", "backtest_trades.json",
             "pending_users.json", "user_trades.json", "weekly_picks.json",
             "picks.json",
             # 🔴 Added Aug 23. The 5 engine-finding dispositions were migrated
             # out of the repo from a LOCAL shell, whose .env has no SUPABASE_*
             # — so they landed in the Gist while production and the daily job
             # read Supabase. Without this the store reads empty there and every
             # finding already ruled on reopens as `open`.
             "engine_findings_state.json")
KEEP_SUPABASE = ("usage_counts.json",)
MERGE = ("price_alerts.json", "traffic_hours.json")


def _merge_alerts(g: dict, s: dict) -> dict:
    """Union per key. An alert's identity is ticker+direction+target+timestamp —
    the same triple `price_alert_manager._alert_key` uses, plus the stamp so a
    re-armed alert at the same level is not collapsed with the original."""
    out = {}
    for key in sorted(set(g or {}) | set(s or {})):
        gv, sv = (g or {}).get(key), (s or {}).get(key)
        if not isinstance(gv, list) or not isinstance(sv, list):
            out[key] = gv if gv is not None else sv
            continue
        seen, merged = set(), []
        for a in list(gv) + list(sv):
            if not isinstance(a, dict):
                continue
            ident = (a.get("ticker"), a.get("direction"), a.get("target"),
                     a.get("set_at") or a.get("triggered_at"))
            if ident in seen:
                continue
            seen.add(ident)
            merged.append(a)
        out[key] = merged
    return out


def _merge_traffic(g: dict, s: dict) -> dict:
    """SUM the counters. Each hit is recorded once, to whichever backend was
    live at the time, so the two sides are DISJOINT event streams — the Aug 19
    finding (807 hits in one store, 154 in the other). Overwriting loses one."""
    out = copy.deepcopy(g or {})
    for month, hours in (s or {}).items():
        m = out.setdefault(month, {})
        for hh, rec in (hours or {}).items():
            cur = m.setdefault(hh, {"hits": 0, "cold": 0, "users": {}})
            cur["hits"] = cur.get("hits", 0) + (rec or {}).get("hits", 0)
            cur["cold"] = cur.get("cold", 0) + (rec or {}).get("cold", 0)
            users = cur.setdefault("users", {})
            for uid, n in ((rec or {}).get("users") or {}).items():
                users[uid] = users.get(uid, 0) + n
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()



def _sb_read(sb, name):
    """🔴 Read the table the file actually lives in.

    SupabaseBackend.read() hits `documents`; user-keyed files live as rows in
    `user_records` and are reached via read_all_users(). Using read() for those
    reports an empty result and reads as "Supabase is missing everything" —
    which is exactly the false alarm this comparison raised on 2026-08-22.
    """
    from config_manager import USER_KEYED_FILES
    if name in USER_KEYED_FILES and sb.supports_rows():
        try:
            return sb.read_all_users(name)
        except Exception:
            return None
    return sb.read(name)


    import storage
    from config_manager import USER_KEYED_FILES
    gist = storage.GistBackend()
    sb = storage.SupabaseBackend()          # raises if unwritable — fail closed

    plan = []
    for name in GIST_WINS + MERGE:
        g, s = gist.read(name), _sb_read(sb, name)
        if name in MERGE:
            merged = (_merge_alerts if name == "price_alerts.json"
                      else _merge_traffic)(g or {}, s or {})
            why = "MERGE"
        else:
            merged, why = g, "GIST→SUPABASE"
        if merged is None:
            print(f"  ! {name}: nothing on the Gist — skipped")
            continue
        same = merged == s          # value equality — JSONB normalises numbers
        plan.append((name, merged, why, same))

    print("\n  plan")
    print("  " + "─" * 66)
    for name, merged, why, same in plan:
        keys = len(merged) if isinstance(merged, dict) else "—"
        print(f"  {name:<24}{why:<16}keys={keys!s:<6}"
              f"{'no change' if same else 'WRITE'}")
    for name in KEEP_SUPABASE:
        print(f"  {name:<24}{'KEEP SUPABASE':<16}(the Gist copy is empty)")

    if args.dry_run:
        print("\n  (dry run — nothing written; the Gist is never written either way)")
        return 0

    print("\n  writing…")
    for name, merged, _why, same in plan:
        if same:
            continue
        if name in USER_KEYED_FILES and sb.supports_rows():
            # Rows need CAS: read the CURRENT version, then overwrite with it.
            # expected_version=None is insert-if-absent and would silently SKIP
            # every stale row — the reason a plain re-run could not repair this.
            for uid, content in (merged or {}).items():
                _cur, ver = sb.read_user(name, str(uid))
                sb.write_user(name, str(uid), content, ver)
        else:
            sb.write(name, merged)
        print(f"    ✓ {name}")

    print("\n  verifying…")
    bad = []
    for name, merged, _why, _same in plan:
        got = _sb_read(sb, name)
        if got != merged:
            bad.append(name)
    if bad:
        print(f"  🚨 VERIFY FAILED: {', '.join(bad)}")
        print("     The Gist is untouched — unset SUPABASE_* on Render to roll back.")
        return 1
    print(f"  ✅ reconciled + verified {len(plan)} file(s). The Gist is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
