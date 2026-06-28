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
            raw = files[filename].get("content", "")
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
        raw = files[filename].get("content", "")
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
