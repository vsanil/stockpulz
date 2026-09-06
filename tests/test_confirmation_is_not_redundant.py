"""🔴 A NEAR-MISS, 2026-09-06. An architecture review read `run_confirmation`'s
one-line docstring — "Load morning picks, fetch live prices, send comparison
message" — concluded the mode duplicated `digest`, and recommended deleting it.

It is 177 lines running FOUR independent fan-outs. Three of them have nothing to
do with picks. Deleting the mode would have silently removed trailing-stop
alerts, earnings warnings and watchlist entry signals, and the only symptom
would have been an ABSENCE — the failure shape this whole project keeps hitting.

🔑 These tests exist because the next reader (human, or a self-heal agent asked
to "simplify") will face the same temptation. The docstring is fixed, but a
docstring is not enforcement.
"""
import ast
import pathlib

import pytest

SRC = pathlib.Path("agent.py").read_text()
TREE = ast.parse(SRC)


def _fn(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} is gone — if that was deliberate, this test must be too")


def _fanout_tags(fn):
    """Every `_fanout(..., tag="X")` inside the function."""
    return {kw.value.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_fanout"
            for kw in node.keywords
            if kw.arg == "tag" and isinstance(kw.value, ast.Constant)}


class TestConfirmationCarriesFourFeatures:
    def test_all_four_fanouts_are_present(self):
        assert _fanout_tags(_fn("run_confirmation")) == {
            "trailing_stops", "earnings_warning", "watchlist_signal", "confirmation"}, (
            "run_confirmation carries FOUR user-facing features. If you are removing "
            "one, move it somewhere else first — do not let it vanish with the mode.")

    @pytest.mark.parametrize("tag", ["trailing_stops", "earnings_warning", "watchlist_signal"])
    def test_the_three_non_pick_features_are_not_about_picks(self, tag):
        """These are why 'confirmation duplicates digest' is false."""
        assert tag in _fanout_tags(_fn("run_confirmation"))

    def test_the_docstring_states_more_than_one_job(self):
        """🔑 The actual defect. A docstring naming 1 of 4 jobs makes a feature
        deletable by accident — which is exactly what nearly happened."""
        doc = ast.get_docstring(_fn("run_confirmation")) or ""
        for tag in ("trailing_stops", "earnings_warning", "watchlist_signal"):
            assert tag in doc, f"docstring must name {tag}; it is not obvious from the code"


class TestDigestIsADifferentSubject:
    def test_digest_reads_positions_and_confirmation_reads_picks(self):
        conf = ast.unparse(_fn("run_confirmation"))
        dig = ast.unparse(_fn("run_digest"))
        assert "load_picks()" in conf and "load_picks()" not in dig, \
            "confirmation is about the day's PICKS"
        assert "load_user_trade_log" in dig, "digest is about the user's OWN POSITIONS"


class TestTheSecondNewsCheckCannotDuplicateTheFirst:
    """🔴 The other half of the same near-miss: `news_check` runs at 08:30 and
    13:00 ET, and the review called the second run redundant. It is not — stories
    are deduped per (symbol, title, DAY), so the afternoon run can only deliver
    news that did not exist in the morning. Removing it drops real alerts."""

    def test_the_dedup_key_is_scoped_to_the_day_not_the_run(self):
        body = ast.unparse(_fn("run_news_check"))
        assert "et_today()" in body, \
            "the dedup key must include the DAY, or the 13:00 run resends the 08:30 stories"
        assert "_is_alerted" in body and "_mark_alerted" in body
