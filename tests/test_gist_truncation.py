"""GitHub truncates gist file content over ~1 MB and hands back a raw_url.

Reading `content` blindly then yields PARTIAL JSON — which on the swallowing
read() path looks like "file missing" and invites a clobber of everyone's data.
At current per-user sizes (~11-13 KB) user_trades/price_alerts cross 1 MB around
75-90 users, so this is a real wall.
"""
import json
import storage


class _Resp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


class TestGistContent:
    def test_untruncated_uses_inline_content(self, monkeypatch):
        called = []
        monkeypatch.setattr(storage.requests, "get",
                            lambda *a, **k: called.append(1) or _Resp("{}"))
        meta = {"content": '{"a":1}', "truncated": False}
        assert storage._gist_content(meta, {}) == '{"a":1}'
        assert not called, "must not hit the network when content is complete"

    def test_truncated_refetches_full_body_from_raw_url(self, monkeypatch):
        full = json.dumps({"u1": {"open": [1, 2, 3]}})
        monkeypatch.setattr(storage.requests, "get", lambda *a, **k: _Resp(full))
        meta = {"content": '{"u1": {"op',        # partial — would fail json.loads
                "truncated": True,
                "raw_url": "https://gist.githubusercontent.com/raw/x"}
        got = storage._gist_content(meta, {})
        assert json.loads(got) == {"u1": {"open": [1, 2, 3]}}

    def test_truncated_without_raw_url_falls_back(self, monkeypatch):
        monkeypatch.setattr(storage.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
        meta = {"content": "partial", "truncated": True}   # no raw_url
        assert storage._gist_content(meta, {}) == "partial"
