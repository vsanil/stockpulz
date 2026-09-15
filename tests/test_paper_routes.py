"""The three paper-trading POST routes — untested until now.

Picked as the next target after the admin mutations because they MUTATE and
because `paper_buy` has a history of silent defects that a green suite never
saw: paper positions stored with `target_price=None` could never sell and piled
up for weeks; paper cash silently drained to $271 so buys began failing with
nothing stored; and `_live_price` once resolved bare `BTC` to an unrelated $28
instrument, rendering a crypto holding at roughly -99.96%.

What is pinned here is the ROUTE's contract, not paper_trader's internals:
  * the authenticated chat_id is used, never one supplied in the body
  * stop_loss / target_price are FORWARDED — dropping them is exactly how a
    position becomes unsellable, and the route is the mini-app's only path in
  * a business rejection (no cash, no position) is a 200 with ok:false, not a
    500 — the mini-app renders the message
  * a SUCCESSFUL sale at a LOSS still reports ok:true

That last one guards a fragile coupling worth stating plainly: the route infers
success from `not msg.startswith("❌")`, while `paper_sell` builds a message
containing `"✅" if gain >= 0 else "❌"` on its proceeds line. It is correct
today only because the success message leads with `📄`. Reorder that template so
the emoji leads and every losing sale starts reporting as a failure. The test
below drives the REAL paper_trader, so it breaks if anyone does.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import paper_trader
import webhook
from tests.conftest import TEST_CHAT_ID


def _post(client, url, body):
    return client.post(url, json={"chat_id": TEST_CHAT_ID, **body})


class TestPaperBuyValidation:
    def test_unauthenticated_callers_buy_nothing(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda *a, **k: called.append(a) or "ok")
        monkeypatch.setattr(webhook, "_miniapp_auth", lambda: None)
        r = client.post("/api/miniapp/paper_buy", json={"ticker": "AAPL"})
        assert r.status_code == 403 and called == []

    def test_a_missing_ticker_is_rejected(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(paper_trader, "paper_buy", lambda *a, **k: called.append(a))
        r = _post(client, "/api/miniapp/paper_buy", {"ticker": "  "})
        assert r.status_code == 400 and called == []

    @pytest.mark.parametrize("bad", [0, -5, "abc", ""])
    def test_non_positive_or_garbage_shares_are_rejected(self, client, monkeypatch, bad):
        called = []
        monkeypatch.setattr(paper_trader, "paper_buy", lambda *a, **k: called.append(a))
        r = _post(client, "/api/miniapp/paper_buy", {"ticker": "AAPL", "shares": bad})
        assert r.status_code == 400, f"shares={bad!r} was accepted"
        assert called == []

    def test_shares_defaults_to_one_unit_when_absent(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda t, s, c, **k: seen.update(ticker=t, shares=s) or "📄 ok")
        r = _post(client, "/api/miniapp/paper_buy", {"ticker": "aapl"})
        assert r.status_code == 200
        assert seen == {"ticker": "AAPL", "shares": 1.0}


class TestPaperBuyForwardsTheLevels:
    """The documented defect class: a paper position stored without a target can
    never be sold by the managing job, so it accumulates silently forever."""

    def test_stop_and_target_reach_paper_trader(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda t, s, c, **k: seen.update(k) or "📄 ok")
        _post(client, "/api/miniapp/paper_buy",
              {"ticker": "AAPL", "shares": 2, "stop_loss": 90.5, "target_price": 120.25})
        assert seen.get("stop_loss") == 90.5, "stop_loss was dropped on the way in"
        assert seen.get("target_price") == 120.25, (
            "target_price was dropped — this position could never sell at target"
        )

    def test_unparseable_levels_degrade_to_none_rather_than_erroring(self, client, monkeypatch):
        """A junk level must not 500 the endpoint or be coerced to 0.0, which
        would read as a real level sitting at zero."""
        seen = {}
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda t, s, c, **k: seen.update(k) or "📄 ok")
        r = _post(client, "/api/miniapp/paper_buy",
                  {"ticker": "AAPL", "stop_loss": "not-a-number",
                   "target_price": None, "price": "junk"})
        assert r.status_code == 200
        assert seen.get("stop_loss") is None and seen.get("target_price") is None
        assert seen.get("price") is None


class TestPaperRoutesAreNotAnIDOR:
    """Every one of these acts on a chat_id. The body must never steer it."""

    @pytest.mark.parametrize("url,body", [
        ("/api/miniapp/paper_buy",   {"ticker": "AAPL", "shares": 1}),
        ("/api/miniapp/paper_sell",  {"ticker": "AAPL"}),
        ("/api/miniapp/paper_reset", {}),
    ])
    def test_the_authenticated_user_is_used_not_the_body(self, client, monkeypatch, url, body):
        seen = []
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda t, s, c, **k: seen.append(c) or "📄 ok")
        monkeypatch.setattr(paper_trader, "paper_sell",
                            lambda t, c, **k: seen.append(c) or "📄 ok")
        monkeypatch.setattr(paper_trader, "paper_reset", lambda c: seen.append(c))
        monkeypatch.setattr(webhook, "_miniapp_auth", lambda: "AUTHED_USER")
        r = client.post(url, json={**body, "chat_id": "VICTIM"})
        assert r.status_code == 200
        assert seen == ["AUTHED_USER"], (
            f"{url} acted as {seen!r} — a client-supplied chat_id must not steer it"
        )


class TestBusinessRejectionsAreNotErrors:
    def test_a_rejected_buy_is_a_200_with_ok_false(self, client, monkeypatch):
        """Insufficient paper cash is a normal outcome the mini-app renders —
        not a 500. This is the state the account was actually in at $271."""
        monkeypatch.setattr(paper_trader, "paper_buy",
                            lambda *a, **k: "❌ Insufficient paper cash.")
        r = _post(client, "/api/miniapp/paper_buy", {"ticker": "AAPL", "shares": 99})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is False and "Insufficient" in body["message"]

    def test_selling_something_you_do_not_hold_is_a_200_with_ok_false(self, client, monkeypatch):
        monkeypatch.setattr(paper_trader, "paper_sell",
                            lambda *a, **k: "❌ No open paper position for <b>AAPL</b>.")
        r = _post(client, "/api/miniapp/paper_sell", {"ticker": "AAPL"})
        assert r.status_code == 200 and r.get_json()["ok"] is False

    def test_sell_without_shares_means_sell_all(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(paper_trader, "paper_sell",
                            lambda t, c, **k: seen.update(k) or "📄 ok")
        _post(client, "/api/miniapp/paper_sell", {"ticker": "AAPL"})
        assert seen.get("shares") is None, "a missing shares must mean ALL, not 1"


class TestALosingSaleStillSucceeds:
    """Drives the REAL paper_trader, because the risk is in the message TEMPLATE.

    The route reads success as `not msg.startswith("❌")`, and paper_sell puts
    `"✅" if gain >= 0 else "❌"` in the body of that message. Correct only
    because the template leads with 📄. If that ever changes, every sale at a
    loss reports as a failure to the user while having actually gone through.
    """

    def test_a_real_sale_at_a_loss_reports_ok_true(self, client, monkeypatch):
        monkeypatch.setattr(paper_trader, "_live_price", lambda t: 100.0)
        buy = _post(client, "/api/miniapp/paper_buy", {"ticker": "AAPL", "shares": 10})
        assert buy.get_json().get("ok") is True, f"setup buy failed: {buy.get_json()}"

        monkeypatch.setattr(paper_trader, "_live_price", lambda t: 50.0)
        sell = _post(client, "/api/miniapp/paper_sell", {"ticker": "AAPL"})
        body = sell.get_json()
        assert sell.status_code == 200
        assert body["ok"] is True, (
            "a successful sale at a loss was reported as a FAILURE — the "
            f"success template no longer leads with a neutral marker: {body['message'][:60]!r}"
        )
        assert "-50" in body["message"] or "-5" in body["message"], (
            f"the loss is not reflected in the message: {body['message'][:80]!r}"
        )


class TestPaperReset:
    def test_unauthenticated_reset_is_refused(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(paper_trader, "paper_reset", lambda c: called.append(c))
        monkeypatch.setattr(webhook, "_miniapp_auth", lambda: None)
        r = client.post("/api/miniapp/paper_reset", json={})
        assert r.status_code == 403 and called == [], "an unauthenticated reset wiped a portfolio"
