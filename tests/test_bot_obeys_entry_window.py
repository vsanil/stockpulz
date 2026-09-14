"""The bot must not buy a pick the morning message told users to SKIP.

🔴 The defect (Sep 13, n=98): 10 of 98 fills breached the published entry
window, up to 11.22% (DOT 2026-09-08) and 8.98% (NVDA 2026-08-27). The message
promises "enter within X% — skip if above $Y", so a user who OBEYED would have
skipped picks the bot itself bought. A trust defect, not a performance one.

The finding's own suggested fix — widen the window — is WRONG for these
magnitudes: an 11% overnight crypto move and an earnings gap are real, so
widening would legitimise the bad fill. The bot obeys the rule instead.
"""
import importlib.util, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _su():
    spec = importlib.util.spec_from_file_location("su_t", ROOT / "scripts" / "synthetic_user.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class TestTheWindowCheck:
    def test_a_fill_above_the_window_is_a_breach(self):
        su = _su()
        # short-term stock: 2% window. 100 -> 105 is +5%.
        assert su._entry_breach(105.0, {"entry": 100.0, "atype": "stock", "lt": False}) == 5.0

    def test_a_fill_inside_the_window_is_not(self):
        su = _su()
        assert su._entry_breach(101.0, {"entry": 100.0, "atype": "stock", "lt": False}) is None

    def test_a_CHEAPER_fill_is_never_a_breach(self):
        """Filling below entry is a BETTER fill — only a positive excess counts."""
        su = _su()
        assert su._entry_breach(80.0, {"entry": 100.0, "atype": "stock", "lt": False}) is None

    def test_crypto_and_long_term_get_the_WIDER_window(self):
        """entry_window_pct gives LT and crypto 3%, short-term stock 2%.
        A 2.5% move breaches one and not the other — pinning the real split."""
        su = _su()
        assert su._entry_breach(102.5, {"entry": 100.0, "atype": "stock",  "lt": False}) == 2.5
        assert su._entry_breach(102.5, {"entry": 100.0, "atype": "crypto", "lt": False}) is None
        assert su._entry_breach(102.5, {"entry": 100.0, "atype": "stock",  "lt": True})  is None

    def test_it_imports_the_ONE_definition_rather_than_hardcoding(self):
        """Three copies of this number is what let a pick breach its own window
        while the gap check stayed silent."""
        import ast
        src = (ROOT / "scripts" / "synthetic_user.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_entry_breach")
        body = ast.unparse(fn)
        assert "entry_window_pct" in body
        for lit in ("2.0", "3.0"):
            assert lit not in body, f"re-hardcoded {lit} instead of importing the window"

    def test_an_unusable_entry_is_NOT_a_violation(self):
        """Unmeasurable is never a breach — same stance as a stop with no ATR.
        Returning a breach here would silently stop the bot buying, and a
        starved bot is indistinguishable from a quiet market."""
        su = _su()
        for bad in (None, 0, -5, "abc", float("nan")):
            assert su._entry_breach(105.0, {"entry": bad, "atype": "stock", "lt": False}) is None

    def test_a_garbage_price_is_not_a_breach_either(self):
        su = _su()
        for bad in (None, 0, -1, float("nan"), float("inf")):
            assert su._entry_breach(bad, {"entry": 100.0, "atype": "stock", "lt": False}) is None

    def test_the_universe_records_the_timeframe(self):
        """Without `lt` every pick would be judged on the short-term window,
        so long-term picks would be skipped 1 point too eagerly."""
        su = _su()
        uni = su._universe({"stocks": {"short_term": [{"ticker": "A", "entry_price": 1}],
                                       "long_term":  [{"ticker": "B", "entry_price": 1}]}})
        assert {u["t"]: u["lt"] for u in uni} == {"A": False, "B": True}


class TestASkipIsRecordedNotJustAvoided:
    """🔴 The half that is easy to miss. `actionability` measures reachability
    from the bot's FILLS, so simply not buying a breached pick makes the breach
    rate fall toward 0% while nothing improved — the flattering-direction
    failure that a closed position once used to erase the worst breach on
    record (COHR, 15% -> 8.7%)."""

    def test_both_buy_loops_record_the_skip(self):
        import ast
        src = (ROOT / "scripts" / "synthetic_user.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "phase_open")
        body = ast.unparse(fn)
        assert body.count("_skips.append") == 2, \
            "each buy loop must record its skip, or that observation vanishes"
        assert body.count("_entry_breach") == 2

    def test_the_skip_is_persisted_to_state(self):
        import ast
        src = (ROOT / "scripts" / "synthetic_user.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "phase_open")
        assert "'skipped'" in ast.unparse(fn) or '"skipped"' in ast.unparse(fn)

    def test_the_skip_is_dated_on_the_SAME_clock_actionability_joins_on(self):
        """actionability joins by DATE. A UTC stamp rolls over at 7-8 PM ET and
        would join the skip to the wrong day — the clock class, six occurrences."""
        import ast
        src = (ROOT / "scripts" / "synthetic_user.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "phase_open")
        body = ast.unparse(fn)
        assert "et_today()" in body
        assert "date.today()" not in body


class TestActionabilityCountsSkips:
    def _led(self):
        return [{"date": "2026-09-08", "ticker": "DOT", "entry": 100.0,
                 "timeframe": "short_term"}]

    def test_a_recorded_skip_counts_as_a_breach_observation(self):
        from actionability import analyse
        out = analyse(self._led(), [], [],
                      [{"t": "DOT", "date": "2026-09-08", "entry": 100.0,
                        "would_pay": 111.22, "slippage_pct": 11.22}])
        e = out["entry"]
        assert e["n"] == 1 and e["outside_window"] == 1, \
            "a skipped breach must stay in the denominator"

    def test_without_the_skip_the_metric_goes_QUIET(self):
        """The regression this exists to prevent: obeying the rule must not
        make the breach rate look better."""
        from actionability import analyse
        assert analyse(self._led(), [], [], [])["entry"]["n"] == 0

    def test_a_skip_is_not_reported_as_a_fill(self):
        """It must never look like money was spent — `fill` is None."""
        from actionability import entry_slippage
        rows, _ = entry_slippage(
            {("2026-09-08", "DOT"): {"entry": 100.0, "timeframe": "short_term"}}, [],
            [{"t": "DOT", "date": "2026-09-08", "entry": 100.0,
              "would_pay": 111.22, "slippage_pct": 11.22}])
        assert rows[0]["fill"] is None and rows[0]["skipped"] is True
        assert rows[0]["would_pay"] == 111.22

    def test_a_fill_and_a_skip_for_the_same_pick_count_ONCE(self):
        """Reachability is a property of the PICK. The real loop may skip while
        a prior fill exists; double-counting would skew the rate."""
        from actionability import entry_slippage
        led = {("2026-09-08", "DOT"): {"entry": 100.0, "timeframe": "short_term"}}
        rows, _ = entry_slippage(
            led, [{"ticker": "DOT", "opened_date": "2026-09-08", "entry_price": 111.22}],
            [{"t": "DOT", "date": "2026-09-08", "entry": 100.0,
              "would_pay": 111.22, "slippage_pct": 11.22}])
        assert len(rows) == 1

    def test_skips_are_optional_so_nothing_changes_without_them(self):
        from actionability import analyse, entry_slippage
        assert analyse([], [], [])["entry"]["n"] == 0
        assert entry_slippage({}, [])[0] == []
