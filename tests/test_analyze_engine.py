"""The daily engine analysis — the standing agenda a session opens with.

🔴 The rule that makes it safe: engine changes are NEVER recommended from the
bot's win rate. Its trades are mechanical fills, and in July feeding them back
had a robot's stop-outs steering real recommendations.
"""
import importlib.util
import os
import sys

import pytest

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "analyze_engine.py")
_spec = importlib.util.spec_from_file_location("analyze_engine", _P)
ae = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ae)


class TestTiers:
    def test_win_rate_questions_are_HELD_below_the_gate(self):
        f = ae._maturity([{"date": "2026-08-20"}] * 5)[0]
        assert f.tier == "HOLD" and f.blocked_until
        assert "not" in f.fix.lower() and "tune" in f.fix.lower()

    def test_the_gate_matches_the_evaluator(self):
        """30 is the bar for calling something conclusive in a report the owner
        reads; it must be the bar here too."""
        assert ae.MIN_N == 30

    def test_a_matured_ledger_unblocks_the_question(self):
        import datetime as dt
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        f = ae._maturity([{"date": old} for _ in range(35)])[0]
        assert f.tier == "MEASURE" and not f.blocked_until

    def test_controls_never_count_toward_maturity(self):
        """Controls are the runners-up we did NOT pick — counting them would
        inflate the sample the honesty gate protects."""
        import datetime as dt
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        f = ae._maturity([{"date": old, "control": True} for _ in range(35)])[0]
        assert f.tier == "HOLD", "controls inflated the matured count"


class TestLevelsGeometry:
    def _t(self, entry, stop, target):
        return {"entry_price": entry, "stop_loss": stop, "target_price": target}

    def test_it_reports_reward_to_risk(self):
        f = ae._levels_geometry([self._t(100, 95, 110)] * 4)[0]
        assert "2.00:1" in f.evidence

    def test_it_compares_against_the_MEASURED_baseline_not_config(self):
        """A backtest's assumptions must come from what users actually got —
        reading config defaults manufactured a false finding once already."""
        f = ae._levels_geometry([self._t(100, 95, 110)] * 4)[0]
        assert "1.9:1" in f.evidence and "config defaults" in f.evidence

    def test_too_few_positions_yields_nothing(self):
        assert ae._levels_geometry([self._t(100, 95, 110)]) == []

    def test_garbage_levels_do_not_crash_it(self):
        assert ae._levels_geometry(
            [{"entry_price": None}, {"entry_price": "x", "stop_loss": 1,
                                     "target_price": 2}] * 3) == []


class TestSafety:
    def test_the_no_win_rate_rule_is_stated_in_the_output(self):
        doc = ae.build(dry=True)
        assert "never recommended from the bot's win rate" in doc.lower()

    def test_the_document_always_renders_even_with_no_data(self, monkeypatch):
        monkeypatch.setattr(ae, "_load", lambda: {
            "uid": "0", "log": {}, "paper": {}, "rows": []})
        doc = ae.build(dry=True)
        assert "Engine findings" in doc and "HOLD" in doc

    def test_tiers_are_ordered_act_first(self, monkeypatch):
        monkeypatch.setattr(ae, "_load", lambda: {
            "uid": "0", "log": {}, "paper": {}, "rows": []})
        doc = ae.build(dry=True)
        tiers = [l.split("]")[0].split("[")[1]
                 for l in doc.splitlines() if l.startswith("### [")]
        rank = {"ACT": 0, "MEASURE": 1, "HOLD": 2}
        assert tiers == sorted(tiers, key=lambda t: rank[t]), \
            "an ACT finding could be buried below a HOLD"


class TestLevelsSourceIsRecorded:
    """🔴 The exit-reason mix was CONFOUNDED until 2026-08-23.

    `_levels_for` substitutes ±5%/8% when a pick's levels do not bracket the
    actual fill. A stop-out on a SUBSTITUTED stop says nothing about the
    engine's published levels — only about the fallback. Without recording
    which was used, the stop:target ratio mixes two different measurements.
    """

    def _lf(self):
        import importlib.util
        import os
        import sys
        os.environ.setdefault("GIST_ID", "x")
        os.environ.setdefault("GH_GIST_TOKEN", "x")
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location(
            "su", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "synthetic_user.py"))
        su = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(su)
        return su._levels_for

    @pytest.mark.parametrize("px,stop,target,want", [
        (100, 95, 110, "pick"),        # both bracket the fill
        (100, 105, 110, "stop"),       # stop above the fill
        (100, 95, 90, "target"),       # target below the fill
        (100, None, None, "both"),
        (1177.74, 1290, 1400, "stop"), # the live FICO case
    ])
    def test_it_reports_which_leg_was_substituted(self, px, stop, target, want):
        s, t, src = self._lf()(px, stop, target)
        assert src == want
        assert s < px < t, "the returned levels must still bracket the fill"

    def test_a_substituted_level_is_never_silently_inherited(self):
        """The whole point: an unusable pick level must not be passed through."""
        s, _t, src = self._lf()(100, 105, 110)
        assert s < 100 and src == "stop"

    def test_the_analysis_separates_pick_levels_from_the_fallback(self):
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        ae2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ae2)
        seg = ae2._exit_mix_by_levels_source(
            {"closed": [{"levels_source": "pick"}, {"levels_source": "stop"}]},
            {"history": [{"levels_source": "pick"}, {}]})
        assert seg == {"pick": 2, "stop": 1, "unrecorded": 1}

    def test_pre_existing_trades_are_unrecorded_not_counted_as_pick(self):
        """Folding them into `pick` would overstate what the engine's own
        levels have actually been measured on."""
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        ae2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ae2)
        seg = ae2._exit_mix_by_levels_source({"closed": [{}, {}]}, {})
        assert seg == {"unrecorded": 2}


class TestStorageAcceptsTheField:
    """A stub narrower than production hides a signature change — the
    add_alert(kind=) and send_inline_keyboard(buttons=) lesson."""

    @pytest.mark.parametrize("mod,fn", [("trade_logger", "add_holding"),
                                        ("paper_trader", "paper_buy")])
    def test_the_writer_accepts_levels_source(self, mod, fn):
        import importlib
        import inspect
        f = getattr(importlib.import_module(mod), fn)
        assert "levels_source" in inspect.signature(f).parameters, \
            f"{mod}.{fn} cannot record where the levels came from"

    def test_paper_sell_carries_it_into_history(self):
        import inspect
        import paper_trader
        src = inspect.getsource(paper_trader.paper_sell)
        assert "levels_source" in src, \
            "a sold paper trade loses the levels source, so closed-trade " \
            "analysis silently reverts to unrecorded"


class TestDispositions:
    """Findings must be closable, and a decision must stick.

    🔴 Two failure modes this guards, both learned elsewhere in this repo:
      • position_audit: a finding marked resolved that is STILL PRESENT means
        the defect is live and the mark is hiding it — so it REOPENS.
      • the canary: a check that re-raises something already ruled on trains
        you to skim it, so a decided finding leaves the worklist.
    """

    def _ae(self, tmp_path, monkeypatch):
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae2", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # No STATE patch: dispositions live in STORAGE now, and every test
        # here passes the state dict in explicitly, so nothing is read from
        # or written to a backend. Patching the old file constant was
        # vestigial — it survived the move and pointed at nothing.
        return m

    def _f(self, m, fid="x/1", status=None):
        return m.Finding(fid, "ACT", "t", "e", "fix", where="file.py")

    def test_a_new_finding_starts_open_and_records_first_seen(self, tmp_path, monkeypatch):
        m = self._ae(tmp_path, monkeypatch)
        f = self._f(m)
        m._apply_state([f], {}, "2026-08-23")
        assert f.status == "open" and f.first_seen == "2026-08-23"

    def test_an_acknowledged_finding_LEAVES_the_worklist(self, tmp_path, monkeypatch):
        m = self._ae(tmp_path, monkeypatch)
        f = self._f(m)
        m._apply_state([f], {"x/1": {"status": "acknowledged"}}, "2026-08-23")
        assert f.status == "acknowledged"
        assert f.status not in m.WORKLIST_STATUSES, \
            "a decision you already made must not keep demanding attention"

    def test_a_finding_marked_FIXED_but_still_present_is_REOPENED(
            self, tmp_path, monkeypatch):
        """Otherwise 'fixed' silently means 'hidden' while the defect is live."""
        m = self._ae(tmp_path, monkeypatch)
        f = self._f(m)
        m._apply_state([f], {"x/1": {"status": "fixed"}}, "2026-08-23")
        assert f.status == "open" and f.reopened is True

    def test_a_finding_that_DISAPPEARS_is_auto_resolved(self, tmp_path, monkeypatch):
        """The intended path: implement the fix, the condition goes, it closes
        itself. No manual bookkeeping."""
        m = self._ae(tmp_path, monkeypatch)
        state = {"gone/1": {"status": "open"}}
        m._apply_state([], state, "2026-08-23")
        assert state["gone/1"]["status"] == "resolved"
        assert state["gone/1"]["resolved_on"] == "2026-08-23"

    def test_something_lived_with_also_resolves_when_it_disappears(
            self, tmp_path, monkeypatch):
        m = self._ae(tmp_path, monkeypatch)
        state = {"gone/2": {"status": "acknowledged"}}
        m._apply_state([], state, "2026-08-23")
        assert state["gone/2"]["status"] == "resolved"

    def test_age_is_measured_from_first_seen_not_today(self, tmp_path, monkeypatch):
        import datetime as dt
        m = self._ae(tmp_path, monkeypatch)
        f = self._f(m)
        old = (dt.date.today() - dt.timedelta(days=12)).isoformat()
        m._apply_state([f], {"x/1": {"status": "open", "first_seen": old}}, "z")
        assert f.age_days == 12, "a stale finding must be visibly stale"

    def test_metrics_are_never_dispositioned(self, tmp_path, monkeypatch):
        """A standing measurement cannot be 'completed' — mixing it into the
        worklist makes 'address every finding' impossible."""
        m = self._ae(tmp_path, monkeypatch)
        met = m.Finding("metric:x", "MEASURE", "t", "e", "fix", kind="metric")
        state = {}
        m._apply_state([met], state, "2026-08-23")
        assert state == {}, "a metric was written into the disposition store"

    def test_every_addressable_finding_names_where_to_fix_it(self, monkeypatch):
        """'Consider reviewing' cannot be actioned at sign-in."""
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae3", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        monkeypatch.setattr(m, "_load", lambda: {
            "uid": "0",
            "log": {"open": [{"ticker": "AAPL", "entry_price": 100,
                              "target_price": 90, "stop_loss": 95}], "closed": []},
            "paper": {}, "rows": []})
        doc = m.build(dry=True)
        assert "**Fix:**" in doc


class TestNewActNotification:
    """DM on NEW ACT findings only.

    🔴 Deliberately narrow. Two monitors cried wolf on 2026-08-22 —
    `weekly.on_github` on every run and `data.completeness` every weekend — and
    the lesson was identical: an alert that fires when nothing is wrong trains
    you to ignore the one time it is right.
    """

    def _ae(self, tmp_path, monkeypatch):
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae4", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # No STATE patch — see the note in TestDispositions. _notify_new_act
        # mutates the state dict it is GIVEN; only main() persists, so these
        # tests never reach a storage backend.
        sent = []
        import telegram_api
        monkeypatch.setattr(telegram_api, "send_message",
                            lambda text, chat_id=None: sent.append(text))
        return m, sent

    def _f(self, m, fid="i/1", tier="ACT", kind="finding"):
        f = m.Finding(fid, tier, "T title", "T evidence", "fix", kind=kind,
                      where="some_file.py")
        f.status = "open"
        return f

    def test_a_new_ACT_finding_is_notified(self, tmp_path, monkeypatch):
        m, sent = self._ae(tmp_path, monkeypatch)
        assert m._notify_new_act([self._f(m)], {}, "2026-08-23") == 1
        assert "T title" in sent[0] and "some_file.py" in sent[0]

    def test_it_is_NOT_re_sent_on_a_later_run(self, tmp_path, monkeypatch):
        """Exactly-once is tracked by notified_on, not by 'first seen today',
        so a second run the same day cannot re-send."""
        m, sent = self._ae(tmp_path, monkeypatch)
        state = {}
        m._notify_new_act([self._f(m)], state, "2026-08-23")
        assert m._notify_new_act([self._f(m)], state, "2026-08-23") == 0
        assert m._notify_new_act([self._f(m)], state, "2026-08-24") == 0
        assert len(sent) == 1

    @pytest.mark.parametrize("tier", ["MEASURE", "HOLD"])
    def test_non_ACT_tiers_never_notify(self, tmp_path, monkeypatch, tier):
        m, sent = self._ae(tmp_path, monkeypatch)
        assert m._notify_new_act([self._f(m, tier=tier)], {}, "2026-08-23") == 0
        assert sent == []

    def test_metrics_never_notify(self, tmp_path, monkeypatch):
        m, sent = self._ae(tmp_path, monkeypatch)
        assert m._notify_new_act(
            [self._f(m, tier="ACT", kind="metric")], {}, "2026-08-23") == 0

    @pytest.mark.parametrize("status", ["acknowledged", "wont_fix"])
    def test_something_already_ruled_on_never_notifies(
            self, tmp_path, monkeypatch, status):
        m, sent = self._ae(tmp_path, monkeypatch)
        f = self._f(m)
        f.status = status
        assert m._notify_new_act([f], {}, "2026-08-23") == 0

    def test_nothing_new_means_NO_message_at_all(self, tmp_path, monkeypatch):
        """Not even an 'all clear' — that is the digest this must not become."""
        m, sent = self._ae(tmp_path, monkeypatch)
        assert m._notify_new_act([], {}, "2026-08-23") == 0
        assert sent == []

    def test_a_send_failure_does_not_break_the_report(self, tmp_path, monkeypatch):
        """Telemetry must never take down the analysis it reports on."""
        m, sent = self._ae(tmp_path, monkeypatch)
        import telegram_api
        monkeypatch.setattr(telegram_api, "send_message",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        state = {}
        assert m._notify_new_act([self._f(m)], state, "2026-08-23") == 0
        assert not state.get("i/1", {}).get("notified_on"), \
            "a failed send must not mark it notified, or the alert is lost"

    def test_a_local_run_never_notifies(self):
        """--notify is opt-in so running this by hand cannot DM the owner."""
        import inspect
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae5", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert inspect.signature(m.build).parameters["notify"].default is False
