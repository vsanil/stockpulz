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

    def test_no_two_arms_share_a_horizon_at_all(self):
        """Total separation, not partial. The technical arms are short-term
        only and `quality` is long-term only, so there is no leg left on which
        two arms could pick the same name — which is what the earlier design
        got wrong: short-term overlap went to 0 while the arms still shared 4
        of 5 LONG-TERM picks, so half of every arm was a duplicate."""
        horizons = {}
        for name, st in sc.STRATEGIES.items():
            if name == "default":
                continue
            horizons[name] = (st.trades_short_term, st.trades_long_term,
                              st.requires_setup)
        assert horizons["breakout"] == (True, False, "breakout")
        assert horizons["pullback"] == (True, False, "pullback")
        assert horizons["quality"] == (False, True, "")
        # No pair shares a tradeable horizon under the same setup label.
        seen = set()
        for name, (short, long_, setup) in horizons.items():
            key = ("ST", setup) if short else ("LT", setup)
            assert key not in seen, f"{name} shares a lane with another arm"
            seen.add(key)

    def test_the_technical_arms_hold_no_long_term_names(self):
        pop = [_cand("L1", rsi=45), _cand("L2", breakout_today=True)]
        for arm in ("breakout", "pullback"):
            assert sc.eligible_candidates(pop, sc.STRATEGIES[arm], is_short=False) == []

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


def _arms():
    spec = importlib.util.spec_from_file_location("run_arms_t", ROOT / "scripts" / "run_arms.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestThePassiveArm:
    """The alternative every other arm has to beat. The owner accepted in
    advance (2026-09-19) that this arm winning is a legitimate outcome, so it
    must be given a fair run — a handicapped benchmark flatters everything
    measured against it."""

    def _wire(self, monkeypatch, cash=10_000.0, positions=(), spy=500.0):
        import market_data, paper_trader, sim_portfolio
        state = {"buys": [], "docs": []}
        monkeypatch.setattr(market_data, "get_live_price",
                            lambda t: spy if t.upper() == "SPY" else None)
        monkeypatch.setattr(paper_trader, "load_user_paper",
                            lambda cid: {"cash": cash, "positions": list(positions),
                                         "history": [], "starting_cash": 10_000.0})
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda *a, **k: (state["buys"].append((a, k)), "📄 ok")[1])
        monkeypatch.setattr(sim_portfolio, "update",
                            lambda acct, fn: (state["docs"].append(fn(None)), state["docs"][-1])[1])
        return state

    def test_it_deploys_the_WHOLE_book_into_the_index(self, monkeypatch):
        """Not 80%. Capping a one-position index arm the way a diversified
        stock book is capped would handicap the benchmark."""
        arms = _arms()
        st = self._wire(monkeypatch, cash=10_000.0, spy=500.0)
        acts = arms.open_passive("900000013", dry=False)
        (ticker, shares, _cid), kw = st["buys"][0]
        assert ticker == "SPY" and shares == 20.0        # the entire $10,000
        assert kw.get("price") == 500.0
        assert "never sold" in acts[0]

    def test_it_buys_no_stop_and_no_target(self, monkeypatch):
        """Buy-and-hold has neither; giving it one makes it a different
        strategy and stops it being the benchmark."""
        arms = _arms()
        st = self._wire(monkeypatch)
        arms.open_passive("900000013", dry=False)
        _, kw = st["buys"][0]
        assert "stop_loss" not in kw and "target_price" not in kw

    def test_it_is_idempotent_because_holding_IS_the_strategy(self, monkeypatch):
        arms = _arms()
        st = self._wire(monkeypatch, positions=[{"ticker": "SPY", "shares": 20,
                                                 "avg_price": 500.0}])
        assert arms.open_passive("900000013", dry=False) == []
        assert st["buys"] == []

    def test_an_unpriceable_index_buys_NOTHING_and_invents_nothing(self, monkeypatch):
        arms = _arms()
        st = self._wire(monkeypatch, spy=None)
        acts = arms.open_passive("900000013", dry=False)
        assert st["buys"] == [] and st["docs"] == []
        assert "did NOT buy" in acts[0]

    def test_a_broke_passive_arm_says_so(self, monkeypatch):
        arms = _arms()
        st = self._wire(monkeypatch, cash=0.0)
        acts = arms.open_passive("900000013", dry=False)
        assert st["buys"] == [] and "no cash" in acts[0]

    def test_a_dry_run_writes_nothing(self, monkeypatch):
        arms = _arms()
        st = self._wire(monkeypatch)
        acts = arms.open_passive("900000013", dry=True)
        assert st["buys"] == [] and st["docs"] == [] and acts

    def test_manage_never_routes_the_passive_arm_through_the_exit_rules(self, monkeypatch):
        """It is buy-and-hold. Target, stop and the time stop would make it a
        trading strategy and it would stop being the benchmark.

        ⚠️ The first version of this test stubbed `paper_sell` and asserted
        nothing sold — and PASSED against a mutant that routed the passive arm
        straight into the exit rules, because the tracked-ticker state read
        came back empty so nothing could have sold either way. It also hit the
        network for 26 seconds. Assert the ROUTING, which is the actual
        invariant, and stub the trader so no I/O happens at all.
        """
        arms = _arms()
        calls = []

        class _Trader:
            def manage_account(self, cid, dry):
                calls.append(("manage_account", cid))
                return []

            def phase_open(self, *a, **k):
                calls.append(("phase_open", a, k))
                return []

        monkeypatch.setattr(arms, "_su", lambda: _Trader())
        monkeypatch.setattr(arms, "_account_for", lambda n: "900000013")
        monkeypatch.setattr(arms, "snapshot_passive", lambda cid, dry: ["marked"])
        assert arms.manage_arm("spy_hold", dry=False)["acts"] == ["marked"]
        assert calls == [], "the passive arm must not be put through the exit rules"

    def test_manage_DOES_route_a_trading_arm_through_the_exit_rules(self, monkeypatch):
        """The other half — a guard that never fires in both directions is not
        a guard."""
        arms = _arms()
        calls = []

        class _Trader:
            def manage_account(self, cid, dry):
                calls.append(cid)
                return ["sold something"]

        monkeypatch.setattr(arms, "_su", lambda: _Trader())
        monkeypatch.setattr(arms, "_account_for", lambda n: "900000010")
        assert arms.manage_arm("breakout", dry=False)["acts"] == ["sold something"]
        assert calls == ["900000010"]

    def test_it_is_marked_daily_so_its_curve_is_comparable(self, monkeypatch):
        """Without a daily mark its equity curve is a single dot and cannot be
        read beside arms that snapshot every day."""
        arms = _arms()
        import paper_trader, sim_portfolio, market_data
        marked = []
        monkeypatch.setattr(market_data, "get_live_price", lambda t: 550.0)
        monkeypatch.setattr(paper_trader, "load_user_paper",
                            lambda cid: {"cash": 0.0, "positions": [
                                {"ticker": "SPY", "shares": 20, "avg_price": 500.0}],
                                "history": [], "starting_cash": 10_000.0})
        monkeypatch.setattr(sim_portfolio, "update",
                            lambda acct, fn: marked.append(acct))
        assert arms.snapshot_passive("900000013", dry=False) == []
        assert marked == ["900000013"]


class TestTheRunnerIsSafeInEveryPhase:
    def test_the_passive_arm_runs_no_screener_and_spends_no_model_call(self):
        """It has nothing to select. A screen here would be pure cost."""
        arms = _arms()
        src = (ROOT / "scripts" / "run_arms.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "open_passive")
        body = ast.unparse(fn)
        assert "run_screener" not in body and "arm_selector" not in body

    def test_no_arm_path_calls_claude(self):
        """Owner's Option B: only the LIVE arm spends a model call."""
        src = (ROOT / "scripts" / "run_arms.py").read_text()
        names = {getattr(n, "attr", None) for n in ast.walk(ast.parse(src))} | \
                {getattr(n, "id", None) for n in ast.walk(ast.parse(src))}
        for banned in ("analyze_with_claude", "anthropic", "Anthropic"):
            assert banned not in names

    def test_a_failed_screen_is_loud_not_reported_as_sitting_out(self):
        """A starved arm and a quiet market must never look the same."""
        import pytest as _pt
        with _pt.raises(TypeError, match="the screen failed"):
            sel.select(None)


class TestTheStandingsSurface:
    def _admin(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client

    def test_every_arm_appears_even_before_it_has_traded(self):
        """An arm that is silently missing looks like an arm that was never
        built — the empty-state rule that cost a day when the findings card
        rendered nothing at all."""
        import sim_portfolio as sp
        labels = {cid: n for n, cid in cm.ARM_CHAT_IDS.items()}
        rows = sp.standings({}, labels)
        assert {r["arm"] for r in rows} == set(cm.ARM_CHAT_IDS)
        assert all(r["active"] is False for r in rows)

    def test_it_ranks_by_return_and_puts_the_best_first(self):
        import sim_portfolio as sp

        def _book(acct, ret):
            d = sp.new_doc(acct, "2026-09-22", 10_000.0)
            return sp.snapshot(d, "2026-09-23", 10_000.0 * (1 + ret / 100), 500.0, 1)

        books = {"1": _book("1", 2.0), "2": _book("2", 9.0), "3": _book("3", -4.0)}
        rows = sp.standings(books, {"1": "breakout", "2": "pullback", "3": "quality"})
        assert [r["arm"] for r in rows] == ["pullback", "breakout", "quality"]

    def test_an_inactive_arm_never_outranks_one_with_a_curve(self):
        import sim_portfolio as sp
        d = sp.snapshot(sp.new_doc("1", "2026-09-22", 10_000.0),
                        "2026-09-23", 9_000.0, 500.0, 1)          # a LOSING arm
        rows = sp.standings({"1": d}, {"1": "breakout", "2": "quality"})
        assert rows[0]["arm"] == "breakout" and rows[1]["active"] is False

    def test_the_dashboard_carries_the_standings(self, client):
        d = self._admin(client).get("/admin/data").get_json()
        assert "tournament" in d
        assert {r["arm"] for r in d["tournament"]["rows"]} == set(cm.ARM_CHAT_IDS)

    def test_the_page_renders_it_after_the_synthetic_book(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        assert "function tournamentSection" in html and 'id="tourney-card"' in html
        assert html.index("bookSection(d.sim_portfolio)") < html.index("tournamentSection(d.tournament)")

    def test_it_names_the_passive_arm_as_the_thing_to_beat(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        assert "spy_hold" in html and "has to beat" in html

    def test_the_book_document_is_read_once_for_every_arm(self):
        """All books share one document on purpose — the dashboard must not pay
        a round trip per arm."""
        src = (ROOT / "sim_portfolio.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "all_books")
        # Count CALL nodes, not the substring: the import statement carries the
        # same name, so a text count reads 2 for a function that reads once.
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_load_gist_file"]
        assert len(calls) == 1


class TestTheWorkflowCannotSpendOnAModel:
    def _wf(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/ab_arms.yml").read_text())

    def test_no_anthropic_key_reaches_the_arms(self):
        """Option B as a STRUCTURAL guarantee: reintroducing a Claude call to
        this path fails loudly instead of quietly tripling the bill."""
        env = self._wf()["jobs"]["run"]["steps"][-1]["env"]
        assert not any("ANTHROPIC" in k.upper() for k in env)

    def test_the_open_phase_is_not_on_githubs_late_scheduler(self):
        """GitHub runs this repo 1.6-6 h late. An arm bought hours after the
        entry window was published measures lateness, not strategy."""
        wf = self._wf()
        crons = [c["cron"] for c in wf[True]["schedule"]]
        assert crons and all(c.startswith("15 14-20") for c in crons), crons
        body = (ROOT / ".github/workflows/ab_arms.yml").read_text()
        i = body.index("Pick phase")
        assert "manage" in body[i:i + 400]

    def test_it_carries_the_storage_secrets_so_it_cannot_split_brain(self):
        env = self._wf()["jobs"]["run"]["steps"][-1]["env"]
        for k in ("SUPABASE_URL", "SUPABASE_KEY", "GIST_ID", "GH_GIST_TOKEN"):
            assert k in env, f"{k} missing — GH Actions would write a different store"
