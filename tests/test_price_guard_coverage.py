"""Every consumer of a live price that ACTS on it must gate on plausible_price.

🔴 Why this file exists. CLAUDE.md declared this bug class "NOW FULLY CLOSED
(Jul 6)". On Aug 19 four live sites were found — _check_take_profit_nudge,
_check_trailing_stops, _check_picks_stop_loss, _check_portfolio_drawdown — each
on an alert that tells a user to act. One fired "Current price $0.01 has
breached today's suggested stop (down 100.0%)", the literal symptom the class is
named for, months after it was called closed.

The Jul 6 sweep fixed the sites it could remember. It never enumerated them.
This scan enumerates them, so the claim is enforced rather than asserted.

A site is IN SCOPE when it both reads a live price and compares that price to a
stop / target / entry. Display-only and aggregate-only readers are out of scope
and listed in REVIEWED below with the reason each is safe.
"""
import ast
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Sites read and judged safe. Each entry needs a REASON, not just a name —
# an unexplained allowlist is how a class re-opens quietly.
REVIEWED = {
    "_check_stale_positions":
        "Prompts 'still convicted?' after 21 days. Compares to target but "
        "recommends no action and quotes no live figure a user trades on.",
    "_check_cash_position":
        "Sums position values against configured capital. No per-position "
        "stop/target comparison; one bad leg cannot invert the decision.",
    "run_eod_summary":
        "Delegates every price decision to format_eod_full_summary and the "
        "_check_* helpers, which carry their own guards.",
    "_eod_msg":
        "The run_eod_summary builder, same delegation.",
    "_target_approach_msgs":
        "run_price_alerts' builder — gated by the enclosing function.",
    "check_and_close_trades":
        "UNREACHABLE: its only production caller, _broadcast_trade_closes, "
        "returns on its second line. 🔴 Gate it BEFORE reviving auto-close — a "
        "garbage tick would book a fabricated loss no later run can undo.",
    "_mut":
        "Inner mutator of check_and_close_trades; same reachability.",
    "_execute_bought":
        "Logs a position the USER states they bought; the price is their fill, "
        "not a feed quote driving an alert.",
    "format_confirmation_message":
        "Renders stored pick levels; the live price it shows leads no P&L "
        "figure and triggers nothing.",
    "miniapp_positions":
        "Display path. Shows P&L but takes no action and sends no alert.",
    "miniapp_paper":
        "Paper portfolio display path. Shows simulated P&L only; no real money "
        "moves and no alert is triggered from these figures.",
    "miniapp_live_prices":
        "Returns raw quotes for the frontend to render.",
}

ACTION_MARKERS = ("stop_loss", "target_price", "entry_price")
PRICE_READS = ("current_prices.get", "get_live_price", "_download_prices",
               "get_live_prices")


def _sites():
    for path in sorted(ROOT.glob("*.py")):
        src = path.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if not any(m in body for m in ACTION_MARKERS):
                continue
            if not any(r in body for r in PRICE_READS):
                continue
            yield path.name, node.name, node.lineno, "plausible_price" in body


class TestEveryActingPriceConsumerIsGuarded:
    def test_no_unguarded_site_outside_the_reviewed_list(self):
        offenders = [
            f"{f}:{ln} {fn}()"
            for f, fn, ln, guarded in _sites()
            if not guarded and fn not in REVIEWED
        ]
        assert not offenders, (
            "these compare a live price to a stop/target/entry without "
            "plausible_price — a $0.01 holiday tick fires a false alert:\n  "
            + "\n  ".join(offenders)
            + "\n\nEither gate on plausible_price(price, reference), or add the "
              "name to REVIEWED with the reason it is safe."
        )

    def test_the_scan_can_actually_detect_an_offender(self, tmp_path, monkeypatch):
        """A guard that cannot fail is not a guard."""
        bad = tmp_path / "leaky.py"
        bad.write_text(
            "def nudge(current_prices, trade):\n"
            "    current = current_prices.get(trade['ticker'])\n"
            "    if current and current > 0 and current < trade['stop_loss']:\n"
            "        send('stop hit')\n"
        )
        monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
        found = [fn for _, fn, _, guarded in _sites() if not guarded]
        assert "nudge" in found, "the scan would not notice a new unguarded site"

    def test_the_four_sites_fixed_on_aug_19_stay_guarded(self):
        """Named explicitly — these are the ones a 'fully closed' note missed."""
        guarded = {fn for _, fn, _, g in _sites() if g}
        for fn in ("_check_take_profit_nudge", "_check_trailing_stops",
                   "_check_picks_stop_loss", "_check_portfolio_drawdown",
                   "_check_hold_or_fold", "_check_portfolio_health"):
            assert fn in guarded, f"{fn} lost its plausible_price gate"

    @pytest.mark.parametrize("name", sorted(REVIEWED))
    def test_every_reviewed_entry_carries_a_real_reason(self, name):
        assert len(REVIEWED[name]) > 40, \
            f"{name} is allowlisted without a real explanation"

    def test_the_reviewed_list_has_no_stale_entries(self):
        """An allowlist that outlives its sites hides the next offender."""
        live = {fn for _, fn, _, _ in _sites()}
        stale = sorted(set(REVIEWED) - live)
        assert not stale, f"REVIEWED names sites that no longer qualify: {stale}"
