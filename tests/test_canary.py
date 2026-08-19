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
        # 🔴 et_today(), NOT date.today(). The check compares the stamp against an
        # America/New_York clock, so a UTC runner between 00:00 and 04:00 UTC
        # stamps tomorrow's date and the check correctly reads it as a FUTURE
        # screener run — failing three tests every night for four hours while the
        # code under test was fine. Same class as the documented UTC-vs-ET bug.
        import json
        from config_manager import et_today
        payload = {"date": date or et_today().isoformat(), "sources": sources}
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

    def test_the_stamp_is_read_on_the_same_clock_the_check_uses(self, monkeypatch):
        """Regression for a suite that went red 00:00-04:00 UTC every night.

        CI runs in UTC; the check compares the stamp against America/New_York.
        In that four-hour window the two calendars differ by a day, so a payload
        stamped with date.today() looked like a FUTURE screener run, `fresh` went
        False, and three tests failed at 100% coverage — with the self-refuting
        note "stale — last screener run recorded <today's date>".

        Forcing the ET clock backwards reproduces the CI condition on any
        machine at any hour, which the original failure could not be.
        """
        import sys, types, datetime as _dt
        fake = types.ModuleType("pytz")
        fake.timezone = lambda _n: _dt.timezone(_dt.timedelta(hours=-12))
        monkeypatch.setitem(sys.modules, "pytz", fake)
        ok, _ = self._dq(monkeypatch, {
            "finnhub_profile": {"ok": 79, "total": 79, "coverage_pct": 100.0},
            "finnhub_metrics": {"ok": 79, "total": 79, "coverage_pct": 100.0}})
        assert ok, "the stamp must use the check's clock (et_today), not date.today()"

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


class TestPaperViewCryptoPriceCheck:
    """The check added after paper_trader._live_price priced ETH at $18.

    The existing paper.crypto_fractional check passed an EXPLICIT price to
    paper_buy, so it never touched the resolver the portfolio VIEW uses — the
    bug lived entirely in the display path and every monitor stayed green.
    """

    def test_it_cross_checks_an_INDEPENDENT_source(self):
        """Two reads from the same resolver agree even when both are wrong —
        the false-pass trap prices.cg_cache already fell into. This must compare
        the paper resolver against CoinGecko, not against itself."""
        import inspect
        src = inspect.getsource(canary)
        # Anchor on the SECTION header — the cg_prices import sits above the
        # first _check(), so slicing from the check name skips it.
        block = src[src.index("Paper VIEW prices crypto correctly"):]
        block = block[:block.index("Alert round-trip")]
        assert "cg_prices" in block, "must cross-check CoinGecko, not the same path"
        assert "_paper_price" in block

    def test_an_unavailable_reference_reads_as_NOT_VERIFIED_not_pass(self):
        """A green line must never mean 'the check could not run'."""
        import inspect
        src = inspect.getsource(canary)
        block = src[src.index("Paper VIEW prices crypto correctly"):]
        assert "NOT VERIFIED" in block[:block.index("Alert round-trip")]

    def test_the_drift_threshold_would_have_caught_the_real_bug(self):
        """Measured live: the paper view returned $18.00 while ETH was $1,891."""
        ref, buggy = 1891.0, 18.0
        drift = abs(buggy - ref) / ref * 100
        assert drift >= 20, "the 20% threshold must reject a 99% error"

    def test_ordinary_intraday_movement_does_not_trip_it(self):
        """CoinGecko and the trade feed differ by seconds, not percent — the
        threshold must not fire on normal drift."""
        ref, real = 1891.0, 1885.0
        assert abs(real - ref) / ref * 100 < 20


class TestWeeklyRelayCheck:
    """🔴 The Saturday weekly path runs `run_morning` and, until 2026-08-15,
    did it ON RENDER — delivering picks while setting ZERO auto alerts, and
    still reporting success. It runs once a week, so nothing noticed for weeks.

    Note the patch target: `check_weekly_relay` does
    `from config_manager import ...` INSIDE the function, so these must be
    patched on config_manager, not on canary. Patching the wrong module is what
    let a real writer hit the live gist on Aug 15.
    """

    import datetime as _dt

    def _wire(self, monkeypatch, *, weekly_today=True, alerts=None,
              gh_dispatch=True, api_ok=True):
        import config_manager as cm
        import datetime as dt, json
        today = dt.date(2026, 8, 22)
        monkeypatch.setattr(cm, "et_today", lambda: today)
        monkeypatch.setattr(cm, "get_config", lambda: {
            "cron_last_weekly": (today.isoformat() if weekly_today else "2026-08-15") + "T12:00:43Z"})
        monkeypatch.setattr(cm, "get_allowed_users", lambda: ["111", "222"])
        monkeypatch.setattr(canary, "_gist_all", lambda: {
            "price_alerts.json": {"content": json.dumps(alerts if alerts is not None else {})}})

        class _R:
            status_code = 200 if api_ok else 503
            def json(self_inner):
                return {"workflow_runs": ([{"event": "workflow_dispatch"}] if gh_dispatch
                                          else [{"event": "schedule"}])}
        monkeypatch.setattr(canary.requests, "get", lambda *a, **k: _R())
        canary.RESULTS.clear()

    def _res(self):
        out = {n: (ok, note) for n, ok, note in canary.RESULTS}
        canary.RESULTS.clear()
        return out

    def _alert(self, uid_alerts):
        return uid_alerts

    def test_silent_on_days_no_weekly_run_fired(self, monkeypatch):
        """Six days out of seven. A check that cries wolf trains you to ignore it."""
        self._wire(monkeypatch, weekly_today=False)
        canary.check_weekly_relay()
        r = self._res()
        assert r["weekly.relay"][0] is True
        assert "nothing to verify" in r["weekly.relay"][1]
        assert "weekly.alerts_set" not in r, "must not assert on a day it did not run"

    def test_the_2026_08_15_failure_would_now_be_CAUGHT(self, monkeypatch):
        """Picks delivered, zero auto alerts for any real user."""
        self._wire(monkeypatch, alerts={"111": [], "222": []})
        canary.check_weekly_relay()
        r = self._res()
        assert r["weekly.alerts_set"][0] is False
        assert "ZERO auto alerts" in r["weekly.alerts_set"][1]

    def test_a_healthy_weekly_run_passes(self, monkeypatch):
        self._wire(monkeypatch, alerts={
            "111": [{"auto": True, "kind": "stop", "set_at": "2026-08-22T12:05:00Z"}],
            "222": [{"auto": True, "kind": "invalidation", "set_at": "2026-08-22T12:05:00Z"}]})
        canary.check_weekly_relay()
        r = self._res()
        assert r["weekly.alerts_set"][0] is True
        assert "2 auto alert(s)" in r["weekly.alerts_set"][1]

    def test_SYNTHETIC_account_alerts_do_not_satisfy_it(self, monkeypatch):
        """🔴 The exact masking failure: on 2026-08-15 production 'looked'
        covered because the synthetic bot logs positions and gets alerts. Only
        allowed_users count."""
        self._wire(monkeypatch, alerts={
            "900000001": [{"auto": True, "set_at": "2026-08-22T12:05:00Z"}],
            "111": [], "222": []})
        canary.check_weekly_relay()
        assert self._res()["weekly.alerts_set"][0] is False, \
            "a synthetic-account alert satisfied a real-user check"

    def test_yesterdays_alerts_do_not_count(self, monkeypatch):
        self._wire(monkeypatch, alerts={
            "111": [{"auto": True, "set_at": "2026-08-21T12:05:00Z"}], "222": []})
        canary.check_weekly_relay()
        assert self._res()["weekly.alerts_set"][0] is False

    def test_it_flags_a_run_that_did_not_reach_github(self, monkeypatch):
        """If the stamps update but no workflow_dispatch exists, the relay did
        not take and it is spawning on Render again."""
        self._wire(monkeypatch, gh_dispatch=False,
                   alerts={"111": [{"auto": True, "set_at": "2026-08-22T12:05:00Z"}], "222": []})
        canary.check_weekly_relay()
        r = self._res()
        assert r["weekly.on_github"][0] is False
        assert "spawning on Render" in r["weekly.on_github"][1]

    def test_an_unreachable_api_says_NOT_VERIFIED_rather_than_passing_clean(self, monkeypatch):
        self._wire(monkeypatch, api_ok=False,
                   alerts={"111": [{"auto": True, "set_at": "2026-08-22T12:05:00Z"}], "222": []})
        canary.check_weekly_relay()
        r = self._res()
        assert r["weekly.on_github"][0] is True and "NOT VERIFIED" in r["weekly.on_github"][1]
