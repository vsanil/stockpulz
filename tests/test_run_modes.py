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


class TestOnlyOneEntryPointToTheAgent:
    """🔑 Exactly one workflow may set RUN_MODE.

    Every protection a run gets lives in daily_run.yml: the run_mode allow-list
    check, the per-mode `concurrency` group, and the full env — above all
    OWNER_ONLY, without which a manual trigger broadcasts to EVERY user. A
    second workflow invoking agent.py silently opts out of all three.

    🔴 price_alerts.yml was exactly that until 2026-09-05. It had no schedule
    and nothing dispatched it (daily_run.yml's own header still claimed price
    alerts lived there), and it ran the same agent with EIGHT env vars missing —
    OWNER_ONLY and APP_URL among them. Pressing its "Run workflow" button would
    have messaged every user with no containment and no mode validation.

    ⚠️ Adding a workflow that runs a mode? Do not. Dispatch daily_run.yml:
        gh workflow run daily_run.yml -f run_mode=<mode> -f owner_only=1
    """

    def _workflows_setting_run_mode(self):
        import pathlib
        return sorted(p.name for p in pathlib.Path(".github/workflows").glob("*.yml")
                      if "RUN_MODE" in p.read_text())

    def test_daily_run_is_the_only_workflow_that_sets_RUN_MODE(self):
        got = self._workflows_setting_run_mode()
        assert got == ["daily_run.yml"], (
            f"a second entry point to agent.py bypasses the run_mode allow-list, "
            f"the concurrency group and OWNER_ONLY containment: {got}")

    def test_no_workflow_points_at_the_removed_price_alerts_workflow(self):
        """A pointer to a workflow that does not exist is worse than no comment:
        it sends the next reader looking for the wrong file, which is exactly
        what daily_run.yml's header did for weeks after the move to cron-job.org.

        ⚠️ The exemption is LINE-level, not file-level. A first version excluded
        any file containing the word "deleted" — and this file's own explanatory
        comment contains it, so the whole file was exempt and re-adding the stale
        line still passed. Mutation-checking caught that; keep the scope tight.
        """
        import pathlib
        stale = []
        for p in pathlib.Path(".github/workflows").glob("*.yml"):
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if "price_alerts.yml" not in line:
                    continue
                # Only a line that says the file is gone may mention it.
                if any(w in line.lower() for w in ("removed", "deleted", "no longer")):
                    continue
                stale.append(f"{p.name}:{n} {line.strip()[:70]}")
        assert not stale, f"dangling reference to the removed workflow: {stale}"

    def test_the_surviving_entry_point_still_carries_its_guards(self):
        wf = open(".github/workflows/daily_run.yml").read()
        for guard in ("from run_modes import check", "concurrency:", "OWNER_ONLY:"):
            assert guard in wf, f"daily_run.yml lost its {guard!r} protection"


class TestEveryDeclaredScheduleResolvesToItsMode:
    """🔴 A declared cron with no mapping in "Set run mode" falls through to the
    `else:` and silently runs PRESCREENER instead — a scheduled job that appears
    to work while doing someone else's work.

    The inverse bit us for months: monthly_commentary and tax_harvest had
    resolver mappings and NO cron entries, so both were "supported" and had
    never once run. Half-wiring is invisible from either end alone; this test
    checks both ends against each other.
    """

    def _wf(self):
        import pathlib
        return pathlib.Path(".github/workflows/daily_run.yml").read_text()

    def _declared_crons(self):
        import re
        import yaml
        d = yaml.safe_load(self._wf())
        on = d.get(True, d.get("on"))
        return [c["cron"] for c in on["schedule"]]

    def _resolver_crons(self):
        import re
        return set(re.findall(r'\$SCHEDULE" == "([^"]+)"', self._wf()))

    def test_every_declared_cron_has_a_mode_mapping(self):
        missing = [c for c in self._declared_crons() if c not in self._resolver_crons()]
        assert not missing, (
            f"cron(s) {missing} are scheduled but unmapped — they hit the `else:` "
            f"and would silently run prescreener at the wrong hour")

    def test_the_two_newly_wired_modes_are_actually_scheduled(self):
        """They existed in agent.py and in the resolver for months with no
        trigger. Nothing else would have noticed."""
        crons = self._declared_crons()
        assert "0 13 1 * *" in crons, "monthly_commentary has no schedule"
        assert "0 14 1,15 11,12 *" in crons, "tax_harvest has no schedule"

    def test_each_scheduled_cron_gets_its_own_concurrency_group(self):
        """⚠️ With a bare `|| 'prescreener'` every scheduled run shared one
        group, and GitHub cancels the PENDING run when a third arrives — so a
        monthly job could be silently dropped behind two prescreener runs.

        ⚠️ Asserts it on the GROUP LINE, not anywhere in the file. A first
        version searched the whole text and passed with the fix reverted,
        because the "Set run mode" step also contains
        `SCHEDULE="${{ github.event.schedule }}"`. Third substring test today to
        match something other than what it meant to check."""
        group = next(l for l in self._wf().splitlines() if l.strip().startswith("group:"))
        assert "github.event.schedule" in group, (
            f"the concurrency group must distinguish schedules, or a scheduled "
            f"mode can be cancelled as a duplicate of prescreener: {group.strip()}")

    def test_no_scheduled_mode_is_rejected_by_run_modes(self):
        import re
        wf = self._wf()
        for cron in self._declared_crons():
            m = re.search(r'\$SCHEDULE" == "%s" \]\];? then\s*\n\s*MODE="([a-z_]+)"'
                          % re.escape(cron), wf)
            assert m, f"could not read the mode for cron {cron!r}"
            assert m.group(1) in VALID_MODES, \
                f"cron {cron!r} resolves to {m.group(1)!r}, which run_modes rejects"
