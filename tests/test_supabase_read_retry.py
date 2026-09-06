"""🔴 MEASURED 2026-09-04..06: 4 of 6 `full_sweep` runs logged "Server
disconnected" on `read_user`/`read_all_users`. ZERO canary runs did.

That asymmetry is the diagnosis, not a coincidence: `full_sweep` is the long job
that walks every endpoint, so a pooled keep-alive connection sits idle long
enough for the server to drop it and the next request on that dead socket fails.
Not a Supabase outage — a fresh connection works immediately.

🔑 Reads are side-effect-free, so retrying them is safe. Writes are NOT retried:
`write_user` is a compare-and-swap whose caller owns the retry loop, and
re-driving it from the storage layer would be a second writer.

⚠️ THE RAISE IS LOAD-BEARING. `_row_mutate` reads a version then writes with it,
so a read that returned None instead of raising would write over another user's
data with a stale version. Exhausting the retries must still raise.
"""
import pytest

from storage import SupabaseBackend


class _Fake(SupabaseBackend):
    """Bypass __init__ (which builds a real client and probes the schema)."""
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def _next(self):
        self.calls += 1
        v = self._outcomes.pop(0)
        if isinstance(v, Exception):
            raise v
        return v


DISCONNECT = Exception("Server disconnected without sending a response.")
RLS = Exception('42501: new row violates row-level security policy')


class TestWhatCountsAsTransient:
    @pytest.mark.parametrize("msg", [
        "Server disconnected without sending a response.",
        "Connection reset by peer",
        "('Connection aborted.', RemoteDisconnected(...))",
        "Read timed out.",
        "503 Service temporarily unavailable",
    ])
    def test_connection_layer_failures_are_transient(self, msg):
        assert SupabaseBackend._is_transient(Exception(msg))

    @pytest.mark.parametrize("msg", [
        '42501: new row violates row-level security policy',
        "Invalid API key",
        'relation "user_records" does not exist',
        "JWT expired",
    ])
    def test_permanent_failures_are_NOT_transient(self, msg):
        """⚠️ A whitelist on purpose. Retrying a permission error turns a clear
        failure into a slow one and hides the real cause."""
        assert not SupabaseBackend._is_transient(Exception(msg))


class TestReadRetry:
    def test_a_disconnect_then_success_returns_the_data(self):
        b = _Fake([DISCONNECT, ({"a": 1}, 7)])
        assert b._read_with_retry("x", b._next, delay_s=0) == ({"a": 1}, 7)
        assert b.calls == 2

    def test_two_disconnects_then_success_still_recovers(self):
        b = _Fake([DISCONNECT, DISCONNECT, ({"a": 1}, 7)])
        assert b._read_with_retry("x", b._next, delay_s=0) == ({"a": 1}, 7)
        assert b.calls == 3

    def test_exhausting_the_retries_RAISES(self):
        """🔑 The no-clobber guarantee. Returning None here would make
        _row_mutate write with a stale version."""
        b = _Fake([DISCONNECT, DISCONNECT, DISCONNECT])
        with pytest.raises(Exception, match="Server disconnected"):
            b._read_with_retry("x", b._next, delay_s=0)
        assert b.calls == 3

    def test_a_PERMANENT_error_raises_at_once_without_retrying(self):
        b = _Fake([RLS, ({"a": 1}, 7)])
        with pytest.raises(Exception, match="42501"):
            b._read_with_retry("x", b._next, delay_s=0)
        assert b.calls == 1, "a permission error must not be retried"

    def test_success_first_time_costs_nothing(self):
        b = _Fake([({"a": 1}, 7)])
        assert b._read_with_retry("x", b._next, delay_s=0) == ({"a": 1}, 7)
        assert b.calls == 1


class TestTheRealReadPathsUseIt:
    def _wire(self, monkeypatch, outcomes):
        b = _Fake([])
        seq = list(outcomes)
        calls = {"n": 0}

        class _Q:
            def table(self, *a, **k): return self
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def maybe_single(self, *a, **k): return self
            def execute(self):
                calls["n"] += 1
                v = seq.pop(0)
                if isinstance(v, Exception):
                    raise v
                return v
        b._client = _Q()
        return b, calls

    def test_read_user_recovers_from_a_disconnect(self, monkeypatch):
        monkeypatch.setattr("storage.time.sleep", lambda s: None)
        resp = type("R", (), {"data": {"content": {"k": "v"}, "version": 3}})()
        b, calls = self._wire(monkeypatch, [DISCONNECT, resp])
        assert b.read_user("user_configs.json", "u1") == ({"k": "v"}, 3)
        assert calls["n"] == 2

    def test_read_all_users_recovers_from_a_disconnect(self, monkeypatch):
        monkeypatch.setattr("storage.time.sleep", lambda s: None)
        resp = type("R", (), {"data": [{"chat_id": "u1", "content": {"k": "v"}}]})()
        b, calls = self._wire(monkeypatch, [DISCONNECT, resp])
        assert b.read_all_users("user_configs.json") == {"u1": {"k": "v"}}
        assert calls["n"] == 2

    def test_read_user_still_raises_when_it_never_recovers(self, monkeypatch):
        monkeypatch.setattr("storage.time.sleep", lambda s: None)
        b, calls = self._wire(monkeypatch, [DISCONNECT, DISCONNECT, DISCONNECT])
        with pytest.raises(Exception, match="Server disconnected"):
            b.read_user("user_configs.json", "u1")


class TestWritesAreNotRetriedHere:
    def test_write_user_does_not_go_through_the_read_retry(self):
        """⚠️ write_user is CAS; its caller owns the retry loop. Re-driving it
        from this layer would be a second writer racing the first."""
        import inspect
        src = inspect.getsource(SupabaseBackend.write_user)
        assert "_read_with_retry" not in src
