"""Row-per-user backend + compare-and-swap — the actual Supabase migration payoff.

The Gist could only rewrite a whole file, so writing user A clobbered user B and
a same-user race was unfixable (its PATCH rejects If-Match, verified 400). With
one row per user and a version token, different users never collide and a
same-user conflict is DETECTED and retried instead of silently losing a write.
"""
import pytest
import config_manager as cm


class _RowBackend:
    """Minimal row store with version-based CAS, mirroring upsert_user_record()."""
    def __init__(self):
        self.rows = {}                      # (filename, chat_id) -> [content, version]
        self.blobs = {}
        self.hook = None                    # optional: fires before a write (race sim)

    def supports_rows(self): return True
    def name(self): return "rowfake"
    def read(self, fn): return self.blobs.get(fn)
    def read_strict(self, fn): return self.blobs.get(fn)
    def write(self, fn, data): self.blobs[fn] = data

    def read_user(self, fn, uid):
        import copy
        row = self.rows.get((fn, str(uid)))
        return (copy.deepcopy(row[0]), row[1]) if row else (None, None)

    def write_user(self, fn, uid, content, expected_version):
        import copy
        if self.hook:
            h, self.hook = self.hook, None   # fire once
            h()
        key = (fn, str(uid))
        row = self.rows.get(key)
        if row is None:
            if expected_version is not None:
                return None
            self.rows[key] = [copy.deepcopy(content), 1]
            return 1
        if row[1] != expected_version:
            return None                      # CAS conflict
        row[0], row[1] = copy.deepcopy(content), row[1] + 1
        return row[1]

    def read_all_users(self, fn):
        import copy
        return {uid: copy.deepcopy(c) for (f, uid), (c, _v) in
                ((k, v) for k, v in self.rows.items()) if f == fn}


@pytest.fixture
def rb(monkeypatch):
    import storage
    b = _RowBackend()
    monkeypatch.setattr(storage, "get_storage_backend", lambda: b)
    storage.reset_backend()
    cm._gist_read_cache.clear()
    cm._recent_user_writes.clear()
    return b


class TestRowIsolation:
    def test_writing_one_user_cannot_touch_another(self, rb):
        cm._update_user_keyed_file("f.json", "u1", {"v": 1})
        cm._update_user_keyed_file("f.json", "u2", {"v": 2})
        assert rb.read_user("f.json", "u1")[0] == {"v": 1}
        assert rb.read_user("f.json", "u2")[0] == {"v": 2}

    def test_rows_reassemble_into_the_legacy_mapping(self, rb):
        """Callers still expect {chat_id: record}; rows must rebuild it exactly.
        (Asserted on the backend: conftest patches _load_gist_file itself, so
        routing through it here would test the mock, not this code.)"""
        cm._update_user_keyed_file("user_trades.json", "u1", {"open": ["A"]})
        cm._update_user_keyed_file("user_trades.json", "u2", {"open": ["B"]})
        assert rb.read_all_users("user_trades.json") == {
            "u1": {"open": ["A"]}, "u2": {"open": ["B"]}}

    def test_user_keyed_file_set_covers_the_per_user_stores(self):
        assert {cm.USER_TRADES_FILE, cm.USER_PAPER_FILE, cm.USER_CONFIGS_FILE,
                cm.PRICE_ALERTS_FILE} <= cm.USER_KEYED_FILES


class TestCompareAndSwap:
    def test_conflicting_same_user_write_is_retried_not_lost(self, rb):
        cm.mutate_user_record("f.json", "u1", lambda r: ({"items": ["first"]}, None))

        # another process appends while our mutator is mid-flight
        def _interleave():
            row = rb.rows[("f.json", "u1")]
            row[0] = {"items": row[0]["items"] + ["other-process"]}
            row[1] += 1
        rb.hook = _interleave

        def _mut(rec):
            rec = dict(rec or {"items": []})
            rec["items"] = rec["items"] + ["mine"]
            return rec, "ok"

        assert cm.mutate_user_record("f.json", "u1", _mut) == "ok"
        final = rb.read_user("f.json", "u1")[0]["items"]
        assert "other-process" in final, "the concurrent write was clobbered"
        assert "mine" in final, "our own write was lost"

    def test_gives_up_loudly_rather_than_clobbering(self, rb):
        cm.mutate_user_record("f.json", "u1", lambda r: ({"n": 0}, None))
        # every attempt conflicts
        def _always():
            rb.rows[("f.json", "u1")][1] += 1
            rb.hook = _always
        rb.hook = _always
        with pytest.raises(RuntimeError, match="CAS"):
            cm.mutate_user_record("f.json", "u1", lambda r: ({"n": 1}, "x"))


class TestMutateGistFileIsRowAware:
    """price_alerts.json is USER-KEYED, but add_alert/remove_alert go through
    mutate_gist_file. If that wrote the whole-blob `documents` table while readers
    reassembled from `user_records` rows, alerts would be written to one place and
    read from another — silently broken on switchover, and green in Gist testing."""

    def test_user_keyed_mutation_persists_as_rows(self, rb):
        F = cm.PRICE_ALERTS_FILE
        cm.mutate_gist_file(F, lambda cur: ({**cur, "u1": [{"ticker": "AAPL"}]}, "ok"),
                            default={})
        # must land in ROWS (where readers look), not the blob table
        assert rb.read_user(F, "u1")[0] == [{"ticker": "AAPL"}]
        assert F not in rb.blobs, "must not write the whole-blob documents table"

    def test_second_user_mutation_preserves_the_first(self, rb):
        F = cm.PRICE_ALERTS_FILE
        cm.mutate_gist_file(F, lambda c: ({**c, "u1": [{"t": "A"}]}, None), default={})
        cm.mutate_gist_file(F, lambda c: ({**c, "u2": [{"t": "B"}]}, None), default={})
        assert rb.read_user(F, "u1")[0] == [{"t": "A"}]
        assert rb.read_user(F, "u2")[0] == [{"t": "B"}]

    def test_mutator_sees_existing_rows(self, rb):
        F = cm.PRICE_ALERTS_FILE
        cm.mutate_gist_file(F, lambda c: ({**c, "u1": ["x"]}, None), default={})
        seen = {}
        cm.mutate_gist_file(F, lambda c: (seen.update(c) or c, None), default={})
        assert seen == {"u1": ["x"]}, "mutator must receive the reassembled mapping"

    def test_non_user_keyed_file_still_uses_the_blob_path(self, rb):
        cm.mutate_gist_file("picks.json", lambda c: ({"a": 1}, None), default={})
        assert rb.blobs["picks.json"] == {"a": 1}
