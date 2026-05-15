"""
cache_layer.py — Backend-agnostic hot-data cache.

Today:  MemoryBackend  (in-process dict, same as current _gist_read_cache)
Future: RedisBackend   (Upstash free tier, survives restarts, shared across workers)

Upgrade path: set UPSTASH_REDIS_URL + UPSTASH_REDIS_TOKEN env vars.
All callers use get_cache() which returns the singleton backend — zero
caller changes when swapping from memory to Redis.

Upstash free tier: 10k commands/day, 256 MB — handles 1000 users comfortably.
Upgrade: Upstash Pay-as-you-go (~$0.2 per 100k commands) when needed.

Why separate from config_manager's _gist_read_cache?
  - config_manager cache: 20s TTL, per-process, only for Gist round-trips
  - cache_layer: configurable TTL, optionally cross-process (Redis), for
    hot computed data (signal scores, screener results, macro regime)
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from typing import Any


# ── Abstract interface ────────────────────────────────────────────────────────

class CacheBackend(ABC):
    """Simple TTL key-value cache interface."""

    @abstractmethod
    def get(self, key: str) -> Any | None:
        """Return cached value, or None if missing / expired."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """Store value under key with TTL. Overwrites if key exists."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a key. No-op if missing."""

    @abstractmethod
    def flush(self) -> None:
        """Clear all cached entries. Used in tests."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name for logging."""


# ── Memory backend (current) ──────────────────────────────────────────────────

class MemoryBackend(CacheBackend):
    """
    In-process dict with manual TTL enforcement.
    Fast, zero dependencies. Entries are lost on process restart.
    Works fine for a single-worker deployment (Render free + GitHub Actions).
    """

    def __init__(self) -> None:
        # {key: (value, expires_at)}
        self._store: dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        self._store[key] = (value, time.time() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def flush(self) -> None:
        self._store.clear()

    def name(self) -> str:
        return "memory"


# ── Redis backend (upgrade path via Upstash) ──────────────────────────────────

class RedisBackend(CacheBackend):
    """
    Redis cache via Upstash HTTP API (no persistent TCP connection needed —
    works in serverless / short-lived GitHub Actions workers).

    Activate by setting:
      UPSTASH_REDIS_URL   = https://<your-db>.upstash.io
      UPSTASH_REDIS_TOKEN = <your-token>

    Values are JSON-serialised so any Python dict/list/scalar is supported.
    Requires: pip install upstash-redis
    """

    def __init__(self) -> None:
        url   = os.environ.get("UPSTASH_REDIS_URL", "")
        token = os.environ.get("UPSTASH_REDIS_TOKEN", "")
        if not url or not token:
            raise EnvironmentError(
                "UPSTASH_REDIS_URL and UPSTASH_REDIS_TOKEN are required for RedisBackend."
            )
        try:
            from upstash_redis import Redis  # type: ignore
            self._redis = Redis(url=url, token=token)
        except ImportError:
            raise ImportError("upstash-redis package not installed. Run: pip install upstash-redis")

    def get(self, key: str) -> Any | None:
        try:
            import json
            raw = self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        except Exception as exc:
            print(f"[cache/redis] get({key}) failed: {exc}")
            return None

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        try:
            import json
            self._redis.set(key, json.dumps(value), ex=ttl_seconds)
        except Exception as exc:
            print(f"[cache/redis] set({key}) failed: {exc}")

    def delete(self, key: str) -> None:
        try:
            self._redis.delete(key)
        except Exception as exc:
            print(f"[cache/redis] delete({key}) failed: {exc}")

    def flush(self) -> None:
        try:
            self._redis.flushdb()
        except Exception as exc:
            print(f"[cache/redis] flush() failed: {exc}")

    def name(self) -> str:
        return "redis"


# ── Factory ───────────────────────────────────────────────────────────────────

_cache_instance: CacheBackend | None = None


def get_cache() -> CacheBackend:
    """
    Auto-select and return the singleton cache backend.

    Priority:
      1. UPSTASH_REDIS_URL + UPSTASH_REDIS_TOKEN set → RedisBackend
      2. Otherwise                                    → MemoryBackend

    The singleton is safe to reuse across threads — all backends are
    thread-safe (MemoryBackend uses CPython GIL; Redis is inherently safe).
    """
    global _cache_instance
    if _cache_instance is not None:
        return _cache_instance

    if os.environ.get("UPSTASH_REDIS_URL") and os.environ.get("UPSTASH_REDIS_TOKEN"):
        try:
            _cache_instance = RedisBackend()
            print("[cache] Using RedisBackend (Upstash).")
            return _cache_instance
        except Exception as exc:
            print(f"[cache] RedisBackend init failed ({exc}), falling back to Memory.")

    _cache_instance = MemoryBackend()
    print("[cache] Using MemoryBackend.")
    return _cache_instance


def reset_cache() -> None:
    """Force re-selection of the backend (useful in tests)."""
    global _cache_instance
    _cache_instance = None


# ── Convenience helpers (same interface as the old signal_cache pattern) ──────

# TTL constants (seconds)
TTL_SIGNAL   = 5  * 24 * 3600   # 5 days  — sentiment / insider signals
TTL_MACRO    = 4  * 3600        # 4 hours — Fear & Greed, FRED rates, regime
TTL_SCREENER = 8  * 3600        # 8 hours — pre-scored screener candidates
TTL_CONGRESS = 24 * 3600        # 24 hours — congressional trade filings


def cache_get(key: str) -> Any | None:
    """Get a value from the active cache backend."""
    return get_cache().get(key)


def cache_set(key: str, value: Any, ttl_seconds: int = TTL_SIGNAL) -> None:
    """Set a value in the active cache backend with TTL."""
    get_cache().set(key, value, ttl_seconds)


def cache_delete(key: str) -> None:
    """Delete a key from the active cache backend."""
    get_cache().delete(key)
