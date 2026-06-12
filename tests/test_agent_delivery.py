"""
test_agent_delivery.py — Tests for the morning delivery path in agent.py.

These tests exist specifically to catch the class of bug that caused
production silence on 2026-05-26: _send_morning_personalised() referencing
variables from the caller's scope (is_weekend, is_holiday, etc.) that were
never passed as parameters, causing NameError for every user.

Patch map (verified against agent.py imports):
  agent.get_user_config          — top-level import from config_manager
  agent.load_user_trade_log      — top-level import from config_manager
  config_manager.get_allowed_users — lazy import inside _all_recipients()
  agent.personalize_picks_batch  — top-level import from ai_analyzer
  agent.format_daily_message     — top-level import from formatters
  agent.build_picks_keyboard     — top-level import from formatters
  agent.broadcast_all            — top-level import from telegram_api
  agent.compute_pick_streaks     — defined in agent
  agent.update_user_config       — NOT imported; called via config_manager
"""

import os
import sys
import inspect
import pytest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TEST_UID = "9999999"

# ── Minimal picks fixture ─────────────────────────────────────────────────────
SAMPLE_PICKS = {
    "stocks": {
        "short_term": [
            {"ticker": "AAPL", "entry_price": 189.50, "stop_loss": 182.0,
             "target_price": 200.0, "conviction": "high",
             "thesis": "Breakout above resistance.", "timeframe": "ST",
             "catalyst": "Earnings beat expected."},
        ],
        "long_term": [
            {"ticker": "NVDA", "entry_price": 875.0, "stop_loss": 840.0,
             "target_price": 950.0, "conviction": "medium",
             "thesis": "AI demand tailwinds.", "timeframe": "LT",
             "catalyst": "Data center growth."},
        ],
    },
    "crypto":     {"short_term": [], "long_term": []},
    "etfs":       {"short_term": [], "long_term": []},
    "commodities":{"short_term": [], "long_term": []},
    "options_plays": [],
}

SAMPLE_CONFIG = {
    "allowed_users":    [TEST_UID],
    "pick_mode":        "both",
    "risk_profile":     "moderate",
    "crypto_enabled":   True,
    "show_buy_counts":  False,
    "stock_budget":     None,
    "crypto_budget":    None,
    "max_short_picks":  2,
    "max_long_picks":   3,
    "stop_loss_pct":    5,
    "target_gain_pct":  8,
    "timezone":         "America/New_York",
    "excluded_sectors": [],
    "watchlist":        [],
    "paused":           False,
}


def _run(market_closed=False, closed_reason="", next_open_label="",
         dry_run=False, user_paused=False):
    """
    Run _send_morning_personalised with all network/storage patched out.
    Returns (n_sent: int, broadcast_mock: MagicMock).
    """
    import agent
    import config_manager

    user_cfg = {**SAMPLE_CONFIG, "paused": user_paused}

    with patch.object(agent, "DRY_RUN", dry_run), \
         patch.object(agent, "broadcast_all") as mock_bc, \
         patch.object(agent, "send_message"), \
         patch.object(agent, "personalize_picks_batch",  return_value={}), \
         patch.object(agent, "get_user_config",          return_value=user_cfg), \
         patch.object(agent, "load_user_trade_log",      return_value={"open": [], "closed": []}), \
         patch.object(agent, "compute_pick_streaks",     return_value={}), \
         patch.object(agent, "format_daily_message",     return_value="<b>Morning picks</b>"), \
         patch.object(agent, "build_picks_keyboard",     return_value=None), \
         patch("config_manager.get_allowed_users",       return_value=[TEST_UID]), \
         patch("config_manager.update_user_config",      return_value=None), \
         patch("config_manager.load_weekly_picks",       return_value={}), \
         patch("config_manager.load_buy_counts",         return_value={}):

        n = agent._send_morning_personalised(
            SAMPLE_PICKS,
            SAMPLE_CONFIG,
            label="Test Morning Briefing",
            market_closed=market_closed,
            closed_reason=closed_reason,
            next_open_label=next_open_label,
        )
    return n, mock_bc


# ─────────────────────────────────────────────────────────────────────────────
class TestSendMorningPersonalised:
    """Critical path: verify _send_morning_personalised delivers to outbox."""

    # ------------------------------------------------------------------
    # Regression: the NameError that killed production on 2026-05-26
    # ------------------------------------------------------------------
    def test_no_name_error_weekday(self):
        """Must NOT raise NameError for is_weekend/is_holiday/etc."""
        n, mock_bc = _run(market_closed=False)
        assert n == 1, "Expected 1 message in outbox for 1 non-paused user"
        mock_bc.assert_called_once()

    def test_no_name_error_weekend(self):
        """Market-closed path must also work without NameError."""
        n, mock_bc = _run(
            market_closed=True,
            closed_reason="Saturday",
            next_open_label="Monday, Jun 2",
        )
        assert n == 1
        mock_bc.assert_called_once()

    # ------------------------------------------------------------------
    # Outbox content
    # ------------------------------------------------------------------
    def test_outbox_has_correct_chat_id(self):
        """The broadcast payload must target the right chat_id."""
        _, mock_bc = _run()
        outbox = mock_bc.call_args[0][0]
        assert len(outbox) == 1
        assert outbox[0]["chat_id"] == TEST_UID

    def test_outbox_has_message_text(self):
        """Each outbox entry must have non-empty text."""
        _, mock_bc = _run()
        outbox = mock_bc.call_args[0][0]
        assert outbox[0]["text"].strip() != ""

    # ------------------------------------------------------------------
    # Paused user
    # ------------------------------------------------------------------
    def test_paused_user_skipped(self):
        """A paused user must not receive picks."""
        n, mock_bc = _run(user_paused=True)
        assert n == 0, "Paused user should produce 0 outbox entries"
        mock_bc.assert_not_called()

    # ------------------------------------------------------------------
    # DRY_RUN
    # ------------------------------------------------------------------
    def test_dry_run_no_broadcast(self, capsys):
        """DRY_RUN=True must print but not broadcast."""
        n, mock_bc = _run(dry_run=True)
        assert n == 0, "DRY_RUN should produce 0 outbox entries (nothing sent)"
        mock_bc.assert_not_called()
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out

    # ------------------------------------------------------------------
    # Return value
    # ------------------------------------------------------------------
    def test_returns_int(self):
        """Function must return an int (outbox count)."""
        n, _ = _run()
        assert isinstance(n, int)

    def test_returns_zero_on_empty_recipients(self):
        """Zero recipients → zero sends."""
        import agent
        with patch("config_manager.get_allowed_users", return_value=[]), \
             patch.object(agent, "broadcast_all") as mock_bc, \
             patch("config_manager.load_weekly_picks", return_value={}):
            n = agent._send_morning_personalised(SAMPLE_PICKS, SAMPLE_CONFIG)
        assert n == 0
        mock_bc.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
class TestSendMorningParamPassing:
    """Verify scope isolation — _send_morning_personalised must not rely on caller scope."""

    def test_no_is_weekend_in_function_body(self):
        """
        _send_morning_personalised must NOT reference is_weekend directly.
        It must use the market_closed parameter instead.
        """
        import agent
        # Get only the body of _send_morning_personalised, not its signature
        src = inspect.getsource(agent._send_morning_personalised)
        body = src.split("market_closed: bool")[1]  # everything after the signature
        assert "is_weekend" not in body, (
            "_send_morning_personalised references 'is_weekend' from caller scope — "
            "use the 'market_closed' parameter instead"
        )

    def test_no_is_holiday_in_function_body(self):
        """_send_morning_personalised must NOT reference is_holiday directly."""
        import agent
        src = inspect.getsource(agent._send_morning_personalised)
        body = src.split("market_closed: bool")[1]
        assert "is_holiday" not in body, (
            "_send_morning_personalised references 'is_holiday' from caller scope — "
            "use the 'market_closed' parameter instead"
        )

    def test_function_signature_has_required_params(self):
        """Signature must include market_closed, closed_reason, next_open_label."""
        import agent
        sig = inspect.signature(agent._send_morning_personalised)
        params = set(sig.parameters.keys())
        for required in ("market_closed", "closed_reason", "next_open_label"):
            assert required in params, (
                f"_send_morning_personalised is missing parameter '{required}'"
            )


# ─────────────────────────────────────────────────────────────────────────────
class TestRunDigestSilentFailure:
    """Scheduled digest must NEVER surface errors to users.

    Regression (2026-06-11): the DIGEST command handler threw, the command
    layer's catch-all returned "Something went wrong. Please try again in a
    moment." and run_digest broadcast that error to every user.
    """

    def _run(self, reply):
        import agent
        sent = []
        with patch("config_manager.get_allowed_users", return_value=["111"]), \
             patch("config_manager.get_user_config", return_value={}), \
             patch("config_manager.load_user_trade_log",
                   return_value={"open": [{"ticker": "NVDA"}]}), \
             patch("config_manager.update_config", return_value=None), \
             patch("bot_commands._parse_and_execute", return_value=reply), \
             patch("telegram_api.send_message",
                   side_effect=lambda text, chat_id=None, **kw: sent.append(text)):
            agent.run_digest()
        return sent

    def test_error_reply_suppressed(self):
        sent = self._run("⚠️ Something went wrong. Please try again in a moment.")
        assert sent == []

    def test_normal_digest_delivered(self):
        sent = self._run("🌅 <b>Morning Digest</b>")
        assert sent == ["🌅 <b>Morning Digest</b>"]

    def test_empty_reply_not_sent(self):
        sent = self._run("")
        assert sent == []
