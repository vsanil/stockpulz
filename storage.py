"""
storage.py — Backend-agnostic storage layer.

Today:  GistBackend  (GitHub Gist, free, works up to ~200 users)
Future: SupabaseBackend (PostgreSQL via Supabase free tier, flip of a switch)

Upgrade path: set SUPABASE_URL + SUPABASE_KEY env vars.  Everything else
in the codebase stays the same — config_manager.py routes all reads/writes
through get_storage_backend(), so swapping backends requires zero caller changes.

Supabase schema (run once in the Supabase SQL editor):
    CREATE TABLE IF NOT EXISTS documents (
        filename    TEXT PRIMARY KEY,
        content     JSONB        NOT NULL DEFAULT '{}'::jsonb,
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
    );
    -- Optional index for timestamp queries:
    CREATE INDEX ON documents (updated_at DESC);
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod

import requests

# ── Abstract interface ────────────────────────────────────────────────────────

class StorageBackend(ABC):
    """Minimal key/value store interface — one JSON blob per filename."""

    @abstractmethod
    def read(self, filename: str) -> dict | list | None:
        """Return parsed JSON for filename, or None if not found."""

    @abstractmethod
    def write(self, filename: str, data: dict | list) -> None:
        """Persist data as JSON under filename. Creates or overwrites."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging."""

    # ── Optional row-level API (fixes the storage races) ──────────────────────
    # A backend that stores ONE ROW PER USER can do what a whole-file blob store
    # cannot: writes for different users never touch the same bytes, and a
    # same-user write can compare-and-swap on a version token. Backends that
    # can't do this return False and callers keep the whole-file path.
    def read_strict(self, filename: str) -> dict | list | None:
        """Like read() but MUST raise on a transport error rather than return
        None — a caller doing read-modify-write must never mistake a failed read
        for 'empty' and clobber everyone. Default is read(); backends that
        swallow errors in read() MUST override this."""
        return self.read(filename)

    def supports_rows(self) -> bool:
        return False

    def read_user(self, filename: str, chat_id: str) -> tuple:
        """Return (content, version). version is opaque; pass it back to write_user."""
        raise NotImplementedError

    def write_user(self, filename: str, chat_id: str, content, expected_version):
        """Compare-and-swap. Returns the new version, or None on version
        conflict (someone else wrote first) so the caller can retry."""
        raise NotImplementedError

    def read_all_users(self, filename: str) -> dict:
        """Reassemble the whole {chat_id: content} mapping from rows."""
        raise NotImplementedError



# GitHub omits/truncates file content over ~1 MB in the gist API response and
# sets truncated=True, handing back a raw_url instead. Reading `content` blindly
# then yields PARTIAL JSON — which either raises (best case) or, on the swallowing
# read() path, looks like "file missing" and invites a clobber. At current
# per-user sizes user_trades/price_alerts cross 1 MB around ~75-90 users, so this
# is a real wall, not a hypothetical.
def _gist_content(meta: dict, headers: dict) -> str:
    raw = meta.get("content", "")
    if meta.get("truncated") and meta.get("raw_url"):
        resp = requests.get(meta["raw_url"], headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.text
    return raw

# ── Gist backend (current) ────────────────────────────────────────────────────

class GistBackend(StorageBackend):
    """
    Stores all files as JSON blobs inside a single private GitHub Gist.
    Free, no infra required. Bottleneck appears around 200+ active users
    (Gist API rate limit: 5000 req/hour authenticated).
    """

    _BASE = "https://api.github.com/gists"

    def __init__(self) -> None:
        self._token   = os.environ.get("GH_GIST_TOKEN", "")
        self._gist_id = os.environ.get("GIST_ID", "")
        if not self._gist_id:
            raise EnvironmentError("GIST_ID env var is required for GistBackend.")

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def _url(self) -> str:
        return f"{self._BASE}/{self._gist_id}"

    def read(self, filename: str) -> dict | list | None:
        try:
            resp = requests.get(self._url(), headers=self._headers(), timeout=10)
            resp.raise_for_status()
            files = resp.json().get("files", {})
            if filename not in files:
                return None
            raw = _gist_content(files[filename], self._headers())
            return json.loads(raw) if raw else None
        except Exception as exc:
            print(f"[storage/gist] read({filename}) failed: {exc}")
            return None

    def read_strict(self, filename: str) -> dict | list | None:
        """
        Like read() but RAISES on a fetch/transport error instead of returning
        None — so a caller doing read-modify-write never mistakes a failed read
        for "empty" and clobbers everyone else's data. Returns None only when the
        file legitimately doesn't exist yet.
        """
        resp = requests.get(self._url(), headers=self._headers(), timeout=10)
        resp.raise_for_status()
        files = resp.json().get("files", {})
        if filename not in files:
            return None
        raw = _gist_content(files[filename], self._headers())
        return json.loads(raw) if raw else None

    def write(self, filename: str, data: dict | list) -> None:
        payload = {"files": {filename: {"content": json.dumps(data, indent=2)}}}
        try:
            resp = requests.patch(self._url(), headers=self._headers(),
                                  json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[storage/gist] write({filename}) failed: {exc}")

    def name(self) -> str:
        return "gist"


# ── Supabase backend (upgrade path) ──────────────────────────────────────────

class SupabaseBackend(StorageBackend):
    """
    Stores all files as rows in a `documents` table (filename PK, content JSONB).
    Activate by setting SUPABASE_URL and SUPABASE_KEY env vars.

    Supabase free tier: 500 MB storage, 50k API calls/day — handles 1000+ users.
    Upgrade to Supabase Pro ($25/month) when you need more.

    Requires: pip install supabase
    """

    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise EnvironmentError("SUPABASE_URL and SUPABASE_KEY are required for SupabaseBackend.")
        try:
            from supabase import create_client  # type: ignore
            self._client = create_client(url, key)
        except ImportError:
            raise ImportError("supabase package not installed. Run: pip install supabase")
        self._verify_schema()

    # Both are required: `documents` holds whole-blob files, `user_records` the
    # per-user rows that every settings/positions/alerts read goes through.
    REQUIRED_TABLES = ("documents", "user_records")

    def _verify_schema(self, timeout: float = 4.0) -> None:
        """Refuse to become the storage backend unless the schema is really there.

        🔴 The outage this prevents (2026-08-19). SUPABASE_URL and SUPABASE_KEY
        were set on Render while `supabase_schema.sql` had never been applied.
        Construction SUCCEEDED — the client is lazy — so this backend took over
        all storage, and then EVERY per-user read failed, one log line at a
        time: `Could not find the table 'public.user_records' in the schema
        cache`. Measured on production: 40 failures on user_trades.json, 18 on
        user_configs.json, 4 each on price_alerts and backtest_trades in a
        45-minute window, while the mini-app and bot silently showed nothing.
        The Saturday weekly run set ZERO alerts for the same reason.

        get_storage_backend() already falls back to Gist when construction
        raises — it just never got the chance. Failing here converts a silent,
        ongoing, user-facing outage into one startup line and no impact.

        A timeout or network error also raises: a backend we cannot verify must
        not be trusted with storage when a working Gist is sitting right there.
        """
        import threading
        missing: list[str] = []
        errors: list[str] = []

        def _probe(table: str) -> None:
            try:
                self._client.table(table).select("*").limit(1).execute()
            except Exception as exc:
                msg = str(exc)
                # PostgREST says this when the relation is absent from the schema cache.
                if "does not exist" in msg or "schema cache" in msg or "PGRST205" in msg:
                    missing.append(table)
                else:
                    errors.append(f"{table}: {msg[:120]}")

        for tbl in self.REQUIRED_TABLES:
            t = threading.Thread(target=_probe, args=(tbl,), daemon=True)
            t.start()
            t.join(timeout=timeout)
            if t.is_alive():
                errors.append(f"{tbl}: probe timed out after {timeout}s")

        if missing:
            raise RuntimeError(
                f"Supabase schema incomplete — missing table(s): {', '.join(missing)}. "
                f"Run supabase_schema.sql (see scripts/migrate_to_supabase.py --dry-run) "
                f"or unset SUPABASE_URL/SUPABASE_KEY to stay on the Gist.")
        if errors:
            raise RuntimeError(f"Supabase unreachable or unverifiable: {'; '.join(errors)}")

    def read(self, filename: str) -> dict | list | None:
        import threading
        result = [None]
        def _do():
            try:
                resp = (
                    self._client.table("documents")
                    .select("content")
                    .eq("filename", filename)
                    .maybe_single()
                    .execute()
                )
                if resp is not None and resp.data:
                    result[0] = resp.data["content"]
            except Exception as exc:
                print(f"[storage/supabase] read({filename}) failed: {exc}")
        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout=2.0)  # max 2s — fall back to Gist if slower
        return result[0]

    def read_strict(self, filename: str) -> dict | list | None:
        """read() above swallows errors and returns None, which a read-modify-write
        caller would misread as 'empty' and then clobber. This variant RAISES."""
        resp = (self._client.table("documents")
                .select("content").eq("filename", filename)
                .maybe_single().execute())
        if resp is not None and resp.data:
            return resp.data["content"]
        return None

    def write(self, filename: str, data: dict | list) -> None:
        try:
            self._client.table("documents").upsert(
                {"filename": filename, "content": data},
                on_conflict="filename",
            ).execute()
        except Exception as exc:
            print(f"[storage/supabase] write({filename}) failed: {exc}")
            # Fall back to Gist so data is never silently lost
            try:
                GistBackend().write(filename, data)
                print(f"[storage/supabase] write({filename}) fell back to Gist OK.")
            except Exception as exc2:
                print(f"[storage/supabase] Gist fallback also failed: {exc2}")

    def name(self) -> str:
        return "supabase"

    # ── Row-level API (one row per user) ──────────────────────────────────────
    # This is the whole point of the migration. On the Gist, saving one user
    # meant rewriting the file every other user shares, so a concurrent write
    # was clobbered and GitHub's read-after-write lag made a stale merge base
    # likely. Here each user is an independent row with a version token, so
    # different users never collide and same-user writes get true CAS.
    def supports_rows(self) -> bool:
        return True

    def read_user(self, filename: str, chat_id: str) -> tuple:
        try:
            resp = (self._client.table("user_records")
                    .select("content,version")
                    .eq("filename", filename).eq("chat_id", str(chat_id))
                    .maybe_single().execute())
            if resp is not None and resp.data:
                return resp.data["content"], resp.data["version"]
        except Exception as exc:
            print(f"[storage/supabase] read_user({filename},{chat_id}) failed: {exc}")
            raise
        return None, None

    def write_user(self, filename: str, chat_id: str, content, expected_version):
        """Atomic CAS via the upsert_user_record() SQL function.
        Returns the new version, or None if another writer got there first."""
        resp = self._client.rpc("upsert_user_record", {
            "p_filename": filename,
            "p_chat_id": str(chat_id),
            "p_content": content,
            "p_expected_version": expected_version,
        }).execute()
        return resp.data if resp is not None else None

    def read_all_users(self, filename: str) -> dict:
        out: dict = {}
        try:
            resp = (self._client.table("user_records")
                    .select("chat_id,content").eq("filename", filename).execute())
            for row in (resp.data or []):
                out[row["chat_id"]] = row["content"]
        except Exception as exc:
            print(f"[storage/supabase] read_all_users({filename}) failed: {exc}")
            raise
        return out


# ── Factory ───────────────────────────────────────────────────────────────────

_backend_instance: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """
    Auto-select and return a singleton storage backend.

    Priority:
      1. SUPABASE_URL + SUPABASE_KEY set → SupabaseBackend
      2. GH_GIST_TOKEN + GIST_ID set     → GistBackend  (default)

    The instance is cached for the process lifetime — backends are
    stateless so it is safe to reuse across threads.
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"):
        try:
            _backend_instance = SupabaseBackend()
            print(f"[storage] Using SupabaseBackend.")
            return _backend_instance
        except Exception as exc:
            print(f"[storage] SupabaseBackend init failed ({exc}), falling back to Gist.")

    _backend_instance = GistBackend()
    print(f"[storage] Using GistBackend.")
    return _backend_instance


def reset_backend() -> None:
    """Force re-selection of the backend (useful in tests)."""
    global _backend_instance
    _backend_instance = None
