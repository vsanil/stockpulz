"""
test_telegram_api.py — Unit tests for telegram_api.py

Coverage:
  - _strip_html     removes tags and unescapes entities
  - send_message    succeeds on first try, falls back to plain text on HTML error,
                    retries on transient failures, returns False when all attempts fail
  - broadcast_all   sends to all entries in parallel, returns count
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import telegram_api
from telegram_api import _strip_html, send_message, broadcast_all


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ok_response():
    """Return a mock requests.Response that looks like a 200 OK."""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "{}"
    return resp


def _err_response(code=500, text="Internal Server Error"):
    resp = MagicMock()
    resp.status_code = code
    resp.text = text
    return resp


def _html_parse_error_response():
    resp = MagicMock()
    resp.status_code = 400
    resp.text = "Bad Request: can't parse entities: parse entities"
    return resp


# ─────────────────────────────────────────────────────────────────────────────
class TestStripHtml:
    """_strip_html removes all HTML tags and unescapes entities."""

    def test_removes_bold_tags(self):
        assert _strip_html("<b>hello</b>") == "hello"

    def test_removes_italic_tags(self):
        assert _strip_html("<i>world</i>") == "world"

    def test_removes_code_tags(self):
        assert _strip_html("<code>NVDA</code>") == "NVDA"

    def test_removes_nested_tags(self):
        assert _strip_html("<b><i>bold italic</i></b>") == "bold italic"

    def test_unescapes_amp(self):
        assert _strip_html("AT&amp;T") == "AT&T"

    def test_unescapes_lt_gt(self):
        assert _strip_html("&lt;b&gt;") == "<b>"

    def test_plain_text_unchanged(self):
        assert _strip_html("hello world") == "hello world"

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_mixed_content(self):
        result = _strip_html("<b>AAPL</b> is up <i>+2%</i>")
        assert result == "AAPL is up +2%"

    def test_tg_spoiler_tag_removed(self):
        result = _strip_html("<tg-spoiler>hidden text</tg-spoiler>")
        assert "hidden text" in result
        assert "<" not in result


# ─────────────────────────────────────────────────────────────────────────────
class TestSendMessage:
    """send_message retry and fallback logic."""

    def test_success_on_first_attempt(self):
        with patch("telegram_api.requests.post", return_value=_ok_response()) as mock_post:
            result = send_message("Hello", chat_id="12345")
        assert result is True
        mock_post.assert_called_once()

    def test_uses_provided_chat_id(self):
        captured = []
        def _capture(url, json=None, timeout=None):
            captured.append(json)
            return _ok_response()
        with patch("telegram_api.requests.post", side_effect=_capture):
            send_message("Hello", chat_id="99999")
        assert captured[0]["chat_id"] == "99999"

    def test_falls_back_to_env_chat_id_when_none(self):
        captured = []
        def _capture(url, json=None, timeout=None):
            captured.append(json)
            return _ok_response()
        with patch("telegram_api.requests.post", side_effect=_capture):
            send_message("Hello")
        # Should use TELEGRAM_CHAT_ID from env (set to "9999999" by conftest)
        assert captured[0]["chat_id"] == os.environ["TELEGRAM_CHAT_ID"]

    def test_html_fallback_on_parse_error(self):
        """On a 400 HTML parse error, retries once as plain text."""
        responses = [_html_parse_error_response(), _ok_response()]
        call_count = [0]
        def _side_effect(url, json=None, timeout=None):
            r = responses[call_count[0]]
            call_count[0] += 1
            return r
        with patch("telegram_api.requests.post", side_effect=_side_effect):
            result = send_message("<b>bad html</b>", chat_id="12345")
        assert result is True
        assert call_count[0] == 2   # first HTML attempt + plain text fallback

    def test_retries_on_server_error(self):
        """Retries up to MAX_RETRIES times on server errors."""
        responses = [_err_response(500)] * (telegram_api.MAX_RETRIES - 1) + [_ok_response()]
        call_count = [0]
        def _side_effect(url, json=None, timeout=None):
            r = responses[call_count[0]]
            call_count[0] += 1
            return r
        with patch("telegram_api.requests.post", side_effect=_side_effect), \
             patch("telegram_api.time.sleep"):   # don't actually sleep in tests
            result = send_message("Hello", chat_id="12345")
        assert result is True

    def test_returns_false_when_all_attempts_fail(self):
        with patch("telegram_api.requests.post", return_value=_err_response(500)), \
             patch("telegram_api.time.sleep"):
            result = send_message("Hello", chat_id="12345")
        assert result is False

    def test_returns_false_on_exception(self):
        with patch("telegram_api.requests.post", side_effect=Exception("network error")), \
             patch("telegram_api.time.sleep"):
            result = send_message("Hello", chat_id="12345")
        assert result is False

    def test_long_message_split_into_chunks(self):
        """Messages longer than MAX_MESSAGE_LENGTH must be split and each chunk sent."""
        long_text = "A" * (telegram_api.MAX_MESSAGE_LENGTH + 500)
        sent_payloads = []
        def _capture(url, json=None, timeout=None):
            sent_payloads.append(json)
            return _ok_response()
        with patch("telegram_api.requests.post", side_effect=_capture):
            result = send_message(long_text, chat_id="12345")
        assert result is True
        assert len(sent_payloads) >= 2   # message was split

    def test_short_message_sent_as_single_chunk(self):
        sent_payloads = []
        def _capture(url, json=None, timeout=None):
            sent_payloads.append(json)
            return _ok_response()
        with patch("telegram_api.requests.post", side_effect=_capture):
            send_message("Short message", chat_id="12345")
        assert len(sent_payloads) == 1


# ─────────────────────────────────────────────────────────────────────────────
class TestBroadcastAll:
    """broadcast_all sends to every entry in the outbox.

    broadcast_all returns dict[str, bool] — {chat_id: success}.
    Uses aiohttp async path when available; falls back to sync send_message.
    We force the sync fallback by raising ImportError on aiohttp import so
    we can intercept requests.post cleanly in tests.
    """

    def _sync_ctx(self):
        """Context manager that forces broadcast_all into the sync fallback path."""
        import sys
        import builtins
        _real_import = builtins.__import__

        def _no_aiohttp(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("aiohttp mocked away")
            return _real_import(name, *args, **kwargs)

        return patch("builtins.__import__", side_effect=_no_aiohttp)

    def test_empty_outbox_returns_empty_dict(self):
        result = broadcast_all([])
        assert result == {}

    def test_single_entry_success(self):
        outbox = [{"chat_id": "111", "text": "Hello", "keyboard": None}]
        with self._sync_ctx(), \
             patch("telegram_api.requests.post", return_value=_ok_response()):
            result = broadcast_all(outbox)
        assert isinstance(result, dict)
        assert "111" in result
        assert result["111"] is True

    def test_multiple_entries_all_attempted(self):
        outbox = [
            {"chat_id": "111", "text": "Pick 1", "keyboard": None},
            {"chat_id": "222", "text": "Pick 2", "keyboard": None},
            {"chat_id": "333", "text": "Pick 3", "keyboard": None},
        ]
        with self._sync_ctx(), \
             patch("telegram_api.requests.post", return_value=_ok_response()):
            result = broadcast_all(outbox)
        assert isinstance(result, dict)
        assert set(result.keys()) == {"111", "222", "333"}

    def test_multiple_entries_all_succeed(self):
        outbox = [
            {"chat_id": "111", "text": "Pick 1", "keyboard": None},
            {"chat_id": "222", "text": "Pick 2", "keyboard": None},
        ]
        with self._sync_ctx(), \
             patch("telegram_api.requests.post", return_value=_ok_response()):
            result = broadcast_all(outbox)
        assert all(v is True for v in result.values())

    def test_returns_dict(self):
        result = broadcast_all([])
        assert isinstance(result, dict)


class TestRetryAfter:
    """429 backpressure: honor Telegram's requested cool-off, capped."""

    class _Resp:
        def __init__(self, body=None, headers=None, raise_json=False):
            self._body = body or {}
            self.headers = headers or {}
            self._raise = raise_json
        def json(self):
            if self._raise:
                raise ValueError("no json")
            return self._body

    def test_from_json_body(self):
        from telegram_api import _retry_after_secs
        assert _retry_after_secs(self._Resp(body={"parameters": {"retry_after": 7}})) == 7

    def test_from_header_when_body_missing(self):
        from telegram_api import _retry_after_secs
        assert _retry_after_secs(self._Resp(body={}, headers={"Retry-After": "12"})) == 12

    def test_capped_at_max(self):
        from telegram_api import _retry_after_secs, MAX_RETRY_AFTER
        assert _retry_after_secs(self._Resp(body={"parameters": {"retry_after": 9999}})) == MAX_RETRY_AFTER

    def test_falls_back_to_default(self):
        from telegram_api import _retry_after_secs, RETRY_DELAY
        got = _retry_after_secs(self._Resp(raise_json=True))
        assert got == max(1, min(RETRY_DELAY, 30))
