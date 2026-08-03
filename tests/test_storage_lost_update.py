"""Regression: cross-user LOST UPDATE from GitHub's read-after-write lag.

Observed live (Aug 3): tagging the owner's 34 trades and then the test account's
4 silently erased the owner's. The per-file lock serializes our writes and
read_strict raises on fetch errors — but neither helps when a fresh GET moments
after a PATCH still returns the PRE-write copy. That stale copy became the merge
base for the next user's write, wiping the previous one.
"""
import time
import pytest
import config_manager as cm


class _LaggyGist:
    """Gist whose reads lag one write behind — exactly the real failure mode."""
    server = {}
    served = {}          # what a read currently returns (one write behind)

    def read_strict(self, filename):
        return _LaggyGist.served.get(filename)

    def write(self, filename, data):
        import copy
        # the write lands on the server, but reads keep serving the OLD copy
        _LaggyGist.served[filename] = copy.deepcopy(
            _LaggyGist.server.get(filename))
        _LaggyGist.server[filename] = copy.deepcopy(data)

    def name(self):
        return "laggy"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    import storage
    _LaggyGist.server, _LaggyGist.served = {}, {}
    cm._recent_user_writes.clear()
    cm._gist_read_cache.clear()
    monkeypatch.setattr(storage, "GistBackend", _LaggyGist)
    yield
    cm._recent_user_writes.clear()


class TestCrossUserLostUpdate:
    def test_second_user_write_does_not_erase_first(self):
        cm._update_user_keyed_file("trades.json", "ownerA", {"trades": ["tagged"]})
        cm._update_user_keyed_file("trades.json", "testB", {"trades": ["bot"]})
        final = _LaggyGist.server["trades.json"]
        assert final.get("testB") == {"trades": ["bot"]}
        assert final.get("ownerA") == {"trades": ["tagged"]}, \
            "user A's write was silently erased by user B's stale merge base"

    def test_three_rapid_users_all_survive(self):
        for uid in ("u1", "u2", "u3"):
            cm._update_user_keyed_file("f.json", uid, {"v": uid})
        final = _LaggyGist.server["f.json"]
        assert {k: v["v"] for k, v in final.items()} == {"u1": "u1", "u2": "u2", "u3": "u3"}

    def test_same_user_rewrite_keeps_latest(self):
        cm._update_user_keyed_file("f.json", "u1", {"v": 1})
        cm._update_user_keyed_file("f.json", "u1", {"v": 2})
        assert _LaggyGist.server["f.json"]["u1"] == {"v": 2}

    def test_echo_expires_so_another_process_is_not_stomped_forever(self, monkeypatch):
        cm._update_user_keyed_file("f.json", "u1", {"v": "mine"})
        # simulate the echo ageing out past the lag window
        vals = cm._recent_user_writes["f.json"]["u1"]
        cm._recent_user_writes["f.json"]["u1"] = (vals[0], time.time() - cm._WRITE_ECHO_TTL - 1)
        # another process legitimately updated u1; server now serves that
        _LaggyGist.served["f.json"] = {"u1": {"v": "theirs"}}
        cm._update_user_keyed_file("f.json", "u2", {"v": "b"})
        assert _LaggyGist.server["f.json"]["u1"] == {"v": "theirs"}, \
            "an expired echo must not resurrect our stale value"
