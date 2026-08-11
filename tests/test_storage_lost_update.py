"""Regression: cross-user LOST UPDATE from GitHub's read-after-write lag.

Observed live (Aug 3): tagging the owner's 34 trades and then the test account's
4 silently erased the owner's. The per-file lock serializes our writes and
read_strict raises on fetch errors — but neither helps when a fresh GET moments
after a PATCH still returns the PRE-write copy. That stale copy became the merge
base for the next user's write, wiping the previous one.
"""
import time
import pytest
import config_manager as cm


class _LaggyGist:
    """Gist whose reads lag one write behind — exactly the real failure mode."""
    server = {}
    served = {}          # what a read currently returns (one write behind)

    def read_strict(self, filename):
        return _LaggyGist.served.get(filename)

    def write(self, filename, data):
        import copy
        # the write lands on the server, but reads keep serving the OLD copy
        _LaggyGist.served[filename] = copy.deepcopy(
            _LaggyGist.server.get(filename))
        _LaggyGist.server[filename] = copy.deepcopy(data)

    def name(self):
        return "laggy"

    def supports_rows(self):
        return False          # blob backend — exercises the whole-file path


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    import storage
    _LaggyGist.server, _LaggyGist.served = {}, {}
    cm._recent_user_writes.clear()
    cm._gist_read_cache.clear()
    monkeypatch.setattr(storage, "GistBackend", _LaggyGist)
    # config_manager now resolves the backend through the factory, so patch that
    # too (and clear its cached instance) — otherwise it returns a real Gist.
    monkeypatch.setattr(storage, "get_storage_backend", lambda: _LaggyGist())
    storage.reset_backend()
    yield
    cm._recent_user_writes.clear()


class TestCrossUserLostUpdate:
    def test_second_user_write_does_not_erase_first(self):
        cm._update_user_keyed_file("trades.json", "ownerA", {"trades": ["tagged"]})
        cm._update_user_keyed_file("trades.json", "testB", {"trades": ["bot"]})
        final = _LaggyGist.server["trades.json"]
        assert final.get("testB") == {"trades": ["bot"]}
        assert final.get("ownerA") == {"trades": ["tagged"]}, \
            "user A's write was silently erased by user B's stale merge base"

    def test_three_rapid_users_all_survive(self):
        for uid in ("u1", "u2", "u3"):
            cm._update_user_keyed_file("f.json", uid, {"v": uid})
        final = _LaggyGist.server["f.json"]
        assert {k: v["v"] for k, v in final.items()} == {"u1": "u1", "u2": "u2", "u3": "u3"}

    def test_same_user_rewrite_keeps_latest(self):
        cm._update_user_keyed_file("f.json", "u1", {"v": 1})
        cm._update_user_keyed_file("f.json", "u1", {"v": 2})
        assert _LaggyGist.server["f.json"]["u1"] == {"v": 2}

    def test_echo_expires_so_another_process_is_not_stomped_forever(self, monkeypatch):
        cm._update_user_keyed_file("f.json", "u1", {"v": "mine"})
        # simulate the echo ageing out past the lag window
        vals = cm._recent_user_writes["f.json"]["u1"]
        cm._recent_user_writes["f.json"]["u1"] = (vals[0], time.time() - cm._WRITE_ECHO_TTL - 1)
        # another process legitimately updated u1; server now serves that
        _LaggyGist.served["f.json"] = {"u1": {"v": "theirs"}}
        cm._update_user_keyed_file("f.json", "u2", {"v": "b"})
        assert _LaggyGist.server["f.json"]["u1"] == {"v": "theirs"}, \
            "an expired echo must not resurrect our stale value"


class _Server:
    """A gist whose reads are always current — models a SECOND PROCESS that can
    write between a caller's load and its save."""
    data = {}

    def read_strict(self, filename):
        import copy
        return copy.deepcopy(_Server.data.get(filename))

    def read(self, filename):
        # the swallowing variant _load_gist_file() uses
        import copy
        return copy.deepcopy(_Server.data.get(filename))

    def write(self, filename, d):
        import copy
        _Server.data[filename] = copy.deepcopy(d)

    def name(self):
        return "server"

    def supports_rows(self):
        return False


class TestSameUserCrossProcessRace:
    """close_trade must not clobber a concurrent write to the SAME user made by
    another process (e.g. the user logs a position in the mini-app while a
    scheduled job closes a trade for them)."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        import storage
        _Server.data = {}
        cm._recent_user_writes.clear()
        cm._gist_read_cache.clear()
        monkeypatch.setattr(storage, "GistBackend", _Server)
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _Server())
        storage.reset_backend()
        yield

    def test_concurrent_position_log_survives_a_close(self):
        import trade_logger as tl
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": [{"ticker": "AAA", "entry_price": 10.0}],
                                  "closed": [], "watchlist": []}}
        # a caller reads here (stale snapshot) …
        stale = _Server().read_strict(F)
        assert len(stale["u1"]["open"]) == 1
        # … meanwhile ANOTHER PROCESS logs a new position for the same user
        cur = _Server().read_strict(F)
        cur["u1"]["open"].append({"ticker": "BBB", "entry_price": 20.0})
        _Server().write(F, cur)
        # now the close runs — it must see BBB, not the stale snapshot
        closed = tl.close_trade("AAA", "u1", exit_price=11.0)
        final = _Server.data[F]["u1"]
        assert closed and closed["ticker"] == "AAA"
        assert [t["ticker"] for t in final["open"]] == ["BBB"], \
            "the concurrent position log was clobbered by the close"
        assert [t["ticker"] for t in final["closed"]] == ["AAA"]

    def test_close_of_missing_ticker_is_a_safe_noop(self):
        import trade_logger as tl
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": [{"ticker": "ZZZ", "entry_price": 5.0}],
                                  "closed": [], "watchlist": []}}
        assert tl.close_trade("NOPE", "u1", exit_price=1.0) is None
        assert [t["ticker"] for t in _Server.data[F]["u1"]["open"]] == ["ZZZ"]


class TestConvertedReadModifyWriteSites:
    """The three remaining load→modify→save sites (add_holding, the paper_trader
    paths, the two agent.py notification-flag jobs) were converted to the mutator
    form. Each test writes to the SAME user from a second process between the
    caller's prep and its write — the pre-conversion code clobbered that write."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        import storage
        _Server.data = {}
        cm._recent_user_writes.clear()
        cm._gist_read_cache.clear()
        monkeypatch.setattr(storage, "GistBackend", _Server)
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _Server())
        storage.reset_backend()
        yield

    # ── trade_logger.add_holding ─────────────────────────────────────────────
    def test_add_holding_does_not_clobber_a_concurrent_write(self, monkeypatch):
        import trade_logger as tl
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": [{"ticker": "AAA", "entry_price": 10.0}],
                                  "closed": [], "watchlist": []}}

        # another PROCESS logs BBB while this caller is still preparing its trade
        cur = _Server().read_strict(F)
        cur["u1"]["open"].append({"ticker": "BBB", "entry_price": 20.0})
        _Server().write(F, cur)

        trade, existed = tl.add_holding("CCC", "u1", entry_override=30.0)
        assert not existed
        final = [t["ticker"] for t in _Server.data[F]["u1"]["open"]]
        assert final == ["AAA", "BBB", "CCC"], \
            f"concurrent write lost — got {final}"

    def test_add_holding_sees_a_duplicate_added_concurrently(self, monkeypatch):
        """The duplicate check must run against the FRESH log: if another process
        opened the same ticker first, this must update levels, not double-add."""
        import trade_logger as tl
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": [], "closed": [], "watchlist": []}}

        cur = _Server().read_strict(F)
        cur["u1"]["open"].append({"ticker": "DDD", "entry_price": 5.0})
        _Server().write(F, cur)

        trade, existed = tl.add_holding("DDD", "u1", entry_override=7.0)
        assert existed, "duplicate check ran against a stale log"
        opens = _Server.data[F]["u1"]["open"]
        assert len(opens) == 1
        assert opens[0]["entry_price"] == 7.0

    def test_add_holding_with_no_overrides_on_existing_writes_nothing(self):
        import trade_logger as tl
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": [{"ticker": "EEE", "entry_price": 1.0}],
                                  "closed": [], "watchlist": []}}
        before = _Server().read_strict(F)
        trade, existed = tl.add_holding("EEE", "u1")
        assert existed
        assert _Server.data[F] == before, "a no-op add_holding burned a write"

    # ── paper_trader ─────────────────────────────────────────────────────────
    def _seed_paper(self, cash=10_000.0, positions=None):
        F = cm.USER_PAPER_FILE
        _Server.data[F] = {"u1": {"positions": positions or [], "history": [],
                                  "starting_cash": cash, "cash": cash}}
        return F

    def test_paper_buy_does_not_clobber_a_concurrent_buy(self, monkeypatch):
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 100.0)
        F = self._seed_paper()

        # another process buys ZZZ for the same user mid-flight
        cur = _Server().read_strict(F)
        cur["u1"]["positions"].append({"ticker": "ZZZ", "shares": 1,
                                       "avg_price": 50.0, "cost_basis": 50.0})
        cur["u1"]["cash"] = 9_950.0
        _Server().write(F, cur)

        pt.paper_buy("AAPL", 10, "u1")
        final = _Server.data[F]["u1"]
        tickers = sorted(p["ticker"] for p in final["positions"])
        assert tickers == ["AAPL", "ZZZ"], f"concurrent buy lost — {tickers}"
        # cash must be debited from the CONCURRENT balance, not the stale one
        assert final["cash"] == pytest.approx(9_950.0 - 1_000.0)

    def test_paper_sell_prices_off_the_fresh_position(self, monkeypatch):
        """A concurrent scale-in changes the share count; the sell must use it."""
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 120.0)
        F = self._seed_paper(positions=[{"ticker": "AAPL", "shares": 5,
                                         "avg_price": 100.0, "cost_basis": 500.0}])
        cur = _Server().read_strict(F)
        cur["u1"]["positions"][0]["shares"] = 8      # another process scaled in
        _Server().write(F, cur)

        pt.paper_sell("AAPL", "u1")                  # sell ALL
        final = _Server.data[F]["u1"]
        assert final["positions"] == []
        assert final["history"][0]["shares"] == 8, \
            "sold the stale share count, leaving phantom shares behind"

    def test_paper_buy_rejection_writes_nothing(self, monkeypatch):
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 100.0)
        F = self._seed_paper(cash=500.0)
        before = _Server().read_strict(F)
        msg = pt.paper_buy("AAPL", 100, "u1")        # $10k against $500
        assert "Insufficient" in msg
        assert _Server.data[F] == before, "a rejected buy burned a write"

    def test_paper_sell_missing_position_writes_nothing(self, monkeypatch):
        import paper_trader as pt
        monkeypatch.setattr(pt, "_live_price", lambda t: 100.0)
        F = self._seed_paper()
        before = _Server().read_strict(F)
        msg = pt.paper_sell("NOPE", "u1")
        assert "No open paper position" in msg
        assert _Server.data[F] == before

    def test_paper_add_cash_does_not_clobber_a_concurrent_buy(self, monkeypatch):
        import paper_trader as pt
        F = self._seed_paper()
        cur = _Server().read_strict(F)
        cur["u1"]["positions"].append({"ticker": "ZZZ", "shares": 1,
                                       "avg_price": 50.0, "cost_basis": 50.0})
        cur["u1"]["cash"] = 9_950.0
        _Server().write(F, cur)

        pt.paper_add_cash(1_000.0, "u1")
        final = _Server.data[F]["u1"]
        assert [p["ticker"] for p in final["positions"]] == ["ZZZ"]
        assert final["cash"] == pytest.approx(10_950.0)

    # ── agent.py notification flags ──────────────────────────────────────────
    def test_notify_flags_do_not_clobber_a_concurrent_position_log(self):
        import agent
        F = cm.USER_TRADES_FILE
        snapshot = [{"ticker": "AAA", "entry_price": 10.0}]
        _Server.data[F] = {"u1": {"open": [dict(snapshot[0])],
                                  "closed": [], "watchlist": []}}

        # the job marks its stale snapshot while another process logs BBB
        snapshot[0]["_notified_stale"] = True
        cur = _Server().read_strict(F)
        cur["u1"]["open"].append({"ticker": "BBB", "entry_price": 20.0})
        _Server().write(F, cur)

        agent._persist_notify_flags("u1", snapshot, ("_notified_stale",))
        final = _Server.data[F]["u1"]["open"]
        assert [t["ticker"] for t in final] == ["AAA", "BBB"], \
            "the notification-flag save clobbered a concurrent position log"
        assert final[0].get("_notified_stale") is True
        assert "_notified_stale" not in final[1]

    def test_notify_flags_with_nothing_marked_writes_nothing(self):
        import agent
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": [{"ticker": "AAA"}],
                                  "closed": [], "watchlist": []}}
        before = _Server().read_strict(F)
        agent._persist_notify_flags("u1", [{"ticker": "AAA"}], ("_notified_stale",))
        assert _Server.data[F] == before


class _FailingReadServer(_Server):
    """A backend whose read fails — models a transient Gist/network error.
    The forbidden `_load_gist_file(F) or {}` pattern turns this into a write of
    {this_user: …}, erasing every other user. read_strict must raise instead."""
    fail = True

    def read_strict(self, filename):
        if _FailingReadServer.fail:
            raise RuntimeError("transient gist read failure")
        return _Server.read_strict(self, filename)


class TestHighTrafficWritersAreClobberSafe:
    """update_user_config(_multi) and the alert writers used the whole-file
    `load … or {} → write` pattern. They are the app's most-called writers, so a
    single failed read could erase every user's settings or alerts."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        import storage
        _Server.data = {}
        _FailingReadServer.data = _Server.data
        _FailingReadServer.fail = False
        cm._recent_user_writes.clear()
        cm._gist_read_cache.clear()
        monkeypatch.setattr(storage, "GistBackend", _Server)
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _Server())
        # conftest's autouse store patches _load_gist_file BY NAME, which would
        # bypass _Server on the read path — point it at _Server too.
        import price_alert_manager as pam
        monkeypatch.setattr(cm,  "_load_gist_file", lambda fn: _Server().read(fn))
        monkeypatch.setattr(pam, "_load_gist_file", lambda fn: _Server().read(fn))
        storage.reset_backend()
        yield

    def test_config_update_does_not_erase_other_users(self):
        F = cm.USER_CONFIGS_FILE
        _Server.data[F] = {"u1": {"stop_loss_pct": 5}, "u2": {"stop_loss_pct": 9}}
        cm.update_user_config("u1", "stop_loss_pct", 7)
        assert _Server.data[F]["u2"] == {"stop_loss_pct": 9}, "u2's config was erased"
        assert _Server.data[F]["u1"]["stop_loss_pct"] == 7

    def test_config_update_does_not_clobber_a_concurrent_write(self):
        F = cm.USER_CONFIGS_FILE
        _Server.data[F] = {"u1": {"a": 1}}
        # another process sets a DIFFERENT key on the same user mid-flight
        cur = _Server().read_strict(F)
        cur["u1"]["b"] = 2
        _Server().write(F, cur)
        cm._gist_read_cache.clear()
        cm.update_user_config("u1", "a", 99)
        assert _Server.data[F]["u1"] == {"a": 99, "b": 2}, \
            "the concurrently-set key was lost"

    def test_config_multi_merges_onto_the_fresh_record(self):
        F = cm.USER_CONFIGS_FILE
        _Server.data[F] = {"u1": {"a": 1}}
        cur = _Server().read_strict(F)
        cur["u1"]["b"] = 2
        _Server().write(F, cur)
        cm._gist_read_cache.clear()
        cm.update_user_config_multi("u1", {"a": 5, "c": 3})
        assert _Server.data[F]["u1"] == {"a": 5, "b": 2, "c": 3}

    def test_failed_read_raises_instead_of_erasing_everyone(self, monkeypatch):
        import storage
        F = cm.USER_CONFIGS_FILE
        _Server.data[F] = {"u1": {"a": 1}, "u2": {"a": 2}}
        before = dict(_Server.data[F])
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _FailingReadServer())
        _FailingReadServer.fail = True
        with pytest.raises(Exception):
            cm.update_user_config("u3", "a", 3)
        assert _Server.data[F] == before, \
            "a failed read was treated as 'empty' and wiped every user"

    # ── price alerts ─────────────────────────────────────────────────────────
    def test_fired_alert_removal_keeps_one_added_concurrently(self, monkeypatch):
        import price_alert_manager as pam
        F = pam.ALERTS_FILENAME
        _Server.data[F] = {"u1": [
            {"ticker": "AAPL", "direction": "above", "target": 100.0,
             "price_at_set": 90.0},
        ]}
        monkeypatch.setattr(pam, "_current_price", lambda t: 110.0)

        # the price fetch is where a concurrent add lands in real life
        real_price = pam._current_price
        def _slow(t):
            cur = _Server().read_strict(F)
            cur["u1"].append({"ticker": "MSFT", "direction": "below",
                              "target": 10.0, "price_at_set": 50.0})
            _Server().write(F, cur)
            cm._gist_read_cache.clear()
            return real_price(t)
        monkeypatch.setattr(pam, "_current_price", _slow)

        fired = pam.check_alerts("u1", send_fn=lambda *a, **k: None)
        assert len(fired) == 1
        left = [a["ticker"] for a in _Server.data[F]["u1"]]
        assert left == ["MSFT"], \
            f"the concurrently-added alert was erased — left={left}"
        assert len(_Server.data[F]["_history_u1"]) == 1

    def test_clear_alerts_only_touches_that_user(self):
        import price_alert_manager as pam
        F = pam.ALERTS_FILENAME
        _Server.data[F] = {
            "u1": [{"ticker": "AAPL", "direction": "above", "target": 1.0}],
            "u2": [{"ticker": "MSFT", "direction": "above", "target": 2.0}],
        }
        assert pam.clear_alerts("u1") == 1
        assert _Server.data[F]["u1"] == []
        assert len(_Server.data[F]["u2"]) == 1, "another user's alerts were cleared"


class TestCommandLayerIsMutatorForm:
    """The command/web-layer sites converted on Aug 5.

    IMPORTANT about what these prove. On a BLOB backend `mutate_user_record`
    shrinks the race window, it does not close it (only row-level CAS does).
    So the race is only reproducible — and only fixable — where the site had
    real PREP between its load and its save. `_execute_add` / `_execute_trim`
    fetch a live price there, which is a network call taking seconds; the
    concurrent write is injected inside that fetch, exactly where it lands in
    production. Sites with no prep (watchlist, /fixticker, level updates) had a
    microsecond window either way — for those the conversion buys uniformity and
    the NO_WRITE no-op guard, so they are asserted on behaviour, not on the race.
    """

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        import storage, price_alert_manager as pam
        _Server.data = {}
        cm._recent_user_writes.clear()
        cm._gist_read_cache.clear()
        monkeypatch.setattr(storage, "GistBackend", _Server)
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _Server())
        monkeypatch.setattr(cm,  "_load_gist_file", lambda fn: _Server().read(fn))
        monkeypatch.setattr(pam, "_load_gist_file", lambda fn: _Server().read(fn))
        storage.reset_backend()
        yield

    def _seed(self, open_trades, closed=None, watchlist=None):
        F = cm.USER_TRADES_FILE
        _Server.data[F] = {"u1": {"open": open_trades,
                                  "closed": closed or [],
                                  "watchlist": watchlist or []}}
        return F

    @staticmethod
    def _scale_in_during_fetch(F, price, shares=20.0):
        """A price fetch that ALSO lands a concurrent same-user write — this is
        the real interleaving: the old code read the log, spent seconds here,
        then wrote its stale snapshot back over whatever arrived meanwhile."""
        def _fetch(_ticker):
            cur = _Server().read_strict(F)
            cur["u1"]["open"][0]["shares"] = shares
            _Server().write(F, cur)
            cm._gist_read_cache.clear()
            return price
        return _fetch

    # ── _execute_add — genuine race regression ───────────────────────────────
    def test_add_averages_against_a_scale_in_that_lands_during_the_price_fetch(self, monkeypatch):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 100.0, "shares": 10.0}])
        monkeypatch.setattr(ex, "_fetch_live_price", self._scale_in_during_fetch(F, 200.0))

        ex._execute_add("AAA", "u1", add_shares=10.0)   # no price → triggers the fetch
        pos = _Server.data[F]["u1"]["open"][0]
        assert pos["shares"] == 30.0, \
            f"averaged against a stale share count — got {pos['shares']}, want 30.0"
        assert pos["entry_price"] == pytest.approx(133.3333, abs=1e-3)

    # ── _execute_trim — genuine race regression ──────────────────────────────
    def test_trim_uses_a_share_count_that_changed_during_the_price_fetch(self, monkeypatch):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 100.0, "shares": 10.0}])
        monkeypatch.setattr(ex, "_fetch_live_price", self._scale_in_during_fetch(F, 110.0))

        ex._execute_trim("AAA", "u1", trim_shares=5.0)  # no price → triggers the fetch
        assert _Server.data[F]["u1"]["open"][0]["shares"] == 15.0, \
            "trimmed off a stale share count, leaving phantom shares"

    def test_trim_defaults_to_half_of_the_fresh_share_count(self, monkeypatch):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 100.0, "shares": 10.0}])
        monkeypatch.setattr(ex, "_fetch_live_price", self._scale_in_during_fetch(F, 110.0))

        ex._execute_trim("AAA", "u1")                   # shares=None → "sell half"
        assert _Server.data[F]["u1"]["open"][0]["shares"] == 10.0, \
            "'sell half' halved the stale count, not what is actually held"

    # ── NO_WRITE no-op guards (behaviour, not race) ──────────────────────────
    def test_add_to_missing_position_writes_nothing(self, monkeypatch):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 1.0, "shares": 1.0}])
        monkeypatch.setattr(ex, "_fetch_live_price", lambda t: 5.0)
        before = _Server().read_strict(F)
        assert "not found" in ex._execute_add("NOPE", "u1", add_price=5.0, add_shares=1.0)
        assert _Server.data[F] == before

    def test_trim_that_would_close_everything_writes_nothing(self, monkeypatch):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 100.0, "shares": 5.0}])
        monkeypatch.setattr(ex, "_fetch_live_price", lambda t: 110.0)
        before = _Server().read_strict(F)
        assert "entire position" in ex._execute_trim("AAA", "u1", trim_shares=5.0, trim_price=110.0)
        assert _Server.data[F] == before

    def test_update_level_missing_ticker_writes_nothing(self):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 1.0}])
        before = _Server().read_strict(F)
        assert "not found" in ex._execute_update_level("NOPE", "stop_loss", 1.0, "u1")
        assert _Server.data[F] == before

    def test_fixticker_no_op_writes_nothing(self):
        import cmd_admin
        F = self._seed([{"ticker": "OLD", "entry_price": 1.0}])
        before = _Server().read_strict(F)
        assert "not found" in (cmd_admin._cmd_admin("FIXTICKER GONE OTHER", "", "u1") or "")
        assert _Server.data[F] == before, "a no-op rename burned a storage write"

    # ── behaviour preserved through the conversion ───────────────────────────
    def test_update_level_sets_the_field_and_clears_trail_flags(self):
        import cmd_trade_exec as ex
        F = self._seed([{"ticker": "AAA", "entry_price": 100.0, "shares": 1.0,
                         "_notified_trail_15": True}])
        ex._execute_update_level("AAA", "stop_loss", 90.0, "u1")
        pos = _Server.data[F]["u1"]["open"][0]
        assert pos["stop_loss"] == 90.0
        assert "_notified_trail_15" not in pos, "trailing nudge flag was not reset"

    def test_fixticker_renames_open_and_closed(self):
        import cmd_admin
        F = self._seed([{"ticker": "OLD", "entry_price": 1.0}],
                       closed=[{"ticker": "OLD", "return_pct": 1.0}])
        assert "Renamed" in (cmd_admin._cmd_admin("FIXTICKER OLD NEW", "", "u1") or "")
        rec = _Server.data[F]["u1"]
        assert rec["open"][0]["ticker"] == "NEW"
        assert rec["closed"][0]["ticker"] == "NEW"

    def test_save_watchlist_dedupes_and_keeps_positions(self, monkeypatch):
        import webhook
        F = self._seed([{"ticker": "AAA", "entry_price": 1.0}], watchlist=["OLD"])
        monkeypatch.setattr(webhook, "get_user_config", lambda cid: {}, raising=False)
        webhook._save_watchlist("u1", ["spy", "qqq", "SPY"])
        rec = _Server.data[F]["u1"]
        assert rec["watchlist"] == ["SPY", "QQQ"]
        assert [t["ticker"] for t in rec["open"]] == ["AAA"]


class TestConfigManagerWholeFileWriters:
    """The last four `_load_gist_file(F) or {} → _write_gist_file(F, whole)` sites.
    A swallowed read error turned each into a write that erased everyone else."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        import storage
        _Server.data = {}
        cm._recent_user_writes.clear()
        cm._gist_read_cache.clear()
        monkeypatch.setattr(storage, "GistBackend", _Server)
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _Server())
        monkeypatch.setattr(cm, "_load_gist_file", lambda fn: _Server().read(fn))
        storage.reset_backend()
        yield

    def test_add_pending_user_keeps_the_others(self):
        F = cm.PENDING_USERS_FILE
        _Server.data[F] = {"u1": {"first_name": "A"}}
        cm.add_pending_user("u2", "B", "bee")
        assert set(_Server.data[F]) == {"u1", "u2"}
        assert _Server.data[F]["u1"] == {"first_name": "A"}, "existing pending user erased"

    def test_remove_pending_user_only_removes_that_one(self):
        F = cm.PENDING_USERS_FILE
        _Server.data[F] = {"u1": {"first_name": "A"}, "u2": {"first_name": "B"}}
        cm.remove_pending_user("u1")
        assert "u1" not in cm.get_pending_users()
        assert cm.get_pending_users()["u2"] == {"first_name": "B"}

    def test_removing_a_missing_pending_user_writes_nothing(self):
        F = cm.PENDING_USERS_FILE
        _Server.data[F] = {"u1": {"first_name": "A"}}
        before = _Server().read_strict(F)
        cm.remove_pending_user("nobody")
        assert _Server.data[F] == before

    def test_empty_pending_record_is_not_a_waiting_user(self):
        """On a row backend a removal writes an empty record rather than deleting
        the row — that tombstone must not read back as someone awaiting approval."""
        _Server.data[cm.PENDING_USERS_FILE] = {"u1": {}, "u2": {"first_name": "B"}}
        assert list(cm.get_pending_users()) == ["u2"]

    def test_backtest_trades_save_keeps_other_users(self):
        F = cm.BACKTEST_TRADES_FILE
        _Server.data[F] = {"u1": [{"ticker": "AAA"}]}
        cm.save_backtest_trades("u2", [{"ticker": "BBB"}])
        assert _Server.data[F]["u1"] == [{"ticker": "AAA"}], "another user's backtest erased"
        assert _Server.data[F]["u2"] == [{"ticker": "BBB"}]

    def test_buy_count_increments_off_a_fresh_read(self):
        F, today = cm.BUY_COUNTS_FILE, cm.et_today().isoformat()
        _Server.data[F] = {"date": today, "counts": {"AAA": 1}}
        # another user buys the same ticker between our read and our write
        cur = _Server().read_strict(F)
        cur["counts"]["AAA"] = 5
        _Server().write(F, cur)
        cm._gist_read_cache.clear()
        assert cm.increment_buy_count("AAA") == 6, "a concurrent buy was lost"
        assert _Server.data[F]["counts"]["AAA"] == 6

    def test_buy_count_resets_on_a_new_day(self):
        from datetime import date
        F = cm.BUY_COUNTS_FILE
        _Server.data[F] = {"date": "1999-01-01", "counts": {"AAA": 9}}
        assert cm.increment_buy_count("AAA") == 1
        assert _Server.data[F]["date"] == cm.et_today().isoformat()

    def test_mutate_gist_file_honours_no_write(self):
        """Regression: mutate_gist_file used to write the NO_WRITE sentinel object
        itself as the file contents, corrupting the file."""
        F = "some_shared_file.json"
        _Server.data[F] = {"keep": "me"}
        out = cm.mutate_gist_file(F, lambda cur: (cm.NO_WRITE, "skipped"), default={})
        assert out == "skipped"
        assert _Server.data[F] == {"keep": "me"}
