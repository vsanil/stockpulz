"""Smoke tests for the daily canary's pure helpers (scripts/canary.py)."""
import os
import importlib.util
import datetime

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "canary.py")
_spec = importlib.util.spec_from_file_location("canary", _PATH)
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)


class TestCanaryHelpers:
    def test_fin_rejects_nan_inf_none(self):
        assert canary._fin(1.5)
        assert not canary._fin(None)
        assert not canary._fin(float("nan"))
        assert not canary._fin(float("inf"))

    def test_pos_requires_positive_finite(self):
        assert canary._pos(1.0)
        assert not canary._pos(0)
        assert not canary._pos(-1)
        assert not canary._pos(float("nan"))

    def test_expected_delivery_is_a_weekday(self):
        d = canary._expected_delivery_date()
        assert datetime.date.fromisoformat(d).weekday() < 5   # never a Sat/Sun

    def test_check_shows_pass_wording_on_pass_and_fail_wording_on_fail(self):
        """A check used to carry ONE note, so a warning-worded note printed on a
        PASS line — price_guard read "lets a $0.01/spike through" while green,
        the exact opposite of what happened."""
        canary.RESULTS.clear()
        canary._check("ok.case",  True,  "observed the good thing", fail_detail="the bad thing happened")
        canary._check("bad.case", False, "observed the good thing", fail_detail="the bad thing happened")
        notes = {name: note for name, _ok, note in canary.RESULTS}
        assert notes["ok.case"]  == "observed the good thing"
        assert notes["bad.case"] == "the bad thing happened"
        canary.RESULTS.clear()

    def test_price_guard_notes_are_not_warnings_when_green(self):
        canary.RESULTS.clear()
        canary.check_price_guard()
        for name, ok, note in canary.RESULTS:
            assert ok, f"{name} unexpectedly failed"
            assert "lets a $0.01" not in note and "rejects a real" not in note, \
                f"{name} printed failure wording on a PASS line: {note}"
        canary.RESULTS.clear()

    # ── self-heal watchdog ───────────────────────────────────────────────────
    def _runs(self, *conclusions):
        return {"workflow_runs": [
            {"conclusion": c, "created_at": "2026-08-06T16:21:00Z",
             "html_url": "https://x/run"} for c in conclusions]}

    def _wire(self, monkeypatch, payload, status=200, boom=False):
        class _R:
            status_code = status
            def json(self_inner): return payload
        def _get(url, **kw):
            if boom:
                raise RuntimeError("network down")
            return _R()
        monkeypatch.setattr(canary.requests, "get", _get)
        canary.RESULTS.clear()

    def _result(self):
        name, ok, note = canary.RESULTS[-1]
        canary.RESULTS.clear()
        return ok, note

    def test_selfheal_skipped_runs_are_not_failures(self, monkeypatch):
        """`skipped` is the NORMAL outcome — the monitor it watched passed."""
        self._wire(monkeypatch, self._runs("skipped", "skipped", "success"))
        canary.check_selfheal_health()
        ok, note = self._result()
        assert ok and "latest skipped" in note

    def test_selfheal_latest_failure_is_reported(self, monkeypatch):
        self._wire(monkeypatch, self._runs("failure", "skipped"))
        canary.check_selfheal_health()
        ok, note = self._result()
        assert not ok
        assert "LATEST run failed" in note and "auto-fix net is down" in note

    def test_a_resolved_transient_does_not_page_for_a_week(self, monkeypatch):
        """The Aug 6 failure was GitHub's action registry returning Service
        Unavailable — over by the next run. Going red for 7 days on a resolved
        blip is how a canary teaches you to ignore it."""
        self._wire(monkeypatch, self._runs("skipped", "skipped", "failure"))
        canary.check_selfheal_health()
        ok, note = self._result()
        assert ok, "a recovered transient must not fail the canary"
        assert "since recovered" in note, "but it must still be visible, not hidden"

    def test_chronic_failures_are_caught_even_if_the_latest_passed(self, monkeypatch):
        self._wire(monkeypatch, self._runs("skipped", "failure", "failure", "failure"))
        canary.check_selfheal_health()
        ok, note = self._result()
        assert not ok and "chronic" in note

    def test_unverifiable_says_so_instead_of_implying_clean(self, monkeypatch):
        """A green line must never mean 'the check could not run' — that is the
        false-pass trap prices.cg_cache fell into."""
        self._wire(monkeypatch, {}, status=503)
        canary.check_selfheal_health()
        ok, note = self._result()
        assert ok and "NOT VERIFIED" in note

        self._wire(monkeypatch, {}, boom=True)
        canary.check_selfheal_health()
        ok, note = self._result()
        assert ok and "NOT VERIFIED" in note

    def test_in_progress_runs_are_ignored(self, monkeypatch):
        self._wire(monkeypatch, {"workflow_runs": [
            {"conclusion": None, "created_at": "2026-08-08T00:00:00Z"},
            {"conclusion": "skipped", "created_at": "2026-08-07T00:00:00Z"}]})
        canary.check_selfheal_health()
        ok, note = self._result()
        assert ok and "latest skipped" in note

    # ── data completeness ────────────────────────────────────────────────────
    def _dq(self, monkeypatch, sources, date=None):
        import json, datetime
        payload = {"date": date or datetime.date.today().isoformat(), "sources": sources}
        monkeypatch.setattr(canary, "_gist_all",
                            lambda: {"data_quality.json": {"content": json.dumps(payload)}})
        canary.RESULTS.clear()
        canary.check_data_completeness()
        name, ok, note = canary.RESULTS[-1]
        canary.RESULTS.clear()
        return ok, note

    def test_full_coverage_passes(self, monkeypatch):
        ok, note = self._dq(monkeypatch, {
            "finnhub_profile": {"ok": 79, "total": 79, "coverage_pct": 100.0},
            "finnhub_metrics": {"ok": 79, "total": 79, "coverage_pct": 100.0}})
        assert ok and "finnhub_profile=100.0%" in note

    def test_the_finnhub_bug_would_now_be_caught(self, monkeypatch):
        """The exact live failure: 51 of 79 candidates got no fundamentals, and
        every existing monitor stayed green for weeks."""
        ok, note = self._dq(monkeypatch, {
            "finnhub_profile": {"ok": 28, "total": 79, "coverage_pct": 35.4},
            "finnhub_metrics": {"ok": 28, "total": 79, "coverage_pct": 35.4}})
        assert not ok, "a 35% fundamentals run must fail the canary"
        assert "long-term scoring is ~90% fundamentals" in note

    def test_optional_sources_are_reported_but_do_not_page(self, monkeypatch):
        """Congressional is at 0% live right now. Visible, but a flaky
        third-party feed must not cry wolf — the crypto-check lesson."""
        ok, note = self._dq(monkeypatch, {
            "finnhub_profile": {"ok": 79, "total": 79, "coverage_pct": 100.0},
            "finnhub_metrics": {"ok": 79, "total": 79, "coverage_pct": 100.0},
            "congressional":   {"ok": 0,  "total": 1,  "coverage_pct": 0.0}})
        assert ok, "an optional feed at 0% must not fail the canary"
        assert "congressional=0.0%" in note, "but it MUST be visible in the note"

    def test_a_stale_report_fails(self, monkeypatch):
        ok, note = self._dq(monkeypatch, {
            "finnhub_profile": {"ok": 79, "total": 79, "coverage_pct": 100.0}},
            date="2020-01-01")
        assert not ok and "stale" in note

    def test_missing_file_says_not_verified_rather_than_green(self, monkeypatch):
        monkeypatch.setattr(canary, "_gist_all", lambda: {})
        canary.RESULTS.clear()
        canary.check_data_completeness()
        _, ok, note = canary.RESULTS[-1]
        canary.RESULTS.clear()
        assert ok and "NOT VERIFIED" in note

    def test_an_unreachable_gist_says_not_verified(self, monkeypatch):
        def _boom():
            raise RuntimeError("gist down")
        monkeypatch.setattr(canary, "_gist_all", _boom)
        canary.RESULTS.clear()
        canary.check_data_completeness()
        _, ok, note = canary.RESULTS[-1]
        canary.RESULTS.clear()
        assert ok and "NOT VERIFIED" in note

    def test_a_dormant_source_reads_as_not_configured(self, monkeypatch):
        """Congressional has no free source; reporting it as 0% would send the
        owner chasing a breakage that does not exist."""
        ok, note = self._dq(monkeypatch, {
            "finnhub_profile": {"ok": 79, "total": 79, "coverage_pct": 100.0},
            "congressional":   {"ok": 0,  "total": 0,  "coverage_pct": 0.0}})
        assert ok
        assert "congressional=n/a (not configured)" in note
        assert "congressional=0.0%" not in note
