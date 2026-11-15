"""
pick_ledger.py — the pick ledger's ONE storage layer.

The ledger is append-only and kept forever: every pick, runner-up (control) and
tournament-arm pick, one row each, keyed by (date, ticker, arm). It is what
`scripts/evaluate_picks.py` grades and what the /admin actionability card joins
fills against.

🔴 WHY IT MOVED (2026-11-14). It used to be one Gist file per year. By November
`pick_ledger_2026.json` was 803 KB and growing ~15 KB a day — and the damage did
not wait for its own 1 MB limit. The Gist API returns files alphabetically and
stops sending content once the RESPONSE passes ~900 KB in total, so from
2026-11-02 every file sorting after the ledger came back truncated and EMPTY.
The synthetic bot read its own state that way, saw no positions, and saved the
emptiness back: twelve days of unmanaged positions behind green runs. Splitting
the file inside the same Gist would not have helped; the ledger had to leave it.

Now: ONE ROW PER MONTH in the primary store, under `LEDGER_FILE`.
  * Supabase — rows in `user_records` (chat_id = "YYYY-MM"), written with the
    table's compare-and-swap, so the evaluator and the tournament arms can
    append concurrently without one erasing the other. One query reads all.
  * Gist (a laptop with no SUPABASE_*) — one blob keyed by month, through the
    same `config_manager.mutate_user_record`.
Every write reads back and verifies its own rows: a lost ledger day cannot be
recovered, because the evaluator only ever reads TODAY's picks.

The legacy Gist files were copied, verified row by row and removed on
2026-11-14 (1,629 rows: 2026-08 276 · 09 427 · 10 633 · 11 293). Reads no longer
look for them; `include_legacy=True` and `migrate_legacy` remain only so the
move can be re-checked.
"""
from __future__ import annotations

import math
import os
import time

LEDGER_FILE = "pick_ledger_monthly.json"
LEGACY_GIST_FILES = ("pick_ledger.json", "pick_ledger_2026.json")


class LedgerUnreadable(RuntimeError):
    """The ledger could not be read. Never treat this as an empty ledger."""


# ── identity ────────────────────────────────────────────────────────────────

def row_key(r: dict) -> tuple:
    """Ledger identity. Includes the ARM so the same ticker picked on the same
    day by two different arms is two independent observations, not a duplicate."""
    return (r["date"], r["ticker"], r.get("arm"))


def month_of(r: dict) -> str | None:
    """The row's own month ("YYYY-MM"), from its ET pick date. Routing by the
    row's date, not the machine clock, means a run at 00:30 UTC on the 1st
    cannot file yesterday's picks under the wrong month."""
    d = r.get("date") if isinstance(r, dict) else None
    if isinstance(d, str) and len(d) >= 7 and d[4] == "-":
        return d[:7]
    return None


def _finite(v):
    """Postgres JSON rejects NaN/Infinity outright, so one non-finite metric
    inside a row's `screen` blob would fail the whole month's write."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


# ── backend ─────────────────────────────────────────────────────────────────

def _backend():
    """The primary store — and a refusal if it silently fell back.

    `get_storage_backend()` falls back to the Gist when Supabase cannot be
    verified at startup (it has, twice). For the ledger that would write the
    day's rows somewhere no reader looks. If SUPABASE_* is configured and we
    did not get Supabase, stop rather than fork the record.
    """
    from storage import get_storage_backend
    be = get_storage_backend()
    if (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY")
            and be.name() != "supabase"):
        raise LedgerUnreadable("Supabase is configured but storage fell back to the "
                               f"{be.name()} — refusing to read or write the ledger there")
    return be


def _gist():
    from storage import GistBackend
    return GistBackend()


# ── reading ─────────────────────────────────────────────────────────────────

def load_months(backend=None) -> dict:
    """{"YYYY-MM": [rows]} from the primary store. Raises LedgerUnreadable."""
    be = backend or _backend()
    try:
        raw = be.read_all_users(LEDGER_FILE) if be.supports_rows() \
            else (be.read_strict(LEDGER_FILE) or {})
    except LedgerUnreadable:
        raise
    except Exception as exc:
        raise LedgerUnreadable(f"{LEDGER_FILE}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LedgerUnreadable(f"{LEDGER_FILE} is a {type(raw).__name__}, not a mapping")
    out = {}
    for month, doc in raw.items():
        picks = (doc or {}).get("picks") if isinstance(doc, dict) else None
        out[str(month)] = [r for r in (picks or []) if isinstance(r, dict)]
    return out


def load_legacy() -> list:
    """Rows still sitting in the old Gist files. [] once they are removed."""
    rows = []
    for name in LEGACY_GIST_FILES:
        try:
            data = _gist().read_strict(name)
        except Exception as exc:
            raise LedgerUnreadable(f"legacy {name}: {type(exc).__name__}: {exc}") from exc
        if isinstance(data, dict):
            rows += [r for r in (data.get("picks") or []) if isinstance(r, dict)]
    return rows


def load_rows(include_legacy: bool = False, backend=None) -> list:
    """Every ledger row, oldest first, de-duplicated by key. Raises
    LedgerUnreadable rather than ever returning a partial ledger as if whole."""
    months = load_months(backend)
    merged = (load_legacy() if include_legacy else []) + \
        [r for m in sorted(months) for r in months[m]]
    seen, out = set(), []
    for r in merged:
        try:
            k = row_key(r)
        except (KeyError, TypeError):
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ── writing ─────────────────────────────────────────────────────────────────

def _read_month(be, month: str) -> list:
    if be.supports_rows():
        doc, _ver = be.read_user(LEDGER_FILE, month)
    else:
        doc = (be.read_strict(LEDGER_FILE) or {}).get(month)
    return [r for r in ((doc or {}).get("picks") or []) if isinstance(r, dict)]


def save_rows(rows: list, attempts: int = 3) -> bool:
    """Append `rows` to their months, keeping every row already stored.

    Merge-by-key on a FRESH read inside a compare-and-swap, so a concurrent
    writer is retried against, never overwritten — and re-saving a row that is
    already there is a no-op. Returns True only once every row has been READ
    BACK from the store. Never raises.
    """
    from config_manager import mutate_user_record, NO_WRITE
    groups: dict = {}
    for r in rows or []:
        m = month_of(r)
        if m is None:
            print(f"[ledger] row without a usable date skipped: {str(r)[:80]}")
            continue
        groups.setdefault(m, []).append(_finite(r))
    if not groups:
        return True

    ok_all = True
    for month, new in sorted(groups.items()):
        want = {row_key(r) for r in new}

        def _merge(doc, _new=new):
            doc = doc if isinstance(doc, dict) else {}
            have = [r for r in (doc.get("picks") or []) if isinstance(r, dict)]
            keys = {row_key(r) for r in have}
            add = [r for r in _new if row_key(r) not in keys]
            if not add:
                return NO_WRITE, 0
            return {**doc, "picks": have + add}, len(add)

        last = ""
        for attempt in range(1, attempts + 1):
            try:
                be = _backend()
                mutate_user_record(LEDGER_FILE, month, _merge, default={"picks": []})
                missing = want - {row_key(r) for r in _read_month(be, month)}
                if not missing:
                    break
                last = f"{len(missing)} row(s) absent on read-back"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * attempt)
        else:
            ok_all = False
            print(f"[ledger] 🚨 SAVE FAILED for {month}: {last} — these picks are "
                  f"NOT recorded and cannot be recovered.")
    return ok_all


# ── one-time move off the Gist ──────────────────────────────────────────────

def migrate_legacy(remove: bool = False) -> dict:
    """Copy the legacy Gist rows into the monthly store, verify EVERY one is
    there, and — only with `remove` and only on Supabase — delete the legacy
    Gist files. Idempotent: re-running copies nothing twice.

    Deleting is what actually shrinks the Gist. It is recoverable from the
    Gist's revision history, and it never happens unless the read-back proves
    the copy is complete."""
    be = _backend()
    legacy = load_legacy()
    report = {"backend": be.name(), "legacy_rows": len(legacy), "copied_ok": False,
              "missing": None, "removed": [], "by_month": {}}
    if not legacy:
        report.update(copied_ok=True, missing=0)
        return report
    report["copied_ok"] = save_rows(legacy)
    stored = load_months(be)
    have = {row_key(r) for rs in stored.values() for r in rs}
    report["missing"] = sum(1 for r in legacy if row_key(r) not in have)
    report["by_month"] = {m: len(rs) for m, rs in sorted(stored.items())}
    if not remove:
        return report
    if be.name() != "supabase":
        report["refused"] = ("not removing: the monthly rows are on the Gist too, so "
                             "deleting the legacy files would not shrink it")
        return report
    if not report["copied_ok"] or report["missing"]:
        report["refused"] = "not removing: the copy is not complete"
        return report
    import requests
    g = _gist()
    resp = requests.patch(g._url(), headers=g._headers(), timeout=30,
                          json={"files": {n: None for n in LEGACY_GIST_FILES}})
    resp.raise_for_status()
    for n in LEGACY_GIST_FILES:
        if _gist().read_strict(n) is None:
            report["removed"].append(n)
    return report
