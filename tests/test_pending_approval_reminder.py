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


def _canary(monkeypatch, payload, rows=True):
    """Drive the check against a fake backend that models BOTH tables.

    🔴 The original fake implemented ONLY `read_strict`, so the rows path did
    not exist to be got wrong — and the check read the wrong table in
    production for a month while every test here passed. `pending_users.json`
    is USER-KEYED, so on a row backend it lives in `user_records` and
    `read_strict` (which queries `documents`) finds nothing.

    With `rows=True` the payload is in the ROWS table and `documents` is EMPTY,
    which is the exact production shape. A stub narrower than production
    certifies bugs.
    """
    spec = importlib.util.spec_from_file_location("can_p", CAN)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    seen = []
    monkeypatch.setattr(m, "_check", lambda n, ok, d="", **k: seen.append((n, ok, d)))

    class _B:
        def supports_rows(self):
            return rows

        def read_all_users(self, _f):          # the `user_records` table
            if not rows:
                raise AssertionError("read_all_users called on a blob backend")
            if isinstance(payload, Exception):
                raise payload
            return payload

        def read_strict(self, _f):             # the `documents` table, STRICT
            if rows:
                return None                    # user-keyed data is NOT here
            if isinstance(payload, Exception):
                raise payload
            return payload

        def read(self, _f):                    # the SWALLOWING variant
            # Production's read() returns None on a transport error instead of
            # raising. Modelled so a caller that drops back to it is caught:
            # without this the swallowing mutation is unreachable in a test.
            if isinstance(payload, Exception):
                return None
            return None if rows else payload

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


class TestItReadsTheTableTheDataLivesIn:
    """🔴 THE BUG THIS FILE MISSED FOR A MONTH.

    `users.pending` printed "nobody waiting for approval" every day from
    2026-09-14 while a real person sat in the queue for 14 days. The check
    deliberately used `read_strict` to avoid a swallowing read reporting a
    false clean — and `read_strict` queries `documents`, while
    `pending_users.json` is user-keyed and lives as ROWS in `user_records`.
    One false clean traded for another.
    """

    def test_a_person_waiting_in_ROWS_is_reported(self, monkeypatch):
        m, seen = _canary(monkeypatch, {"42": _rec(14, first_name="Waiting")})
        m.check_pending_approvals()
        name, ok, detail = seen[-1]
        assert name == "users.pending"
        assert "nobody waiting" not in detail.lower(), (
            "read the documents table and reported a clean queue while a real "
            "person waited — the exact production failure")
        assert "14" in detail, "the AGE is what makes it actionable"

    def test_it_asks_user_records_not_documents(self, monkeypatch):
        """Pin the ROUTING, not just the outcome."""
        asked = []
        spec = importlib.util.spec_from_file_location("can_r", CAN)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        monkeypatch.setattr(m, "_check", lambda *a, **k: None)

        class _B:
            def supports_rows(self): return True
            def read_all_users(self, f): asked.append(("rows", f)); return {}
            def read_strict(self, f):    asked.append(("documents", f)); return None

        import storage
        monkeypatch.setattr(storage, "get_storage_backend", lambda: _B())
        m.check_pending_approvals()
        assert asked and asked[0][0] == "rows", f"queried {asked}"

    def test_a_blob_backend_still_uses_the_documents_table(self, monkeypatch):
        """On the Gist there are no rows — routing must not break the fallback."""
        m, seen = _canary(monkeypatch, {"7": _rec(3, first_name="Gisty")}, rows=False)
        m.check_pending_approvals()
        assert "nobody waiting" not in seen[-1][2].lower()

    def test_a_tombstone_still_does_not_count_as_waiting(self, monkeypatch):
        """A removal writes an EMPTY record on a row backend, not a deletion."""
        m, seen = _canary(monkeypatch, {"42": {}})
        m.check_pending_approvals()
        assert "nobody waiting" in seen[-1][2].lower()

    def test_an_unreadable_store_is_still_NOT_VERIFIED(self, monkeypatch):
        """The strictness the original was reaching for must survive the fix."""
        m, seen = _canary(monkeypatch, RuntimeError("supabase down"))
        m.check_pending_approvals()
        name, ok, detail = seen[-1]
        assert ok is True, "it must never fail — it would summon a healer"
        assert "NOT VERIFIED" in detail, (
            "an unreachable store must not read as a clean queue")

    def test_an_unreadable_DOCUMENTS_store_is_also_NOT_VERIFIED(self, monkeypatch):
        """The strict/swallowing distinction must hold on the blob path too.

        Caught by mutation: with only a rows-mode test, swapping read_strict()
        for the swallowing read() changed nothing observable, because the rows
        branch never reaches it.
        """
        m, seen = _canary(monkeypatch, RuntimeError("gist down"), rows=False)
        m.check_pending_approvals()
        name, ok, detail = seen[-1]
        assert ok is True
        assert "NOT VERIFIED" in detail, (
            "a swallowing read turns an unreachable store into a clean queue")

    def test_the_findings_reminder_routes_the_same_way(self, monkeypatch):
        """engine_findings_state.json is a DOCUMENT today, but the caller must
        not hardcode that — routing by file type is what keeps it correct if it
        ever becomes user-keyed."""
        src = CAN.read_text()
        i = src.index("def check_findings_awaiting")
        body = src[i:src.index("\ndef ", i + 10)]
        assert "_store_read_strict" in body
        assert "get_storage_backend().read_strict" not in body
