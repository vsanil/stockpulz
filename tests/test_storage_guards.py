"""Storage guards — the highest-consequence class in this project.

Every data-loss incident here came from the same shape: a read that quietly
returned nothing, then a write that treated "nothing" as the truth. Three
guards exist because of three real incidents:

  * `read_strict` RAISES instead of returning None, so a transient fetch failure
    can never become `or {}` and erase every user.
  * `_gist_content` refetches via `raw_url` when GitHub truncates past ~1 MB —
    otherwise partial JSON parses as "file missing" and invites a clobber.
  * `get_storage_backend()` stays on the Gist unless BOTH Supabase vars are set;
    the migration is built but DORMANT, and switching it accidentally would
    write to a different store than readers read from.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import storage


class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestTruncationGuard:
    """GitHub truncates gist file content past ~1 MB and sets truncated:true
    with a raw_url. Reading `content` blindly yields PARTIAL JSON — which on the
    swallowing read path looks like a missing file."""

    def test_untruncated_content_is_returned_directly(self, monkeypatch):
        called = []
        monkeypatch.setattr(storage.requests, "get",
                            lambda *a, **k: called.append(1) or _Resp("{}"))
        out = storage._gist_content({"content": '{"a":1}', "truncated": False}, {})
        assert out == '{"a":1}'
        assert not called, "must not refetch when the payload is complete"

    def test_truncated_content_is_REFETCHED_from_raw_url(self, monkeypatch):
        full = '{"a":1,"b":2,"c":3}'
        seen = {}

        def _get(url, headers=None, timeout=None):
            seen["url"] = url
            return _Resp(full)

        monkeypatch.setattr(storage.requests, "get", _get)
        meta = {"content": '{"a":1,"b"', "truncated": True,
                "raw_url": "https://gist.githubusercontent.com/x/raw"}
        assert storage._gist_content(meta, {}) == full
        assert seen["url"].endswith("/raw")

    def test_truncated_without_a_raw_url_falls_back_rather_than_crashing(self, monkeypatch):
        monkeypatch.setattr(storage.requests, "get",
                            lambda *a, **k: _Resp("should not be used"))
        out = storage._gist_content({"content": "partial", "truncated": True}, {})
        assert out == "partial"

    def test_a_failed_refetch_propagates_rather_than_returning_partial(self, monkeypatch):
        """Silently returning the truncated half is the dangerous outcome —
        it parses as a smaller-but-valid file and invites a clobber."""
        monkeypatch.setattr(storage.requests, "get",
                            lambda *a, **k: _Resp("", status=500))
        with pytest.raises(Exception):
            storage._gist_content({"content": "partial", "truncated": True,
                                   "raw_url": "https://x/raw"}, {})


class TestBackendSelectionStaysDormant:
    """The Supabase migration is BUILT but must stay off until a verified
    migration runs — the owner's SUPABASE_* credentials once pointed at a
    different app's project holding stale StockPulz data."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        storage.reset_backend()
        yield
        storage.reset_backend()

    def test_gist_is_the_default(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        assert isinstance(storage.get_storage_backend(), storage.GistBackend)

    @pytest.mark.parametrize("url,key", [("https://x.supabase.co", ""), ("", "k")])
    def test_ONE_supabase_var_is_not_enough(self, monkeypatch, url, key):
        """A half-set pair must not flip the backend — readers and writers would
        end up on different stores."""
        monkeypatch.setenv("SUPABASE_URL", url)
        monkeypatch.setenv("SUPABASE_KEY", key)
        assert isinstance(storage.get_storage_backend(), storage.GistBackend)

    def test_the_backend_is_a_singleton(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        assert storage.get_storage_backend() is storage.get_storage_backend()

    def test_reset_forces_reselection(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        a = storage.get_storage_backend()
        storage.reset_backend()
        assert storage.get_storage_backend() is not a

    def test_gist_backend_does_not_claim_row_support(self):
        """`supports_rows()` False is what keeps every path byte-identical to
        pre-migration behaviour."""
        assert storage.GistBackend().supports_rows() is False


class TestSupabaseSchemaGuard:
    """🔴 The 2026-08-19 production outage.

    SUPABASE_URL and SUPABASE_KEY were set on Render while supabase_schema.sql
    had never been applied. Construction SUCCEEDED (the client is lazy), so
    SupabaseBackend took over ALL storage and every per-user read then failed —
    40 on user_trades.json, 18 on user_configs.json, 4 each on price_alerts and
    backtest_trades in one 45-minute window — while the mini-app and bot showed
    nothing and the Saturday weekly run set zero alerts.

    get_storage_backend() already falls back to Gist when construction raises.
    It simply never got the chance.
    """

    def _client(self, missing=(), hang=(), other_error=(), write_error=None):
        class _Q:
            def __init__(self, table): self.t = table
            def select(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def delete(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def execute(self):
                import time
                if self.t in hang:
                    time.sleep(30)
                if self.t in missing:
                    raise Exception(
                        "{'message': \"Could not find the table 'public."
                        f"{self.t}' in the schema cache\", 'code': 'PGRST205'}}")
                if self.t in other_error:
                    raise Exception("connection refused")
                return type("R", (), {"data": []})()
        class _Rpc:
            def __init__(self, err): self.err = err
            def execute(self):
                if self.err:
                    raise Exception(self.err)
                return type("R", (), {"data": 1})()
        class _C:
            def table(self_inner, name): return _Q(name)
            def rpc(self_inner, name, params): return _Rpc(write_error)
        return _C()

    def _backend(self, monkeypatch, client):
        import storage
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "test-key")
        b = storage.SupabaseBackend.__new__(storage.SupabaseBackend)
        b._client = client
        return b

    def test_a_missing_user_records_table_raises(self, monkeypatch):
        b = self._backend(monkeypatch, self._client(missing=("user_records",)))
        with pytest.raises(RuntimeError) as e:
            b._verify_schema(timeout=1.0)
        assert "user_records" in str(e.value)
        assert "unset SUPABASE_URL" in str(e.value), "the message must name the rollback"

    def test_a_missing_documents_table_raises(self, monkeypatch):
        b = self._backend(monkeypatch, self._client(missing=("documents",)))
        with pytest.raises(RuntimeError):
            b._verify_schema(timeout=1.0)

    def test_a_complete_schema_passes(self, monkeypatch):
        b = self._backend(monkeypatch, self._client())
        b._verify_schema(timeout=1.0)          # must not raise

    def test_an_unreachable_supabase_raises_rather_than_taking_over(self, monkeypatch):
        """A backend we cannot verify must not be trusted with storage while a
        working Gist is sitting right there."""
        b = self._backend(monkeypatch, self._client(other_error=("documents",)))
        with pytest.raises(RuntimeError) as e:
            b._verify_schema(timeout=1.0)
        assert "unreachable or unverifiable" in str(e.value)

    def test_a_hanging_probe_times_out_instead_of_blocking_boot(self, monkeypatch):
        import time
        b = self._backend(monkeypatch, self._client(hang=("documents",)))
        t0 = time.time()
        with pytest.raises(RuntimeError) as e:
            b._verify_schema(timeout=0.5)
        assert time.time() - t0 < 5, "a hung probe must not stall startup"
        assert "timed out" in str(e.value)

    def test_rls_blocked_writes_raise_even_though_the_schema_reads_fine(self, monkeypatch):
        """🔴 2026-08-21: SELECT under RLS just returns fewer rows (no error),
        so the schema-existence probe alone stayed green while every real
        write ('new row violates row-level security policy...') threw into
        production handlers. Must be caught here, at construction."""
        b = self._backend(monkeypatch, self._client(
            write_error="{'message': 'new row violates row-level security "
                        "policy for table \"user_records\"', 'code': '42501'}"))
        with pytest.raises(RuntimeError) as e:
            b._verify_schema(timeout=1.0)
        msg = str(e.value)
        assert "not writable" in msg
        assert "service_role" in msg, "must point at the fix: wrong key or missing RLS policy"

    def test_a_writable_schema_passes(self, monkeypatch):
        b = self._backend(monkeypatch, self._client())
        b._verify_schema(timeout=1.0)          # must not raise

    def test_get_storage_backend_FALLS_BACK_to_gist_when_the_schema_is_missing(self, monkeypatch):
        """The whole point: the outage becomes one startup line and no impact."""
        import storage
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_KEY", "test-key")
        monkeypatch.setenv("GH_GIST_TOKEN", "t")
        monkeypatch.setenv("GIST_ID", "g")
        monkeypatch.setattr(storage, "_backend_instance", None)

        def _boom(*a, **k):
            raise RuntimeError("Supabase schema incomplete — missing table(s): user_records.")
        monkeypatch.setattr(storage, "SupabaseBackend", _boom)

        b = storage.get_storage_backend()
        assert isinstance(b, storage.GistBackend), \
            "a half-configured Supabase must never take over storage"
        monkeypatch.setattr(storage, "_backend_instance", None)
