"""🔴 A green job that quietly did nothing is this codebase's most expensive
recurring bug, and until 2026-09-05 nothing watched for it.

Four instances were found in a single day, all reporting healthy:

    vix_check          "VIX fetch failed: ... not 'Series'"   exit 0, green
    digest             "handler errored -- suppressed"        exit 0, green
    storage.surfaces   "NOT VERIFIED this run"                PASS
    endpoint.health    failing by design on every run         FAIL, ignored

They share one shape: **a success with a failure printed inside it.** An exit
code is what a job CLAIMS; the log is what happened. `check_silent_failures`
reads the logs of SUCCESSFUL runs.

🔑 The hard part is not finding failures — it is not crying wolf. A check that
fires on a legitimately quiet day gets ignored, and then it is worth less than
nothing. Half these tests exist to pin the NEGATIVE: real no-op output from real
green runs that must stay silent.
"""
import importlib
import os
import sys

import pytest


@pytest.fixture
def canary(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_ACTIONS_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    spec = importlib.util.spec_from_file_location("canary_sf", "scripts/canary.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RESULTS.clear()
    return m


def _wire(canary, monkeypatch, log_text, *, runs=1, list_status=200, log_status=200):
    class _R:
        def __init__(s, status, payload=None, text=""):
            s.status_code, s._p, s.text = status, payload or {}, text

        def json(s):
            return s._p

    def _get(url, **k):
        if "/runs?" in url or "workflows/daily_run.yml/runs" in url:
            return _R(list_status, {"workflow_runs": [{"id": 100 + i} for i in range(runs)]})
        if url.endswith("/jobs"):
            return _R(200, {"jobs": [{"id": 900}]})
        if url.endswith("/logs"):
            return _R(log_status, text=log_text)
        return _R(404)

    monkeypatch.setattr(canary.requests, "get", _get)


def _verdict(canary):
    return {n: (ok, note) for n, ok, note in canary.RESULTS}["runs.silent_failures"]


# Real output from real GREEN runs on 2026-09-05. None may raise an alarm.
QUIET_BUT_HEALTHY = [
    "[agent] Running 3:30 PM close check...\n[agent] No picks for today — nothing to check.",
    "[agent] Running macro alert check...\n[agent] macro_alert: skipping — not Mon–Thu.",
    "[agent] Running position news check...\n[agent] news_check: no relevant news found.",
    "[watchdog] Non-trading day (2026-09-05) — nothing to check.",
    "[agent] run_digest: delivered to 1 of 1 user(s) (1 sent here, 0 sent by the handler; "
    "skipped: 0 no open positions, 0 paused/opted out; problems: 0 handler errored, "
    "0 no reply, 0 exception)",
    "[agent] Pre-screener: 5 ST, 5 LT candidates cached.",
    "[agent] vix_check: VIX = 14.5\n[agent] vix_check: VIX below 20 — no alert needed.",
]


class TestItCatchesTheRealSilentFailures:
    @pytest.mark.parametrize("log,label", [
        ("[agent] vix_check: VIX fetch failed: float() argument must be a string "
         "or a real number, not 'Series'", "the vix_check break"),
        ("[agent] run_digest: handler errored for 123 — suppressed.\n"
         "[agent] run_digest: sent to 0 user(s).", "the digest break"),
        ("[agent] run_digest: ⚠️ DIGEST IS BROKEN for 1 of 3 user(s)", "the explicit alarm"),
        ("Traceback (most recent call last):\n  File x\nUnboundLocalError: nope", "a swallowed traceback"),
        ("PASS  storage.surfaces  · NOT VERIFIED this run — ReadTimeout", "a check that could not check"),
    ])
    def test_a_green_run_hiding_a_failure_is_caught(self, canary, monkeypatch, log, label):
        _wire(canary, monkeypatch, log)
        canary.check_silent_failures()
        ok, note = _verdict(canary)
        assert not ok, f"{label} must not pass"
        assert "EXITED 0 WHILE FAILING" in note


class TestItDoesNotCryWolf:
    """🔑 The load-bearing half. Every string here came from a real green run."""

    @pytest.mark.parametrize("log", QUIET_BUT_HEALTHY)
    def test_legitimate_noop_output_stays_silent(self, canary, monkeypatch, log):
        _wire(canary, monkeypatch, log)
        canary.check_silent_failures()
        ok, note = _verdict(canary)
        assert ok, f"false alarm on healthy output: {note}"

    def test_the_authors_own_non_critical_marker_is_honoured(self, canary, monkeypatch):
        """agent.py prints "(non-critical)" for failures it deliberately tolerates.
        Flagging those would fire on most healthy runs."""
        _wire(canary, monkeypatch,
              "[agent] Recent losers fetch failed (non-critical): timeout")
        canary.check_silent_failures()
        assert _verdict(canary)[0]

    def test_a_tolerated_traceback_two_lines_up_is_honoured(self, canary, monkeypatch):
        """A Traceback's explanatory print comes BEFORE it, so the tolerance
        window has to look back, not just at the matching line."""
        _wire(canary, monkeypatch,
              "[agent] Company names fetch failed (non-critical): x\n"
              "some detail\n"
              "Traceback (most recent call last):")
        canary.check_silent_failures()
        assert _verdict(canary)[0]


class TestUnverifiableIsNotAPass:
    """The lesson from storage.surfaces, applied to the check that generalises it."""

    def test_no_token_fails(self, canary, monkeypatch):
        monkeypatch.delenv("GH_ACTIONS_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_GIST_TOKEN", raising=False)
        canary.check_silent_failures()
        ok, note = _verdict(canary)
        assert not ok and "CANNOT VERIFY" in note

    def test_an_api_error_fails(self, canary, monkeypatch):
        _wire(canary, monkeypatch, "", list_status=500)
        canary.check_silent_failures()
        ok, note = _verdict(canary)
        assert not ok and "CANNOT VERIFY" in note

    def test_unreadable_logs_fail_rather_than_pass_vacuously(self, canary, monkeypatch):
        """A token without actions:read would otherwise scan zero runs and
        report 'none hiding a failure' — true, and meaningless."""
        _wire(canary, monkeypatch, "", log_status=403)
        canary.check_silent_failures()
        ok, note = _verdict(canary)
        assert not ok and "CANNOT VERIFY" in note

    def test_genuinely_no_runs_is_a_pass_with_the_reason_stated(self, canary, monkeypatch):
        _wire(canary, monkeypatch, "", runs=0)
        canary.check_silent_failures()
        ok, note = _verdict(canary)
        assert ok and "no successful daily_run runs" in note


class TestItIsRegisteredAndBounded:
    def test_it_actually_runs(self):
        src = open("scripts/canary.py").read()
        assert "check_silent_failures," in src, "the check exists but nothing calls it"

    def test_it_runs_after_endpoints_so_it_never_competes_for_the_wake(self):
        import ast
        tree = ast.parse(open("scripts/canary.py").read())
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        order = next([e.id for e in n.elts if isinstance(e, ast.Name)]
                     for n in ast.walk(main) if isinstance(n, ast.Tuple)
                     and any(isinstance(e, ast.Name) and e.id == "check_silent_failures"
                             for e in n.elts))
        assert order.index("check_endpoints") < order.index("check_silent_failures")

    def test_the_workflow_supplies_a_token_with_actions_read(self):
        """GH_GIST_TOKEN is gist-scoped and cannot read run logs; without the
        automatic workflow token this check would report CANNOT VERIFY daily."""
        wf = open(".github/workflows/canary.yml").read()
        assert "GH_ACTIONS_TOKEN" in wf
