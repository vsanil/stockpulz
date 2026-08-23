"""🔴 The canary read every file through the raw Gist API while production and
CI resolve to Supabase — so it monitored the ROLLBACK COPY, not the app.

Two live consequences, both measured 2026-08-23:
  • `data.completeness` FAILED on a 2026-08-19 stamp while Supabase held
    2026-08-21. A false alarm, and it fired self_heal.
  • `check_mutations` WROTE through the app (Supabase) and restored the GIST,
    so the restore undid nothing. Supabase's price_alerts.json had 10 keys to
    the Gist's 6, including a synthetic `999000999`.

A monitor that reads a different store than the app is not monitoring the app.
"""
import ast
import importlib.util
import os
import pathlib

import pytest

SRC = pathlib.Path("scripts/canary.py")


def _canary():
    spec = importlib.util.spec_from_file_location("canary_t", str(SRC.resolve()))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Rows:
    """Row backend, like Supabase."""
    def __init__(self, data=None):
        self.data = data or {}
        self.versions = {}
        self.writes = []

    def name(self): return "supabase"
    def supports_rows(self): return True
    def read(self, fn): return self.data.get(fn)
    def write(self, fn, v): self.data[fn] = v; self.writes.append(fn)
    def read_all_users(self, fn): return dict(self.data.get(fn) or {})
    def read_user(self, fn, uid):
        return (self.data.get(fn, {}).get(uid), self.versions.get((fn, uid), 1))
    def write_user(self, fn, uid, content, expected_version):
        self.data.setdefault(fn, {})[uid] = content
        self.writes.append((fn, uid))


class TestReadsGoThroughTheAppsBackend:
    def test_no_data_read_uses_the_raw_gist_api(self):
        """Only the headroom check may touch the Gist directly — it measures
        GitHub's per-file limit, which is a property of the Gist alone."""
        tree = ast.parse(SRC.read_text())
        callers = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "_gist_all"):
                    callers.append(fn.name)
        assert callers == ["check_storage_headroom"], (
            f"_gist_all() is called from {callers} — those read the rollback "
            f"copy, not the store the app writes to"
        )

    def test_user_keyed_files_are_read_as_ROWS(self):
        """read() hits `documents`; user-keyed files live in `user_records`.
        Using read() reports them EMPTY — the false alarm that nearly caused a
        rollback on 2026-08-22."""
        m = _canary()
        b = _Rows({"price_alerts.json": {"42": [{"ticker": "AAPL"}]}})
        m._store = lambda: b
        assert m._store_read("price_alerts.json") == {"42": [{"ticker": "AAPL"}]}

    def test_documents_are_read_as_documents(self):
        m = _canary()
        m._store = lambda: _Rows({"picks.json": {"_saved_date": "2026-08-22"}})
        assert m._raw_picks()["_saved_date"] == "2026-08-22"


class TestRestoreActuallyRestores:
    def test_a_row_the_run_CREATED_is_removed(self):
        """🔴 The residue bug. A Gist restore rewrote the whole blob so a new
        chat_id vanished for free; rows persist and must be tombstoned."""
        m = _canary()
        b = _Rows({"price_alerts.json": {"42": [{"ticker": "AAPL"}]}})
        m._store = lambda: b
        snap = m._snapshot()
        b.data["price_alerts.json"]["999000999"] = [{"ticker": "ST"}]   # canary residue
        m._restore(snap)
        assert not b.data["price_alerts.json"]["999000999"], \
            "the synthetic row survived the restore — this is how 999000999 got there"

    def test_a_modified_row_is_put_back(self):
        m = _canary()
        b = _Rows({"user_paper.json": {"42": {"cash": 1000}}})
        m._store = lambda: b
        snap = m._snapshot()
        b.data["user_paper.json"]["42"] = {"cash": 3}
        m._restore(snap)
        assert b.data["user_paper.json"]["42"] == {"cash": 1000}

    def test_the_snapshot_is_taken_from_the_live_backend(self):
        m = _canary()
        b = _Rows({"user_trades.json": {"7": {"open": []}}})
        m._store = lambda: b
        assert m._snapshot()["user_trades.json"] == {"7": {"open": []}}

    def test_an_unreadable_file_does_not_abort_the_snapshot(self):
        m = _canary()
        class _Bad(_Rows):
            def read_all_users(self, fn): raise RuntimeError("boom")
        m._store = lambda: _Bad({})
        snap = m._snapshot()
        assert set(snap) == set(m._SNAP_FILES)

    def test_a_file_missing_from_the_snapshot_is_never_written(self):
        """Restoring None would blank a store the run could not read."""
        m = _canary()
        b = _Rows({})
        m._store = lambda: b
        m._restore({"trade_log.json": None})
        assert b.writes == []


class TestSurfaceAgreement:
    """🔴 Render wrote to the Gist while GitHub Actions wrote to Supabase for
    four days and nothing said so. Configuration closed that split-brain once
    before, in Aug 19's d11a1c9, and it reopened because no monitor stood
    behind the configuration."""

    def _run(self, m, mine, health, monkeypatch):
        m._store = lambda: type("B", (), {"name": staticmethod(lambda: mine)})()
        monkeypatch.setattr(m.requests, "get",
                            lambda *a, **k: type("R", (), {"json": staticmethod(lambda: health)})())
        m.RESULTS.clear()
        m.check_storage_surfaces()
        return dict((n, (ok, note)) for n, ok, note in m.RESULTS)["storage.surfaces"]

    def test_agreement_passes(self, monkeypatch):
        ok, _ = self._run(_canary(), "supabase", {"storage": "supabase"}, monkeypatch)
        assert ok

    def test_the_exact_aug_23_split_fails(self, monkeypatch):
        m = _canary()
        ok, note = self._run(m, "supabase", {"storage": "gist"}, monkeypatch)
        assert not ok and "SPLIT BRAIN" in note

    @pytest.mark.parametrize("health", [{}, {"storage": "unavailable"}])
    def test_an_unusable_answer_is_NOT_VERIFIED_not_a_pass(self, health, monkeypatch):
        ok, note = self._run(_canary(), "supabase", health, monkeypatch)
        assert ok and "NOT VERIFIED" in note, \
            "a green line must never imply a check that could not run"

    def test_an_unreachable_service_does_not_cry_wolf(self, monkeypatch):
        m = _canary()
        m._store = lambda: type("B", (), {"name": staticmethod(lambda: "gist")})()
        def _boom(*a, **k): raise OSError("down")
        monkeypatch.setattr(m.requests, "get", _boom)
        m.RESULTS.clear()
        m.check_storage_surfaces()
        ok, note = [(o, n) for k, o, n in m.RESULTS if k == "storage.surfaces"][0]
        assert ok and "NOT VERIFIED" in note

    def test_it_is_registered_in_the_run(self):
        assert "check_storage_surfaces," in SRC.read_text(), \
            "the check exists but nothing calls it"
