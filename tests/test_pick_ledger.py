"""The pick ledger's storage — one row per month in the primary store.

Moved off the Gist on 2026-11-14: the ledger had pushed the Gist past the API's
response limit, and every file sorting after it came back EMPTY (the synthetic
bot lost track of its positions for twelve days). See pick_ledger.py.

What is pinned, and why:
  * rows are routed by their OWN date, so a clock near midnight cannot file a
    pick under the wrong month;
  * a save MERGES by key on a fresh read — re-saving is a no-op, and a
    concurrent writer (the evaluator and the arms do overlap) is retried
    against, never overwritten;
  * a save is only reported as done once the rows are READ BACK;
  * an unreadable ledger RAISES — never a partial ledger presented as whole;
  * a silent fallback to the Gist is refused, not written to;
  * the legacy Gist files are merged on read until removed, and removal only
    happens after every legacy row is verified in the store.
"""
import copy
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_manager as cm  # noqa: E402
import pick_ledger as pl  # noqa: E402
import storage  # noqa: E402


class _RowStore:
    """Supabase's user_records, with the real compare-and-swap semantics."""

    def __init__(self):
        self.rows = {}              # (filename, key) -> [content, version]
        self.before_write = None    # fires once, just before a write lands
        self.lose_writes = False    # accept a write but keep nothing
        self.fail_reads = False

    def supports_rows(self): return True
    def name(self): return "supabase"
    def read(self, fn): return None
    def read_strict(self, fn): return None
    def write(self, fn, data): raise AssertionError("the ledger must never write a blob here")

    def read_user(self, fn, key):
        if self.fail_reads:
            raise RuntimeError("Server disconnected")
        row = self.rows.get((fn, str(key)))
        return (copy.deepcopy(row[0]), row[1]) if row else (None, None)

    def write_user(self, fn, key, content, expected_version):
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook()
        k = (fn, str(key))
        row = self.rows.get(k)
        if row is None:
            if expected_version is not None:
                return None
            self.rows[k] = [copy.deepcopy(content), 1]
            return 1
        if row[1] != expected_version:
            return None
        if not self.lose_writes:
            row[0] = copy.deepcopy(content)
        row[1] += 1
        return row[1]

    def read_all_users(self, fn):
        if self.fail_reads:
            raise RuntimeError("Server disconnected")
        return {k: copy.deepcopy(c) for (f, k), (c, _v) in self.rows.items() if f == fn}


@pytest.fixture
def db(monkeypatch):
    b = _RowStore()
    monkeypatch.setattr(storage, "get_storage_backend", lambda: b)
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    cm._gist_read_cache.clear()
    cm._recent_user_writes.clear()
    return b


@pytest.fixture
def legacy(monkeypatch):
    """The old Gist files, served through GistBackend.read_strict."""
    files = {}
    monkeypatch.setattr(storage.GistBackend, "read_strict",
                        lambda self, fn: copy.deepcopy(files.get(fn)))
    return files


def _row(date, ticker, arm=None, **kw):
    r = {"date": date, "ticker": ticker, "asset": "stock", "timeframe": "short_term",
         "entry": 100.0}
    if arm:
        r["arm"] = arm
    r.update(kw)
    return r


def _keys(rows):
    return sorted(pl.row_key(r) for r in rows)


# ── routing ──────────────────────────────────────────────────────────────────

class TestRouting:
    def test_rows_go_to_their_own_month(self, db, legacy):
        assert pl.save_rows([_row("2026-10-31", "A"), _row("2026-11-01", "B")])
        months = pl.load_months()
        assert _keys(months["2026-10"]) == [("2026-10-31", "A", None)]
        assert _keys(months["2026-11"]) == [("2026-11-01", "B", None)]

    @pytest.mark.parametrize("bad", [None, "", "2026", "11/01/2026", 20261101])
    def test_a_row_without_a_usable_date_is_skipped_not_misfiled(self, db, legacy, bad):
        assert pl.save_rows([{"date": bad, "ticker": "X"}, _row("2026-11-02", "OK")])
        assert list(pl.load_months()) == ["2026-11"]

    def test_non_finite_numbers_are_stored_as_null(self, db, legacy):
        """Postgres JSON rejects NaN outright — one would fail the month's write."""
        pl.save_rows([_row("2026-11-02", "N", screen={"rs": float("nan"),
                                                      "deep": [float("inf"), 1.5]})])
        stored = pl.load_months()["2026-11"][0]["screen"]
        assert stored == {"rs": None, "deep": [None, 1.5]}


# ── merging, idempotency and concurrency ─────────────────────────────────────

class TestMergeSemantics:
    def test_resaving_the_same_rows_adds_nothing(self, db, legacy):
        rows = [_row("2026-11-03", "A"), _row("2026-11-03", "B")]
        pl.save_rows(rows)
        v1 = db.rows[(pl.LEDGER_FILE, "2026-11")][1]
        assert pl.save_rows(copy.deepcopy(rows))
        assert db.rows[(pl.LEDGER_FILE, "2026-11")][1] == v1, "a no-op must not write"
        assert len(pl.load_months()["2026-11"]) == 2

    def test_a_save_keeps_every_row_already_stored(self, db, legacy):
        pl.save_rows([_row("2026-11-03", "OLD")])
        pl.save_rows([_row("2026-11-04", "NEW")])
        assert _keys(pl.load_months()["2026-11"]) == [("2026-11-03", "OLD", None),
                                                      ("2026-11-04", "NEW", None)]

    def test_the_same_ticker_from_two_arms_is_two_rows(self, db, legacy):
        pl.save_rows([_row("2026-11-05", "A", arm="pullback"),
                      _row("2026-11-05", "A", arm="breakout"), _row("2026-11-05", "A")])
        assert len(pl.load_months()["2026-11"]) == 3

    def test_a_concurrent_writer_is_retried_against_not_overwritten(self, db, legacy):
        """The evaluator (09:00 ET, often late) and the arms (08:05 ET) write
        the same month. Simulate the other process landing between our read
        and our write: both sets of rows must survive."""
        pl.save_rows([_row("2026-11-06", "FIRST")])

        def _other_process():
            row = db.rows[(pl.LEDGER_FILE, "2026-11")]
            row[0] = {"picks": row[0]["picks"] + [_row("2026-11-06", "ARM", arm="quality")]}
            row[1] += 1
        db.before_write = _other_process
        assert pl.save_rows([_row("2026-11-06", "EVAL")])
        assert _keys(pl.load_months()["2026-11"]) == [
            ("2026-11-06", "ARM", "quality"), ("2026-11-06", "EVAL", None),
            ("2026-11-06", "FIRST", None)]


# ── verification and failure ────────────────────────────────────────────────

class TestSavesAreVerified:
    def test_a_write_that_did_not_stick_is_reported_as_failed(self, db, legacy):
        pl.save_rows([_row("2026-11-07", "SEED")])
        db.lose_writes = True
        assert pl.save_rows([_row("2026-11-07", "LOST")]) is False

    def test_a_store_that_errors_is_reported_as_failed_not_raised(self, db, legacy):
        db.fail_reads = True
        assert pl.save_rows([_row("2026-11-07", "X")]) is False

    def test_nothing_to_save_is_success(self, db, legacy):
        assert pl.save_rows([]) is True and pl.save_rows(None) is True


class TestReadsNeverPretend:
    def test_an_unreadable_store_raises(self, db, legacy):
        db.fail_reads = True
        with pytest.raises(pl.LedgerUnreadable):
            pl.load_rows()

    def test_an_unreadable_legacy_file_raises(self, db, monkeypatch):
        def _boom(self, fn):
            raise RuntimeError("gist 502")
        monkeypatch.setattr(storage.GistBackend, "read_strict", _boom)
        with pytest.raises(pl.LedgerUnreadable):
            pl.load_rows(include_legacy=True)

    def test_a_silent_fallback_to_the_gist_is_refused(self, monkeypatch, legacy):
        """Supabase configured, but the backend fell back to the Gist (its
        startup probe has timed out before). Writing there would put the day's
        picks where no reader looks."""
        class _Fallback(_RowStore):
            def name(self): return "gist"
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _Fallback())
        monkeypatch.setenv("SUPABASE_URL", "https://x")
        monkeypatch.setenv("SUPABASE_KEY", "k")
        monkeypatch.setattr(pl.time, "sleep", lambda s: None)
        with pytest.raises(pl.LedgerUnreadable):
            pl.load_rows()
        assert pl.save_rows([_row("2026-11-08", "X")]) is False


# ── the bridge from the old Gist files ──────────────────────────────────────

class TestLegacyBridge:
    def test_reads_merge_legacy_and_monthly_without_duplicates(self, db, legacy):
        legacy["pick_ledger_2026.json"] = {"picks": [_row("2026-10-01", "OLD"),
                                                     _row("2026-11-01", "BOTH")]}
        legacy["pick_ledger.json"] = {"picks": [_row("2026-08-04", "OLDEST")]}
        pl.save_rows([_row("2026-11-01", "BOTH"), _row("2026-11-02", "NEW")])
        assert _keys(pl.load_rows(include_legacy=True)) == [("2026-08-04", "OLDEST", None),
                                         ("2026-10-01", "OLD", None),
                                         ("2026-11-01", "BOTH", None),
                                         ("2026-11-02", "NEW", None)]

    def test_migration_copies_every_row_and_is_idempotent(self, db, legacy):
        legacy["pick_ledger_2026.json"] = {"picks": [_row("2026-09-01", "A"),
                                                     _row("2026-10-01", "B", arm="pullback")]}
        rep = pl.migrate_legacy()
        assert rep["copied_ok"] and rep["missing"] == 0
        assert rep["by_month"] == {"2026-09": 1, "2026-10": 1}
        rep2 = pl.migrate_legacy()
        assert rep2["missing"] == 0 and rep2["by_month"] == {"2026-09": 1, "2026-10": 1}
        assert rep["removed"] == [], "removal must never happen without being asked"

    def test_removal_deletes_the_gist_files_only_after_a_verified_copy(self, db, legacy,
                                                                        monkeypatch):
        legacy["pick_ledger_2026.json"] = {"picks": [_row("2026-09-01", "A")]}
        sent = {}

        class _Resp:
            def raise_for_status(self): pass

        def _patch(url, **kw):
            sent.update(kw.get("json") or {})
            for name in (kw.get("json") or {}).get("files", {}):
                legacy.pop(name, None)
            return _Resp()
        monkeypatch.setattr("requests.patch", _patch)
        rep = pl.migrate_legacy(remove=True)
        assert sent == {"files": {n: None for n in pl.LEGACY_GIST_FILES}}
        assert rep["removed"] == list(pl.LEGACY_GIST_FILES)
        assert _keys(pl.load_rows()) == [("2026-09-01", "A", None)], \
            "the rows must still be there after the legacy files are gone"

    def test_an_incomplete_copy_is_never_followed_by_removal(self, db, legacy, monkeypatch):
        legacy["pick_ledger_2026.json"] = {"picks": [_row("2026-09-01", "A")]}
        db.lose_writes = True
        db.rows[(pl.LEDGER_FILE, "2026-09")] = [{"picks": []}, 1]
        monkeypatch.setattr("requests.patch",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("deleted!")))
        rep = pl.migrate_legacy(remove=True)
        assert rep["missing"] == 1 and rep.get("refused") and rep["removed"] == []

    def test_removal_is_refused_when_the_store_is_the_gist_itself(self, monkeypatch, legacy):
        """Deleting the legacy files would not shrink the Gist if the monthly
        rows live in it too — a laptop run must not do that."""
        class _GistMode(_RowStore):
            def name(self): return "gist"
        g = _GistMode()
        monkeypatch.setattr(storage, "get_storage_backend", lambda: g)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.setattr(pl.time, "sleep", lambda s: None)
        monkeypatch.setattr("requests.patch",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("deleted!")))
        legacy["pick_ledger_2026.json"] = {"picks": [_row("2026-09-01", "A")]}
        cm._gist_read_cache.clear()
        rep = pl.migrate_legacy(remove=True)
        assert rep.get("refused") and rep["removed"] == []


# ── the Gist fallback path (a laptop with no SUPABASE_*) ────────────────────

class TestGistBlobMode:
    def test_save_and_load_round_trip_through_one_blob(self, _gist_store, monkeypatch):
        """conftest's in-memory Gist. Monthly rows become one blob keyed by
        month; the save → load round trip must hold (the rule every cache pair
        in this repo carries). No `legacy` fixture here: in Gist mode the
        legacy files and the monthly blob are the SAME store."""
        monkeypatch.setattr(pl.time, "sleep", lambda s: None)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        storage.reset_backend()
        assert pl.save_rows([_row("2026-11-09", "A"), _row("2026-10-09", "B")])
        assert _keys(pl.load_rows()) == [("2026-10-09", "B", None), ("2026-11-09", "A", None)]
        assert set(_gist_store.load(pl.LEDGER_FILE)) == {"2026-10", "2026-11"}


# ── every caller goes through this module ───────────────────────────────────

class TestLegacyIsNoLongerRead:
    def test_a_default_load_never_touches_the_old_gist_files(self, db, monkeypatch):
        """They were removed on 2026-11-14. Looking for them costs two full
        Gist downloads per read, on /admin and on every evaluator run."""
        def _boom(self, fn):
            raise AssertionError(f"read the legacy file {fn}")
        monkeypatch.setattr(storage.GistBackend, "read_strict", _boom)
        pl.save_rows([_row("2026-11-10", "A")])
        assert _keys(pl.load_rows()) == [("2026-11-10", "A", None)]


class TestOneReader:
    def test_evaluate_picks_uses_this_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ev_pl", os.path.join(ROOT, "scripts", "evaluate_picks.py"))
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        assert ev._key({"date": "d", "ticker": "t", "arm": "a"}) == ("d", "t", "a")
        assert ev._key is not None and "pick_ledger" in ev._load_ledger.__code__.co_names

    @pytest.mark.parametrize("rel", ["webhook.py", "scripts/evaluate_picks.py",
                                     "scripts/run_arms.py", "scripts/analyze_engine.py"])
    def test_no_one_reads_a_ledger_file_by_name(self, rel):
        """The /admin actionability card read pick_ledger*.json by name through
        the storage backend — which on Render meant a three-month-old copy.
        AST, not text: comments explaining the fix name those files."""
        import ast
        tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
        names = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value.startswith("pick_ledger") and n.value.endswith(".json")]
        assert names == [], f"{rel} names ledger files directly: {names}"
