#!/usr/bin/env python3
"""Which storage backend is this surface ACTUALLY using, and can it WRITE?

Run this after changing SUPABASE_KEY / RLS policies to confirm the fix took.

🔴 Why it exists (2026-08-21). SUPABASE_* was wired into 8 workflows on Aug 19
and verified two ways — the migration read every row back, and the workflows
declared the env vars. Neither proves a WRITE still works. `user_records` had
RLS enabled with no policy for the key in use, so every per-user write threw
`42501 new row violates row-level security policy` while reads stayed green.
The synthetic-user bot opened ZERO positions for two days across 30 "success"
runs. "Wired" is not "working" — the same family as "committed is not deployed".

READ-ONLY on real data. The write test uses a disposable row
(filename `__verify_probe__`, chat_id `0`) and deletes it afterwards.

Exit codes:  0 = consistent and healthy    1 = broken or split-brain
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE_FILE, PROBE_UID = "__verify_probe__", "0"


def _load_dotenv() -> bool:
    """Load .env when running locally. On CI/Render the vars are already set."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        return True
    except OSError:
        return False


def _describe(v: str) -> str:
    """Presence and shape ONLY — never any characters of the value.

    An earlier version printed the first 12 chars to identify the key type.
    GitHub Actions masks exact secret values, but a PREFIX may not match the
    mask and would be published to the workflow log. The prefix test below
    tells us what we need without ever emitting key material.
    """
    return "<unset>" if not v else f"set ({len(v)} chars)"


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


def _which_is_newer(name, g, v) -> str:
    """Say WHICH side is authoritative — never assume the Gist wins.

    picks.json diverged in BOTH directions (daily_run wrote Supabase while the
    bot wrote the Gist), and on Aug 19 traffic_hours held DISJOINT event streams
    that had to be merged, not overwritten. Blindly taking one side loses data.
    """
    # Whole-file docs often carry their own date stamp.
    for key in ("_saved_date", "_confirmation_sent_date"):
        if isinstance(g, dict) and isinstance(v, dict) and (key in g or key in v):
            gd, vd = str(g.get(key, "")), str(v.get(key, ""))
            if gd != vd:
                return f"gist={gd or '—'} supabase={vd or '—'} → {'GIST' if gd > vd else 'SUPABASE'} newer"
    # Per-user files: compare which chat_ids each side holds.
    if isinstance(g, dict) and isinstance(v, dict):
        gk, vk = set(g), set(v)
        only_g, only_v = gk - vk, vk - gk
        if only_g or only_v:
            return (f"gist-only keys={len(only_g)} supabase-only={len(only_v)}"
                    f" → {'GIST' if len(only_g) >= len(only_v) else 'SUPABASE'} more complete")
        return "same keys, different contents → inspect before overwriting"
    return "DIFFERS — cannot rank automatically"


def compare() -> int:
    """Report DRIFT between the Gist and Supabase for every user-keyed file.

    🔴 Why this matters after a stalled migration. The Aug 19 migration copied
    Gist → Supabase, then Supabase became unwritable, so every write for the
    next three days landed on the GIST. Switching the app back to Supabase
    therefore risks serving a stale copy and writing that stale state forward.
    Compare before trusting the cutover.
    """
    import json
    import storage
    from config_manager import USER_KEYED_FILES

    gist = storage.GistBackend()
    try:
        sb = storage.SupabaseBackend()
    except Exception as exc:
        print(f"  🔴 cannot reach Supabase: {str(exc)[:200]}")
        return 1

    files = sorted(set(USER_KEYED_FILES) | {
        "picks.json", "traffic_hours.json", "usage_counts.json", "weekly_picks.json"})
    print(f"\n  {'file':<26}{'gist':>10}{'supabase':>11}   verdict")
    print("  " + "─" * 62)
    drift, _last_verdict = [], {}
    for f in files:
        try:
            g = gist.read(f)
        except Exception:
            g = None
        try:
            v = _sb_read(sb, f)
        except Exception:
            v = None
        gj = json.dumps(g, sort_keys=True) if g is not None else ""
        vj = json.dumps(v, sort_keys=True) if v is not None else ""
        # 🔴 Compare VALUES, not serialised strings. Postgres JSONB normalises
        # numbers (220.0 → 220), so a byte-for-byte comparison reports drift on
        # data that is semantically identical — user_trades.json looked 30 bytes
        # "smaller" on Supabase purely from that.
        if g == v:
            verdict = "identical" if gj else "both empty"
        else:
            verdict = "🔴 " + _which_is_newer(f, g, v)
            _last_verdict[f] = verdict
            drift.append(f)
        print(f"  {f:<26}{len(gj):>10}{len(vj):>11}   {verdict}")

    print()
    if not drift:
        print("  ✅ Every file matches. The cutover is safe.")
        return 0
    # A Supabase SUPERSET is the expected end state after reconciliation — it
    # holds everything the Gist has plus rows the Gist never received. Only
    # Gist-only content is genuine, unmigrated drift.
    superset = [f for f in drift if "gist-only keys=0" in _last_verdict.get(f, "")
                or f in ("usage_counts.json", "traffic_hours.json")]
    real = [f for f in drift if f not in superset]
    if not real:
        print(f"  ✅ Supabase is a SUPERSET of the Gist — nothing is unmigrated.")
        print(f"     Ahead on: {', '.join(superset)} (merged rows / Supabase-only counters).")
        print("     The Gist remains as the rollback copy, one reconcile behind.")
        return 0
    print(f"  🔴 {len(real)} file(s) have GIST-ONLY content: {', '.join(real)}")
    print("     The Gist received writes Supabase never got. Run")
    print("     scripts/reconcile_storage.py --dry-run, then apply, BEFORE")
    print("     relying on Supabase — otherwise those writes are lost.")
    return 1


def main() -> int:
    print("═" * 68)
    print("  StockPulz storage verification")
    print("═" * 68)
    where = "loaded .env (local)" if _load_dotenv() else "using the ambient environment (CI/Render)"
    print(f"  {where}")

    url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", "")
    print("\n1. Environment")
    print(f"   SUPABASE_URL : {_describe(url)}")
    print(f"   SUPABASE_KEY : {_describe(key)}")
    if key:
        if key.startswith("sb_secret_"):
            print("   key type     : service_role — bypasses RLS ✅")
        elif key.startswith("sb_publishable_") or key.startswith("eyJ"):
            print("   key type     : 🔴 anon/publishable — RLS WILL block user_records writes.")
            print("                  This is almost certainly the whole problem: a")
            print("                  service_role key bypasses RLS, so a 42501 error")
            print("                  cannot happen with one.")
        else:
            print("   key type     : unrecognised prefix — the write probe below is the answer")

    # ── 2. What does the app actually resolve to? ────────────────────────────
    print("\n2. Backend the app resolves to")
    import storage
    storage.reset_backend()
    try:
        resolved = storage.get_storage_backend().name()
        print(f"   → {resolved}  ({'Supabase rows' if resolved.lower().startswith('supabase') else 'Gist whole-file blobs'})")
    except Exception as exc:
        resolved = "<none>"
        print(f"   → 🔴 no usable backend at all: {str(exc)[:140]}")
        print("      (GIST_ID / GH_GIST_TOKEN missing? this surface cannot store anything)")

    # ── 3. If Supabase is configured, why did/didn't it take? ────────────────
    supabase_ok, detail = None, ""
    if url and key:
        print("\n3. Supabase write probe (the check that was missing)")
        try:
            sb = storage.SupabaseBackend()          # runs _verify_schema + write probe
            supabase_ok = True
            print("   → construction PASSED: schema present AND writable ✅")
            # Real per-user round trip.
            ver = sb.write_user(PROBE_FILE, PROBE_UID, {"probe": True}, None)
            got, _ = sb.read_user(PROBE_FILE, PROBE_UID)
            ok = got == {"probe": True}
            print(f"   → round-trip write/read: {'✅ matched' if ok else '🔴 MISMATCH ' + repr(got)}")
            supabase_ok = ok
            try:
                sb._client.table("user_records").delete() \
                    .eq("filename", PROBE_FILE).eq("chat_id", PROBE_UID).execute()
                print("   → probe row cleaned up")
            except Exception as exc:
                print(f"   → ⚠️ could not clean up the probe row: {str(exc)[:80]}")
        except Exception as exc:
            supabase_ok, detail = False, str(exc)
            print(f"   → construction FAILED, falling back to Gist:\n      {detail[:300]}")
    else:
        print("\n3. Supabase not configured on this surface — Gist is expected.")

    # ── 4. Verdict ───────────────────────────────────────────────────────────
    print("\n4. Verdict")
    if not (url and key):
        print("   ✅ Gist only. Consistent, and the documented rollback state.")
        print("      NOTE: the ~30-user gist truncation wall still applies.")
        return 0
    if supabase_ok and resolved.lower().startswith("supabase"):
        print("   ✅ Supabase is live and writable on this surface.")
        print("      Re-run on EVERY surface — Render, and each workflow — before")
        print("      believing the migration is complete. A per-surface env var is")
        print("      what caused the Aug 19 split-brain.")
        return 0
    print("   🔴 Supabase is CONFIGURED but NOT USABLE — this surface fell back to Gist.")
    print("      Consequence: whichever surfaces DO reach Supabase write there while")
    print("      this one writes the Gist. That is a split-brain, not a safe fallback.")
    print("\n   Fix, in order:")
    print("     a) Use the service_role key (sb_secret_…) in BOTH the GitHub secret")
    print("        SUPABASE_KEY and the Render env var. The anon/publishable key is")
    print("        RLS-bound and cannot write user_records.")
    print("     b) Or add an RLS policy on user_records permitting the key in use.")
    print("     c) Or unset SUPABASE_URL/SUPABASE_KEY everywhere to return to one")
    print("        consistent store (known-working; shelves the migration).")
    print("\n   Then re-run this script on each surface until all report ✅.")
    return 1


def diff_file(name: str) -> int:
    """Show exactly WHAT differs for one file — never guess from byte counts."""
    import json
    import storage
    gist = storage.GistBackend()
    sb = storage.SupabaseBackend()
    g, v = gist.read(name) or {}, _sb_read(sb, name) or {}
    print(f"\n  {name}: gist keys={len(g)} supabase keys={len(v)}")
    for k in sorted(set(g) | set(v)):
        gv, vv = g.get(k), v.get(k)
        if gv == vv:
            continue
        print(f"\n  ── key {k!r}")
        if isinstance(gv, dict) and isinstance(vv, dict):
            for sub in sorted(set(gv) | set(vv)):
                a, b = gv.get(sub), vv.get(sub)
                if a == b:
                    continue
                if isinstance(a, list) and isinstance(b, list):
                    print(f"     {sub}: gist={len(a)} items, supabase={len(b)} items")
                    ga = {json.dumps(x, sort_keys=True) for x in a}
                    vb = {json.dumps(x, sort_keys=True) for x in b}
                    for m in list(ga - vb)[:3]:
                        print(f"       only in GIST     : {m[:150]}")
                    for m in list(vb - ga)[:3]:
                        print(f"       only in SUPABASE : {m[:150]}")
                else:
                    print(f"     {sub}: gist={str(a)[:70]!r} supabase={str(b)[:70]!r}")
        else:
            print(f"     gist={str(gv)[:120]!r}\n     supa={str(vv)[:120]!r}")
    return 0


if __name__ == "__main__":
    if "--diff" in sys.argv:
        _load_dotenv()
        sys.exit(diff_file(sys.argv[sys.argv.index("--diff") + 1]))
    if "--compare" in sys.argv:
        _load_dotenv()
        sys.exit(compare())
    sys.exit(main())
