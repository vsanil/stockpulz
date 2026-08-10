"""Tests for the pick-quality evaluator (scripts/evaluate_picks.py).

The whole point of this script is HONESTY about small samples — these tests lock
that in, because a silently-confident report on n=8 is how an engine gets tuned
into overfitting.
"""
import os
import json
import importlib.util

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "evaluate_picks.py")
_spec = importlib.util.spec_from_file_location("evaluate_picks", _PATH)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _rows(n, win_every_other=True, alpha=1.0):
    return [{"date": "2026-06-01", "ticker": f"T{i}", "asset": "stock",
             "timeframe": "short_term", "entry": 100, "target": 110, "stop": 95,
             "conviction": 4, "outcome": "target", "exit": 110,
             "ret_pct": 5.0 if (i % 2 or not win_every_other) else -3.0,
             "spy_pct": 1.0, "alpha_pct": alpha if (i % 2) else -alpha}
            for i in range(n)]


class TestWilsonInterval:
    def test_small_n_interval_is_wide(self):
        lo, hi = ev._wilson(5, 7)
        assert hi - lo > 40, "a 7-trade sample must NOT look precise"

    def test_large_n_interval_narrows(self):
        lo, hi = ev._wilson(55, 100)
        assert hi - lo < 25
        assert lo < 55 < hi

    def test_zero_n_safe(self):
        assert ev._wilson(0, 0) == (0.0, 0.0)


class TestHonestyGate:
    def test_small_sample_refuses_to_conclude(self):
        r = ev.build_report(_rows(8))
        assert "NOT CONCLUSIVE" in r
        assert "Do not tune the engine on this" in r

    def test_large_sample_is_allowed_to_conclude(self):
        r = ev.build_report(_rows(40))
        assert "NOT CONCLUSIVE" not in r
        assert "Sample is meaningful" in r

    def test_no_matured_picks_says_so(self):
        r = ev.build_report([])
        assert "No picks have matured" in r


class TestBenchmarkVerdict:
    def test_zero_alpha_reads_in_line_not_trailing(self):
        rows = _rows(40, alpha=0.0)
        for x in rows:
            x["alpha_pct"] = 0.0
        r = ev.build_report(rows)
        assert "in line" in r
        assert "trailing by" not in r      # "+0.00% trailing" was a real bug

    def test_positive_alpha_reads_beating(self):
        rows = _rows(40)
        for x in rows:
            x["alpha_pct"] = 2.0
        assert "beating" in ev.build_report(rows)


class TestScoringGuards:
    def test_unmatured_pick_is_not_scored(self):
        import datetime as dt
        today = dt.date.today().isoformat()
        assert ev.score_pick({"date": today, "ticker": "AAPL", "asset": "stock",
                              "entry": 100, "target": 110, "stop": 95}) is None, \
            "counting unmatured picks biases the sample toward fast winners"


class TestScreenProvenanceInLedger:
    """Screener features are recorded at pick time so the report can say WHICH
    signal earned a win. They cannot be backfilled — screener_cache has a 1-day
    TTL and holds survivors only — so a silent break here loses data forever."""

    def _picks(self, screen=None):
        p = {"ticker": "AAA", "entry_price": 100.0, "target_price": 110.0,
             "stop_loss": 95.0, "conviction": 4}
        if screen is not None:
            p["_screen"] = screen
        return {"_saved_date": "2026-08-10",
                "stocks": {"short_term": [p], "long_term": []}}

    def test_screen_is_copied_onto_the_ledger_row(self, monkeypatch):
        scr = {"score": 95, "rsi": 67.8, "setup_type": "breakout"}
        monkeypatch.setattr(ev, "_gist_file", lambda name: self._picks(scr))
        led = {}
        assert ev.record_today(led) == 1
        assert led["picks"][0]["screen"] == scr

    def test_row_still_records_when_screen_is_absent(self, monkeypatch):
        """Older picks (and any pick whose candidate wasn't matched) must still
        be ledgered — instrumentation is additive, never a filter."""
        monkeypatch.setattr(ev, "_gist_file", lambda name: self._picks(None))
        led = {}
        assert ev.record_today(led) == 1
        row = led["picks"][0]
        assert row["ticker"] == "AAA" and "screen" not in row

    def test_score_band_buckets_and_tolerates_missing(self):
        assert ev._score_band({"screen": {"score": 96}}) == "95+"
        assert ev._score_band({"screen": {"score": 90}}) == "85-94"
        assert ev._score_band({"screen": {"score": 71}}) == "70-84"
        assert ev._score_band({"screen": {"score": 40}}) == "<70"
        assert ev._score_band({}) == "?"
        assert ev._score_band({"screen": {"score": None}}) == "?"

    def test_slices_are_tagged_low_n_and_never_read_as_findings(self):
        rows = _rows(8)
        for i, r in enumerate(rows):
            r["screen"] = {"score": 95, "setup_type": "breakout" if i % 2 else "pullback"}
        out = ev.build_report(rows)
        assert "By setup" in out and "By screener score" in out
        assert "low n" in out, "an 8-pick slice must be marked low n"
        assert "NOT CONCLUSIVE" in out

    def test_setup_slice_is_skipped_when_nothing_was_recorded(self):
        out = ev.build_report(_rows(6))          # no `screen` on any row
        assert "By setup" not in out, "an all-unknown slice must not be printed"

    def test_report_flags_how_many_rows_predate_the_instrumentation(self):
        rows = _rows(10)
        for r in rows[:4]:
            r["screen"] = {"score": 95, "setup_type": "breakout"}
        out = ev.build_report(rows)
        assert "4/10" in out, "must say how much of the sample carries features"


class TestScreenProvenanceAttach:
    """ai_analyzer re-joins the screener features Claude's response drops."""

    def _mod(self):
        import importlib, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("ai_analyzer")

    def test_setup_type_separates_breakout_from_pullback(self):
        """One definition, living in screener.py — the rubric's owner."""
        import importlib, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        st = importlib.import_module("screener").setup_type
        assert st({"breakout_today": True, "rsi": 67.8}) == "breakout"
        assert st({"breakout_today": False, "rsi": 45.0}) == "pullback"
        assert st({"breakout_today": False, "rsi": 78.0}) == "other"
        assert st({}) == "other"

    def test_attach_joins_by_ticker_and_symbol_and_compresses(self):
        a = self._mod()
        picks = {"stocks": {"short_term": [{"ticker": "AAA", "entry_price": 10}]},
                 "crypto": {"short_term": [{"symbol": "BTC", "entry_price": 1}]}}
        cands = [{"ticker": "AAA", "score": 95, "rsi": 67.83291, "breakout_today": True,
                  "market_cap": 64_000_000_000, "sector": "Health Care"},
                 {"symbol": "BTC", "score": 80, "rsi": 44.4444}]
        a._attach_screen_provenance(picks, cands)
        s = picks["stocks"]["short_term"][0]["_screen"]
        assert s["score"] == 95 and s["setup_type"] == "breakout"
        assert s["rsi"] == 67.83, "float must be rounded — the ledger keeps it forever"
        assert s["mcap_b"] == 64.0 and "market_cap" not in s
        assert picks["crypto"]["short_term"][0]["_screen"]["setup_type"] == "pullback"

    def test_attach_is_measurement_only_and_changes_no_pick(self):
        """The evaluator must never contaminate the engine: attaching provenance
        may ONLY add `_screen` — no pick added, dropped, reordered or edited."""
        import copy, json
        a = self._mod()
        picks = {"stocks": {"short_term": [
            {"ticker": "AAA", "entry_price": 10.0, "target_price": 12.0,
             "stop_loss": 9.0, "conviction": 5, "thesis": "x"}]}}
        before = copy.deepcopy(picks)
        a._attach_screen_provenance(picks, [{"ticker": "AAA", "score": 90, "rsi": 50}])
        after = picks["stocks"]["short_term"][0]
        assert set(after) - set(before["stocks"]["short_term"][0]) == {"_screen"}
        after_wo = {k: v for k, v in after.items() if k != "_screen"}
        assert after_wo == before["stocks"]["short_term"][0]

    def test_unmatched_pick_is_left_untouched(self):
        a = self._mod()
        picks = {"stocks": {"short_term": [{"ticker": "ZZZ", "entry_price": 1}]}}
        a._attach_screen_provenance(picks, [{"ticker": "AAA", "score": 90}])
        assert "_screen" not in picks["stocks"]["short_term"][0]

    def test_attach_tolerates_empty_and_malformed_input(self):
        a = self._mod()
        a._attach_screen_provenance({}, [])
        a._attach_screen_provenance({"stocks": {"short_term": None}}, None)
        a._attach_screen_provenance({"stocks": {}}, [{"no_ticker": 1}])


class TestControlGroup:
    """Near-misses are recorded as CONTROLS — the runners-up we passed over —
    to answer whether the final selection step beats its own alternatives."""

    def _picks(self, nm_st=(), nm_lt=()):
        return {"_saved_date": "2026-08-10",
                "stocks": {
                    "short_term": [{"ticker": "PICKED", "entry_price": 100.0,
                                    "target_price": 110.0, "stop_loss": 95.0,
                                    "conviction": 5, "_screen": {"score": 95,
                                                                 "setup_type": "breakout"}}],
                    "long_term": [],
                    "near_misses": {"short_term": list(nm_st), "long_term": list(nm_lt)}}}

    def test_near_misses_are_recorded_as_controls(self, monkeypatch):
        nm = [{"ticker": "RUNNERUP", "current_price": 50.0, "score": 88,
               "rsi": 45.0, "breakout_today": False}]
        monkeypatch.setattr(ev, "_gist_file", lambda n: self._picks(nm_st=nm))
        led = {}
        assert ev.record_today(led) == 2
        ctl = [r for r in led["picks"] if r.get("control")]
        assert len(ctl) == 1
        assert ctl[0]["ticker"] == "RUNNERUP" and ctl[0]["entry"] == 50.0
        assert ctl[0]["screen"] == {"score": 88, "setup_type": "pullback"}
        # the real pick is NOT flagged as a control
        assert not [r for r in led["picks"] if r["ticker"] == "PICKED"][0].get("control")

    def test_control_without_a_usable_price_is_skipped(self, monkeypatch):
        nm = [{"ticker": "NOPRICE", "current_price": 0, "score": 80},
              {"ticker": "NONE_PX", "score": 80}]
        monkeypatch.setattr(ev, "_gist_file", lambda n: self._picks(nm_st=nm))
        led = {}
        ev.record_today(led)
        assert [r["ticker"] for r in led["picks"]] == ["PICKED"]

    def test_a_control_never_shadows_a_real_pick_of_the_same_ticker(self, monkeypatch):
        nm = [{"ticker": "PICKED", "current_price": 99.0, "score": 80}]
        monkeypatch.setattr(ev, "_gist_file", lambda n: self._picks(nm_st=nm))
        led = {}
        assert ev.record_today(led) == 1
        assert not led["picks"][0].get("control")

    # ── the guard that matters ───────────────────────────────────────────────
    def test_controls_never_enter_the_headline_or_the_slices(self):
        """A control is a trade we did NOT recommend. If it leaked into the
        headline it would dilute the engine's record with picks it never made."""
        picks = _rows(30)                       # all winners-ish, alpha +1.0
        ctl = _rows(30)
        for r in ctl:
            r["control"] = True
            r["ret_pct"] = -99.0                # catastrophic, unmistakable
            r["alpha_pct"] = -99.0
            r["mtm_pct"] = -99.0
            r["mtm_alpha_pct"] = -99.0
        for r in picks:
            r["mtm_pct"] = r["ret_pct"]
            r["mtm_alpha_pct"] = r["alpha_pct"]
        out = ev.build_report(picks + ctl)
        assert "-99" not in out.split("not picked")[0], \
            "control returns leaked into the headline/slices"
        assert "30 matured picks" in out, "headline n must count picks only"

    def test_picked_vs_control_reports_the_edge_and_gates_on_n(self):
        picks = _rows(30)
        ctl = _rows(30)
        for r in picks:
            r["mtm_pct"], r["mtm_alpha_pct"] = 5.0, 3.0
        for r in ctl:
            r["control"] = True
            r["mtm_pct"], r["mtm_alpha_pct"] = 1.0, -1.0
        out = ev.build_report(picks + ctl)
        assert "Picked vs the runners-up" in out
        assert "selection added" in out and "+4.00%" in out

    def test_edge_below_the_gate_is_not_presented_as_a_finding(self):
        picks, ctl = _rows(30), _rows(5)
        for r in picks:
            r["mtm_pct"], r["mtm_alpha_pct"] = 5.0, 3.0
        for r in ctl:
            r["control"] = True
            r["mtm_pct"], r["mtm_alpha_pct"] = 1.0, -1.0
        out = ev.build_report(picks + ctl)
        assert "NOT a finding yet" in out
        assert "selection added" not in out

    def test_negative_edge_is_stated_plainly(self):
        picks, ctl = _rows(30), _rows(30)
        for r in picks:
            r["mtm_pct"], r["mtm_alpha_pct"] = 1.0, -1.0
        for r in ctl:
            r["control"] = True
            r["mtm_pct"], r["mtm_alpha_pct"] = 6.0, 4.0
        out = ev.build_report(picks + ctl)
        assert "not adding value" in out, "a negative result must not be softened"


class TestNearMissReason:
    """The reason shown under 'Near misses' in the morning message. It read a
    NESTED st_metrics key that flattened candidates don't have, so `m` was always
    {} and every near-miss rendered the same boilerplate regardless of why it
    actually missed."""

    def _sc(self):
        import importlib, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("screener")

    def test_opposite_candidates_get_different_reasons(self):
        sc = self._sc()
        strong = {"ticker": "A", "score": 88, "rsi": 45.0, "macd_crossover": True,
                  "volume_ratio": 2.5, "above_ema20": True}
        weak = {"ticker": "B", "score": 60, "rsi": 72.0, "macd_crossover": False,
                "volume_ratio": 0.8, "above_ema20": False}
        r1 = sc._add_near_miss_reason(strong, is_st=True)["near_miss_reason"]
        r2 = sc._add_near_miss_reason(weak, is_st=True)["near_miss_reason"]
        assert r1 != r2, "every near-miss showed identical boilerplate"
        assert "overbought" in r2 and "72" in r2
        assert "no MACD crossover" not in r1, "a candidate WITH a crossover must not be told it lacks one"

    def test_nested_metrics_still_work(self):
        """Raw (un-flattened) candidates must keep working."""
        sc = self._sc()
        nested = {"ticker": "C", "st_metrics": {"rsi": 72.0, "macd_crossover": True}}
        assert "overbought" in sc._add_near_miss_reason(nested, is_st=True)["near_miss_reason"]


class TestShardedLedger:
    """The ledger is append-only and kept forever; a Gist file is unreadable past
    ~1 MB. Picks + controls cross that inside one year, so it shards by year."""

    def test_shard_name_is_per_year(self):
        assert ev._shard_name(2026) == "pick_ledger_2026.json"
        assert ev._shard_name(2027) == "pick_ledger_2027.json"

    def test_truncated_file_is_refetched_not_parsed_partially(self, monkeypatch):
        """A truncated read parsed as JSON looks like an EMPTY ledger, which
        would re-record every pick ever made."""
        calls = {}
        class _R:
            text = '{"picks": [{"date": "2026-01-01", "ticker": "FULL"}]}'
            def raise_for_status(self): pass
        def _get(url, **kw):
            calls["raw"] = url
            return _R()
        monkeypatch.setattr(ev.requests, "get", _get)
        out = ev._content({"truncated": True, "raw_url": "https://raw/x",
                           "content": '{"picks": [{"date": "2026-01-01"'})  # partial
        assert json.loads(out)["picks"][0]["ticker"] == "FULL"
        assert calls["raw"] == "https://raw/x"

    def test_untruncated_file_uses_inline_content(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("must not refetch an untruncated file")
        monkeypatch.setattr(ev.requests, "get", _boom)
        assert ev._content({"content": '{"picks": []}'}) == '{"picks": []}'

    def test_dedupe_spans_shards_across_a_year_rollover(self, monkeypatch):
        """A pick already recorded in LAST year's shard must not be re-recorded
        into this year's."""
        monkeypatch.setattr(ev, "_gist_file", lambda n: {
            "_saved_date": "2026-12-31",
            "stocks": {"short_term": [{"ticker": "AAA", "entry_price": 10.0}],
                       "long_term": [], "near_misses": {}}})
        shard = {"picks": []}
        prior = {("2026-12-31", "AAA", None)}    # lives in another shard (date,ticker,arm)
        assert ev.record_today(shard, known=prior) == 0
        assert shard["picks"] == []
        assert ev.record_today({"picks": []}, known=set()) == 1


class TestLedgerSaveIsVerified:
    """A silently-dropped ledger write is UNRECOVERABLE: record_today only ever
    reads today's picks.json, so there is no later run that can backfill the
    missing day — and a lost day isn't random, it correlates with the busy
    morning window, quietly biasing the sample the launch call rests on."""

    class _Resp:
        def __init__(self, code): self.status_code, self.text = code, "throttled"

    def test_returns_true_on_success(self, monkeypatch):
        monkeypatch.setattr(ev.requests, "patch", lambda *a, **k: self._Resp(200))
        assert ev._save_ledger({"picks": []}, "pick_ledger_2026.json") is True

    def test_retries_then_reports_failure(self, monkeypatch):
        calls = {"n": 0}
        def _patch(*a, **k):
            calls["n"] += 1
            return self._Resp(403)
        monkeypatch.setattr(ev.requests, "patch", _patch)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda s: None)
        assert ev._save_ledger({"picks": []}, "x.json") is False
        assert calls["n"] == 5, "must retry, not give up on the first throttle"

    def test_exception_is_retried_not_swallowed(self, monkeypatch):
        calls = {"n": 0}
        def _boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("network down")
        monkeypatch.setattr(ev.requests, "patch", _boom)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda s: None)
        assert ev._save_ledger({"picks": []}, "x.json") is False
        assert calls["n"] == 5


class TestEtfAndCommodityProvenance:
    """🔴 Found live on 2026-08-10: XLB was the ONLY pick that day without a
    `screen` blob. `_attach_screen_provenance` joined against stock + crypto
    candidates only, so every ETF and commodity pick carried no provenance and
    would be silently absent from every setup_type and score-band slice in the
    Sep 9 report — a whole asset class missing from the evidence base."""

    def _mod(self):
        import importlib, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("ai_analyzer")

    # real shapes, from etf_screener.py and _build_commodity_candidates
    ETF = {"ticker": "XLB", "price": 92.1, "ret_1m": 4.9, "rsi": 51.6,
           "vol_ratio": 1.08, "score": 72.5}
    COMMODITY = {"ticker": "PDBC", "name": "Invesco", "commodity": "Diversified",
                 "current_price": 15.2, "ret_1m": 2.1, "rsi": 47.0, "vol_ratio": 0.9}

    def _picks(self):
        return {"etfs": {"short_term": [{"ticker": "XLB", "entry_price": 92.1}], "long_term": []},
                "commodities": {"short_term": [{"ticker": "PDBC", "entry_price": 15.2}],
                                "long_term": []}}

    def test_etf_picks_get_provenance(self):
        a = self._mod()
        p = self._picks()
        a._attach_screen_provenance(p, [self.ETF])
        s = p["etfs"]["short_term"][0].get("_screen")
        assert s, "ETF picks still carry no provenance"
        assert s["score"] == 72.5 and s["setup_type"] == "pullback"

    def test_the_etf_volume_field_is_aliased(self):
        """ETF/commodity screeners call it `vol_ratio`; everything downstream
        reads `volume_ratio`. Without the alias the signal is silently dropped."""
        a = self._mod()
        p = self._picks()
        a._attach_screen_provenance(p, [self.ETF])
        assert p["etfs"]["short_term"][0]["_screen"]["volume_ratio"] == 1.08

    def test_commodity_picks_get_provenance_even_without_a_score(self):
        a = self._mod()
        p = self._picks()
        a._attach_screen_provenance(p, [self.COMMODITY])
        s = p["commodities"]["short_term"][0].get("_screen")
        assert s and "score" not in s, "commodities have no score — must not invent one"
        assert s["volume_ratio"] == 0.9 and s["ret_1m"] == 2.1

    def test_ret_1m_is_recorded(self):
        """The momentum signal ETFs are actually ranked on — without it their
        rows carry almost nothing to slice by."""
        a = self._mod()
        p = self._picks()
        a._attach_screen_provenance(p, [self.ETF])
        assert p["etfs"]["short_term"][0]["_screen"]["ret_1m"] == 4.9

    def test_an_alias_never_overwrites_a_real_value(self):
        a = self._mod()
        cand = {**self.ETF, "volume_ratio": 3.3}   # both names present
        p = self._picks()
        a._attach_screen_provenance(p, [cand])
        assert p["etfs"]["short_term"][0]["_screen"]["volume_ratio"] == 3.3
