"""Delivery: the send paths every user-facing message goes through.

telegram_api was at 49% while carrying all 37 per-user send sites plus every
fired price alert. The two behaviours worth pinning are the ones with a
documented production history: 429 backpressure (a blind fixed retry made the
bot hammer Telegram during rate limits) and the synthetic-account short-circuit
(a dead chat_id burns ~10s of retries per send and masks a REAL user who blocked
the bot).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import telegram_api


class _Resp:
    def __init__(self, payload=None, headers=None, status=200):
        self._p, self.headers, self.status_code = payload or {}, headers or {}, status
        self.ok = status < 400
        self.text = str(payload)
    def json(self):
        return self._p


class TestRetryAfter:
    """Telegram states its own cool-off. Ignoring it and retrying on a fixed
    delay is how a rate-limited bot keeps making it worse."""

    def test_prefers_the_json_body(self):
        r = _Resp({"parameters": {"retry_after": 7}}, status=429)
        assert telegram_api._retry_after_secs(r) == 7

    def test_falls_back_to_the_header(self):
        r = _Resp({}, headers={"Retry-After": "5"}, status=429)
        assert telegram_api._retry_after_secs(r) == 5

    def test_body_wins_over_header(self):
        r = _Resp({"parameters": {"retry_after": 9}},
                  headers={"Retry-After": "2"}, status=429)
        assert telegram_api._retry_after_secs(r) == 9

    def test_is_capped_so_one_user_cannot_stall_a_worker(self):
        r = _Resp({"parameters": {"retry_after": 99999}}, status=429)
        assert telegram_api._retry_after_secs(r) == telegram_api.MAX_RETRY_AFTER

    def test_never_returns_zero_or_negative(self):
        for payload in ({"parameters": {"retry_after": 0}},
                        {"parameters": {"retry_after": -5}}):
            assert telegram_api._retry_after_secs(_Resp(payload, status=429)) >= 1

    @pytest.mark.parametrize("junk", [{}, {"parameters": {}},
                                      {"parameters": {"retry_after": "abc"}}])
    def test_garbage_falls_back_to_the_default(self, junk):
        got = telegram_api._retry_after_secs(_Resp(junk, status=429))
        assert 1 <= got <= telegram_api.MAX_RETRY_AFTER

    def test_a_response_that_raises_on_json_is_survivable(self):
        class _Bad:
            headers = {}
            def json(self):
                raise ValueError("not json")
        assert telegram_api._retry_after_secs(_Bad()) >= 1


class TestTestUserShortCircuit:
    """A virtual chat_id has no Telegram user behind it. Sending returns HTTP
    400 'chat not found', which misses the 429/parse-entity branches and burns
    ~10s per send across every per-user site."""

    def test_a_test_user_is_skipped(self, monkeypatch):
        import config_manager
        monkeypatch.setattr(config_manager, "is_test_user", lambda cid: True)
        assert telegram_api._skip_test_user("900000001") is True

    def test_a_real_user_is_not(self, monkeypatch):
        import config_manager
        monkeypatch.setattr(config_manager, "is_test_user", lambda cid: False)
        assert telegram_api._skip_test_user("1699321994") is False

    def test_it_fails_OPEN_if_the_lookup_breaks(self, monkeypatch):
        """A broken lookup must not silently stop delivery to real users —
        better to attempt a send than to drop everyone's picks."""
        import config_manager
        def _boom(cid):
            raise RuntimeError("gist down")
        monkeypatch.setattr(config_manager, "is_test_user", _boom)
        assert telegram_api._skip_test_user("1699321994") is False

    def test_send_message_makes_NO_network_call_for_a_test_user(self, monkeypatch):
        import config_manager
        monkeypatch.setattr(config_manager, "is_test_user", lambda cid: True)
        calls = []
        monkeypatch.setattr(telegram_api.requests, "post",
                            lambda *a, **k: calls.append(a) or _Resp({"ok": True}))
        telegram_api.send_message("hello", chat_id="900000001")
        assert calls == [], "a test-user send must not hit the network"


class TestStripHtml:
    """Telegram rejects malformed entities with 400; the fallback re-sends as
    plain text, so the stripper must not itself mangle the message."""

    def test_tags_are_removed_but_text_survives(self):
        out = telegram_api._strip_html("<b>NVDA</b> is <i>up</i> 5%")
        assert "NVDA" in out and "up" in out and "<b>" not in out

    def test_plain_text_is_untouched(self):
        assert telegram_api._strip_html("no tags here") == "no tags here"

    def test_empty_is_safe(self):
        assert telegram_api._strip_html("") == ""
