"""Somebody waiting for access must not be forgotten.

🔴 A real person requested access and waited TEN DAYS. The notification WORKED
— a DM with one-tap Approve/Reject fires at request time — and then nothing
mentioned it again. Same one-shot failure as the unmerged self-heal branches
and the awaiting findings, both of which already have standing reminders.

This one costs more: the requester is TOLD "usually within a few hours", so
the broken promise lands on a stranger trying the product.
"""
import datetime as dt
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAN = ROOT / "scripts" / "canary.py"


def _canary(monkeypatch, payload):
    spec = importlib.util.spec_from_file_location("can_p", CAN)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    seen = []
    monkeypatch.setattr(m, "_check", lambda n, ok, d="", **k: seen.append((n, ok, d)))

    class _B:
        def read_strict(self, _f):
            if isinstance(payload, Exception):
                raise payload
            return payload

    import storage
    monkeypatch.setattr(storage, "get_storage_backend", lambda: _B())
    return m, seen


def _rec(days_ago, **kw):
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return {"requested_at": when.isoformat(), **kw}


class TestItCannotFeedItself:
    """A canary failure triggers self_heal, which writes a branch — and
    self-heal cannot approve a user, so it would burn credits on something no
    code change can fix."""

    def _fn(self):
        src = CAN.read_text()
        i = src.index("def check_pending_approvals")
        return src[i:src.index("def check_storage_surfaces", i)]

    def test_it_never_reports_a_failure(self):
        import re
        for m in re.finditer(r'_check\("users\.pending",\s*(\w+)', self._fn()):
            assert m.group(1) == "True", \
                "a failing reminder would summon a healer that cannot fix it"

    def test_it_is_listed_as_owner_only(self):
        import owner_only_checks as o
        assert "users.pending" in o.OWNER_ONLY_CHECKS

    def test_it_is_registered(self):
        assert "check_pending_approvals," in CAN.read_text()


class TestWhatItReports:
    def test_it_is_silent_when_nobody_is_waiting(self, monkeypatch):
        """A check that speaks every day trains you to ignore it."""
        m, seen = _canary(monkeypatch, {})
        m.check_pending_approvals()
        assert seen and seen[0][1] is True
        assert "nobody waiting" in seen[0][2]

    def test_it_names_the_oldest_age(self, monkeypatch):
        """'10 days' is the fact that makes it actionable — a bare count does not."""
        m, seen = _canary(monkeypatch, {"1": _rec(10, username="aymaan"),
                                        "2": _rec(2, first_name="Bob")})
        m.check_pending_approvals()
        note = seen[0][2]
        assert "oldest 10d" in note and "2 person" in note

    def test_it_repeats_the_promise_that_was_made(self, monkeypatch):
        """The requester was told 'within a few hours'. That is why the delay
        matters, and the reminder should say so."""
        m, seen = _canary(monkeypatch, {"1": _rec(10)})
        assert "few hours" in (m.check_pending_approvals() or seen[0][2])


class TestTheTrapsItAvoids:
    def test_an_unreadable_store_is_NOT_VERIFIED_not_a_clean_pass(self, monkeypatch):
        """get_pending_users() ends in `or {}`, so an unreadable store would
        report 'nobody waiting' — the false pass check_findings_awaiting
        shipped with."""
        m, seen = _canary(monkeypatch, OSError("supabase down"))
        m.check_pending_approvals()
        assert seen[0][1] is True, "must never fail, even unreadable"
        assert "NOT VERIFIED" in seen[0][2]
        assert "nobody waiting" not in seen[0][2]

    def test_it_does_not_use_the_swallowing_reader(self):
        import io, tokenize
        src = CAN.read_text()
        i = src.index("def check_pending_approvals")
        body = src[i:src.index("def check_storage_surfaces", i)]
        # ⚠️ Strip comments/strings: the comment explaining the removal NAMES
        # get_pending_users. Twelfth time that trap has appeared here.
        code = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(body).readline):
                if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                    code.append(tok.string)
        except (tokenize.TokenError, IndentationError):
            code = [ln.split("#", 1)[0] for ln in body.splitlines()]
        src_code = " ".join(code)
        assert "read_strict" in src_code
        assert "get_pending_users" not in src_code

    def test_a_TOMBSTONE_is_not_somebody_waiting(self, monkeypatch):
        """On a row backend a removal writes an EMPTY record rather than
        deleting the row. Counting it would report a person who was already
        approved 10 days ago as still waiting."""
        m, seen = _canary(monkeypatch, {"1": {}, "2": {}})
        m.check_pending_approvals()
        assert "nobody waiting" in seen[0][2]

    def test_the_age_is_read_on_the_clock_the_WRITER_used(self):
        """add_pending_user stamps datetime.now(timezone.utc). An et_today()
        comparison here would be the mismatch, not the fix — the clock class
        has bitten six times."""
        import io, tokenize
        src = CAN.read_text()
        i = src.index("def check_pending_approvals")
        body = src[i:src.index("def check_storage_surfaces", i)]
        code = []
        for tok in tokenize.generate_tokens(io.StringIO(body).readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                code.append(tok.string)
        s = " ".join(code)
        assert "timezone" in s and "utc" in s.lower()
        assert "et_today" not in s

    def test_a_missing_timestamp_still_counts_the_person(self, monkeypatch):
        """An unparseable date must not make somebody disappear from the count."""
        m, seen = _canary(monkeypatch, {"1": {"username": "x"}})
        m.check_pending_approvals()
        assert "1 person" in seen[0][2]
