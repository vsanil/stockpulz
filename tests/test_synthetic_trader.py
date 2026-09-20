"""Phase 1 of the tournament: the synthetic user's paper book trades like a
SIZED, RULE-OBEYING user whose every position RESOLVES.

Before (2026-09-19) it bought a flat $500 of everything, never exited on time,
refilled its own cash, and gave a long-term pick a 5% stop the engine never
published — a bug detector, not a model of a user, so nothing it produced
could be read as "how would a user have done".
"""
import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SU = ROOT / "scripts" / "synthetic_user.py"


def _su():
    spec = importlib.util.spec_from_file_location("su_trader", SU)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fn_body(name: str) -> str:
    tree = ast.parse(SU.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.unparse(fn)


def _const(path: pathlib.Path, name: str):
    """Read a module-level constant WITHOUT importing the module (agent costs
    ~121 MB, backtest_longterm drags in pandas + a 65 MB SEC cache)."""
    for n in ast.parse(path.read_text()).body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(f"{name} not found in {path.name}")


# ── a harness that drives phase_open with every writer captured ──────────────

class _Harness:
    def __init__(self, monkeypatch, su, picks, paper, prices, state=None):
        import market_data, trade_logger, paper_trader, price_alert_manager, webhook, sim_portfolio
        self.paper_buys, self.real_adds, self.sim_docs, self.saved = [], [], [], []
        self.state = dict(state or {})
        monkeypatch.setattr(su, "_raw_picks", lambda: picks)
        monkeypatch.setattr(su, "_state", lambda cid: dict(self.state))
        monkeypatch.setattr(su, "_save_state", lambda cid, st: (self.saved.append(st), True)[1])
        monkeypatch.setattr(market_data, "get_live_price", lambda t: prices.get(t.upper()))
        monkeypatch.setattr(trade_logger, "load_user_trade_log", lambda cid: {"open": []})
        monkeypatch.setattr(trade_logger, "add_holding",
                            lambda *a, **k: (self.real_adds.append((a, k)), ({}, False))[1])
        monkeypatch.setattr(paper_trader, "load_user_paper", lambda cid: paper)
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda *a, **k: (self.paper_buys.append((a, k)), "📄 ok")[1])
        monkeypatch.setattr(price_alert_manager, "add_alert", lambda *a, **k: None)
        monkeypatch.setattr(webhook, "_load_watchlist", lambda cid: [])
        monkeypatch.setattr(webhook, "_save_watchlist", lambda cid, w: None)
        monkeypatch.setattr(sim_portfolio, "update",
                            lambda acct, fn: (self.sim_docs.append(fn(None)), self.sim_docs[-1])[1])

    def shares_for(self, t):
        return next(a[1] for a, k in self.paper_buys if a[0] == t)


def _picks(*rows):
    """rows: (ticker, section, tf, entry, stop, target, conviction)"""
    out = {"stocks": {"short_term": [], "long_term": []}, "crypto": {"short_term": [], "long_term": []},
           "etfs": {"short_term": [], "long_term": []}, "commodities": {"short_term": [], "long_term": []}}
    for t, sec, tf, e, s, tg, conv in rows:
        key = "symbol" if sec == "crypto" else "ticker"
        out[sec][tf].append({key: t, "entry_price": e, "stop_loss": s, "target_price": tg, "conviction": conv})
    return out


def _book(cash=10_000.0, positions=()):
    return {"cash": cash, "starting_cash": 10_000.0, "positions": list(positions), "history": []}


class TestPaperBuysAreSized:
    def test_the_sizer_decides_the_share_count_not_a_flat_500(self, monkeypatch):
        """$10k book, 1% risk = $100, 5% stop → 20 shares = $2,000 → position
        cap 10% → 10 shares. The old bot would have bought 5 ($500 / $100)."""
        su = _su()
        h = _Harness(monkeypatch, su, _picks(("AAA", "stocks", "short_term", 100.0, 95.0, 120.0, 5)),
                     _book(), {"AAA": 100.0, "SPY": 500.0})
        su.phase_open("900000001", dry=False)
        assert h.shares_for("AAA") == 10.0
        assert h.paper_buys[0][1]["stop_loss"] == 95.0 and h.paper_buys[0][1]["levels_source"] == "pick"

    def test_conviction_scales_the_size(self, monkeypatch):
        su = _su()
        h = _Harness(monkeypatch, su, _picks(("AAA", "stocks", "short_term", 100.0, 95.0, 120.0, 1)),
                     _book(), {"AAA": 100.0, "SPY": 500.0})
        su.phase_open("900000001", dry=False)
        assert h.shares_for("AAA") == 5.0           # 20 × 0.25 (probe size)

    def test_crypto_is_sized_in_dollars_and_bought_fractionally(self, monkeypatch):
        su = _su()
        h = _Harness(monkeypatch, su, _picks(("BTC", "crypto", "short_term", 60_000.0, 57_000.0, 70_000.0, 5)),
                     _book(), {"BTC": 60_000.0, "SPY": 500.0})
        su.phase_open("900000001", dry=False)
        sh = h.shares_for("BTC")
        assert 0 < sh < 1 and abs(sh * 60_000.0 - 1_000.0) < 1.0   # 10% cap → $1,000 of BTC

    def test_the_size_is_recorded_on_the_twin_as_a_buy(self, monkeypatch):
        su = _su()
        h = _Harness(monkeypatch, su, _picks(("AAA", "stocks", "short_term", 100.0, 95.0, 120.0, 5)),
                     _book(), {"AAA": 100.0, "SPY": 500.0})
        su.phase_open("900000001", dry=False)
        doc = h.sim_docs[-1]
        assert doc["twin_lots"]["AAA"]["usd"] == 1000.0 and doc["twin_cash"] == 9000.0
        assert doc["snapshots"][-1]["bot"] == 10_000.0            # equity unchanged by a buy

    def test_ONE_store_write_per_run_for_the_book(self):
        """Up to eight buys plus a snapshot must not be eight PATCHes — rapid
        successive writes to one gist are what wiped the admin's alerts on Jul 3."""
        assert _fn_body("phase_open").count("sp.update(") == 1
        assert _fn_body("_manage_account").count("sp.update(") == 1

    def test_no_free_cash_top_up_remains(self):
        """A book that prints its own money has no equity curve worth reading."""
        assert "paper_add_cash" not in _fn_body("phase_open")


class TestTheBookRulesAreHard:
    def _pos(self, t, px=100.0, sh=10, stop=95.0):
        return {"ticker": t, "shares": sh, "avg_price": px, "entry_price": px, "stop_loss": stop,
                "bought_date": "2026-09-01"}

    def test_max_positions_refuses_the_ninth(self, monkeypatch):
        su = _su()
        held = [self._pos(f"P{i}") for i in range(8)]
        prices = {f"P{i}": 100.0 for i in range(8)}; prices.update({"NEW": 100.0, "SPY": 500.0})
        h = _Harness(monkeypatch, su, _picks(("NEW", "stocks", "short_term", 100.0, 95.0, 120.0, 5)),
                     _book(cash=2_000.0, positions=held), prices)
        acts = su.phase_open("900000001", dry=False)
        assert h.paper_buys == []
        assert any("HOLD NEW" in a and "max_positions" in a for a in acts)
        assert h.saved[-1]["held"][0]["why"].startswith("max_positions")

    def test_the_deployed_cap_refuses_a_buy_that_crosses_80pct(self, monkeypatch):
        su = _su()
        held = [self._pos("P1", px=100.0, sh=75)]                     # $7,500 of a $10k book
        h = _Harness(monkeypatch, su, _picks(("NEW", "stocks", "short_term", 100.0, 95.0, 120.0, 5)),
                     _book(cash=2_500.0, positions=held), {"P1": 100.0, "NEW": 100.0, "SPY": 500.0})
        acts = su.phase_open("900000001", dry=False)
        assert h.paper_buys == [] and any("deployed cap" in a for a in acts)

    def test_the_total_risk_cap_refuses_a_buy_that_crosses_5pct(self, monkeypatch):
        su = _su()
        held = [self._pos("P1", px=100.0, sh=30, stop=85.0)]          # $450 at risk = 4.5%
        h = _Harness(monkeypatch, su, _picks(("NEW", "stocks", "short_term", 100.0, 90.0, 120.0, 5)),
                     _book(cash=7_000.0, positions=held), {"P1": 100.0, "NEW": 100.0, "SPY": 500.0})
        acts = su.phase_open("900000001", dry=False)
        assert h.paper_buys == [] and any("risk cap" in a for a in acts)

    def test_a_rule_refusal_is_not_recorded_as_a_window_breach(self, monkeypatch):
        """Different facts: the pick was reachable, the TRADER had no room."""
        su = _su()
        held = [self._pos(f"P{i}") for i in range(8)]
        prices = {f"P{i}": 100.0 for i in range(8)}; prices.update({"NEW": 100.0, "SPY": 500.0})
        h = _Harness(monkeypatch, su, _picks(("NEW", "stocks", "short_term", 100.0, 95.0, 120.0, 5)),
                     _book(cash=2_000.0, positions=held), prices)
        su.phase_open("900000001", dry=False)
        assert "skipped" not in h.saved[-1] and h.saved[-1]["held"]

    def test_the_running_tally_judges_the_NEXT_candidate(self, monkeypatch):
        """Eight fresh picks on an empty book: the ninth must be refused by the
        count the run itself built up, not by a stale read."""
        su = _su()
        # conviction 1 → $250 each: 9 × $250 is 22% deployed and 1.1% at risk,
        # so the COUNT is the only rule that can refuse the ninth. (A first
        # version used $1,000 picks and passed with the tally removed, because
        # the deployed cap fired first — a guard that passes for the wrong
        # reason is not a guard.)
        rows = [(f"N{i}", "stocks", "short_term", 100.0, 95.0, 120.0, 1) for i in range(9)]
        prices = {f"N{i}": 100.0 for i in range(9)}; prices["SPY"] = 500.0
        h = _Harness(monkeypatch, su, _picks(*rows), _book(), prices)
        su.phase_open("900000001", dry=False)
        assert len(h.paper_buys) == 8
        assert h.saved[-1]["held"][0]["why"].startswith("max_positions")


class TestLongTermLevels:
    def test_an_lt_pick_with_no_stop_gets_the_invalidation_level(self):
        su = _su()
        s, t, src = su._levels_for(100.0, None, 130.0, lt=True)
        assert s == 85.0 and t == 130.0 and src == "invalidation"

    def test_an_lt_pick_missing_both_says_so(self):
        su = _su()
        s, t, src = su._levels_for(100.0, None, None, lt=True)
        assert s == 85.0 and t == 108.0 and src == "invalidation+target"

    def test_an_lt_pick_that_HAS_a_valid_stop_keeps_it(self):
        su = _su()
        assert su._levels_for(100.0, 92.0, 130.0, lt=True) == (92.0, 130.0, "pick")

    def test_short_term_behaviour_is_unchanged(self):
        su = _su()
        assert su._levels_for(100.0, None, None) == (95.0, 108.0, "both")
        assert su._levels_for(1177.74, 1290.0, 1540.0)[2] == "stop"

    def test_the_invalidation_pct_is_the_apps_own(self):
        su = _su()
        assert su._LT_INVALIDATION_PCT == _const(ROOT / "agent.py", "LT_INVALIDATION_PCT")

    def test_the_open_phase_passes_lt_to_the_levels(self):
        body = _fn_body("phase_open")
        assert body.count("lt=bool(u.get('lt'))") == 2, "both loops must level LT picks as LT"


class TestTimeStops:
    def test_horizons_match_the_evaluators(self):
        su = _su()
        assert su._ST_HORIZON_DAYS == _const(ROOT / "scripts" / "evaluate_picks.py", "_HORIZON_DAYS")
        assert su._LT_HORIZON_DAYS == _const(ROOT / "scripts" / "backtest_longterm.py", "LT_HORIZON_DAYS")

    def _drive(self, monkeypatch, su, paper_positions, real_open, state, prices, today):
        import market_data, trade_logger, paper_trader, sim_portfolio, config_manager
        import datetime as dt
        sells, closes = [], []
        monkeypatch.setattr(su, "_state", lambda cid: dict(state))
        monkeypatch.setattr(su, "_save_state", lambda cid, st: True)
        monkeypatch.setattr(config_manager, "et_today", lambda: dt.date.fromisoformat(today))
        monkeypatch.setattr(market_data, "get_live_price", lambda t: prices.get(t.upper()))
        monkeypatch.setattr(trade_logger, "load_user_trade_log", lambda cid: {"open": real_open})
        monkeypatch.setattr(trade_logger, "close_trade",
                            lambda t, cid, exit_price=None, outcome="manual": closes.append((t, outcome)))
        monkeypatch.setattr(paper_trader, "load_user_paper", lambda cid: {"positions": paper_positions})
        monkeypatch.setattr(paper_trader, "paper_sell",
                            lambda t, cid, shares=None, price=None, outcome=None: sells.append((t, outcome)) or "📄")
        monkeypatch.setattr(sim_portfolio, "update", lambda acct, fn: None)
        acts = su._manage_account("900000001", dry=False)
        return sells, closes, acts

    def _paper(self, t, opened):
        return {"ticker": t, "shares": 10, "avg_price": 100.0, "stop_loss": 90.0,
                "target_price": 120.0, "bought_date": opened}

    def test_a_short_term_paper_position_expires_at_30_days(self, monkeypatch):
        su = _su()
        sells, _, acts = self._drive(monkeypatch, su, [self._paper("A", "2026-08-20")], [],
                                     {"paper": ["A"], "real": []}, {"A": 100.0, "SPY": 500.0},
                                     today="2026-09-19")
        assert sells == [("A", "expired")] and any("time stop" in a for a in acts)

    def test_a_day_short_of_the_horizon_is_held(self, monkeypatch):
        su = _su()
        sells, _, _ = self._drive(monkeypatch, su, [self._paper("A", "2026-08-21")], [],
                                  {"paper": ["A"], "real": []}, {"A": 100.0}, today="2026-09-19")
        assert sells == []

    def test_a_long_term_position_gets_180_days_not_30(self, monkeypatch):
        su = _su()
        st = {"paper": ["A"], "real": [], "book": {"A": {"lt": True}}}
        sells, _, _ = self._drive(monkeypatch, su, [self._paper("A", "2026-08-01")], [], st,
                                  {"A": 100.0}, today="2026-09-19")
        assert sells == [], "an LT pick expiring at 30 days is the survivor-sample bug inverted"
        sells, _, _ = self._drive(monkeypatch, su, [self._paper("A", "2026-03-01")], [], st,
                                  {"A": 100.0, "SPY": 500.0}, today="2026-09-19")
        assert sells == [("A", "expired")]

    def test_target_and_stop_still_win_and_carry_their_reason(self, monkeypatch):
        su = _su()
        sells, _, _ = self._drive(monkeypatch, su, [self._paper("A", "2026-01-01"), self._paper("B", "2026-01-01")],
                                  [], {"paper": ["A", "B"], "real": []},
                                  {"A": 125.0, "B": 80.0, "SPY": 500.0}, today="2026-09-19")
        assert sorted(sells) == [("A", "target"), ("B", "stop")]

    def test_a_real_position_expires_too_with_outcome_expired(self, monkeypatch):
        su = _su()
        real = [{"ticker": "R", "stop_loss": 90.0, "target_price": 120.0,
                 "opened_date": "2026-08-01", "timeframe": "short_term"}]
        _, closes, _ = self._drive(monkeypatch, su, [], real, {"paper": [], "real": ["R"]},
                                   {"R": 100.0}, today="2026-09-19")
        assert closes == [("R", "expired")]

    def test_a_real_LONG_TERM_position_is_not_expired_at_30_days(self, monkeypatch):
        su = _su()
        real = [{"ticker": "R", "stop_loss": 90.0, "target_price": 120.0,
                 "opened_date": "2026-08-01", "timeframe": "long_term"}]
        _, closes, _ = self._drive(monkeypatch, su, [], real, {"paper": [], "real": ["R"]},
                                   {"R": 100.0}, today="2026-09-19")
        assert closes == []

    def test_an_undated_position_is_never_expired(self, monkeypatch):
        su = _su()
        p = self._paper("A", None)
        sells, _, _ = self._drive(monkeypatch, su, [p], [], {"paper": ["A"], "real": []},
                                  {"A": 100.0}, today="2026-09-19")
        assert sells == []

    def test_the_real_loop_records_the_timeframe_the_time_stop_reads(self):
        assert "timeframe_override=" in _fn_body("phase_open")


class TestPaperSellRecordsTheOutcome:
    def test_the_history_row_carries_the_reason(self, monkeypatch, _gist_store):
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 100.0)
        pt.paper_buy("AAA", 2, "900000001", price=100.0, stop_loss=90.0, target_price=120.0)
        pt.paper_sell("AAA", "900000001", price=95.0, outcome="expired")
        assert pt.load_user_paper("900000001")["history"][-1]["outcome"] == "expired"

    def test_an_unknown_reason_falls_back_to_manual(self, monkeypatch, _gist_store):
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 100.0)
        pt.paper_buy("BBB", 2, "900000001", price=100.0)
        pt.paper_sell("BBB", "900000001", price=95.0, outcome="felt like it")
        assert pt.load_user_paper("900000001")["history"][-1]["outcome"] == "manual"

    def test_a_human_sell_is_manual(self, monkeypatch, _gist_store):
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 100.0)
        pt.paper_buy("CCC", 2, "900000001", price=100.0)
        pt.paper_sell("CCC", "900000001", price=95.0)
        assert pt.load_user_paper("900000001")["history"][-1]["outcome"] == "manual"


class TestReset:
    def _drive(self, monkeypatch, su, positions, prices, history=()):
        import market_data, paper_trader, sim_portfolio
        sells, muts, docs = [], [], []
        monkeypatch.setattr(market_data, "get_live_price", lambda t: prices.get(t.upper()))
        monkeypatch.setattr(paper_trader, "load_user_paper",
                            lambda cid: {"positions": positions, "history": list(history), "cash": 1.0})
        monkeypatch.setattr(paper_trader, "paper_sell",
                            lambda t, cid, shares=None, price=None, outcome=None: sells.append((t, outcome)) or "📄")
        monkeypatch.setattr(paper_trader, "_mutate_paper",
                            lambda cid, m: (muts.append(m), m({"positions": [], "history": [], "cash": 1.0,
                                                               "starting_cash": 5.0})[1])[1])
        monkeypatch.setattr(sim_portfolio, "update", lambda acct, fn: docs.append(fn(None)))
        monkeypatch.setattr(su, "_state", lambda cid: {"paper": ["A", "B"], "book": {"A": {}}})
        monkeypatch.setattr(su, "_save_state", lambda cid, st: True)
        return sells, muts, docs, su.phase_reset("900000001", dry=False)

    def test_it_liquidates_at_market_with_its_own_outcome_and_starts_the_curve(self, monkeypatch):
        su = _su()
        pos = [{"ticker": "A", "shares": 3, "avg_price": 50.0}, {"ticker": "B", "shares": 1, "avg_price": 20.0}]
        sells, muts, docs, acts = self._drive(monkeypatch, su, pos, {"A": 55.0, "B": 18.0}, history=[{"x": 1}])
        assert sorted(sells) == [("A", "liquidated"), ("B", "liquidated")]
        d = muts[0]({"positions": [], "history": [{"x": 1}], "cash": 9.0, "starting_cash": 5.0})[0]
        assert d["cash"] == su._BOOK_START_USD and d["starting_cash"] == su._BOOK_START_USD
        assert d["history"] == [{"x": 1}], "history is KEPT — reachability depends on it"
        assert docs[-1]["starting_equity"] == su._BOOK_START_USD
        assert any("history kept (1 closed rows)" in a for a in acts)

    def test_it_REFUSES_when_a_position_cannot_be_priced(self, monkeypatch):
        su = _su()
        pos = [{"ticker": "A", "shares": 3, "avg_price": 50.0}, {"ticker": "B", "shares": 1, "avg_price": 20.0}]
        sells, muts, docs, acts = self._drive(monkeypatch, su, pos, {"A": 55.0})
        assert sells == [] and muts == [] and docs == []
        assert any("REFUSED" in a for a in acts)

    def test_the_cash_is_not_zeroed_over_a_position_that_reappeared(self, monkeypatch):
        su = _su()
        _, muts, _, _ = self._drive(monkeypatch, su, [], {})
        d, res = muts[0]({"positions": [{"ticker": "GHOST"}], "cash": 7.0, "starting_cash": 5.0})
        assert res["left"] == 1 and d["cash"] == 7.0


class TestTheAdminSurface:
    def _admin(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client

    def test_the_book_file_is_prefetched(self):
        import webhook
        from config_manager import SIM_PORTFOLIO_FILE
        assert SIM_PORTFOLIO_FILE in webhook._ADMIN_PREFETCH

    def test_admin_data_carries_the_book(self, client):
        d = self._admin(client).get("/admin/data").get_json()
        assert "sim_portfolio" in d and d["sim_portfolio"]["active"] is False

    def test_the_page_renders_the_section_beside_actionability(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        assert "function bookSection" in html and 'id="book-card"' in html
        assert html.index("bookSection(d.sim_portfolio)") < html.index("actionSection(d.actionability)")

    def test_a_live_book_renders_its_numbers(self, client, _gist_store):
        import sim_portfolio as sp
        sp.update("900000001", lambda d: sp.snapshot(
            sp.record_buy(sp.new_doc("900000001", "2026-09-22", 10_000.0), "X", 5_000.0, 500.0, "2026-09-22"),
            "2026-09-23", 10_800.0, 550.0, 1))
        d = self._admin(client).get("/admin/data").get_json()["sim_portfolio"]
        assert d["active"] and d["alpha_pct"] == 3.0 and d["curve"]

    def test_the_engine_report_carries_it_as_a_METRIC_never_a_finding(self, _gist_store):
        import sim_portfolio as sp
        spec = importlib.util.spec_from_file_location("ae", ROOT / "scripts" / "analyze_engine.py")
        ae = importlib.util.module_from_spec(spec); spec.loader.exec_module(ae)
        assert ae._book_vs_twin() == []                                  # no book yet: nothing
        sp.update("900000001", lambda d: sp.snapshot(
            sp.new_doc("900000001", "2026-09-22", 10_000.0), "2026-09-23", 10_100.0, 500.0, 0))
        items = ae._book_vs_twin()
        assert len(items) == 1 and items[0].kind == "metric" and items[0].tier == "MEASURE"
        assert "+1.00%" in items[0].evidence
