"""Findings are split into TECHNICAL BUG vs DECISION-ENGINE CHANGE.

The owner asked for this, and the reason it matters is the evidence bar:

  bug     restores intended behaviour, changes no strategy. ONE instance is
          enough to act on.
  engine  alters what real users are told to buy. Needs OUTCOME evidence over
          time (n>=30) and never the synthetic bot's win rate — that loop was
          cut in July after a robot's mechanical fills steered real picks.

Reading them as one list is how an engine change gets waved through on a single
observation.
"""
import importlib.util
import pathlib

import pytest

SRC = pathlib.Path("scripts/analyze_engine.py")


def _ae():
    spec = importlib.util.spec_from_file_location("ae_cat", str(SRC.resolve()))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestTheSplitExists:
    def test_a_bug_and_an_engine_change_render_differently(self):
        m = _ae()
        bug = m.Finding("a", "ACT", "t", "e", "f", category="bug").render()
        eng = m.Finding("b", "ACT", "t", "e", "f", category="engine").render()
        assert "TECHNICAL BUG" in bug and "DECISION-ENGINE CHANGE" not in bug
        assert "DECISION-ENGINE CHANGE" in eng

    def test_an_engine_change_states_the_evidence_bar(self):
        r = _ae().Finding("b", "ACT", "t", "e", "f", n=12, category="engine").render()
        assert "changes what users are recommended" in r
        assert "n=12" in r and "30 needed" in r
        assert "never on the synthetic bot" in r.replace("'", "")

    def test_a_bug_does_NOT_carry_the_engine_caveat(self):
        """Otherwise the warning is everywhere and means nothing."""
        r = _ae().Finding("a", "ACT", "t", "e", "f", category="bug").render()
        assert "changes what users are recommended" not in r

    def test_an_unknown_category_is_refused(self):
        with pytest.raises(ValueError):
            _ae().Finding("a", "ACT", "t", "e", "f", category="cosmetic")

    def test_the_default_is_engine_not_bug(self):
        """🔴 Fail toward the closer look. A bug mislabelled as an engine change
        costs a moment's scrutiny; an engine change mislabelled as a bug gets
        waved through and silently alters everyone's picks."""
        assert _ae().Finding("a", "ACT", "t", "e", "f").category == "engine"


class TestTheDerivedFindingsAreClassified:
    """Classification is a judgement, so it is pinned rather than inferred."""

    def test_integrity_is_a_bug(self):
        src = SRC.read_text()
        i = src.index('f"integrity/{f.get(')
        assert 'category="bug"' in src[i:src.index("))", i)]

    def test_entry_window_is_a_bug(self):
        """A published promise not honoured. Binary — one breach is enough, no
        outcome statistics required."""
        src = SRC.read_text()
        i = src.index('f"entry_window/{tk}/')
        assert 'category="bug"' in src[i:src.index("))", i)]

    def test_stop_tightness_is_an_engine_change(self):
        """Stop placement decides when a thesis is abandoned, so changing it
        changes outcomes."""
        src = SRC.read_text()
        i = src.index('f"stop_tight/{tk}/{pct}"')
        assert 'category="engine"' in src[i:src.index("))", i)]


class TestStatePreservesTheProposal:
    """🔴 `state[f.id] = {...}` REPLACED the record, dropping proposed_change,
    proposed_summary and proposed_files. The night after I proposed a fix for a
    derived finding, the card would have rendered '(no description recorded)'
    and the owner could not see what they were approving."""

    def test_a_proposal_survives_the_nightly_run(self):
        m = _ae()
        f = m.Finding("x/1", "ACT", "t", "e", "fix", category="bug")
        state = {"x/1": {"status": "awaiting_approval",
                         "proposed_change": "do the thing",
                         "proposed_summary": "plain words",
                         "proposed_files": "a.py"}}
        m._apply_state([f], state, "2026-08-24")
        rec = state["x/1"]
        assert rec["proposed_change"] == "do the thing"
        assert rec["proposed_summary"] == "plain words"
        assert rec["proposed_files"] == "a.py"
        assert rec["status"] == "awaiting_approval"

    def test_the_category_reaches_the_store(self):
        """The admin card reads dispositions, not findings — an unpersisted
        category would render every finding as an engine change."""
        m = _ae()
        f = m.Finding("x/2", "ACT", "t", "e", "fix", n=41, category="bug")
        state = {}
        m._apply_state([f], state, "2026-08-24")
        assert state["x/2"]["category"] == "bug"
        assert state["x/2"]["n"] == 41


class TestTheCardShowsIt:
    def _js(self):
        src = pathlib.Path("webhook.py").read_text()
        i = src.index("function findingsCard(")
        return src[i:src.index("\nasync function setFinding", i)]

    def test_both_labels_are_rendered(self):
        js = self._js()
        assert "Technical bug" in js and "Decision-engine change" in js

    def test_an_engine_change_warns_when_below_the_gate(self):
        js = self._js()
        assert "x.n < 30" in js and "Below the 30 needed" in js

    def test_an_unclassified_finding_renders_as_engine(self):
        assert "x.category === 'bug'" in self._js(), \
            "the test must be for bug, so anything else falls to engine"

    def test_the_two_chips_are_visually_distinct(self):
        css = pathlib.Path("webhook.py").read_text()
        assert ".fbug{" in css and ".feng{" in css
        i = css.index(".feng{")
        assert "251,191,36" in css[i:i + 160], "engine should be amber — the closer look"


class TestTheCliAndWorkflowCarryIt:
    def test_the_cli_takes_a_category(self):
        src = pathlib.Path("scripts/findings.py").read_text()
        assert 'choices=("bug", "engine")' in src
        assert '"category": category' in src

    def test_the_workflow_passes_it_through(self):
        """A flag wired to the CLI but not the workflow means every CI proposal
        silently takes the default."""
        wf = pathlib.Path(".github/workflows/findings.yml").read_text()
        assert "--category" in wf and "options: [engine, bug]" in wf
