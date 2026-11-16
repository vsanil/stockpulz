"""A blank account read must never erase the bot's record of what it holds.

On 2026-08-19, the bot's first run against Supabase read the owner's account
while reads were blocked. `load_user_trade_log` swallows a failed read into an
empty account, `_manage_account` treated every tracked ticker as "already
closed", and saved an empty record. The owner's account is never re-adopted
(the bot must not take over a human's positions), so eight bot positions sat
there unmanaged for three months. Found 2026-11-15.
"""
import datetime as dt
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER = "1699321994"           # a real account: never adopted, only what state names


def _su():
    spec = importlib.util.spec_from_file_location(
        "synthetic_user_blank", ROOT / "scripts" / "synthetic_user.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _drive(monkeypatch, *, state, real_log, paper_book, prices=None, today="2026-11-16"):
    import market_data, trade_logger, paper_trader, sim_portfolio, config_manager
    su = _su()
    saved, closes, sells = {}, [], []
    monkeypatch.setattr(su, "_state", lambda cid: dict(state))
    monkeypatch.setattr(su, "_save_state", lambda cid, st: saved.update(st) or True)
    monkeypatch.setattr(config_manager, "et_today", lambda: dt.date.fromisoformat(today))
    monkeypatch.setattr(market_data, "get_live_price", lambda t: (prices or {}).get(t.upper()))
    monkeypatch.setattr(trade_logger, "load_user_trade_log", lambda cid: real_log)
    monkeypatch.setattr(trade_logger, "close_trade",
                        lambda t, cid, exit_price=None, outcome="manual": closes.append((t, outcome)))
    monkeypatch.setattr(paper_trader, "load_user_paper", lambda cid: paper_book)
    monkeypatch.setattr(paper_trader, "paper_sell",
                        lambda t, cid, shares=None, price=None, outcome=None:
                        sells.append((t, outcome)) or "📄")
    monkeypatch.setattr(sim_portfolio, "update", lambda acct, fn: None)
    acts = su._manage_account(OWNER, dry=False, sim=False)
    return saved, closes, sells, acts


EIGHT = ["AVTR", "MTB", "IRM", "GD", "PKG", "RTX", "KO", "PPL"]


class TestABlankReadKeepsTheRecord:
    def test_the_2026_08_19_failure_no_longer_empties_the_record(self, monkeypatch):
        saved, closes, _, acts = _drive(
            monkeypatch, state={"real": EIGHT, "paper": []},
            real_log={"open": [], "closed": []},
            paper_book={"positions": [], "history": []})
        assert saved["real"] == EIGHT, "a blank read erased the record again"
        assert closes == []
        assert any("account read empty" in a for a in acts), "it must say so, not stay quiet"

    def test_a_blank_paper_read_keeps_paper_tracking(self, monkeypatch):
        saved, _, sells, acts = _drive(
            monkeypatch, state={"real": [], "paper": ["ETH"]},
            real_log={"open": [], "closed": []},
            paper_book={"positions": [], "history": []})
        assert saved["paper"] == ["ETH"] and sells == []
        assert any("paper read empty" in a for a in acts)


class TestARealCloseIsStillDropped:
    """Raising the bar must not make the record keep positions forever."""

    def test_dropped_when_the_read_has_other_rows(self, monkeypatch):
        other = {"ticker": "XEL", "entry_price": 80.0, "opened_date": "2026-05-13"}
        saved, _, _, acts = _drive(
            monkeypatch, state={"real": ["MTB"], "paper": []},
            real_log={"open": [other], "closed": []},
            paper_book={"positions": [], "history": []}, prices={"XEL": 85.0})
        assert saved["real"] == [] and not any("read empty" in a for a in acts)

    def test_dropped_when_the_read_has_only_closed_rows(self, monkeypatch):
        saved, _, _, _ = _drive(
            monkeypatch, state={"real": ["MTB"], "paper": []},
            real_log={"open": [], "closed": [{"ticker": "MTB"}]},
            paper_book={"positions": [], "history": []})
        assert saved["real"] == []

    def test_paper_dropped_when_history_exists(self, monkeypatch):
        saved, _, _, _ = _drive(
            monkeypatch, state={"real": [], "paper": ["ETH"]},
            real_log={"open": [], "closed": []},
            paper_book={"positions": [], "history": [{"ticker": "ETH"}]})
        assert saved["paper"] == []


class TestTheRestoredEightWindDown:
    def test_all_eight_expire_on_the_next_run(self, monkeypatch):
        """No timeframe means short-term, 30-day limit; every one is 100+ days old,
        so the normal path closes them as `expired` — at the price of the day,
        between their stop and target."""
        opened = {"AVTR": "2026-07-15", "MTB": "2026-07-16", "IRM": "2026-07-20",
                  "GD": "2026-07-27", "PKG": "2026-07-27", "RTX": "2026-07-28",
                  "KO": "2026-07-29", "PPL": "2026-07-29"}
        rows = [{"ticker": t, "entry_price": 100.0, "stop_loss": 50.0, "target_price": 200.0,
                 "opened_date": d, "source": "synthetic_bot"} for t, d in opened.items()]
        saved, closes, _, _ = _drive(
            monkeypatch, state={"real": EIGHT, "paper": []},
            real_log={"open": rows, "closed": []},
            paper_book={"positions": [], "history": []},
            prices={t: 100.0 for t in EIGHT})
        assert sorted(closes) == sorted((t, "expired") for t in EIGHT)
        assert saved["real"] == []
