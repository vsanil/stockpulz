"""Every asset class that reaches a user must actually be produced, and every
exclusion must be a decision rather than an oversight.

Found 2026-08-14 by asking "do alerts cover all six asset classes?":

  * COMMODITIES had been structurally dead for at least ten days. `yf.download(
    ..., group_by="ticker")` returns columns as (ticker, field) — ticker at
    level 0 — but the extractor asked for level 1, so every lookup raised
    KeyError, was swallowed into `return None`, and skipped. Zero candidates,
    zero log output, and the model then "correctly returns []" exactly as its
    own docstring predicts. Verified in production: the real 11:02 UTC run on
    2026-08-14 printed "Built 0 commodity candidates".
  * OPTIONS shipped with strike: null and expiry: null on every play, because
    the Polygon parser never produced `nearest_otm_call` — a field only the
    yfinance FALLBACK built. With POLYGON_API_KEY set (i.e. in production) it
    was always None, and the prompt says "NEVER invent a strike; if
    nearest_otm_call is absent, describe the trade directionally". The model
    obeyed. It was being starved.

Both failures were invisible because the correct downstream behaviour for
missing data looks exactly like a quiet day.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_analyzer
import options_flow


# ── commodities ──────────────────────────────────────────────────────────────
def _frame(outer, inner, names):
    idx = pd.date_range("2026-05-01", periods=70, freq="B")
    cols = pd.MultiIndex.from_product([outer, inner], names=names)
    data = np.random.default_rng(0).uniform(50, 150, size=(len(idx), len(cols)))
    return pd.DataFrame(data, index=idx, columns=cols)


FIELDS = ["Open", "High", "Low", "Close", "Volume"]


class TestCommodityColumnLayout:
    """🔴 The regression. Pins BOTH yfinance layouts, so neither a hardcoded
    level nor a future layout flip can silently empty the section again."""

    def _tickers(self):
        return [t for t, _, _ in ai_analyzer._COMMODITY_TICKERS]

    def _run(self, monkeypatch, raw):
        monkeypatch.setattr(ai_analyzer.yf, "download", lambda *a, **k: raw)
        return ai_analyzer._build_commodity_candidates()

    def test_group_by_ticker_layout_produces_candidates(self, monkeypatch):
        """(ticker, field) — what group_by="ticker" actually returns, and the
        layout production has been receiving all along. Fails pre-fix."""
        t = self._tickers()
        got = self._run(monkeypatch, _frame(t, FIELDS, ("Ticker", "Price")))
        assert len(got) == len(t), f"group_by='ticker' layout yielded {len(got)}/{len(t)}"
        assert all(c["current_price"] > 0 for c in got)

    def test_field_first_layout_still_works(self, monkeypatch):
        """(field, ticker) — the no-group_by layout. Must not regress while
        fixing the other one."""
        t = self._tickers()
        got = self._run(monkeypatch, _frame(FIELDS, t, ("Price", "Ticker")))
        assert len(got) == len(t)

    def test_the_signals_the_rubric_scores_are_populated(self, monkeypatch):
        """Without ret_1m / rsi / vol_ratio the model cannot rate above 2 stars
        and correctly returns [] — so a candidate missing them is no better
        than no candidate at all."""
        got = self._run(monkeypatch, _frame(self._tickers(), FIELDS, ("Ticker", "Price")))
        c = got[0]
        assert c["ret_1m"] is not None and c["rsi"] is not None and c["vol_ratio"] is not None

    def test_an_unusable_frame_yields_zero_without_raising(self, monkeypatch):
        got = self._run(monkeypatch, pd.DataFrame())
        assert got == []

    def test_a_zero_build_is_LOUD(self, monkeypatch, capsys):
        """The silence is what hid this. A plain 'Built 0' line read as benign
        for at least ten days."""
        self._run(monkeypatch, pd.DataFrame())
        out = capsys.readouterr().out
        assert "⚠️" in out and "COMMODITIES SECTION WILL BE EMPTY" in out

    def test_a_healthy_build_does_not_cry_wolf(self, monkeypatch, capsys):
        self._run(monkeypatch, _frame(self._tickers(), FIELDS, ("Ticker", "Price")))
        assert "⚠️" not in capsys.readouterr().out


# ── options ──────────────────────────────────────────────────────────────────
def _contract(ctype, strike, exp, vol, oi, spot=100.0):
    return {"details": {"contract_type": ctype, "strike_price": strike,
                        "expiration_date": exp},
            "day": {"volume": vol}, "open_interest": oi,
            "underlying_asset": {"price": spot}}


class TestPolygonProducesACitableStrike:
    """🔴 `nearest_otm_call` was built ONLY by the yfinance fallback, so with a
    Polygon key set it was always None and every options play shipped with
    strike: null / expiry: null."""

    @pytest.fixture
    def wired(self, monkeypatch):
        def _fake_get(url, params=None, timeout=None):
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    return {"results": [
                        _contract("call", 105.0, "2026-09-18", 12, 400),
                        _contract("call", 110.0, "2026-09-18", 30, 900),
                        _contract("call", 102.0, "2026-10-16", 44, 100),   # cheaper, LATER
                        _contract("call",  95.0, "2026-09-18", 50, 100),   # ITM, not OTM
                        _contract("call", 107.0, "2026-09-18",  0, 700),   # never traded
                        _contract("put",  95.0, "2026-09-18", 20, 300),
                    ]}
            return R()
        monkeypatch.setattr(options_flow.requests, "get", _fake_get)
        monkeypatch.setattr(options_flow, "_polygon_key", lambda: "test-key")
        return options_flow._get_polygon_options("TEST")

    def test_polygon_returns_a_strike_and_expiry(self, wired):
        assert wired is not None
        assert wired["nearest_otm_call"] == 105.0
        assert wired["nearest_call_expiry"] == "2026-09-18"

    def test_it_prefers_the_earliest_expiry_not_the_lowest_strike(self, wired):
        """Matches the yfinance path, which walks expiries in order. The 102
        strike is cheaper but expires a month later — a different trade."""
        assert wired["nearest_otm_call"] != 102.0

    def test_in_the_money_strikes_are_never_offered_as_OTM(self, wired):
        assert wired["nearest_otm_call"] > wired["current_price"]

    def test_an_untraded_contract_is_not_citable(self, wired):
        """Volume 0 means nobody has traded it — quoting it as 'the nearest
        call' sends the user at an illiquid contract."""
        assert wired["nearest_otm_call"] != 107.0

    def test_the_gamma_pin_is_populated_too(self, wired):
        """Same silent gap in the same function: consumed by ai_analyzer and
        described in the prompt, but only ever built by the fallback."""
        assert wired["gamma_pin_strike"] == 110.0        # highest aggregate OI
        assert wired["gamma_pin_dist_pct"] == 10.0

    def test_both_sources_agree_on_the_contract(self, wired):
        """A caller must not be able to tell which source it got. `_compute_signal`
        reads these keys off whichever raw dict it is handed."""
        for k in ("nearest_otm_call", "nearest_call_expiry", "current_price",
                  "gamma_pin_strike", "gamma_pin_dist_pct"):
            assert k in wired, f"polygon path is missing {k}, so it degrades to None"

    def test_no_underlying_price_means_no_invented_strike(self, monkeypatch):
        """Better to emit nothing than a strike derived from a missing spot —
        the prompt's whole rule is 'never invent a strike'."""
        def _fake_get(url, params=None, timeout=None):
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    c = _contract("call", 105.0, "2026-09-18", 12, 400)
                    c["underlying_asset"] = {}
                    return {"results": [c]}
            return R()
        monkeypatch.setattr(options_flow.requests, "get", _fake_get)
        monkeypatch.setattr(options_flow, "_polygon_key", lambda: "test-key")
        raw = options_flow._get_polygon_options("TEST")
        assert raw["nearest_otm_call"] is None and raw["current_price"] is None


# ── options are withheld while their signal is empty ─────────────────────────
class TestOptionsStrikeGate:
    """🔴 Owner's call, Aug 15: stop shipping options while the signal is dead.

    Implemented as a SIGNAL gate, not a deletion. Verified on CI that
    options_flow returns source="none" in production — Polygon 403s on the free
    tier and yfinance is blocked from GitHub Actions IPs — so put_call_ratio,
    sweep_detected, unusual AND the strike are all empty, and the model was
    writing option theses from nothing. Gating on the strike is self-healing:
    when the feed returns, strikes populate and the section comes back with no
    code change.
    """

    def _clean(self, plays):
        return ai_analyzer._validate_and_clean_picks(
            {"stocks": {}, "options_plays": plays}, {"KMI", "NVDA"})

    def test_a_play_with_no_strike_is_withheld(self):
        out = self._clean([{"ticker": "KMI", "action": "CALL",
                            "strike": None, "expiry": None, "note": "x"}])
        assert not out.get("options_plays"), \
            "an unactionable play ('CALL, strike: null') must not reach a user"

    def test_a_fully_specified_play_still_ships(self):
        """The gate must not become a deletion — this is what auto-restores."""
        out = self._clean([{"ticker": "NVDA", "action": "CALL", "strike": 185.0,
                            "expiry": "2026-09-18", "note": "x"}])
        assert len(out.get("options_plays") or []) == 1

    def test_a_strike_without_an_expiry_is_not_citable(self):
        out = self._clean([{"ticker": "NVDA", "action": "CALL",
                            "strike": 185.0, "expiry": None, "note": "x"}])
        assert not out.get("options_plays")

    @pytest.mark.parametrize("bad", [0, -5.0, float("nan"), float("inf"), True, "185"])
    def test_garbage_strikes_are_rejected(self, bad):
        """A NaN strike is truthy and every comparison against it is False, so a
        bare `if strike:` would ship it. Same family as _is_pos."""
        out = self._clean([{"ticker": "NVDA", "action": "CALL",
                            "strike": bad, "expiry": "2026-09-18", "note": "x"}])
        assert not out.get("options_plays"), f"strike={bad!r} reached a user"

    def test_suppression_is_announced(self, capsys):
        self._clean([{"ticker": "KMI", "action": "CALL", "strike": None,
                      "expiry": None, "note": "x"}])
        out = capsys.readouterr().out
        assert "Suppressed 1 options play" in out and "WITHHELD" in out

    def test_a_withheld_section_is_not_reported_as_a_quiet_MARKET(self):
        """'no setups today' claims we looked and found nothing. We cannot look,
        so saying it would be a false statement to the user."""
        import formatters
        msg = formatters.format_daily_message(
            {"stocks": {"short_term": [{"ticker": "AAPL", "entry_price": 100.0,
                                        "target_price": 110.0, "stop_loss": 95.0,
                                        "conviction": 4, "thesis": "t"}],
                        "long_term": []},
             "crypto": {"short_term": [], "long_term": []},
             "etfs": {"short_term": [], "long_term": []},
             "commodities": {"short_term": [], "long_term": []},
             "options_plays": []}, {})
        assert "🎯 Options" not in msg, \
            "a withheld section must be silent, not listed as a quiet market"


# ── the exclusions are decisions, not oversights ─────────────────────────────
class TestExclusionsAreDocumented:
    def test_alert_scope_explains_what_it_skips(self):
        import inspect
        doc = inspect.getdoc(__import__("agent")._auto_set_pick_alerts) or ""
        assert "options_plays" in doc and "crypto long_term" in doc, \
            "an excluded asset class must be an explicit decision in the docstring"

    def test_every_delivered_non_options_section_is_alerted(self):
        """The real invariant: anything a user is shown, other than the two
        documented exclusions, gets an alert."""
        import inspect
        import agent
        src = inspect.getsource(agent._auto_set_pick_alerts)
        # Anchor on the statement that FOLLOWS the block, not on a bare ")" —
        # the first ")" belongs to picks.get("stocks", {}).
        start = src.index("_lt_picks = (")
        body = src[start:src.index("ENTRY_ALERT_MIN_ABOVE", start)]
        for sec in ("stocks", "crypto", "etfs", "commodities"):
            assert f'"{sec}"' in body, f"{sec} picks reach users with no alert"
        for tf in ("short_term", "long_term"):
            assert tf in body

    def test_short_term_picks_are_processed_before_long_term(self):
        """Order is load-bearing: a ticker that is both (KMI on 2026-08-14) must
        take its real short-term stop, after which the wide long-term
        invalidation level is skipped for it as redundant."""
        import inspect
        import agent
        src = inspect.getsource(agent._auto_set_pick_alerts)
        block = src[src.index("all_sections = ("):src.index("ENTRY_ALERT_MIN_ABOVE")]
        assert block.index('"short_term"') < block.index("_lt_picks\n"), \
            "long-term picks must be appended AFTER the short-term ones"

    def test_the_ledger_states_why_options_are_unmeasured(self):
        import importlib.util
        p = os.path.join(ROOT, "scripts", "evaluate_picks.py")
        spec = importlib.util.spec_from_file_location("evaluate_picks", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        doc = (m.record_picks.__doc__ or "")
        assert "options_plays" in doc and "UNMEASURED" in doc
