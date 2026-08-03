"""Synthetic-bot trades are TAGGED, never deleted — and must be excluded from
every user-facing aggregate and from the LLM pick-feedback prompt.

Two distinct harms this prevents:
  1. a robot's mechanical fills shown to prospective users as a track record
  2. those same fills injected into Claude's stock-picking prompt, where the
     "BOT TRACK RECORD" block emits directives that steer tomorrow's real picks
"""
import pytest
from config_manager import (SYNTHETIC_SOURCE, is_synthetic_trade, human_trades)
import performance_tracker as pt


def _t(ticker, ret, synthetic=False, date="2026-07-20"):
    d = {"ticker": ticker, "return_pct": ret, "closed_date": date,
         "asset_type": "stock", "manual": True}
    if synthetic:
        d["source"] = SYNTHETIC_SOURCE
    return d


class TestTagging:
    def test_flags_synthetic_only(self):
        assert is_synthetic_trade(_t("A", 1, synthetic=True))
        assert not is_synthetic_trade(_t("A", 1))
        assert not is_synthetic_trade({})
        assert not is_synthetic_trade(None)

    def test_manual_flag_cannot_distinguish(self):
        """add_holding sets manual=True for bot AND human — only source can tell."""
        bot, human = _t("A", 1, synthetic=True), _t("B", 1)
        assert bot["manual"] is human["manual"] is True
        assert is_synthetic_trade(bot) and not is_synthetic_trade(human)

    def test_human_trades_filters(self):
        rows = [_t("A", 5), _t("B", -3, synthetic=True), _t("C", 2)]
        assert [t["ticker"] for t in human_trades(rows)] == ["A", "C"]

    def test_human_trades_handles_empty(self):
        assert human_trades(None) == [] and human_trades([]) == []


class TestCommunityStatsExcludeBot:
    def test_bot_losses_do_not_drag_community_stats(self, monkeypatch):
        monkeypatch.setattr(pt, "_spy_return", lambda *_a, **_k: 1.0)
        humans = [_t(f"H{i}", 5.0) for i in range(12)]          # 12 human winners
        bots   = [_t(f"B{i}", -20.0, synthetic=True) for i in range(30)]
        s = pt.build_community_stats([{"closed": humans + bots}])
        assert s is not None
        assert s["total_trades"] == 12, "bot trades must not be counted"
        assert s["win_rate"] == 100.0

    def test_hidden_below_minimum_sample(self, monkeypatch):
        monkeypatch.setattr(pt, "_spy_return", lambda *_a, **_k: 1.0)
        # 3 winners — the real post-tagging state; a "100% win rate" here would
        # mislead a prospective user far more than showing nothing.
        s = pt.build_community_stats([{"closed": [_t("A", 5), _t("B", 4), _t("C", 3)]}])
        assert s is None

    def test_all_synthetic_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(pt, "_spy_return", lambda *_a, **_k: 1.0)
        bots = [_t(f"B{i}", 9.0, synthetic=True) for i in range(50)]
        assert pt.build_community_stats([{"closed": bots}]) is None


class TestPromptExcludesBot:
    def test_bot_trades_never_reach_the_pick_prompt(self, monkeypatch):
        import performance_context as pc
        bots = [_t(f"B{i}", -8.0, synthetic=True) for i in range(40)]
        monkeypatch.setattr(pc, "get_allowed_users", lambda: ["u1"])
        monkeypatch.setattr(pc, "load_user_trade_log", lambda uid: {"closed": bots})
        ctx = pc.get_performance_context(lookback_days=3650)
        assert ctx == "" or "BOT TRACK RECORD" not in ctx, \
            "a robot's fills must never steer real picks"
