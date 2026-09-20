"""Phase 2: arms that actually differ, selected without Claude.

Two properties make the tournament worth running, and both are pinned here.

1. THE ARMS ARE DISJOINT BY CONSTRUCTION. Before this they were weight variants
   of one pool and overlapped the default 44-63% (measured 2026-08-14), so most
   of each arm's budget bought picks whose outcome was identical in both arms.
   `setup_type` returns exactly ONE label per candidate, so two arms requiring
   different labels cannot intersect — a structural guarantee, not a hope.

2. ONLY THE LIVE ARM CALLS CLAUDE (owner's Option B, 2026-09-19). Everything
   else is the screener's own ranking, which is what isolates the one thing
   never measured: what the model's selection adds over taking the top N.
"""
import ast
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import arm_selector as sel
import config_manager as cm
import screener as sc


def _const(rel: str, name: str):
    """Read a module-level constant WITHOUT importing the module — these pull
    pandas, the screener and a 65 MB SEC cache for a single float."""
    for n in ast.parse((ROOT / rel).read_text()).body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(f"{name} not found in {rel}")


def _cand(t, score=90, price=100.0, **kw):
    d = {"ticker": t, "company": f"{t} Inc", "sector": "Tech",
         "current_price": price, "score": score}
    d.update(kw)
    return d


class TestArmsAreDisjointByConstruction:
    """The property the whole tournament rests on."""

    POP = [
        _cand("BRK1", breakout_today=True, rsi=70),
        _cand("BRK2", breakout_today=True, rsi=45),      # BOTH signals present
        _cand("PUL1", rsi=45),
        _cand("PUL2", rsi=35),                            # band edge
        _cand("OTH1", rsi=80),
        _cand("OTH2"),                                    # no rsi at all
    ]

    def _pool(self, arm):
        return {c["ticker"] for c in
                sc.eligible_candidates(list(self.POP), sc.STRATEGIES[arm], is_short=True)}

    def test_breakout_and_pullback_pools_cannot_intersect(self):
        assert not (self._pool("breakout") & self._pool("pullback"))

    def test_a_candidate_carrying_BOTH_signals_lands_in_exactly_one_arm(self):
        """BRK2 is breaking out AND sits in the RSI band. `setup_type` resolves
        it to one label, which is precisely why the pools stay disjoint."""
        assert "BRK2" in self._pool("breakout")
        assert "BRK2" not in self._pool("pullback")

    def test_quality_cannot_intersect_either_technical_arm(self):
        """A third AXIS, not a third weighting: it does not trade their pool."""
        assert self._pool("quality") == set()
        assert not sc.STRATEGIES["quality"].trades_short_term

    def test_every_candidate_is_claimed_by_at_most_one_technical_arm(self):
        for c in self.POP:
            hits = sum(1 for a in ("breakout", "pullback") if c["ticker"] in self._pool(a))
            assert hits <= 1, f"{c['ticker']} reached two arms"

    def test_the_union_is_a_SUBSET_of_what_the_default_sees(self):
        """An arm narrows the live pool; it must never invent a candidate."""
        default = self._pool("default")
        assert default == {c["ticker"] for c in self.POP}
        assert (self._pool("breakout") | self._pool("pullback")) <= default


class TestProductionIsUntouched:
    def test_the_default_strategy_filters_nothing(self):
        pop = list(TestArmsAreDisjointByConstruction.POP)
        for is_short in (True, False):
            out = sc.eligible_candidates(pop, sc.DEFAULT_STRATEGY, is_short=is_short)
            assert out == pop, "the live engine must see every candidate"

    def test_the_new_fields_default_to_no_op(self):
        d = sc.Strategy()
        assert d.requires_setup == ""
        assert d.trades_short_term and d.trades_long_term

    def test_the_default_strategy_is_unchanged_in_every_field(self):
        """Adding fields must not move a single live weight — a 'tidy' of a
        default silently changes what real users are recommended."""
        assert sc.DEFAULT_STRATEGY == sc.Strategy()
        assert sc.STRATEGIES["default"] is sc.DEFAULT_STRATEGY

    def test_a_starved_arm_says_so_rather_than_looking_like_a_quiet_market(self, capsys):
        sc.eligible_candidates([_cand("OTH", rsi=80)], sc.STRATEGIES["pullback"], is_short=True)
        out = capsys.readouterr().out
        assert "NO candidate matched" in out and "not an error" in out

    def test_a_healthy_arm_does_not_cry_wolf(self, capsys):
        sc.eligible_candidates([_cand("P", rsi=45)], sc.STRATEGIES["pullback"], is_short=True)
        assert "NO candidate matched" not in capsys.readouterr().out


class TestArmAccounts:
    def test_every_arm_has_its_own_account(self):
        ids = list(cm.ARM_CHAT_IDS.values())
        assert len(ids) == len(set(ids))

    def test_no_arm_points_at_a_real_user(self):
        """The independent fact — `is_test_user` is DERIVED from ARM_CHAT_IDS
        and so cannot catch this."""
        allowed = set(cm.get_allowed_users())
        for name, cid in cm.ARM_CHAT_IDS.items():
            assert cid not in allowed, f"arm {name} points at an allow-listed user"

    def test_the_new_arms_are_registered_as_test_users(self):
        for arm in ("quality", "spy_hold"):
            assert cm.is_test_user(cm.ARM_CHAT_IDS[arm])

    def test_every_screener_arm_has_an_account(self):
        for name in sc.STRATEGIES:
            if name != "default":
                assert name in cm.ARM_CHAT_IDS, f"{name} has nowhere to trade"


class TestDeterministicSelection:
    def _screen(self, st=(), lt=()):
        return {"short_term": list(st), "long_term": list(lt), "near_misses": {}}

    def test_short_term_levels_come_from_the_candidates_own_volatility(self):
        p = sel.select(self._screen(st=[_cand("A", price=100.0, suggested_stop_pct=7.5)]))
        pick = p["stocks"]["short_term"][0]
        assert pick["stop_loss"] == 92.5                      # 7.5% below entry
        assert pick["target_price"] == round(100 * 1.103, 4)  # measured median

    def test_a_missing_stop_falls_back_to_the_measured_median(self):
        pick = sel.select(self._screen(st=[_cand("A", price=100.0)]))["stocks"]["short_term"][0]
        assert pick["stop_loss"] == round(100 * (1 - sel.STOP_PCT / 100), 4)

    def test_the_stop_percent_is_read_as_a_PERCENT_not_a_fraction(self):
        """Reading 7.5 as 0.075 clamped every ATR stop to the maximum once."""
        assert sel.stop_pct_for({"suggested_stop_pct": 7.5}) == 7.5

    def test_an_absurd_stop_is_clamped_both_ways(self):
        assert sel.stop_pct_for({"suggested_stop_pct": 0.4}) == sel.MIN_STOP_PCT
        assert sel.stop_pct_for({"suggested_stop_pct": 95.0}) == sel.MAX_STOP_PCT

    def test_a_long_term_pick_carries_NO_stop_by_design(self):
        """Production long-term picks have none; the trader applies the
        invalidation level. Substituting a short-term stop here would make
        every long-term arm pick a 5%-stop trade the engine never published."""
        pick = sel.select(self._screen(lt=[_cand("L", price=100.0)]))["stocks"]["long_term"][0]
        assert pick["stop_loss"] is None
        assert pick["target_price"] == round(100 * (1 + sel.LT_TARGET_PCT / 100), 4)

    def test_conviction_tracks_the_screener_score(self):
        assert sel.conviction_for(99) == 5 and sel.conviction_for(90) == 4
        assert sel.conviction_for(75) == 3 and sel.conviction_for(40) == 2
        assert sel.conviction_for(None) == 2 and sel.conviction_for(float("nan")) == 2

    def test_an_unpriceable_candidate_is_never_a_pick(self):
        for bad in (None, 0, -5, float("nan"), float("inf"), "abc"):
            out = sel.select(self._screen(st=[_cand("A", price=bad)]))
            assert out["stocks"]["short_term"] == []

    def test_provenance_is_recorded_so_every_slice_works_for_arms_too(self):
        pick = sel.select(self._screen(
            st=[_cand("A", rsi=45, breakout_today=False)]))["stocks"]["short_term"][0]
        assert pick["_screen"]["score"] == 90 and pick["_screen"]["rsi"] == 45

    def test_the_output_matches_the_shape_the_ledger_expects(self):
        ev_spec = importlib.util.spec_from_file_location(
            "ev_arms", ROOT / "scripts" / "evaluate_picks.py")
        ev = importlib.util.module_from_spec(ev_spec); ev_spec.loader.exec_module(ev)
        picks = sel.select(self._screen(st=[_cand("A")], lt=[_cand("B")]))
        led: dict = {}
        n = ev.record_picks(led, picks, "2026-09-22", arm="breakout")
        assert n == 2
        assert {r["ticker"] for r in led["picks"]} == {"A", "B"}
        assert all(r["arm"] == "breakout" for r in led["picks"])

    def test_a_duplicate_ticker_in_one_section_is_taken_once(self):
        out = sel.select(self._screen(st=[_cand("A"), _cand("A")]))
        assert len(out["stocks"]["short_term"]) == 1

    def test_only_stocks_are_produced_and_the_other_sections_stay_empty(self):
        out = sel.select(self._screen(st=[_cand("A")]))
        for section in ("crypto", "etfs", "commodities"):
            assert out[section] == {"short_term": [], "long_term": []}


class TestTheConstantsArePinnedToTheirSources:
    def test_levels_match_the_measured_backtest_constants(self):
        assert sel.TARGET_PCT == _const("scripts/backtest_walkforward.py", "TARGET_PCT")
        assert sel.STOP_PCT == _const("scripts/backtest_walkforward.py", "STOP_PCT")

    def test_the_long_term_stop_distance_is_the_apps_own_invalidation(self):
        assert sel.LT_INVALIDATION_PCT == _const("agent.py", "LT_INVALIDATION_PCT")

    def test_the_long_term_target_is_derived_not_chosen(self):
        assert sel.LT_TARGET_PCT == round(
            sel.LT_INVALIDATION_PCT * (sel.TARGET_PCT / sel.STOP_PCT), 1)

    def test_the_score_bands_line_up_with_the_evaluators(self):
        """A conviction slice of the report must mean the same thing as the
        sizing that produced it."""
        ev_spec = importlib.util.spec_from_file_location(
            "ev_band", ROOT / "scripts" / "evaluate_picks.py")
        ev = importlib.util.module_from_spec(ev_spec); ev_spec.loader.exec_module(ev)
        for score, band in ((99, "95+"), (90, "85-94"), (75, "70-84"), (40, "<70")):
            assert ev._score_band({"screen": {"score": score}}) == band


class TestTheArmSelectorIsMeasurementOnly:
    def test_the_engine_never_imports_it(self):
        """Wiring an arm back into production is the contamination the
        evaluator was built to avoid.

        Checks real IMPORT statements via the AST, not a substring. The first
        version scanned text and failed on a screener COMMENT that names this
        module while explaining that arms do not call Claude — the self-flagging
        scan trap this repo has now hit more than a dozen times. Anchor on
        structure; prose is not code.
        """
        for f in ("screener.py", "ai_analyzer.py", "agent.py", "webhook.py",
                  "position_sizer.py", "formatters.py"):
            imported = set()
            for n in ast.walk(ast.parse((ROOT / f).read_text())):
                if isinstance(n, ast.Import):
                    imported |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module:
                    imported.add(n.module.split(".")[0])
            assert "arm_selector" not in imported, f"{f} imports the arm selector"

    def test_it_calls_no_model(self):
        src = (ROOT / "arm_selector.py").read_text()
        tree = ast.parse(src)
        names = {getattr(n, "attr", None) for n in ast.walk(tree)} | \
                {getattr(n, "id", None) for n in ast.walk(tree)}
        for banned in ("analyze_with_claude", "anthropic", "Anthropic", "_get_client"):
            assert banned not in names, f"an arm must not spend a Claude call: {banned}"
