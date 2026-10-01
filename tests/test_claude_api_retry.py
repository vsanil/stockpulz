"""The morning run must survive a transient Anthropic failure.

🔴 WHY: on 2026-09-30 Anthropic returned HTTP 529 "Overloaded" at 11:01 UTC and
BOTH real users got no picks that day. `analyze_with_claude` HAD a Haiku
fallback, but it was gated on `except (json.JSONDecodeError, KeyError,
IndexError)` — parse errors only — so an API error bypassed it and raised at
once. The run still exited 0, printing the failure inside a "success".

These tests DRIVE the real functions with a stubbed client rather than scanning
source: a source scan is how a whole page once 500'd for 40 minutes with twenty
green tests.
"""
from __future__ import annotations

import json
import pytest
import anthropic

import llm_client
import ai_analyzer


def _status_error(code: int) -> anthropic.APIStatusError:
    """A real APIStatusError carrying `code`, without needing an httpx response."""
    exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
    exc.status_code = code
    return exc


# ── the taxonomy ─────────────────────────────────────────────────────────────
class TestWhatCountsAsTransient:
    @pytest.mark.parametrize("code", [529, 500, 502, 503, 408, 409, 429])
    def test_overload_and_server_errors_are_retryable(self, code):
        assert llm_client.is_transient_api_error(_status_error(code)) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_client_errors_are_NOT_retryable(self, code):
        """A credit-balance failure arrives as 400 and must stay immediate.

        Retrying a billing or auth error only turns a clear failure into a slow
        one — the same rule SupabaseBackend._read_with_retry follows for RLS.
        """
        assert llm_client.is_transient_api_error(_status_error(code)) is False

    @pytest.mark.parametrize("cls,code,why", [
        ("InternalServerError", 529, "the ACTUAL 2026-09-30 failure"),
        ("InternalServerError", 503, "service unavailable"),
        ("RateLimitError", 429, "rate limited"),
    ])
    def test_the_REAL_sdk_classes_are_retryable(self, cls, code, why):
        """Production never raises a bare APIStatusError — the SDK maps a status
        to a SUBCLASS (>=500 becomes InternalServerError). A predicate verified
        only against the base class would be untested against what actually
        arrives, which is how the 529 reached users in the first place.
        """
        exc = getattr(anthropic, cls).__new__(getattr(anthropic, cls))
        exc.status_code = code
        assert llm_client.is_transient_api_error(exc) is True, why

    def test_the_REAL_credit_balance_class_is_NOT_retryable(self):
        """"Your credit balance is too low" arrives as BadRequestError (400)."""
        exc = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
        exc.status_code = 400
        assert llm_client.is_transient_api_error(exc) is False

    def test_a_connection_drop_is_retryable(self):
        assert llm_client.is_transient_api_error(
            anthropic.APIConnectionError(request=None)) is True

    def test_a_timeout_is_retryable(self):
        """APITimeoutError subclasses APIConnectionError — covered by one check."""
        assert llm_client.is_transient_api_error(
            anthropic.APITimeoutError(request=None)) is True

    @pytest.mark.parametrize("exc", [ValueError("x"), KeyError("k"),
                                     json.JSONDecodeError("m", "d", 0)])
    def test_unrelated_exceptions_are_never_retryable(self, exc):
        assert llm_client.is_transient_api_error(exc) is False

    def test_a_missing_status_code_is_not_retryable(self):
        """Unrecognised shape => permanent. Never retry what you cannot classify."""
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        assert llm_client.is_transient_api_error(exc) is False


# ── the retry loop ───────────────────────────────────────────────────────────
class TestTheRetryLoop:
    def test_a_529_is_retried_and_then_succeeds(self, monkeypatch):
        calls, slept = [], []
        def fake(system, user, model="m", use_caching=False):
            calls.append(model)
            if len(calls) < 3:
                raise _status_error(529)
            return {"ok": True}
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep", lambda s: slept.append(s))

        out = ai_analyzer._call_claude_with_retry("sys", "usr", sleeps=(1.0, 2.0))
        assert out == {"ok": True}
        assert len(calls) == 3, "must actually re-call, not give up after one"
        assert slept == [1.0, 2.0], "must back off between attempts"

    def test_it_gives_up_after_the_last_attempt(self, monkeypatch):
        calls = []
        def fake(system, user, model="m", use_caching=False):
            calls.append(1)
            raise _status_error(529)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep", lambda s: None)

        with pytest.raises(anthropic.APIStatusError):
            ai_analyzer._call_claude_with_retry("s", "u", sleeps=(1.0, 2.0))
        assert len(calls) == 3, "len(sleeps) + 1 attempts, no more and no fewer"

    def test_a_PARSE_error_is_NOT_retried_here(self, monkeypatch):
        """It must propagate so the caller's stricter-prompt branch owns it.

        Re-calling the identical prompt that just produced malformed JSON burns
        the delivery window to obtain the same bad output.
        """
        calls = []
        def fake(system, user, model="m", use_caching=False):
            calls.append(1)
            raise json.JSONDecodeError("bad", "doc", 0)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep",
                            lambda s: pytest.fail("must not sleep on a parse error"))

        with pytest.raises(json.JSONDecodeError):
            ai_analyzer._call_claude_with_retry("s", "u", sleeps=(1.0, 2.0))
        assert len(calls) == 1, "exactly one attempt"

    def test_a_credit_balance_400_fails_on_the_FIRST_attempt(self, monkeypatch):
        calls = []
        def fake(system, user, model="m", use_caching=False):
            calls.append(1)
            raise _status_error(400)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep",
                            lambda s: pytest.fail("must not sleep on a 400"))

        with pytest.raises(anthropic.APIStatusError):
            ai_analyzer._call_claude_with_retry("s", "u", sleeps=(1.0, 2.0))
        assert len(calls) == 1

    def test_a_first_attempt_success_sleeps_not_at_all(self, monkeypatch):
        monkeypatch.setattr(ai_analyzer, "_call_claude",
                            lambda *a, **k: {"ok": 1})
        monkeypatch.setattr(ai_analyzer.time, "sleep",
                            lambda s: pytest.fail("healthy call must not sleep"))
        assert ai_analyzer._call_claude_with_retry("s", "u") == {"ok": 1}

    def test_the_real_default_backoff_is_wired(self):
        """The call site relies on the module default, not a test-only value."""
        assert ai_analyzer.API_RETRY_SLEEPS == llm_client.API_RETRY_SLEEPS
        assert len(llm_client.API_RETRY_SLEEPS) >= 2
        assert all(s > 0 for s in llm_client.API_RETRY_SLEEPS)


# ── the fallback model choice ────────────────────────────────────────────────
class TestTheApiFallbackUsesTheTaskPrompt:
    """🔴 The naive fix routes a 529 into the existing parse branch, which uses
    STRICT_RETRY_SYSTEM — only "You are a JSON generator", carrying NO
    pick-selection instructions. That would hand the model no task and turn an
    outage into a garbage briefing. The API path must reuse SYSTEM_PROMPT.
    """

    def test_strict_prompt_really_has_no_task_instructions(self):
        strict = ai_analyzer.STRICT_RETRY_SYSTEM.lower()
        for word in ("pick", "stock", "entry", "stop", "target", "conviction"):
            assert word not in strict, (
                f"STRICT_RETRY_SYSTEM mentions {word!r}; this test's premise — and "
                "the fallback's prompt choice — needs rechecking")

    def test_a_529_falls_back_to_haiku_with_the_FULL_system_prompt(self, monkeypatch):
        seen = []
        def fake(system, user, model="claude-sonnet-4-6", use_caching=False):
            seen.append((model, system))
            if "haiku" in model:
                return {"stocks": []}
            raise _status_error(529)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep", lambda s: None)
        monkeypatch.setattr(ai_analyzer, "_validate_and_clean_picks", lambda *a, **k: a[0])

        picks = _run_analyze(monkeypatch)
        assert picks == {"stocks": []}

        models = [m for m, _ in seen]
        assert any("haiku" in m for m in models), "must fall back to Haiku"
        haiku_system = next(s for m, s in seen if "haiku" in m)
        assert haiku_system == ai_analyzer.SYSTEM_PROMPT
        assert haiku_system != ai_analyzer.STRICT_RETRY_SYSTEM

    def test_sonnet_is_retried_BEFORE_falling_back(self, monkeypatch):
        seen = []
        def fake(system, user, model="claude-sonnet-4-6", use_caching=False):
            seen.append(model)
            if "haiku" in model:
                return {"stocks": []}
            raise _status_error(529)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep", lambda s: None)
        monkeypatch.setattr(ai_analyzer, "_validate_and_clean_picks", lambda *a, **k: a[0])

        _run_analyze(monkeypatch)
        sonnet = [m for m in seen if "sonnet" in m]
        assert len(sonnet) >= 2, (
            "a capacity blip deserves another go at the GOOD model before "
            "downgrading what users are shown")

    def test_a_parse_error_still_uses_the_STRICT_prompt(self, monkeypatch):
        """Non-regression: the pre-existing cure must not be rerouted."""
        seen = []
        def fake(system, user, model="claude-sonnet-4-6", use_caching=False):
            seen.append((model, system))
            if "haiku" in model:
                return {"stocks": []}
            raise json.JSONDecodeError("bad", "doc", 0)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer, "_validate_and_clean_picks", lambda *a, **k: a[0])

        _run_analyze(monkeypatch)
        haiku_system = next(s for m, s in seen if "haiku" in m)
        assert haiku_system == ai_analyzer.STRICT_RETRY_SYSTEM

    def test_a_credit_400_raises_WITHOUT_trying_haiku(self, monkeypatch):
        """The owner must see a billing failure at once, not after a downgrade."""
        seen = []
        def fake(system, user, model="claude-sonnet-4-6", use_caching=False):
            seen.append(model)
            raise _status_error(400)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep", lambda s: None)

        with pytest.raises(anthropic.APIStatusError):
            _run_analyze(monkeypatch)
        assert not any("haiku" in m for m in seen), (
            "a 400 is permanent; spending a Haiku call on it only delays the alert")

    def test_both_models_down_raises_RuntimeError(self, monkeypatch):
        def fake(system, user, model="claude-sonnet-4-6", use_caching=False):
            raise _status_error(529)
        monkeypatch.setattr(ai_analyzer, "_call_claude", fake)
        monkeypatch.setattr(ai_analyzer.time, "sleep", lambda s: None)

        with pytest.raises(RuntimeError, match="Claude analysis failed"):
            _run_analyze(monkeypatch)


def _run_analyze(monkeypatch):
    """Drive the REAL analyze_with_claude with only its I/O stubbed.

    Every stub is `*a, **k`: a stub narrower than production hides a signature
    change, which is how a missing `kind=` argument once produced zero alerts
    while the tests reported only a wrong count.
    """
    monkeypatch.setattr(ai_analyzer, "_build_stock_candidates", lambda *a, **k: [])
    monkeypatch.setattr(ai_analyzer, "_build_commodity_candidates", lambda *a, **k: [])
    monkeypatch.setattr(ai_analyzer, "_build_user_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(ai_analyzer, "_backfill_allocations", lambda *a, **k: None)
    monkeypatch.setattr(ai_analyzer, "_attach_screen_provenance", lambda *a, **k: None)
    return ai_analyzer.analyze_with_claude({}, {})
