"""The untested MUTATING routes with the widest blast radius.

Route coverage re-measured 2026-09-15: **30 of 86 routed handlers had no test
hitting their path** (Aug 19 baseline: 33 of 84 — two routes added, three
covered, so it had barely moved). The count is not the story; **11 of the 30
mutate state**, and this suite is the only gate between a self-heal fix and real
users. These are the four with the most to lose:

    /admin/broadcast              messages EVERY allowed user, irreversibly
    /admin/user/<id>/approve      grants access, and must clear the pending row
    /admin/user/<id>/ban|unban    revokes and restores access
    /api/miniapp/unlog_bought     DELETES a logged position — and is the IDOR
                                  surface, since it acts on a chat_id

Why these and not all thirty: a GET that renders a number wrong is a bug, while
a mutation that runs as the wrong user, or half-runs, changes data nobody can
restore. `full_sweep` already proves the GETs return 200.

⚠️ Patches target `config_manager` / `trade_logger`, NOT `webhook`, because the
handlers import those names INSIDE the function body — the scope trap that once
let a "patched" test write to the live gist. `send_message` is different: it IS
module-level in webhook, so it is patched there.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import config_manager
import trade_logger
import webhook


def _admin(client):
    with client.session_transaction() as s:
        s["admin"] = True
    return client


@pytest.fixture
def sent(monkeypatch):
    """Capture every Telegram send; never let a test reach the network."""
    out = []
    monkeypatch.setattr(webhook, "send_message",
                        lambda text, *a, **k: out.append((k.get("chat_id"), text)))
    return out


class TestAdminBroadcastReachesEveryone:
    """It messages every allowed user at once. There is no undo."""

    def test_it_refuses_without_an_admin_session(self, client, sent):
        r = client.post("/admin/broadcast", json={"text": "hello"})
        assert r.status_code in (302, 401, 403), (
            "broadcast is reachable without an admin session"
        )
        assert sent == [], "a message went out to users on an unauthenticated call"

    def test_empty_text_is_rejected_before_anyone_is_messaged(self, client, sent, monkeypatch):
        monkeypatch.setattr(config_manager, "get_allowed_users", lambda: ["1", "2"])
        r = _admin(client).post("/admin/broadcast", json={"text": "   "})
        assert r.status_code == 400
        assert sent == [], "an empty broadcast still messaged users"

    def test_it_reaches_every_allowed_user(self, client, sent, monkeypatch):
        monkeypatch.setattr(config_manager, "get_allowed_users", lambda: ["11", "22", "33"])
        r = _admin(client).post("/admin/broadcast", json={"text": "ship it"})
        assert r.status_code == 200 and r.get_json()["sent"] == 3
        assert {c for c, _ in sent} == {"11", "22", "33"}
        assert all("ship it" in t for _, t in sent)

    def test_one_failed_send_does_not_abort_the_rest(self, client, monkeypatch):
        """The rule this repo already applies to every per-user delivery loop:
        one user's failure must never cost everyone else the message."""
        seen = []

        def _flaky(text, *a, **k):
            cid = k.get("chat_id")
            seen.append(cid)
            if cid == "22":
                raise RuntimeError("chat not found")

        monkeypatch.setattr(webhook, "send_message", _flaky)
        monkeypatch.setattr(config_manager, "get_allowed_users", lambda: ["11", "22", "33"])
        r = _admin(client).post("/admin/broadcast", json={"text": "hi"})
        body = r.get_json()
        assert seen == ["11", "22", "33"], "delivery stopped at the failing user"
        assert body["sent"] == 2 and body["failed"] == 1


class TestAccessControlRoutes:
    def test_approve_requires_an_admin_session(self, client, sent, monkeypatch):
        calls = []
        monkeypatch.setattr(config_manager, "add_allowed_user", lambda c: calls.append(c))
        r = client.post("/admin/user/999/approve")
        assert r.status_code in (302, 401, 403)
        assert calls == [], "an unauthenticated call granted access"

    def test_approve_grants_access_AND_clears_the_pending_row(self, client, sent, monkeypatch):
        """Both halves matter. Leaving the pending row behind is how somebody
        ends up waiting ten days for access that was already granted — the
        failure `canary.check_pending_approvals` exists to catch."""
        added, cleared = [], []
        monkeypatch.setattr(config_manager, "add_allowed_user", lambda c: added.append(c))
        monkeypatch.setattr(config_manager, "remove_pending_user", lambda c: cleared.append(c))
        r = _admin(client).post("/admin/user/4242/approve")
        assert r.status_code == 200
        assert added == ["4242"], "user was not added to the allowlist"
        assert cleared == ["4242"], "pending row left behind — they stay 'waiting'"
        assert any(c == "4242" for c, _ in sent), "the user was never told"

    def test_reject_clears_pending_and_does_not_grant_access(self, client, sent, monkeypatch):
        added, cleared = [], []
        monkeypatch.setattr(config_manager, "add_allowed_user", lambda c: added.append(c))
        monkeypatch.setattr(config_manager, "remove_pending_user", lambda c: cleared.append(c))
        r = _admin(client).post("/admin/user/777/reject")
        assert r.status_code == 200
        assert cleared == ["777"] and added == []

    def test_banning_the_owner_is_refused_with_a_reason(self, client, monkeypatch):
        """config_manager.ban_user raises ValueError for the owner. The route
        must surface that as a 400 with the message, not a 500."""
        def _boom(c):
            raise ValueError("Cannot ban the bot owner.")
        monkeypatch.setattr(config_manager, "ban_user", _boom)
        r = _admin(client).post("/admin/user/1/ban")
        assert r.status_code == 400
        assert "owner" in (r.get_json().get("error") or "").lower()

    def test_unban_restores_access_not_merely_the_ban_flag(self, client, monkeypatch):
        """Unbanning without re-adding to the allowlist leaves the user unbanned
        AND still locked out — a silent half-fix."""
        unbanned, added = [], []
        monkeypatch.setattr(config_manager, "unban_user", lambda c: unbanned.append(c))
        monkeypatch.setattr(config_manager, "add_allowed_user", lambda c: added.append(c))
        r = _admin(client).post("/admin/user/555/unban")
        assert r.status_code == 200
        assert unbanned == ["555"] and added == ["555"]

    def test_messaging_a_user_with_empty_text_is_rejected(self, client, sent):
        r = _admin(client).post("/admin/user/9/message", json={"text": ""})
        assert r.status_code == 400 and sent == []


class TestUnlogBoughtIsNotAnIDOR:
    """It DELETES a logged position, and it acts on a chat_id.

    CLAUDE.md's rule: a mutating endpoint uses the AUTHENTICATED chat_id, never
    a client-supplied one. That rule exists because `seed_backtest` once let the
    body override it.
    """

    def test_unauthenticated_callers_are_refused(self, client, monkeypatch):
        removed = []
        monkeypatch.setattr(trade_logger, "remove_holding",
                            lambda t, c: removed.append((t, c)))
        monkeypatch.setattr(webhook, "_miniapp_auth", lambda: None)
        r = client.post("/api/miniapp/unlog_bought", json={"ticker": "AAPL"})
        assert r.status_code == 403
        assert removed == [], "an unauthenticated call deleted a position"

    def test_a_missing_ticker_is_rejected(self, client, monkeypatch):
        """Auth is pinned so this isolates the ticker branch. Passing an
        arbitrary chat_id instead returns 403 — which would pass a sloppy
        `!= 200` assertion for entirely the wrong reason."""
        removed = []
        monkeypatch.setattr(trade_logger, "remove_holding",
                            lambda t, c: removed.append((t, c)))
        monkeypatch.setattr(webhook, "_miniapp_auth", lambda: "AUTHED_USER")
        r = client.post("/api/miniapp/unlog_bought", json={"ticker": "   "})
        assert r.status_code == 400 and removed == []

    def test_it_deletes_for_the_AUTHENTICATED_user_not_the_body(self, client, monkeypatch):
        """The load-bearing one. A body naming someone else must not redirect
        the delete at them."""
        removed = []
        monkeypatch.setattr(trade_logger, "remove_holding",
                            lambda t, c: removed.append((t, c)) or True)
        monkeypatch.setattr(webhook, "_miniapp_auth", lambda: "AUTHED_USER")
        r = client.post("/api/miniapp/unlog_bought",
                        json={"ticker": "aapl", "chat_id": "VICTIM"})
        assert r.status_code == 200
        assert removed == [("AAPL", "AUTHED_USER")], (
            f"delete ran as {removed and removed[0][1]!r} — a client-supplied "
            f"chat_id must never steer it"
        )
