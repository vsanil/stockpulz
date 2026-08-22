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


if __name__ == "__main__":
    sys.exit(main())
