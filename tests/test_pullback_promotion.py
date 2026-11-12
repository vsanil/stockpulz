"""The pullback arm's promotion to production (finding engine/promote-pullback-st).

Since 2026-11-12 the morning run's SHORT-TERM STOCK picks come from the
tournament's pullback rules, not from Claude. Everything else is unchanged:
Claude still picks long-term stocks, crypto, ETFs and commodities.

What is pinned, and why:
  * Claude is handed NO short-term stock candidate, and a short-term stock pick
    it returns anyway never ships — otherwise two engines blur into one record.
  * The picks are built by the SAME rules the tournament measured, carry what
    the message needs, and say plainly that no AI chose them.
  * They are exempt from the 3-star gate (the tournament never gated them).
  * The overnight job runs the pullback screen BEFORE the default one, because
    every screen overwrites data_quality.json and the canary grades the
    default (fundamentals) run.
  * A missing pullback shortlist is screened live where allowed, else the
    section is empty and the owner is alerted — never a quiet Claude fallback.
"""
import os
import sys
from datetime import datetime

import pytest
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent as ag  # noqa: E402
import config_manager as cm  # noqa: E402

_ET = pytz.timezone("America/New_York")
WEEKDAY = _ET.localize(datetime(2026, 11, 13, 7, 1))   # Friday, not a holiday


def _cand(t, score, price=100.0, stop_pct=6.0, rsi=48.0):
    return {"ticker": t, "company": f"{t} Corp", "sector": "Tech",
            "current_price": price, "score": score,
            "suggested_stop_pct": stop_pct, "atr_pct": stop_pct / 1.5, "rsi": rsi}


def _pullback_screen(n=7):
    return {"short_term": [_cand(f"PB{i}", 99 - i) for i in range(n)],
            "long_term": [],
            "near_misses": {"short_term": [_cand("NMP", 60)], "long_term": []}}


# ── The pick builder ─────────────────────────────────────────────────────────

class TestRuleBasedPicks:
    def test_top_five_with_the_measured_levels(self):
        picks, near = ag._rule_based_st_picks(_pullback_screen())
        assert [p["ticker"] for p in picks] == ["PB0", "PB1", "PB2", "PB3", "PB4"]
        for p in picks:
            e, s, t = p["entry_price"], p["stop_loss"], p["target_price"]
            assert s < e < t
            assert t == pytest.approx(e * 1.103, rel=1e-6)
            assert s == pytest.approx(e * (1 - 0.06), rel=1e-6)
        assert [c["ticker"] for c in near] == ["NMP"]

    def test_each_pick_renders_and_says_no_ai_chose_it(self):
        picks, _ = ag._rule_based_st_picks(_pullback_screen())
        for p in picks:
            assert p["company"] == f"{p['ticker']} Corp"
            assert p["action"] == "BUY"
            assert "not AI" in p["plain_english"]
            assert p["thesis"] and p["theme"]
            assert p["strategy"] == "pullback"
            assert p["_rule_based"] is True
            assert p.get("_screen"), "provenance must survive for the evaluator"

    @pytest.mark.parametrize("bad", [None, [], "x", 0])
    def test_a_missing_or_broken_screen_yields_nothing(self, bad):
        assert ag._rule_based_st_picks(bad) == ([], [])

    def test_an_empty_screen_yields_nothing(self):
        assert ag._rule_based_st_picks({"short_term": [], "long_term": []}) == ([], [])


class TestConvictionGate:
    def test_rule_based_picks_are_exempt_and_model_picks_are_not(self):
        picks = {"stocks": {"short_term": [
                    {"ticker": "RULE", "conviction": 2, "_rule_based": True},
                    {"ticker": "MODEL", "conviction": 2}],
                 "long_term": []}}
        out = ag._filter_by_conviction(picks, 3)
        assert [p["ticker"] for p in out["stocks"]["short_term"]] == ["RULE"]


# ── The morning run, end to end with Claude stubbed ──────────────────────────

class _Stop(Exception):
    pass


def _wire_morning(monkeypatch, cache, live_ok=False, live_result=None):
    """Drive run_morning with every external call stubbed. Returns a dict that
    collects what Claude was given, what was saved, alerts and live screens."""
    seen = {"claude_stocks": None, "saved": None, "alerts": [], "live": []}
    import yfinance as _yf
    monkeypatch.setattr(ag, "MOCK_DATA", False)
    monkeypatch.setattr(ag, "_log_cron_run", lambda *a, **k: None)
    monkeypatch.setattr(ag, "is_market_holiday", lambda d: False)
    monkeypatch.setattr(_yf, "download", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setattr(ag, "_screener_cache_with_retry", lambda *a, **k: cache)
    monkeypatch.setattr(ag, "_record_morning_cache_outcome", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_can_run_live_screener", lambda: live_ok)
    monkeypatch.setattr(ag, "_run_crypto_with_retry", lambda: {"short_term": [], "long_term": []})
    monkeypatch.setattr(ag, "run_etf_screener", lambda: {"short_term": [], "long_term": []})
    monkeypatch.setattr(ag, "get_dynamic_pick_counts", lambda cfg: {})
    monkeypatch.setattr(ag, "_alert", lambda msg, **k: seen["alerts"].append(msg))

    def _live(**kw):
        seen["live"].append(getattr(kw.get("strategy"), "name", None))
        if live_result is None:
            raise RuntimeError("live screen unavailable")
        return live_result
    monkeypatch.setattr(ag, "run_screener", _live)

    def _claude(stocks, config, **kw):
        seen["claude_stocks"] = stocks
        return {"stocks": {
                    "short_term": [{"ticker": "CLAUDEST", "entry_price": 50,
                                    "target_price": 55, "stop_loss": 47, "conviction": 5}],
                    "long_term": [{"ticker": "LTPICK", "entry_price": 200,
                                   "target_price": 260, "conviction": 4}]},
                "crypto": {"short_term": [{"ticker": "BTC", "entry_price": 90000,
                                           "target_price": 99000, "stop_loss": 85000,
                                           "conviction": 4}], "long_term": []}}
    monkeypatch.setattr(ag, "analyze_with_claude", _claude)

    def _save(picks):
        seen["saved"] = picks
        raise _Stop()
    monkeypatch.setattr(ag, "save_picks", _save)
    return seen


def _cache(with_pullback=True):
    c = {"stocks": {"short_term": [_cand("DEFAULTST", 95)],
                    "long_term": [dict(_cand("LTPICK", 80), lt_score=70)],
                    "near_misses": {"short_term": [_cand("DEFNM", 50)],
                                    "long_term": [_cand("LTNM", 40)]}},
         "crypto": {"short_term": [_cand("BTC", 70)], "long_term": []}}
    if with_pullback:
        c["stocks_pullback"] = _pullback_screen(3)
    return c


def _run(monkeypatch, **kw):
    seen = _wire_morning(monkeypatch, **kw)
    with pytest.raises(_Stop):
        ag.run_morning({}, WEEKDAY)
    return seen


class TestMorningRun:
    def test_claude_gets_no_short_term_stocks_but_keeps_the_rest(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache())
        assert seen["claude_stocks"]["short_term"] == []
        assert [c["ticker"] for c in seen["claude_stocks"]["long_term"]] == ["LTPICK"]

    def test_the_short_term_stocks_shipped_are_the_pullback_picks(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache())
        st = seen["saved"]["stocks"]["short_term"]
        assert [p["ticker"] for p in st] == ["PB0", "PB1", "PB2"]
        assert "CLAUDEST" not in [p["ticker"] for p in st], \
            "a short-term stock pick from the model must never ship"

    def test_every_other_section_is_still_claudes(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache())
        assert [p["ticker"] for p in seen["saved"]["stocks"]["long_term"]] == ["LTPICK"]
        assert [p["ticker"] for p in seen["saved"]["crypto"]["short_term"]] == ["BTC"]

    def test_near_misses_short_term_from_pullback_long_term_unchanged(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache())
        nm = seen["saved"]["stocks"]["near_misses"]
        assert [c["ticker"] for c in nm["short_term"]] == ["NMP"]
        assert [c["ticker"] for c in nm["long_term"]] == ["LTNM"]

    def test_a_cache_without_the_shortlist_screens_pullback_live(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache(with_pullback=False),
                    live_ok=True, live_result=_pullback_screen(2))
        assert seen["live"] == ["pullback"]
        assert [p["ticker"] for p in seen["saved"]["stocks"]["short_term"]] == ["PB0", "PB1"]
        assert not any("Pullback shortlist missing" in a for a in seen["alerts"])

    def test_no_shortlist_and_no_live_screen_means_no_st_stocks_and_an_alert(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache(with_pullback=False), live_ok=False)
        assert seen["live"] == []
        assert seen["saved"]["stocks"]["short_term"] == [], \
            "never a quiet fallback to Claude choosing short-term stocks"
        assert seen["claude_stocks"]["short_term"] == []
        assert any("Pullback shortlist missing" in a for a in seen["alerts"])

    def test_a_failing_live_screen_is_also_loud(self, monkeypatch):
        seen = _run(monkeypatch, cache=_cache(with_pullback=False),
                    live_ok=True, live_result=None)
        assert seen["live"] == ["pullback"]
        assert seen["saved"]["stocks"]["short_term"] == []
        assert any("Pullback shortlist missing" in a for a in seen["alerts"])


# ── The overnight job ────────────────────────────────────────────────────────

class TestPrescreener:
    def _wire(self, monkeypatch, pullback_fails=False):
        import run_prescreener as rp
        calls, saved = [], {}
        monkeypatch.setattr(rp, "_log_cron_run", lambda *a, **k: None)
        monkeypatch.setattr(rp, "is_market_holiday", lambda d: False)
        monkeypatch.setattr(rp, "get_config", lambda: {})
        monkeypatch.setattr(rp, "_run_crypto_with_retry",
                            lambda: {"short_term": [], "long_term": []})

        def _screen(**kw):
            name = getattr(kw.get("strategy"), "name", "default")
            calls.append(name)
            if name == "pullback":
                if pullback_fails:
                    raise RuntimeError("boom")
                return _pullback_screen(2)
            return {"short_term": [_cand("D", 90)], "long_term": []}
        monkeypatch.setattr(rp, "run_screener", _screen)

        def _save(stocks, crypto, pullback=None):
            saved.update(stocks=stocks, crypto=crypto, pullback=pullback)
        monkeypatch.setattr(rp, "save_screener_cache", _save)
        rp.main()
        return calls, saved

    def test_pullback_runs_first_so_the_default_screen_writes_data_quality_last(self, monkeypatch):
        calls, _ = self._wire(monkeypatch)
        assert calls == ["pullback", "default"]

    def test_the_shortlist_is_saved_into_the_cache(self, monkeypatch):
        _, saved = self._wire(monkeypatch)
        assert [c["ticker"] for c in saved["pullback"]["short_term"]] == ["PB0", "PB1"]

    def test_a_failing_pullback_screen_never_blocks_the_default_cache(self, monkeypatch):
        calls, saved = self._wire(monkeypatch, pullback_fails=True)
        assert calls == ["pullback", "default"]
        assert saved["stocks"]["short_term"][0]["ticker"] == "D"
        assert saved["pullback"] is None


class TestCacheRoundTrip:
    STOCKS = {"short_term": [{"ticker": "RJF", "score": 80}], "long_term": []}
    CRYPTO = {"short_term": [], "long_term": []}

    def test_the_pullback_shortlist_round_trips(self, _gist_store):
        pb = {"short_term": [{"ticker": "PB0", "score": 90}], "long_term": []}
        cm.save_screener_cache(self.STOCKS, self.CRYPTO, pb)
        cache = cm.load_screener_cache()
        assert cache["stocks_pullback"] == pb
        assert cache["stocks"] == self.STOCKS

    def test_an_old_style_save_has_no_shortlist_and_still_loads(self, _gist_store):
        cm.save_screener_cache(self.STOCKS, self.CRYPTO)
        cache = cm.load_screener_cache()
        assert cache is not None and "stocks_pullback" not in cache
