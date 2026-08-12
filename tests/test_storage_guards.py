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
