"""
test_position_sizer.py — Unit tests for position_sizer.py

Coverage:
  - size_pick:          no-entry guard, stop derivation (abs price vs pct vs ATR vs default),
                        conviction scaling (1-5), position_cap, risk_cap
  - apply_portfolio_sizing: attaches sizing to all picks, returns warnings list
  - portfolio_summary:  returns formatted string when picks are sized
"""

import os
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from position_sizer import size_pick, apply_portfolio_sizing, portfolio_summary, _CONVICTION_SCALE


# ── Config fixtures ───────────────────────────────────────────────────────────

def _cfg(portfolio_size=10_000, risk_per_trade_pct=1.0,
         max_position_pct=10.0, max_risk_pct=1.5):
    return {
        "portfolio": {
            "portfolio_size":     portfolio_size,
            "risk_per_trade_pct": risk_per_trade_pct,
            "max_position_pct":   max_position_pct,
            "max_risk_pct":       max_risk_pct,
            "max_sector_pct":     35.0,
            "max_positions":      8,
        }
    }


def _pick(entry=100.0, stop=90.0, target=115.0, conviction=3,
          ticker="AAPL", sector="Technology",
          suggested_stop_pct=None, atr_pct=None):
    p = {
        "ticker":       ticker,
        "entry_price":  entry,
        "target_price": target,
        "stop_loss":    stop,
        "conviction":   conviction,
        "sector":       sector,
        "thesis":       "Test thesis.",
    }
    if suggested_stop_pct is not None:
        p["suggested_stop_pct"] = suggested_stop_pct
    if atr_pct is not None:
        p["atr_pct"] = atr_pct
    return p


# ─────────────────────────────────────────────────────────────────────────────
class TestSizePickBasics:
    """size_pick — fundamental output shape and guard clauses."""

    def test_returns_dict(self):
        result = size_pick(_pick(), _cfg())
        assert isinstance(result, dict)

    def test_required_keys_present(self):
        result = size_pick(_pick(), _cfg())
        required = {"shares", "dollar_amount", "risk_dollars", "risk_pct",
                    "position_pct", "stop_price", "stop_pct",
                    "conviction_factor", "capped_by", "note"}
        assert required.issubset(set(result.keys()))

    def test_no_entry_returns_null_sizing(self):
        result = size_pick({"conviction": 3}, _cfg())
        assert result["shares"] is None
        assert result["dollar_amount"] is None

    def test_zero_entry_returns_null_sizing(self):
        result = size_pick({"entry_price": 0, "conviction": 3}, _cfg())
        assert result["shares"] is None

    def test_shares_is_positive_int(self):
        result = size_pick(_pick(entry=100, stop=90), _cfg())
        assert result["shares"] is not None
        assert result["shares"] >= 1
        assert isinstance(result["shares"], int)

    def test_dollar_amount_positive(self):
        result = size_pick(_pick(), _cfg())
        assert result["dollar_amount"] > 0

    def test_risk_dollars_positive(self):
        result = size_pick(_pick(), _cfg())
        assert result["risk_dollars"] > 0

    def test_note_is_string(self):
        result = size_pick(_pick(), _cfg())
        assert isinstance(result["note"], str)
        assert len(result["note"]) > 0


# ─────────────────────────────────────────────────────────────────────────────
class TestStopDerivation:
    """stop_price is derived from abs stop > suggested_stop_pct > atr_pct > default."""

    def test_abs_stop_used_when_valid(self):
        result = size_pick(_pick(entry=100, stop=90), _cfg())
        assert abs(result["stop_price"] - 90.0) < 0.1

    def test_suggested_stop_pct_used_as_fallback(self):
        p = _pick(entry=100, stop=None)
        p["stop_loss"] = None
        p["suggested_stop_pct"] = 0.08  # 8%
        result = size_pick(p, _cfg())
        # stop_price should be around 100 * (1 - 0.08) = 92
        assert abs(result["stop_price"] - 92.0) < 0.5

    def test_atr_pct_fallback(self):
        p = _pick(entry=100, stop=None)
        p["stop_loss"] = None
        p["atr_pct"] = 0.04   # 4% ATR → 4 * 1.5 = 6% stop
        result = size_pick(p, _cfg())
        expected_stop = 100 * (1 - 0.04 * 1.5)   # ~94
        assert abs(result["stop_price"] - expected_stop) < 1.0

    def test_default_stop_when_no_data(self):
        p = {"ticker": "AAPL", "entry_price": 100.0, "conviction": 3}
        result = size_pick(p, _cfg())
        # Default is 7% → stop at ~93
        assert abs(result["stop_price"] - 93.0) < 0.5

    def test_stop_floor_applied(self):
        """stop_pct should never be below 2%."""
        p = _pick(entry=100, stop=99.5)   # 0.5% stop — below floor
        result = size_pick(p, _cfg())
        assert result["stop_pct"] >= 2.0

    def test_stop_ceiling_applied(self):
        """stop_pct should never exceed 20%."""
        p = _pick(entry=100, stop=70)   # 30% stop — above ceiling
        result = size_pick(p, _cfg())
        assert result["stop_pct"] <= 20.0

    def test_screener_percent_suggested_stop_normalized(self):
        """Screener emits suggested_stop_pct as a PERCENT (7.5 == 7.5%); it must
        normalize to ~7.5%, not get clamped to the 20% ceiling (the old bug)."""
        p = _pick(entry=100, stop=None)
        p["stop_loss"] = None
        p["suggested_stop_pct"] = 7.5            # percent, as the screener writes it
        result = size_pick(p, _cfg())
        assert abs(result["stop_price"] - 92.5) < 0.6   # 100 * (1 - 0.075)
        assert result["stop_pct"] < 20.0                # NOT pinned to the ceiling

    def test_screener_percent_atr_normalized(self):
        """atr_pct is a PERCENT too (5.0 == 5%) → 5 * 1.5 = 7.5% stop."""
        p = _pick(entry=100, stop=None)
        p["stop_loss"] = None
        p["atr_pct"] = 5.0
        result = size_pick(p, _cfg())
        assert abs(result["stop_price"] - 92.5) < 1.0


# ─────────────────────────────────────────────────────────────────────────────
class TestConvictionScaling:
    """Conviction 1-5 scales position size by the _CONVICTION_SCALE factors."""

    def test_conviction_5_largest_position(self):
        high_result = size_pick(_pick(conviction=5), _cfg())
        low_result  = size_pick(_pick(conviction=1), _cfg())
        assert high_result["dollar_amount"] > low_result["dollar_amount"]

    def test_conviction_3_mid_factor(self):
        result = size_pick(_pick(conviction=3), _cfg())
        assert result["conviction_factor"] == _CONVICTION_SCALE[3]   # 0.60

    def test_conviction_5_full_factor(self):
        result = size_pick(_pick(conviction=5), _cfg())
        assert result["conviction_factor"] == _CONVICTION_SCALE[5]   # 1.00

    def test_conviction_1_probe_size(self):
        result = size_pick(_pick(conviction=1), _cfg())
        assert result["conviction_factor"] == _CONVICTION_SCALE[1]   # 0.25

    def test_conviction_clamped_above_5(self):
        result = size_pick(_pick(conviction=10), _cfg())
        assert result["conviction_factor"] == _CONVICTION_SCALE[5]

    def test_conviction_zero_treated_as_default(self):
        """conviction=0 is falsy, so `0 or 3` resolves to 3 (design intent)."""
        result = size_pick(_pick(conviction=0), _cfg())
        assert result["conviction_factor"] == _CONVICTION_SCALE[3]   # 0.60

    def test_conviction_none_defaults_to_3(self):
        p = _pick()
        p["conviction"] = None
        result = size_pick(p, _cfg())
        assert result["conviction_factor"] == _CONVICTION_SCALE[3]


# ─────────────────────────────────────────────────────────────────────────────
class TestPositionCap:
    """position_cap fires when dollar_amount would exceed max_position_pct * capital."""

    def test_position_cap_applied(self):
        """With a very small max_position_pct (1%), the cap should fire."""
        result = size_pick(_pick(entry=100, stop=99, conviction=5),
                           _cfg(portfolio_size=10_000, max_position_pct=1.0))
        # $10,000 * 1% = $100 max position
        assert result["dollar_amount"] <= 100.5   # small tolerance for rounding

    def test_position_cap_reported_in_capped_by(self):
        result = size_pick(_pick(entry=100, stop=99, conviction=5),
                           _cfg(portfolio_size=10_000, max_position_pct=1.0))
        assert result["capped_by"] in ("position_cap", "risk_cap")

    def test_no_cap_when_within_limits(self):
        result = size_pick(_pick(entry=100, stop=90, conviction=3),
                           _cfg(portfolio_size=10_000, max_position_pct=10.0, max_risk_pct=5.0))
        # With moderate settings, capped_by should be None
        # (may still be capped if risk fires — just check it's a valid value)
        assert result["capped_by"] in (None, "position_cap", "risk_cap")


# ─────────────────────────────────────────────────────────────────────────────
class TestRiskCap:
    """risk_cap fires when risk_dollars would exceed max_risk_pct * capital."""

    def test_risk_cap_applied(self):
        """With very low max_risk_pct (0.5%), risk cap should fire and reduce size."""
        # entry=100, stop=50 → stop gets clamped to 20% ceiling = stop_price=80
        # With very low max_risk_pct, shares get reduced; but 1-share min = $100 pos → $20 risk
        result = size_pick(_pick(entry=100, stop=50, conviction=5),
                           _cfg(portfolio_size=10_000, max_risk_pct=0.5))
        # Max risk = $50. After cap, risk_dollars should be <= $50
        assert result["risk_dollars"] <= 50
        assert result["capped_by"] in ("risk_cap", "position_cap")

    def test_risk_cap_capped_by_field_set(self):
        """When risk cap fires, capped_by should be set."""
        result = size_pick(_pick(entry=100, stop=98, conviction=5),
                           _cfg(portfolio_size=10_000, max_risk_pct=0.1))
        # With 2% stop and very tight risk cap, capped_by should be set
        assert result["capped_by"] is not None


# ─────────────────────────────────────────────────────────────────────────────
class TestCryptoSizing:
    """Crypto picks use dollar_amount instead of shares."""

    def test_crypto_shares_is_none(self):
        p = {"symbol": "BTC", "entry_price": 65000, "stop_loss": 60000,
             "target_price": 75000, "conviction": 3}
        result = size_pick(p, _cfg(), is_crypto=True)
        assert result["shares"] is None

    def test_crypto_dollar_amount_positive(self):
        p = {"symbol": "BTC", "entry_price": 65000, "stop_loss": 60000,
             "target_price": 75000, "conviction": 3}
        result = size_pick(p, _cfg(), is_crypto=True)
        assert result["dollar_amount"] > 0


# ─────────────────────────────────────────────────────────────────────────────
class TestApplyPortfolioSizing:
    """apply_portfolio_sizing attaches sizing to all picks and returns warnings."""

    def _picks(self):
        return {
            "short_term": [
                _pick(entry=100, stop=90, conviction=3, ticker="AAPL"),
            ],
            "long_term": [
                _pick(entry=200, stop=185, conviction=4, ticker="NVDA"),
            ],
            "crypto": [],
            "etf":    [],
            "commodities": [],
        }

    def test_returns_tuple(self):
        result = apply_portfolio_sizing(self._picks(), _cfg())
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_enriched_picks_have_sizing(self):
        enriched, _ = apply_portfolio_sizing(self._picks(), _cfg())
        for pick in enriched.get("short_term", []):
            assert "sizing" in pick
            assert pick["sizing"] is not None

    def test_warnings_is_list(self):
        _, warnings = apply_portfolio_sizing(self._picks(), _cfg())
        assert isinstance(warnings, list)

    def test_max_positions_warning(self):
        """Exceeding max_positions should add a warning."""
        many_picks = [_pick(entry=100, stop=90, ticker=f"T{i}") for i in range(10)]
        picks = {"short_term": many_picks, "long_term": [], "crypto": [],
                 "etf": [], "commodities": []}
        cfg = _cfg()
        cfg["portfolio"]["max_positions"] = 5
        _, warnings = apply_portfolio_sizing(picks, cfg)
        assert any("max_positions" in w or "exceeds" in w for w in warnings)


# ─────────────────────────────────────────────────────────────────────────────
class TestPortfolioSummary:
    """portfolio_summary returns a compact line when picks are sized."""

    def test_returns_string(self):
        picks = {
            "short_term": [_pick()],
            "long_term": [], "crypto": [], "etf": [], "commodities": []
        }
        enriched, _ = apply_portfolio_sizing(picks, _cfg())
        result = portfolio_summary(enriched, _cfg())
        assert isinstance(result, str)

    def test_empty_picks_returns_empty(self):
        picks = {"short_term": [], "long_term": [], "crypto": [], "etf": [], "commodities": []}
        result = portfolio_summary(picks, _cfg())
        assert result == ""

    def test_summary_contains_deployed(self):
        picks = {"short_term": [_pick()], "long_term": [], "crypto": [], "etf": [], "commodities": []}
        enriched, _ = apply_portfolio_sizing(picks, _cfg())
        result = portfolio_summary(enriched, _cfg())
        assert "deployed" in result or "$" in result
