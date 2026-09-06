"""🔴 A silent degradation behind a working fallback, MEASURED 2026-09-06.

Two of six sampled mornings ran the full live 600-ticker screen because the
midnight prescreener cache was stale: 12m32s instead of 2m17s, plus a second
pass against Yahoo/Finnhub. **Picks still went out.** The workflow was green,
users were served, and nothing anywhere reported it — invisible by construction,
the same shape as the vix_check and digest outages.

🔑 Two halves, and the tests cover both: `run_morning` must RECORD the outcome
(a stamp, not a log line nobody owns), and the canary must turn those stamps
into a rate that is visible even when green.
"""
import datetime as dt
import importlib
import importlib.util
import sys

import pytest
import pytz

ET = pytz.timezone("America/New_York")


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    if "agent" in sys.modules:
        del sys.modules["agent"]
    return importlib.import_module("agent")


@pytest.fixture
def canary():
    sp = importlib.util.spec_from_file_location("canary_mod", "scripts/canary.py")
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    return mod


def _wire(agent, monkeypatch, existing):
    written = {}
    monkeypatch.setattr(agent, "get_config",
                        lambda: {"morning_cache_history": list(existing)})
    monkeypatch.setattr(agent, "update_config",
                        lambda k, v: written.__setitem__(k, v))
    return written


class TestTheMorningRecordsWhatHappened:
    def test_a_hit_is_recorded_with_its_age(self, agent, monkeypatch):
        w = _wire(agent, monkeypatch, [])
        cached = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
        agent._record_morning_cache_outcome({"cached_at": cached}, ET.localize(
            dt.datetime(2026, 9, 8, 7, 0)))
        (e,) = w["morning_cache_history"]
        assert e["date"] == "2026-09-08" and e["hit"] is True
        assert 2.9 < e["age_h"] < 3.1

    def test_a_miss_is_recorded(self, agent, monkeypatch):
        w = _wire(agent, monkeypatch, [])
        agent._record_morning_cache_outcome(None, ET.localize(dt.datetime(2026, 9, 8, 7, 0)))
        assert w["morning_cache_history"] == [{"date": "2026-09-08", "hit": False}]

    def test_a_rerun_OVERWRITES_the_same_day(self, agent, monkeypatch):
        """⚠️ The morning can be re-triggered with force=true. Appending would
        let one day count twice and quietly skew the rate."""
        w = _wire(agent, monkeypatch, [{"date": "2026-09-08", "hit": False}])
        cached = dt.datetime.now(dt.timezone.utc).isoformat()
        agent._record_morning_cache_outcome({"cached_at": cached},
                                            ET.localize(dt.datetime(2026, 9, 8, 11, 0)))
        hist = w["morning_cache_history"]
        assert len(hist) == 1 and hist[0]["hit"] is True

    def test_history_is_capped(self, agent, monkeypatch):
        old = [{"date": f"2026-08-{d:02d}", "hit": True} for d in range(1, 26)]
        w = _wire(agent, monkeypatch, old)
        agent._record_morning_cache_outcome(None, ET.localize(dt.datetime(2026, 9, 8, 7, 0)))
        assert len(w["morning_cache_history"]) == agent.CACHE_HISTORY_MAX

    def test_it_NEVER_raises(self, agent, monkeypatch):
        """🔑 A monitoring write must not be able to cost users their picks."""
        def boom():
            raise RuntimeError("gist down")
        monkeypatch.setattr(agent, "get_config", boom)
        agent._record_morning_cache_outcome(None, ET.localize(dt.datetime(2026, 9, 8, 7, 0)))

    def test_run_morning_actually_calls_it(self, agent):
        """A recorder nothing calls is the failure it was built to fix."""
        import ast, pathlib
        src = pathlib.Path("agent.py").read_text()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "run_morning")
        assert "_record_morning_cache_outcome" in ast.unparse(fn)


class TestTheCanaryTurnsStampsIntoARate:
    def _verdict(self, canary, monkeypatch, hist):
        import config_manager
        monkeypatch.setattr(config_manager, "get_config",
                            lambda: {"morning_cache_history": hist})
        canary.RESULTS.clear()
        canary.check_morning_cache_hit_rate()
        name, ok, note = canary.RESULTS[-1]
        assert name == "morning.cache_hit_rate"
        return ok, note

    def test_below_the_minimum_it_reports_a_count_not_a_rate(self, canary, monkeypatch):
        ok, note = self._verdict(canary, monkeypatch,
                                 [{"date": "d1", "hit": True}] * 3)
        assert ok and "building baseline" in note and "3/5" in note

    def test_the_observed_33pc_miss_rate_PASSES(self, canary, monkeypatch):
        """⚠️ Load-bearing. ~33% is GitHub Actions scheduler lateness, which no
        code change fixes. A check that reddens on it is permanently red and
        therefore permanently ignored."""
        hist = [{"date": f"d{i}", "hit": i % 3 != 0} for i in range(9)]
        ok, note = self._verdict(canary, monkeypatch, hist)
        assert ok, "must not cry wolf on unfixable scheduler jitter"

    def test_a_mostly_broken_pipeline_FAILS(self, canary, monkeypatch):
        hist = [{"date": f"d{i}", "hit": i % 4 == 0} for i in range(8)]
        ok, note = self._verdict(canary, monkeypatch, hist)
        assert not ok and "NOT SERVING THE MORNING" in note

    def test_a_totally_dead_prescreener_FAILS_and_names_the_days(self, canary, monkeypatch):
        hist = [{"date": f"2026-09-0{i}", "hit": False} for i in range(1, 7)]
        ok, note = self._verdict(canary, monkeypatch, hist)
        assert not ok and "2026-09-01" in note

    def test_the_RATE_IS_VISIBLE_even_when_green(self, canary, monkeypatch):
        """🔑 Most of the value. A silent degradation stops being silent."""
        hist = [{"date": f"d{i}", "hit": True, "age_h": 3.0} for i in range(8)]
        ok, note = self._verdict(canary, monkeypatch, hist)
        assert ok and "8/8" in note and "100%" in note and "3.0h" in note

    def test_malformed_entries_do_not_crash_or_count(self, canary, monkeypatch):
        hist = [{"date": "d1", "hit": True}, "junk", {"nope": 1}, None]
        ok, note = self._verdict(canary, monkeypatch, hist)
        assert ok and "1/5" in note


class TestSelfHealMustNotTouchIt:
    def test_it_is_owner_only(self):
        """Self-heal cannot fix GitHub's scheduler — its only available 'fix'
        would be to loosen the threshold, i.e. delete the signal."""
        from owner_only_checks import OWNER_ONLY_CHECKS, all_owner_only
        assert "morning.cache_hit_rate" in OWNER_ONLY_CHECKS
        assert all_owner_only({"morning.cache_hit_rate"})
