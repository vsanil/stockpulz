"""Traffic telemetry: when is the bot used, and did anyone pay for a cold server?

This is the evidence base for a future "upgrade Render" decision, so the tests
care most about the ways it could QUIETLY under-report — an empty chart that
looks like "nobody uses it at night" when really the counts were lost.
"""
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import traffic_tracker as tt


@pytest.fixture(autouse=True)
def _clean():
    tt._pending.clear()
    tt._pending_hits = 0
    tt._last_flush = tt.time.time()
    tt._last_cold_flush = 0.0
    yield
    tt._pending.clear()
    tt._pending_hits = 0


class _Store:
    """Stands in for the gist, honouring the REAL contract.

    🔴 The first version of this stub did `self.data = mutator(self.data)`,
    i.e. it accepted a bare return value. The real `mutate_gist_file` does
    `new_contents, result = mutator(current)`. That mismatch let a broken
    tracker ship green: every live flush raised "not enough values to unpack"
    and was swallowed, so nothing was ever written. A stub must enforce the
    contract, not a convenient version of it.
    """

    def __init__(self, fail=False):
        self.data, self.writes, self.fail = {}, 0, fail

    def mutate(self, _file, mutator, default=None):
        if self.fail:
            raise RuntimeError("gist unavailable")
        self.writes += 1
        new_contents, result = mutator(self.data if self.data else (default or {}))
        self.data = new_contents
        return result


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    import config_manager
    monkeypatch.setattr(config_manager, "mutate_gist_file", s.mutate, raising=False)
    return s


def _at(hour, minute=0):
    return datetime(2026, 8, 11, hour, minute, tzinfo=timezone.utc)


# `summarise()` defaults to the REAL current month when none is given — correct
# for production (the admin card wants "this month"), but a test that stamps
# hits into a hardcoded past month and then calls summarise() with no month
# reads the (empty) real-world month instead. Passing this explicitly is the
# same fix as every other UTC/ET clock mismatch in this codebase: a test must
# read on the SAME clock it wrote with, not on whatever day it happens to run.
_MONTH = _at(0).strftime("%Y-%m")


class TestColdHitsSurviveSpinDown:
    """🔴 The reason this module is not just 'count in memory, flush on a timer'.

    Render kills the process ~15 min after the last request. A lone 3 AM tap
    would increment a counter and then die with the process — and isolated
    cold-window events are exactly the data the upgrade decision needs. Cold
    hits are rare, so they are written out immediately.
    """

    def test_a_cold_hit_is_written_immediately(self, store):
        tt.record("123", cold=True, now=_at(4))
        assert store.writes == 1, "cold hit was left in memory and would die with the process"
        assert store.data["2026-08"]["04"]["cold"] == 1

    def test_a_warm_hit_is_not_written_immediately(self, store):
        tt.record("123", cold=False, now=_at(14))
        assert store.writes == 0, "warm hits must batch — gist writes are rate limited"

    def test_the_count_survives_a_simulated_process_death(self, store):
        """Record a cold hit, then throw away all in-memory state exactly as a
        spin-down would. The stored count must still be there."""
        tt.record("123", cold=True, now=_at(3))
        tt._pending.clear()                        # <- the process dies here
        tt._pending_hits = 0
        assert tt.summarise(store.data, month=_MONTH)["cold_starts"] == 1

    def test_warm_hits_flush_once_the_threshold_is_reached(self, store):
        for _ in range(tt._FLUSH_AFTER_HITS):
            tt.record("123", cold=False, now=_at(14))
        assert store.writes == 1
        assert store.data["2026-08"]["14"]["hits"] == tt._FLUSH_AFTER_HITS


class TestCountsAreNotLostOnFailure:
    def test_a_failed_flush_keeps_the_counts_for_the_next_attempt(self, monkeypatch):
        """A transient gist error must not silently discard telemetry — the
        counts are only cleared once the write is known to have landed."""
        import config_manager
        bad = _Store(fail=True)
        monkeypatch.setattr(config_manager, "mutate_gist_file", bad.mutate, raising=False)
        tt.record("123", cold=True, now=_at(4))
        assert tt._pending["2026-08"]["04"]["hits"] == 1

        good = _Store()
        monkeypatch.setattr(config_manager, "mutate_gist_file", good.mutate, raising=False)
        assert tt.flush() is True
        assert good.data["2026-08"]["04"]["hits"] == 1
        assert not tt._pending.get("2026-08")

    def test_a_hit_counted_during_the_write_is_not_dropped(self, monkeypatch):
        """flush() subtracts exactly what it wrote instead of clearing outright.

        The injection has to happen INSIDE the write, after the batch snapshot
        was taken — bumping the counter before calling flush() proves nothing,
        because that hit is simply part of the batch.
        """
        import config_manager
        s = _Store()
        real = s.mutate

        def racing(_file, mutator, default=None):
            out = real(_file, mutator, default)
            tt._pending["2026-08"]["14"]["hits"] += 1        # a request lands mid-write
            tt._pending["2026-08"]["14"]["users"]["999"] = 1
            return out

        monkeypatch.setattr(config_manager, "mutate_gist_file", racing, raising=False)
        tt.record("123", cold=False, now=_at(14))
        tt.flush()
        assert s.data["2026-08"]["14"]["hits"] == 1
        assert tt._pending["2026-08"]["14"]["hits"] == 1, "concurrent hit was discarded"
        assert tt._pending["2026-08"]["14"]["users"] == {"999": 1}

    def test_telemetry_never_raises_into_the_request(self, monkeypatch):
        import config_manager
        monkeypatch.setattr(config_manager, "mutate_gist_file",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
                            raising=False)
        tt.record("123", cold=True, now=_at(4))            # must not raise


class TestColdStartDetection:
    def test_a_young_process_is_a_cold_start(self, monkeypatch):
        monkeypatch.setattr(tt, "_PROC_START", tt.time.time())
        assert tt.is_cold_start() is True

    def test_a_long_lived_process_is_not(self, monkeypatch):
        monkeypatch.setattr(tt, "_PROC_START", tt.time.time() - tt.COLD_START_SECS - 1)
        assert tt.is_cold_start() is False


class TestSummary:
    def test_hits_outside_the_warm_window_are_the_headline(self, store, monkeypatch):
        monkeypatch.setenv("KEEPWARM_HOURS_UTC", "10,11,12")
        tt.record("123", cold=True, now=_at(4))            # asleep
        tt.record("123", cold=False, now=_at(11))          # warm
        s = tt.summarise(store.data, month=_MONTH)
        assert s["total"] == 2
        assert s["outside_window"] == 1 and s["outside_window_cold"] == 1

    def test_unflushed_hits_still_show_on_the_card(self, store):
        """Warm hits batch behind a 25-hit / 10-min threshold. At two users a
        real interaction could sit invisible for hours, and a card that
        under-reports is the exact failure this module guards against."""
        tt.record("123", cold=False, now=_at(14))
        assert store.writes == 0                            # nothing written yet
        assert tt.summarise(store.data, month=_MONTH)["total"] == 1

    def test_merging_pending_never_mutates_the_stored_blob(self, store):
        tt.record("123", cold=True, now=_at(4))             # flushes
        tt.record("123", cold=False, now=_at(14))           # stays pending
        before = tt._copy(store.data)
        tt.summarise(store.data)
        assert store.data == before, "summarise mutated the caller's data"

    def test_a_warm_hit_in_the_cold_window_is_counted_but_not_as_a_cold_start(
            self, store, monkeypatch):
        """The distinction the whole card rests on: a 4 AM request that landed on
        an already-warm server cost the user nothing and argues for nothing."""
        monkeypatch.setenv("KEEPWARM_HOURS_UTC", "10,11,12")
        tt.record("123", cold=False, now=_at(4))
        s = tt.summarise(store.data, month=_MONTH)
        assert s["outside_window"] == 1 and s["outside_window_cold"] == 0

    def test_per_user_cold_window_activity_is_attributed(self, store, monkeypatch):
        monkeypatch.setenv("KEEPWARM_HOURS_UTC", "10")
        tt.record("night_owl", cold=True, now=_at(3))
        tt.record("day_user", cold=False, now=_at(10))
        users = {u["chat_id"]: u for u in tt.summarise(store.data, month=_MONTH)["users"]}
        assert users["night_owl"]["cold_window"] == 1
        assert users["day_user"]["cold_window"] == 0

    def test_an_empty_month_is_flagged_as_no_data(self):
        """An empty chart must never read as 'nobody uses it at night' — it may
        just mean counting started yesterday."""
        assert tt.summarise({})["no_data"] is True

    def test_unidentified_hits_still_count(self, store):
        tt.record(None, cold=True, now=_at(4))
        s = tt.summarise(store.data, month=_MONTH)
        assert s["total"] == 1
        assert s["users"][0]["chat_id"] == "anon"

    def test_every_hour_is_present_so_the_chart_has_no_gaps(self, store):
        tt.record("123", cold=False, now=_at(14))
        assert [h["hour"] for h in tt.summarise(store.data)["hours"]] == list(range(24))


class TestKeepWarmPingIsNeverCounted:
    """🔴 Load-bearing exclusion. /health fires 4x an hour around the clock; if
    it were counted every hour would look busy and the measurement would be
    worthless."""

    @pytest.mark.parametrize("path", ["/health", "/trigger/morning", "/static/x.png",
                                      "/favicon.ico", "/admin"])
    def test_infrastructure_paths_are_skipped(self, path):
        import webhook
        assert any(path.startswith(p) for p in webhook._TRAFFIC_SKIP_PREFIXES), \
            f"{path} would be counted as user traffic"

    @pytest.mark.parametrize("path", ["/webhook", "/api/miniapp/picks"])
    def test_real_user_paths_are_counted(self, path):
        import webhook
        assert not any(path.startswith(p) for p in webhook._TRAFFIC_SKIP_PREFIXES)


class TestColdFlushIsDebounced:
    """🔴 Found by reading a GREEN full sweep's log body (2026-08-12): six
    `409 Conflict` writes to traffic_hours.json in one run, plus collateral 409s
    on user_configs and a trade log.

    Cause: `_PROC_START` makes EVERY request in the first COLD_START_SECS look
    cold, so a burst fired one gist PATCH per request. Rapid successive PATCHes
    to one gist are what tripped GitHub's secondary rate limit on 2026-07-03 and
    left the admin's alerts wiped when the RESTORE was the blocked call.
    """

    def test_the_first_cold_hit_still_flushes_immediately(self, store):
        """The whole point of the design — a lone 3 AM tap must survive the
        spin-down that follows it."""
        tt.record("123", cold=True, now=_at(4))
        assert store.writes == 1

    def test_a_burst_of_cold_hits_does_NOT_write_per_request(self, store):
        for _ in range(20):
            tt.record("123", cold=True, now=_at(4))
        assert store.writes == 1, f"burst caused {store.writes} gist writes"

    def test_nothing_is_lost_when_the_burst_is_throttled(self, store):
        for _ in range(20):
            tt.record("123", cold=True, now=_at(4))
        tt.flush()
        assert store.data["2026-08"]["04"]["hits"] == 20
        assert store.data["2026-08"]["04"]["cold"] == 20

    def test_a_later_cold_hit_flushes_again_once_the_gap_has_passed(self, store):
        tt.record("123", cold=True, now=_at(4))
        assert store.writes == 1
        tt._last_cold_flush -= tt._MIN_COLD_FLUSH_GAP + 1     # simulate the gap
        tt.record("123", cold=True, now=_at(5))
        assert store.writes == 2

    def test_the_gap_is_short_enough_to_beat_a_spin_down(self):
        """Render idles at ~15 min. The debounce must be far below that or a
        throttled cold hit could die with the process."""
        assert tt._MIN_COLD_FLUSH_GAP < 60, "must be well under the ~15 min idle timeout"
