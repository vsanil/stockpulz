"""🔴 DRY_RUN was documented as a safety and was not one.

`agent.py`'s module header says:

    DRY_RUN=true    → print message, don't send

It checked the flag at 13 call sites — but NOT in the path that does most of
the delivering. `agent._fanout` → `telegram_api.broadcast_all` never consulted
it, and `telegram_api.py` contained no reference to DRY_RUN at all. So the
documented guarantee held for `_alert()` and the morning outbox, and was
FICTION for confirmation, eod_summary, vix_check, macro_alert, pre_earnings,
weekly, week_ahead and the rest — every one of which fans out through
`_fanout`.

MEASURED 2026-09-05 while testing all 17 modes: running a mode with
`dry_run=true` would have messaged every real user. The only thing that
actually contained those runs was `OWNER_ONLY=1`, which narrows the RECIPIENT
LIST rather than stopping sends — a different property that happened to be
enough.

🔑 A flag whose guarantee depends on every caller remembering it is not a
guarantee. These tests pin it at the choke point, so "did I remember to check
DRY_RUN?" stops being a question anyone has to get right.
"""
import importlib

import pytest


@pytest.fixture
def tg(monkeypatch):
    import telegram_api
    importlib.reload(telegram_api)
    monkeypatch.setattr(telegram_api, "_bot_token", lambda: "t")
    monkeypatch.setattr(telegram_api, "_chat_id", lambda: "owner")
    monkeypatch.setattr(telegram_api, "_skip_test_user", lambda cid: False)

    # ⚠️ RECORD, do not raise. send_message wraps its POST in a retry loop with
    # a broad `except`, so an exception raised here is swallowed and the test
    # merely burns ~60s of retry backoff before reporting a misleading pass.
    posted = []

    class _Resp:
        status_code = 200
        text = "ok"
        headers: dict = {}

        @staticmethod
        def json():
            return {"ok": True, "result": {}}

    def _record(url=None, *a, **k):
        posted.append(url)
        return _Resp()

    monkeypatch.setattr(telegram_api.requests, "post", _record)
    telegram_api._posted = posted
    return telegram_api


class TestDryRunSuppressesEveryOutboundPath:
    def test_send_message(self, tg, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        assert tg.send_message("hi", chat_id="u1") is True
        assert tg._posted == [], "no HTTP call may leave the process"

    def test_send_inline_keyboard(self, tg, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        assert tg.send_inline_keyboard("hi", [[{"text": "x"}]], chat_id="u1") is True
        assert tg._posted == [], "no HTTP call may leave the process"

    def test_send_photo(self, tg, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        assert tg.send_photo(b"png", "cap", chat_id="u1") is True
        assert tg._posted == [], "no HTTP call may leave the process"

    def test_typing_action_does_not_even_appear_to_type(self, tg, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        assert tg.send_typing_action(chat_id="u1") is None
        assert tg._posted == [], "no HTTP call may leave the process"

    def test_broadcast_all_the_path_the_flag_used_to_miss(self, tg, monkeypatch):
        """🔴 THE regression. _fanout sends here, and this never checked DRY_RUN."""
        monkeypatch.setenv("DRY_RUN", "true")
        out = tg.broadcast_all([{"chat_id": "a", "text": "x"},
                                {"chat_id": "b", "text": "y"}])
        assert out == {"a": True, "b": True}, \
            "must report success-shaped results so callers' counters stay sane"
        assert tg._posted == [], "no HTTP call may leave the process"


class TestItIsOffByDefault:
    """A safety that is on by default is an outage."""

    @pytest.mark.parametrize("val", ["", "false", "0", "no", None])
    def test_unset_or_falsey_still_sends(self, tg, monkeypatch, val):
        if val is None:
            monkeypatch.delenv("DRY_RUN", raising=False)
        else:
            monkeypatch.setenv("DRY_RUN", val)
        tg.send_message("hi", chat_id="u1")
        assert tg._posted, "with DRY_RUN off a real send must still happen"

    @pytest.mark.parametrize("val", ["true", "TRUE", " True ", "1", "yes"])
    def test_truthy_spellings_all_suppress(self, tg, monkeypatch, val):
        monkeypatch.setenv("DRY_RUN", val)
        assert tg.send_message("hi", chat_id="u1") is True
        assert tg._posted == [], f"{val!r} must suppress the send, not just return True"


class TestReadAtCallTimeNotImportTime:
    def test_a_value_set_after_import_is_honoured(self, tg, monkeypatch):
        """agent.py binds its own DRY_RUN at import. If this module did the
        same, a flag set later (tests, a wrapper, a re-exec) would be ignored —
        and the failure would be silent sends, not an error."""
        monkeypatch.delenv("DRY_RUN", raising=False)
        tg.send_message("hi", chat_id="u1")
        assert len(tg._posted) == 1, "sends while unset"
        monkeypatch.setenv("DRY_RUN", "true")
        assert tg.send_message("hi", chat_id="u1") is True
        assert len(tg._posted) == 1, "same import, now suppressed — no new POST"


class TestTheFanoutPathIsCoveredEndToEnd:
    """Belt and braces: exercise agent._fanout itself, not just broadcast_all,
    because the defect was the WIRING between them."""

    def test_fanout_sends_nothing_under_dry_run(self, monkeypatch):
        import sys
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "owner")
        monkeypatch.setenv("OWNER_ONLY", "1")
        if "agent" in sys.modules:
            del sys.modules["agent"]
        import agent
        import telegram_api

        posted = []
        monkeypatch.setattr(telegram_api.requests, "post",
                            lambda url=None, *a, **k: posted.append(url))
        monkeypatch.setattr(telegram_api, "_bot_token", lambda: "t")
        monkeypatch.setattr(telegram_api, "_skip_test_user", lambda cid: False)

        agent._fanout(lambda uid: "a message", tag="test")
        assert posted == [], f"a real send escaped DRY_RUN via _fanout: {posted}"
