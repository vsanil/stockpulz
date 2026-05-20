"""
test_alert_manager.py — Unit tests for price_alert_manager.py

Covers:
  - add_alert: happy path, auto direction, duplicate prevention
  - remove_alert: exact match, float tolerance (BUG-05 regression), bulk remove
  - list_alerts: empty and populated
  - check_alerts: triggered vs not triggered
  - clear_alerts
"""

import pytest
from price_alert_manager import (
    add_alert,
    remove_alert,
    list_alerts,
    check_alerts,
    clear_alerts,
)

CHAT = "9999999"


# ── add_alert ────────────────────────────────────────────────────────────────

class TestAddAlert:
    def test_above_target_when_target_higher_than_price(self):
        """Auto direction resolves to 'above' when target > current price."""
        # AAPL fake price = 189.50; target 200 → above
        msg = add_alert(CHAT, "AAPL", 200.0, direction="auto")
        assert "above" in msg
        assert "AAPL" in msg
        assert "200" in msg

    def test_below_target_when_target_lower_than_price(self):
        """Auto direction resolves to 'below' when target < current price."""
        # AAPL fake price = 189.50; target 180 → below
        msg = add_alert(CHAT, "AAPL", 180.0, direction="auto")
        assert "below" in msg

    def test_explicit_direction_above(self):
        msg = add_alert(CHAT, "NVDA", 900.0, direction="above")
        assert "above" in msg

    def test_explicit_direction_below(self):
        msg = add_alert(CHAT, "NVDA", 800.0, direction="below")
        assert "below" in msg

    def test_crypto_ticker(self):
        """BTC should work — price fetcher handles crypto symbols."""
        msg = add_alert(CHAT, "BTC", 70000.0, direction="above")
        assert "BTC" in msg

    def test_decimal_target_price(self):
        """Decimal prices should not cause issues."""
        msg = add_alert(CHAT, "AAPL", 199.99, direction="above")
        assert "199.99" in msg

    def test_duplicate_same_ticker_target_direction_blocked(self):
        """Adding the same alert twice should raise ValueError."""
        add_alert(CHAT, "MSFT", 420.0, direction="above")
        with pytest.raises(ValueError, match="Alert already exists"):
            add_alert(CHAT, "MSFT", 420.0, direction="above")

    def test_duplicate_float_tolerance_blocked(self):
        """
        BUG-06 regression: 420.0 and 420.00000001 are the same price —
        duplicate check must use tolerance, not strict equality.
        """
        add_alert(CHAT, "MSFT", 420.0, direction="above")
        with pytest.raises(ValueError, match="Alert already exists"):
            add_alert(CHAT, "MSFT", 420.00000001, direction="above")

    def test_same_ticker_different_target_allowed(self):
        """Two alerts on the same ticker at different prices are both valid."""
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert(CHAT, "AAPL", 210.0, direction="above")  # should not raise

    def test_same_ticker_same_target_different_direction_allowed(self):
        """above $200 and below $200 are distinct alerts."""
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert(CHAT, "AAPL", 200.0, direction="below")  # should not raise

    def test_unknown_ticker_raises(self):
        """Ticker not in FAKE_PRICES → _current_price returns None → ValueError."""
        with pytest.raises(ValueError, match="Could not fetch price"):
            add_alert(CHAT, "FAKEXYZ", 100.0)

    def test_returns_pct_away_in_message(self):
        """Confirmation message should include the % distance from current price."""
        msg = add_alert(CHAT, "AAPL", 200.0, direction="above")
        assert "%" in msg


# ── remove_alert ─────────────────────────────────────────────────────────────

class TestRemoveAlert:
    def _seed(self):
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert(CHAT, "AAPL", 180.0, direction="below")
        add_alert(CHAT, "NVDA", 900.0, direction="above")

    def test_remove_specific_alert_by_target(self):
        self._seed()
        msg = remove_alert(CHAT, "AAPL", 200.0)
        assert "Removed 1" in msg
        # Remaining AAPL alert should still be there
        listing = list_alerts(CHAT)
        assert "180" in listing

    def test_remove_all_alerts_for_ticker(self):
        self._seed()
        msg = remove_alert(CHAT, "AAPL")  # no target_price → remove all
        assert "Removed 2" in msg
        listing = list_alerts(CHAT)
        assert "AAPL" not in listing
        assert "NVDA" in listing

    def test_remove_nonexistent_returns_warning(self):
        self._seed()
        msg = remove_alert(CHAT, "TSLA", 300.0)
        assert "No active alerts" in msg

    def test_remove_decimal_price_float_tolerance(self):
        """
        BUG-05 regression: removing alert at 199.99 should work even if
        the stored float is 199.99000000000001 due to IEEE 754 representation.
        """
        add_alert(CHAT, "AAPL", 199.99, direction="above")
        # Remove using a slightly different float representation
        msg = remove_alert(CHAT, "AAPL", 199.99)
        assert "Removed 1" in msg

    def test_remove_leaves_other_users_alerts_intact(self):
        """Removing alerts for one chat_id must not affect another."""
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert("1111111", "AAPL", 200.0, direction="above")
        remove_alert(CHAT, "AAPL", 200.0)
        # Other user's alert should still exist
        other_listing = list_alerts("1111111")
        assert "AAPL" in other_listing


# ── list_alerts ───────────────────────────────────────────────────────────────

class TestListAlerts:
    def test_empty_returns_no_alerts_message(self):
        msg = list_alerts(CHAT)
        assert "No active" in msg

    def test_populated_shows_all_alerts(self):
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert(CHAT, "NVDA", 900.0, direction="above")
        msg = list_alerts(CHAT)
        assert "AAPL" in msg
        assert "NVDA" in msg
        assert "2 active" in msg

    def test_shows_direction(self):
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert(CHAT, "MSFT", 400.0, direction="below")
        msg = list_alerts(CHAT)
        assert "above" in msg
        assert "below" in msg


# ── check_alerts ──────────────────────────────────────────────────────────────

class TestCheckAlerts:
    def test_alert_not_triggered_when_price_not_reached(self):
        # AAPL = 189.50, target 200 → not triggered
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        fired = check_alerts(CHAT)
        assert fired == []

    def test_alert_triggered_when_price_above_target(self):
        # AAPL = 189.50, set below target so it's already above 185
        add_alert(CHAT, "AAPL", 185.0, direction="above")
        fired = check_alerts(CHAT)
        assert len(fired) == 1
        assert "AAPL" in fired[0]

    def test_alert_triggered_when_price_below_target(self):
        # AAPL = 189.50, below 195 → triggered
        add_alert(CHAT, "AAPL", 195.0, direction="below")
        fired = check_alerts(CHAT)
        assert len(fired) == 1

    def test_triggered_alert_is_removed(self):
        add_alert(CHAT, "AAPL", 185.0, direction="above")
        check_alerts(CHAT)
        # Alert should be gone after firing
        listing = list_alerts(CHAT)
        assert "No active" in listing

    def test_untriggered_alert_remains(self):
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        check_alerts(CHAT)
        listing = list_alerts(CHAT)
        assert "AAPL" in listing

    def test_multiple_alerts_only_triggered_ones_removed(self):
        add_alert(CHAT, "AAPL", 185.0, direction="above")  # triggers (189.50 > 185)
        add_alert(CHAT, "AAPL", 200.0, direction="above")  # does not trigger
        fired = check_alerts(CHAT)
        assert len(fired) == 1
        listing = list_alerts(CHAT)
        assert "200" in listing
        assert "1 active" in listing

    def test_empty_alerts_returns_empty_list(self):
        assert check_alerts(CHAT) == []


# ── clear_alerts ──────────────────────────────────────────────────────────────

class TestClearAlerts:
    def test_clear_removes_all_alerts(self):
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert(CHAT, "NVDA", 900.0, direction="above")
        count = clear_alerts(CHAT)
        assert count == 2
        assert "No active" in list_alerts(CHAT)

    def test_clear_empty_returns_zero(self):
        assert clear_alerts(CHAT) == 0

    def test_clear_only_affects_target_chat(self):
        add_alert(CHAT, "AAPL", 200.0, direction="above")
        add_alert("1111111", "AAPL", 200.0, direction="above")
        clear_alerts(CHAT)
        assert "AAPL" in list_alerts("1111111")
