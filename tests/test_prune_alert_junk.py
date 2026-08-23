"""Guards for the approved prune of price_alerts.json junk keys.

This script DELETES production data, so its safety rails are the feature. The
classifier is pure, which is what makes it testable without a backend.
"""
import importlib.util
import pathlib

import pytest

SRC = pathlib.Path("scripts/prune_alert_junk.py")


def _m():
    spec = importlib.util.spec_from_file_location("prune_t", str(SRC.resolve()))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PROTECTED = {"1699321994", "8602468968", "900000001",
             "_history_1699321994", "_history_8602468968", "_history_900000001"}


class TestClassifier:
    def test_the_measured_supabase_keys_split_correctly(self):
        """The exact 10 keys observed on 2026-08-23."""
        keys = ["1699321994", "8602468968", "999000999",
                "_history_1699321994", "_history_8602468968",
                "_history__history_1699321994", "_history__history_8602468968",
                "_history__history__history_8602468968"]
        junk, kept = _m().classify(keys, PROTECTED)
        # Set comparison, not a hand-sorted list: '_' sorts AFTER digits, and
        # hand-ordering that is a way to fail for a reason unrelated to the code.
        assert set(junk) == {"999000999",
                             "_history__history_1699321994",
                             "_history__history_8602468968",
                             "_history__history__history_8602468968"}
        assert "1699321994" in kept and "_history_1699321994" in kept

    def test_a_real_users_history_key_is_never_junk(self):
        junk, _ = _m().classify(["_history_1699321994"], PROTECTED)
        assert junk == []

    def test_protection_beats_every_other_rule(self):
        """Even a key that looks like an artifact is kept if it is protected —
        the protected check runs LAST so no rule can override it."""
        junk, _ = _m().classify(["_history__history_x"],
                                PROTECTED | {"_history__history_x"})
        assert junk == []

    def test_an_unknown_chat_id_is_NOT_deleted(self):
        """🔴 The failure that would matter: a real user who has not been added
        to the allowlist yet must not have their alerts pruned. Only NAMED
        synthetic ids go."""
        junk, kept = _m().classify(["555000111"], PROTECTED)
        assert junk == [] and "555000111" in kept

    def test_single_history_prefix_is_not_an_artifact(self):
        m = _m()
        assert not m._is_recursion_artifact("_history_42")
        assert m._is_recursion_artifact("_history__history_42")


class TestSafetyRails:
    def test_it_refuses_to_run_without_the_allowlist(self, monkeypatch, capsys):
        """Fail CLOSED: with no allowlist every key looks unprotected."""
        m = _m()
        import config_manager as cm
        monkeypatch.setattr(cm, "get_allowed_users", lambda: [])
        monkeypatch.setattr("sys.argv", ["prune", "--apply"])
        assert m.main() == 2
        assert "REFUSING TO RUN" in capsys.readouterr().out

    def test_an_unreadable_allowlist_also_refuses(self, monkeypatch, capsys):
        m = _m()
        import config_manager as cm
        def _boom(): raise RuntimeError("gist down")
        monkeypatch.setattr(cm, "get_allowed_users", _boom)
        monkeypatch.setattr("sys.argv", ["prune", "--apply"])
        assert m.main() == 2
        assert "REFUSING TO RUN" in capsys.readouterr().out

    def test_dry_run_is_the_default(self):
        src = SRC.read_text()
        assert 'ap.add_argument("--apply", action="store_true"' in src
        assert "if not args.apply:" in src

    def test_it_reads_back_rather_than_trusting_the_write(self):
        """A 'deleted N' line is not evidence of deletion."""
        src = SRC.read_text()
        assert "still = [k for k in junk if k in (after or {})]" in src

    def test_a_vanished_protected_key_is_reported_as_failure(self):
        assert "PROTECTED KEY" in SRC.read_text()


class TestRowDeletePrimitive:
    def test_the_default_backend_cannot_delete(self):
        """A tombstone leaves the key present; pruning needs a real delete, and
        a backend that cannot do it must SAY so rather than silently no-op."""
        import storage
        assert storage.StorageBackend.delete_user(None, "f", "1") is False

    def test_supabase_implements_it(self):
        import storage
        assert (storage.SupabaseBackend.delete_user
                is not storage.StorageBackend.delete_user)
