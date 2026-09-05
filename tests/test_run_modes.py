"""🔴 An unknown run mode used to become a DIFFERENT job, silently.

`detect_run_mode` treated an unrecognised `RUN_MODE` as "not forced" and fell
back to CLOCK-BASED detection. MEASURED 2026-09-05, dispatching
`run_mode=typo_not_a_real_mode`:

    Resolved RUN_MODE=typo_not_a_real_mode
    Starting [CONFIRMATION]        <- ran a different job
    conclusion: success

⚠️ Between 03:00 and 09:59 ET that fallback is `morning`, which BROADCASTS PICKS
TO EVERY USER. A typo in one of seventeen cron-job.org bodies could send the
morning briefing at the wrong hour and report success.

🚨 It got worse on the day the triggers moved. `webhook.trigger_mode` used to
reject unknown modes with 400; dispatching straight to GitHub's API removed that
gate, because GitHub accepts any string for a workflow input.

The root cause was DRIFT: the mode list existed three times (detect_run_mode's
accept-tuple, main()'s elif chain, webhook._VALID_MODES) and they disagreed —
`tax_harvest` was dispatched by main() and allowed by webhook while
detect_run_mode rejected it, so that job could never have worked. These tests
pin the single source of truth and the loud failure.
"""
import ast
import pathlib
import re

import pytest

from run_modes import VALID_MODES, check, is_valid, normalise

AGENT = pathlib.Path("agent.py")


class TestUnknownModesFailLoudly:
    def test_a_typo_raises_instead_of_guessing(self):
        with pytest.raises(ValueError) as e:
            check("typo_not_a_real_mode")
        assert "unknown run mode" in str(e.value)

    def test_the_error_names_the_valid_modes(self):
        """An operator reading a failed job log must not have to grep source."""
        with pytest.raises(ValueError) as e:
            check("nope")
        for m in ("morning", "prescreener", "watchdog"):
            assert m in str(e.value)

    def test_a_near_miss_gets_a_suggestion(self):
        with pytest.raises(ValueError) as e:
            check("vix")
        assert "vix_check" in str(e.value)

    @pytest.mark.parametrize("good", sorted(VALID_MODES))
    def test_every_valid_mode_passes(self, good):
        assert check(good) == good and is_valid(good)

    def test_whitespace_and_case_are_tolerated(self):
        assert check("  VIX_Check \n") == "vix_check"

    def test_unset_is_not_an_error(self):
        """Empty means 'detect from the clock', which is the legitimate default
        for a scheduled run — only a NON-EMPTY unknown value is a defect."""
        assert normalise("") is None and normalise(None) is None


class TestDetectRunModeUsesTheGate:
    def test_an_unknown_forced_mode_raises_not_falls_back_to_the_clock(self, monkeypatch):
        """The exact production regression."""
        import datetime as dt

        import agent
        monkeypatch.setenv("RUN_MODE", "typo_not_a_real_mode")
        with pytest.raises(ValueError):
            agent.detect_run_mode(dt.datetime(2026, 9, 5, 13, 0))

    def test_a_valid_forced_mode_still_wins_over_the_clock(self, monkeypatch):
        import datetime as dt

        import agent
        monkeypatch.setenv("RUN_MODE", "tax_harvest")
        # A Saturday 08:00 would otherwise detect "weekly".
        assert agent.detect_run_mode(dt.datetime(2026, 9, 5, 8, 0)) == "tax_harvest"

    def test_no_RUN_MODE_still_detects_from_the_clock(self, monkeypatch):
        import datetime as dt

        import agent
        monkeypatch.delenv("RUN_MODE", raising=False)
        got = agent.detect_run_mode(dt.datetime(2026, 9, 6, 8, 0))   # Sunday am
        assert got in VALID_MODES


class TestTheThreeConsumersCannotDriftApart:
    """🔑 The drift WAS the bug. tax_harvest was dispatched and allowed but not
    accepted, so the mode was unreachable and nothing reported it."""

    def _main_dispatch(self):
        tree = ast.parse(AGENT.read_text())
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        modes = set()
        for n in ast.walk(main):
            if (isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                    and n.left.id == "mode"):
                for c in n.comparators:
                    if isinstance(c, ast.Constant):
                        modes.add(c.value)
        return modes

    def test_every_dispatched_mode_is_valid(self):
        missing = self._main_dispatch() - set(VALID_MODES)
        assert not missing, (
            f"main() dispatches {sorted(missing)} but run_modes rejects them — "
            "they would raise instead of running")

    def test_every_valid_mode_is_reachable_in_main(self):
        """`confirmation` is reached via main()'s `else:`, so it is exempt."""
        unreachable = set(VALID_MODES) - self._main_dispatch() - {"confirmation"}
        assert not unreachable, (
            f"run_modes allows {sorted(unreachable)} but main() has no branch — "
            "they would silently fall through to run_confirmation()")

    def test_agent_no_longer_hardcodes_its_own_list(self):
        """The accept-tuple is how the lists drifted in the first place."""
        assert not re.search(r'if forced in \(\s*"', AGENT.read_text()), \
            "detect_run_mode must use run_modes.check, not a private tuple"

    def test_webhook_imports_the_shared_list(self):
        src = pathlib.Path("webhook.py").read_text()
        assert "from run_modes import VALID_MODES" in src
        assert not re.search(r'_VALID_MODES = \{\s*"', src), \
            "webhook must not keep a second copy of the mode list"


class TestTheWorkflowGate:
    """daily_run.yml validates before the expensive, user-visible step."""

    def test_the_workflow_validates_the_resolved_mode(self):
        wf = pathlib.Path(".github/workflows/daily_run.yml").read_text()
        assert "from run_modes import check" in wf, \
            "daily_run.yml must reject an unknown run_mode before running the agent"

    def test_the_validation_python_is_a_single_line(self):
        """⚠️ Inside `run: |` every line carries the block indentation, so
        multi-line source in `python3 -c` is an IndentationError — which would
        fail EVERY run, valid or not. Keep it on one line."""
        wf = pathlib.Path(".github/workflows/daily_run.yml").read_text()
        line = next(l for l in wf.splitlines() if "from run_modes import check" in l)
        assert line.count("python3 -c") == 1 and line.rstrip().endswith("; then")
