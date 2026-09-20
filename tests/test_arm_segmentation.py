"""Phase 2: arm rows must never reach the headline, and the standings must be
hard to win.

🔴 THE BUG THIS CLOSES. `_key` tagged rows with their arm and `record_picks`
wrote the tag, and BOTH docstrings said the report "segments them away from the
headline exactly like controls". `build_report` had zero arm references. It was
dormant only because the arms workflow has never been scheduled — so the first
scheduled arm run would have silently folded every experimental variant into
the one number the product is judged on. A comment asserting a property that no
code checks, again.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ev():
    spec = importlib.util.spec_from_file_location("ev_seg", ROOT / "scripts" / "evaluate_picks.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _row(ticker, date, ret, arm=None, control=False, spy=0.0):
    r = {"date": date, "ticker": ticker, "asset": "stock", "timeframe": "short_term",
         "entry": 100.0, "target": 120.0, "stop": 90.0, "conviction": 3,
         "outcome": "open", "exit": 100.0 + ret, "ret_pct": ret, "mtm_pct": ret,
         "mtm_alpha_pct": ret - spy, "spy_pct": spy, "alpha_pct": ret - spy}
    if arm:
        r["arm"] = arm
    if control:
        r["control"] = True
    return r


def _days(n, start=1):
    return [f"2026-0{1 + (d // 28)}-{(d % 28) + 1:02d}" for d in range(start, start + n)]


class TestArmRowsNeverReachTheHeadline:
    def test_the_headline_counts_production_picks_only(self):
        ev = _ev()
        prod = [_row(f"P{i}", d, +5.0) for i, d in enumerate(_days(4))]
        arm = [_row(f"A{i}", d, -50.0, arm="breakout") for i, d in enumerate(_days(4))]
        rep = ev.build_report(prod + arm)
        assert "4 matured picks" in rep, "the arm's 4 rows must not inflate n"
        assert "win 100.0%" in rep, "a losing arm must not drag the headline down"

    def test_an_arm_cannot_change_the_win_rate(self):
        ev = _ev()
        prod = [_row(f"P{i}", d, +5.0) for i, d in enumerate(_days(6))]
        alone = ev.build_report(list(prod))
        with_arm = ev.build_report(prod + [_row(f"A{i}", d, -9.0, arm="pullback")
                                           for i, d in enumerate(_days(6))])
        head = lambda t: t.split("<b>Overall</b>")[1].split("\n")[0]
        assert head(alone) == head(with_arm)

    def test_an_arm_never_enters_the_picked_vs_runners_up_comparison(self):
        ev = _ev()
        prod = [_row(f"P{i}", d, +5.0) for i, d in enumerate(_days(3))]
        ctl = [_row(f"C{i}", d, +1.0, control=True) for i, d in enumerate(_days(3))]
        arm = [_row(f"A{i}", d, -80.0, arm="breakout") for i, d in enumerate(_days(3))]
        rep = ev.build_report(prod + ctl + arm)
        block = rep.split("Picked vs the runners-up")[1]
        assert "picked:     n=3" in block and "not picked: n=3" in block

    def test_an_arm_never_enters_a_slice(self):
        ev = _ev()
        prod = [_row("P1", "2026-01-02", +5.0)]
        arm = [_row(f"A{i}", d, -5.0, arm="breakout") for i, d in enumerate(_days(5))]
        rep = ev.build_report(prod + arm)
        conv = rep.split("<b>By conviction</b>")[1].split("\n")[1]
        assert "n=1" in conv, f"slice absorbed arm rows: {conv}"

    def test_an_ARM_ONLY_ledger_still_reports_the_standings(self):
        """Returning 'nothing matured' while arm rows sit scored is the same
        class of lie as a green monitor that could not run."""
        ev = _ev()
        rep = ev.build_report([_row(f"A{i}", d, +5.0, arm="breakout")
                               for i, d in enumerate(_days(3))])
        assert "No PRODUCTION picks have matured" in rep
        assert "Tournament standings" in rep and "breakout" in rep


class TestTheStandingsAreHardToWin:
    def _pair(self, n_live_win, n_live, n_arm_win, n_arm):
        """Disjoint tickers, so live and arm disagree on every pick."""
        live = [_row(f"L{i}", d, +5.0 if i < n_live_win else -5.0)
                for i, d in enumerate(_days(n_live))]
        arm = [_row(f"A{i}", d, +5.0 if i < n_arm_win else -5.0, arm="breakout")
               for i, d in enumerate(_days(n_arm))]
        return live, arm

    def test_it_renders_nothing_when_no_arm_has_run(self):
        """The report is already close to Telegram's 4096 ceiling — today this
        section must cost zero characters."""
        ev = _ev()
        rep = ev.build_report([_row(f"P{i}", d, +5.0) for i, d in enumerate(_days(3))])
        assert "Tournament standings" not in rep

    def test_total_agreement_reports_no_measurable_difference(self):
        """Shared picks have identical outcomes by construction; counting them
        would manufacture 'no difference' however far apart the arms are."""
        ev = _ev()
        live = [_row(f"S{i}", d, +5.0) for i, d in enumerate(_days(40))]
        arm = [_row(f"S{i}", d, +5.0, arm="breakout") for i, d in enumerate(_days(40))]
        rep = ev.build_report(live + arm)
        assert "no disagreement to measure" in rep
        assert "beats" not in rep.split("Tournament standings")[1]

    def test_a_small_sample_is_never_a_winner_however_lopsided(self):
        ev = _ev()
        live, arm = self._pair(0, 12, 12, 12)          # arm 12-for-12, live 0-for-12
        rep = ev.build_report(live + arm)
        block = rep.split("Tournament standings")[1]
        assert "NOT a finding" in block and "beats" not in block

    def test_a_large_decisive_gap_IS_called(self):
        ev = _ev()
        live, arm = self._pair(10, 60, 50, 60)
        block = _ev().build_report(live + arm).split("Tournament standings")[1]
        assert "🟢 <b>beats</b>" in block

    def test_a_large_but_inconclusive_gap_is_NOT_called(self):
        ev = _ev()
        live, arm = self._pair(30, 60, 36, 60)         # 50% vs 60%, CI spans zero
        block = ev.build_report(live + arm).split("Tournament standings")[1]
        assert "interval includes zero" in block and "beats" not in block

    def test_a_losing_arm_is_reported_as_losing(self):
        ev = _ev()
        live, arm = self._pair(50, 60, 10, 60)
        block = _ev().build_report(live + arm).split("Tournament standings")[1]
        assert "🔴 <b>loses to</b>" in block

    def test_the_overlap_is_reported_because_it_decides_what_the_test_is_worth(self):
        ev = _ev()
        shared = [_row(f"S{i}", d, +5.0) for i, d in enumerate(_days(30))]
        live = shared + [_row(f"L{i}", d, +5.0) for i, d in enumerate(_days(30, start=60))]
        arm = ([_row(f"S{i}", d, +5.0, arm="breakout") for i, d in enumerate(_days(30))]
               + [_row(f"A{i}", d, -5.0, arm="breakout") for i, d in enumerate(_days(30, start=120))])
        block = ev.build_report(live + arm).split("Tournament standings")[1]
        assert "50.0% overlap" in block

    def test_every_arm_gets_a_row_with_its_own_n_and_ci(self):
        ev = _ev()
        rows = ([_row(f"P{i}", d, +5.0) for i, d in enumerate(_days(5))]
                + [_row(f"B{i}", d, +5.0, arm="breakout") for i, d in enumerate(_days(3))]
                + [_row(f"U{i}", d, -5.0, arm="pullback") for i, d in enumerate(_days(4))])
        block = ev.build_report(rows).split("Tournament standings")[1]
        assert "<b>live</b>: n=5" in block
        assert "<b>breakout</b>: n=3" in block
        assert "<b>pullback</b>: n=4" in block


class TestHeadToHeadMechanics:
    def test_only_disagreeing_rows_are_scored(self):
        ev = _ev()
        live = [_row("SAME", "2026-01-02", +5.0), _row("LONLY", "2026-01-03", -5.0)]
        arm = [_row("SAME", "2026-01-02", +5.0, arm="x"),
               _row("AONLY", "2026-01-04", +5.0, arm="x")]
        h = ev._head_to_head(live, arm)
        assert h["shared"] == 1 and h["n_live"] == 1 and h["n_arm"] == 1

    def test_a_shared_ticker_on_a_DIFFERENT_day_is_a_disagreement(self):
        """The join key is (date, ticker): buying the same name a week later is
        a different decision, not the same one."""
        ev = _ev()
        live = [_row("AAA", "2026-01-02", +5.0)]
        arm = [_row("AAA", "2026-01-09", -5.0, arm="x")]
        assert ev._head_to_head(live, arm)["shared"] == 0

    def test_production_rows_are_keyed_as_live(self):
        ev = _ev()
        assert ev._arm_of({"ticker": "A"}) == ev.LIVE_ARM
        assert ev._arm_of({"ticker": "A", "arm": "breakout"}) == "breakout"

    def test_overlap_of_an_empty_arm_is_none_not_zero(self):
        """0% overlap would read as 'perfectly independent'; the truth is that
        there is nothing to compare."""
        ev = _ev()
        assert ev._overlap_pct([], [_row("A", "2026-01-02", 1.0)]) is None


class TestTheCIMathHasOneDefinition:
    def test_all_three_modules_delegate_to_stats_ci(self):
        """Three copies of the arithmetic that decides whether a result is real
        is how they drift — and the tournament promotes a strategy on it."""
        import ast
        for rel, fn in (("scripts/evaluate_picks.py", "_wilson"),
                        ("scripts/backtest_compare.py", "_wilson"),
                        ("scripts/backtest_compare.py", "_diff_ci"),
                        ("scripts/backtest_walkforward.py", "wilson")):
            src = (ROOT / rel).read_text()
            node = next(n for n in ast.walk(ast.parse(src))
                        if isinstance(n, ast.FunctionDef) and n.name == fn)
            body = ast.unparse(node)
            assert "stats_ci" in body, f"{rel}:{fn} no longer uses the one definition"
            assert "1.96" not in body, f"{rel}:{fn} re-implements the interval"

    def test_wald_would_certify_a_perfect_arm_and_wilson_does_not(self):
        """The failure mode the method is chosen for: at p=1 Wald's variance
        term collapses to a zero-width interval."""
        from stats_ci import diff_ci, wilson
        _, lo, hi = diff_ci(12, 12, 6, 12)
        assert hi - lo > 0.3, "a 12-observation arm must not get a tight interval"
        _, wlo, whi = wilson(12, 12)
        assert wlo < 1.0, "a perfect record must not have a zero-width interval"

    def test_an_empty_sample_is_the_whole_interval_not_a_confident_zero(self):
        from stats_ci import wilson
        assert wilson(0, 0) == (0.0, 0.0, 1.0)

    def test_percentages_are_not_multiplied_twice(self):
        """A run once printed CI[4530.0-6540.0]. A garbled interval is the worst
        number to get wrong — it is the one that decides the rest."""
        from stats_ci import wilson_pct
        lo, hi = wilson_pct(45, 90)
        assert 0.0 <= lo <= hi <= 100.0

    def test_the_aggregate_never_asks_for_an_empty_interval(self):
        """Routing changed the n=0 return; both callers guard above it, and this
        pins that they still do."""
        ev = _ev()
        assert ev._agg([]) == {"n": 0}

    def test_excludes_zero_is_direction_agnostic(self):
        from stats_ci import excludes_zero
        assert excludes_zero(0.1, 0.4) and excludes_zero(-0.4, -0.1)
        assert not excludes_zero(-0.1, 0.4) and not excludes_zero(0.0, 0.4)
