"""Small fixes from the 2026-11-15 "quiet corners" sweep. Each was invisible:
nothing failed, a feature just did less than it claimed."""
import ast
import datetime as dt
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ev = _load("ev_qc", "scripts/evaluate_picks.py")


def _scored(band_score):
    return {"date": "2026-09-01", "ticker": "T", "asset": "stock", "timeframe": "short_term",
            "conviction": 3, "outcome": "target", "ret_pct": 5.0, "alpha_pct": 1.0,
            "mtm_pct": 5.0, "mtm_alpha_pct": 1.0, "screen": {"score": band_score,
                                                             "setup_type": "pullback"}}


class TestTheWeeklyReportIsValidTelegramHtml:
    """The "<70" score band was read as an HTML tag; Telegram rejected the whole
    report and every weekly one arrived as plain text with raw <b> tags."""

    def test_the_low_score_band_is_escaped(self):
        rep = ev.build_report([_scored(40), _scored(99)])
        assert "&lt;70" in rep
        assert "  <70:" not in rep

    def test_the_report_has_no_stray_angle_brackets(self):
        import re
        rep = ev.build_report([_scored(40), _scored(99)])
        stray = re.findall(r"<(?!/?(?:b|i)>)", rep)
        assert stray == [], f"unescaped '<' would break Telegram HTML: {stray[:3]}"


class TestUnpricedPicksAreReported:
    def _row(self, days_ago, ticker="WBD"):
        d = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
        return {"date": d, "ticker": ticker, "entry": 10.0}

    def test_maturity_is_judged_from_the_pick_date(self):
        assert ev._matured(self._row(ev._HORIZON_DAYS + 1))
        assert not ev._matured(self._row(ev._HORIZON_DAYS - 1))
        assert not ev._matured({"ticker": "X"}) and not ev._matured({"date": "junk"})

    def test_the_note_counts_and_names_them(self):
        note = ev._unpriced_note([self._row(40), self._row(41), self._row(40, "KVUE")])
        assert "3 matured pick(s) could not be priced" in note
        assert "WBD ×2" in note and "KVUE" in note

    def test_nothing_to_report_adds_nothing(self):
        assert ev._unpriced_note([]) == ""

    def test_the_main_loop_appends_the_note_to_the_report(self):
        src = open(os.path.join(ROOT, "scripts", "evaluate_picks.py"), encoding="utf-8").read()
        assert "build_report(scored) + _unpriced_note(unpriced)" in src


class TestTheDollarIndexSymbol:
    def test_markets_asks_yahoo_for_a_symbol_that_exists(self):
        import webhook
        syms = {s for s, _cat, _label in webhook._MARKETS_INSTRUMENTS}
        assert "DX-Y.NYB" in syms and "^DXY" not in syms
        labels = {lbl for s, _c, lbl in webhook._MARKETS_INSTRUMENTS if s == "DX-Y.NYB"}
        assert labels == {"DXY"}, "the mini-app keys the row on the DXY label"


class TestTheSweepDrivesRealCommands:
    def test_no_bare_paper_command(self):
        """There is no /paper command; it fell through to the natural-language
        handler (a Claude call per run) and tested nothing."""
        src = open(os.path.join(ROOT, "scripts", "full_sweep.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "sweep_bot_commands")
        cmds = [e.value for n in ast.walk(fn) if isinstance(n, ast.List)
                for e in n.elts if isinstance(e, ast.Constant)]
        assert "/paper" not in cmds and "PAPER PORTFOLIO" in cmds
