"""Every DIRECT Gist read must survive the API's response limit.

Found 2026-11-14. The Gist API returns files alphabetically and stops sending
content once the response passes ~900 KB in total: every file after that point
comes back `truncated: true` with EMPTY content, however small it is. The pick
ledger (803 KB) had pushed the gist past that limit on 2026-11-02, and three
readers took `files[name]["content"]` on trust:

    synthetic_user._state      -> saw NO tracked positions on every run, and
                                  `manage` saved that emptiness back. 38 real
                                  positions and every tournament arm's paper
                                  book went unmanaged for twelve days, behind
                                  runs that all reported success.
    synthetic_user._raw_picks  -> next in line (picks.json sorts just after
    config_manager.load_picks     the ledger): "no picks today" for the bot,
                                  the confirmation, the end-of-day summary.

The older tests could not see it: every one of them STUBBED `_state`, so the
real reader was never run against a truncated response. *A stub more permissive
than production certifies bugs.* These drive the real readers.
"""
import ast
import datetime as dt
import importlib.util
import json
import pathlib
import sys

import pytest
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SU = ROOT / "scripts" / "synthetic_user.py"

ACCOUNT = "900000001"                       # the bot-only test account
STATE_FILE = f"synthetic_state_{ACCOUNT}.json"
FULL_STATE = {"real": ["AAA", "BBB"], "paper": ["CCC"],
              "book": {"CCC": {"lt": True}}, "skipped": [{"t": "DDD", "date": "2026-11-01"}]}


def _su():
    spec = importlib.util.spec_from_file_location("su_trunc", SU)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _truncated(name):
    """What the API really returns for a file past the response limit."""
    return {"filename": name, "size": 999, "truncated": True, "content": "",
            "raw_url": f"https://gist.example/raw/{name}"}


def _wire(monkeypatch, files, raw=None, list_status=200, raw_status=200):
    """Serve a gist listing + raw files. Returns the list of URLs requested."""
    seen = []

    def _get(url, **k):
        seen.append(url)
        if "/raw/" in url:
            name = url.rsplit("/", 1)[1]
            return _Resp(raw_status, text=(raw or {}).get(name, ""))
        return _Resp(list_status, {"files": files})

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return seen


# ── The bot's state file ─────────────────────────────────────────────────────

class TestStateRead:
    def test_a_truncated_state_is_fetched_in_full(self, monkeypatch):
        """THE incident. The listing says truncated with empty content; the
        real state is one more request away."""
        su = _su()
        seen = _wire(monkeypatch, {STATE_FILE: _truncated(STATE_FILE)},
                     raw={STATE_FILE: json.dumps(FULL_STATE)})
        assert su._state(ACCOUNT) == FULL_STATE
        assert any("/raw/" in u for u in seen), "the full file was never fetched"

    def test_an_untruncated_state_needs_no_second_request(self, monkeypatch):
        su = _su()
        seen = _wire(monkeypatch, {STATE_FILE: {"truncated": False,
                                                "content": json.dumps(FULL_STATE)}})
        assert su._state(ACCOUNT) == FULL_STATE
        assert not any("/raw/" in u for u in seen)

    def test_a_file_that_does_not_exist_is_an_empty_state(self, monkeypatch):
        """A brand-new account. This is the ONLY case allowed to return {}."""
        su = _su()
        _wire(monkeypatch, {"picks.json": {"content": "{}"}})
        assert su._state(ACCOUNT) == {}

    @pytest.mark.parametrize("kw", [dict(list_status=500), dict(raw_status=500)])
    def test_an_unreadable_state_raises_instead_of_reading_as_empty(self, monkeypatch, kw):
        su = _su()
        _wire(monkeypatch, {STATE_FILE: _truncated(STATE_FILE)},
              raw={STATE_FILE: json.dumps(FULL_STATE)}, **kw)
        with pytest.raises(su.StateUnreadable):
            su._state(ACCOUNT)

    @pytest.mark.parametrize("body", ['{"real": ["AAA"', "[1, 2]", "not json"])
    def test_a_corrupt_state_raises(self, monkeypatch, body):
        su = _su()
        _wire(monkeypatch, {STATE_FILE: {"truncated": False, "content": body}})
        with pytest.raises(su.StateUnreadable):
            su._state(ACCOUNT)

    def test_a_transient_failure_is_retried_before_giving_up(self, monkeypatch):
        su = _su()
        calls = {"n": 0}

        def _get(url, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("blip")
            return _Resp(200, {"files": {STATE_FILE: {"content": json.dumps(FULL_STATE)}}})
        monkeypatch.setattr(requests, "get", _get)
        monkeypatch.setattr("time.sleep", lambda s: None)
        assert su._state(ACCOUNT) == FULL_STATE


class TestAnUnreadableStateStopsTheRun:
    """Every phase ends by writing state back. A run that could not READ it
    must write nothing — that write is what turned a read glitch into twelve
    days of lost tracking."""

    @pytest.mark.parametrize("phase", ["open", "manage"])
    def test_nothing_is_saved_and_the_run_fails(self, monkeypatch, phase):
        su = _su()
        saved = []
        _wire(monkeypatch, {STATE_FILE: _truncated(STATE_FILE),
                            "picks.json": {"content": json.dumps({
                                "_saved_date": "2026-11-13",
                                "stocks": {"short_term": [{"ticker": "AAA", "entry_price": 10,
                                                           "target_price": 11, "stop_loss": 9}]}})}},
              raw_status=500)
        monkeypatch.setattr(su, "_save_state", lambda cid, st: saved.append(st) or True)
        monkeypatch.setattr(su, "_OWNER_ID", "")
        monkeypatch.setattr(sys, "argv", ["synthetic_user.py", "--phase", phase])
        assert su.main() == 1
        assert saved == []


# ── picks.json and config.json ───────────────────────────────────────────────

class TestPicksAndConfigReads:
    def test_the_bots_picks_read_survives_truncation(self, monkeypatch):
        su = _su()
        picks = {"_saved_date": "2026-11-13", "stocks": {"short_term": [{"ticker": "AAA"}]}}
        _wire(monkeypatch, {"picks.json": _truncated("picks.json")},
              raw={"picks.json": json.dumps(picks)})
        assert su._raw_picks() == picks

    def test_load_picks_survives_truncation(self, monkeypatch):
        """The reader behind the confirmation, the end-of-day summary and the
        mini-app's picks tab."""
        import config_manager as cm
        today = cm.et_today().isoformat()
        picks = {"_saved_date": today, "stocks": {"short_term": [{"ticker": "AAA"}]}}
        _wire(monkeypatch, {"picks.json": _truncated("picks.json")},
              raw={"picks.json": json.dumps(picks)})
        assert cm.load_picks() == picks

    def test_get_config_survives_truncation(self, monkeypatch):
        import config_manager as cm
        monkeypatch.setattr(cm, "_cache_get", lambda name: None)
        monkeypatch.setattr(cm, "_cache_set", lambda name, value: None)
        _wire(monkeypatch, {cm.GIST_FILENAME: _truncated(cm.GIST_FILENAME)},
              raw={cm.GIST_FILENAME: json.dumps({"allowed_users": ["42"]})})
        assert cm.get_config()["allowed_users"] == ["42"]

    @pytest.mark.parametrize("rel", ["config_manager.py", "scripts/synthetic_user.py"])
    def test_no_reader_takes_gist_content_on_trust(self, rel):
        """The class, mechanically: these two files talk to the Gist API directly
        and must never index a file's `content` themselves — only through the
        one guard (storage._gist_content). AST, not text: the comments and
        docstrings explaining this fix name the very key it bans."""
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        offenders = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and n.slice.value == "content"):
                offenders.append(n.lineno)
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get" and n.args
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value == "content"):
                offenders.append(n.lineno)
        assert offenders == [], f"{rel} reads gist content unguarded at line(s) {offenders}"

    def test_the_scan_can_detect_an_offender(self):
        tree = ast.parse('raw = files["picks.json"]["content"]')
        assert any(isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                   and n.slice.value == "content" for n in ast.walk(tree))


# ── The account, not the state file, says what to manage ─────────────────────

class TestTheAccountIsTheRecord:
    def _drive(self, monkeypatch, su, admin, state, real_open=(), paper=(), prices=None,
               today="2026-11-14"):
        import market_data, trade_logger, paper_trader, sim_portfolio, config_manager
        sells, closes, saved = [], [], []
        monkeypatch.setattr(su, "_state", lambda cid: json.loads(json.dumps(state)))
        monkeypatch.setattr(su, "_save_state", lambda cid, st: saved.append(st) or True)
        monkeypatch.setattr(config_manager, "et_today", lambda: dt.date.fromisoformat(today))
        monkeypatch.setattr(market_data, "get_live_price", lambda t: (prices or {}).get(t.upper()))
        monkeypatch.setattr(trade_logger, "load_user_trade_log",
                            lambda cid: {"open": list(real_open)})
        monkeypatch.setattr(trade_logger, "close_trade",
                            lambda t, cid, exit_price=None, outcome="manual": closes.append((t, outcome)))
        monkeypatch.setattr(paper_trader, "load_user_paper",
                            lambda cid: {"positions": list(paper)})
        monkeypatch.setattr(paper_trader, "paper_sell",
                            lambda t, cid, shares=None, price=None, outcome=None:
                            sells.append((t, outcome)) or "📄")
        monkeypatch.setattr(sim_portfolio, "update", lambda acct, fn: None)
        su._manage_account(admin, dry=False)
        return sells, closes, saved

    def _real(self, t, source):
        return {"ticker": t, "stop_loss": 90.0, "target_price": 120.0,
                "opened_date": "2026-11-10", "timeframe": "short_term", "source": source}

    def _paper(self, t, opened="2026-11-10", levels_source="pick"):
        return {"ticker": t, "shares": 10, "avg_price": 100.0, "stop_loss": 90.0,
                "target_price": 120.0, "bought_date": opened, "levels_source": levels_source}

    def test_a_wiped_state_no_longer_orphans_the_accounts_positions(self, monkeypatch):
        """The twelve days, replayed: empty state, positions in the account,
        one past its target and one through its stop."""
        import config_manager as cm
        su = _su()
        sells, closes, saved = self._drive(
            monkeypatch, su, ACCOUNT, {"real": [], "paper": [], "book": {}},
            real_open=[self._real("WIN", cm.SYNTHETIC_SOURCE)],
            paper=[self._paper("LOSE")], prices={"WIN": 125.0, "LOSE": 80.0, "SPY": 500.0})
        assert closes == [("WIN", "target")]
        assert sells == [("LOSE", "stop")]

    def test_positions_it_keeps_are_written_back_into_state(self, monkeypatch):
        import config_manager as cm
        su = _su()
        _, _, saved = self._drive(
            monkeypatch, su, ACCOUNT, {},
            real_open=[self._real("HOLD", cm.SYNTHETIC_SOURCE)],
            paper=[self._paper("KEEP")], prices={"HOLD": 100.0, "KEEP": 100.0})
        assert saved[-1]["real"] == ["HOLD"] and saved[-1]["paper"] == ["KEEP"]

    def test_only_the_bots_own_real_trades_are_adopted(self, monkeypatch):
        su = _su()
        _, closes, _ = self._drive(monkeypatch, su, ACCOUNT, {},
                                   real_open=[self._real("HUMAN", None)],
                                   prices={"HUMAN": 125.0})
        assert closes == []

    def test_a_real_persons_account_is_never_adopted(self, monkeypatch):
        """The legacy owner wind-down: the bot touches ONLY what its state
        names. Adopting there would sell a human's position."""
        import config_manager as cm
        su = _su()
        owner = su._OWNER_ID
        assert owner and not su._is_bot_only(owner)
        sells, closes, _ = self._drive(
            monkeypatch, su, owner, {"real": [], "paper": []},
            real_open=[self._real("MINE", cm.SYNTHETIC_SOURCE)],
            paper=[self._paper("ALSOMINE")], prices={"MINE": 125.0, "ALSOMINE": 80.0})
        assert closes == [] and sells == []

    def test_the_owner_is_never_bot_only_even_if_listed_as_a_test_user(self, monkeypatch):
        import config_manager as cm
        su = _su()
        monkeypatch.setattr(cm, "is_test_user", lambda cid: True)
        assert su._is_bot_only(su._OWNER_ID) is False
        assert su._is_bot_only(ACCOUNT) is True
        assert su._is_bot_only("") is False

    def test_an_adopted_long_term_position_keeps_its_180_day_horizon(self, monkeypatch):
        """Without the state's book entry the position still says what it is:
        a long-term pick's stop is the invalidation level. Reading it as
        short-term would expire it 150 days early."""
        su = _su()
        lt = self._paper("LT", opened="2026-09-01", levels_source="invalidation+target")
        st = self._paper("ST", opened="2026-09-01", levels_source="pick")
        sells, _, _ = self._drive(monkeypatch, su, ACCOUNT, {}, paper=[lt, st],
                                  prices={"LT": 100.0, "ST": 100.0, "SPY": 500.0})
        assert sells == [("ST", "expired")]

    def test_the_states_own_flag_still_wins(self):
        su = _su()
        assert su._paper_is_long_term({"levels_source": "invalidation"}, {"lt": False}) is False
        assert su._paper_is_long_term({"levels_source": "pick"}, {"lt": True}) is True
        assert su._paper_is_long_term({"levels_source": "pick"}, None) is False
        assert su._paper_is_long_term({}, {}) is False
