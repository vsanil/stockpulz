"""
position_audit.py — arithmetic integrity checks on stored positions.

Why this exists
---------------
The synthetic bot's value has been entirely as a BUG DETECTOR, not as evidence
about pick quality. Its trade data has now surfaced three defects that no
win-rate calculation and no mocked test would ever have shown, because each one
only appears when a real position is created by the real code path against live
prices:

  * paper positions stored with target_price=None — they could never sell
  * paper cash drained to $271, so paper_buy silently started rejecting
  * two AMBA positions whose TARGET sat BELOW their ENTRY — a long trade that
    is mathematically incapable of winning

The third was found by hand. This module is that check, run every day instead.

Design
------
Findings are DERIVED from current state on every run, never accumulated in a
file. A stale worklist is worse than none: it rots, and people stop reading it.
The only thing persisted is the owner's DISPOSITION (resolved / ignored), which
is overlaid at render time.

A finding on a LIVE position that is marked resolved but is still present next
run REOPENS — otherwise "resolved" quietly becomes "hidden", which is exactly
how a defect gets forgotten. Findings about closed trades are historical: they
cannot be fixed retroactively, so acknowledging them is final.
"""
from __future__ import annotations

import hashlib


def _num(x):
    try:
        if x is None:
            return None
        v = float(x)
        return v if v == v else None          # NaN is not a number here
    except (TypeError, ValueError):
        return None


def _fid(*parts) -> str:
    """Stable id, so the same defect is one row across runs rather than a new
    one each day (which is how a findings list becomes unreadable)."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _check_one(pos: dict, account: str, live: bool, entry_key: str = "entry_price") -> list[dict]:
    """Arithmetic that must hold for ANY long position, regardless of strategy."""
    out = []
    tkr = (pos.get("ticker") or "?").upper()
    when = pos.get("opened_date") or pos.get("bought_date") or ""
    entry = _num(pos.get(entry_key))
    tgt = _num(pos.get("target_price"))
    stop = _num(pos.get("stop_loss"))
    shares = _num(pos.get("shares"))

    def add(check, detail):
        # 🔴 The id must distinguish two positions in the SAME ticker opened on
        # the SAME day — the bot opens those routinely (it scaled into AMBA twice
        # on 2026-08-03). Hashing only (check, account, ticker, date) collided,
        # so resolving one finding silently resolved the other. The levels are
        # what the finding is ABOUT, so they belong in its identity; if a level
        # is corrected the id changes, which is right — that is a different
        # finding, and the old disposition should not carry over.
        out.append({
            "id": _fid(check, account, tkr, when, entry, tgt, stop, shares),
            "check": check,
            "account": str(account),
            "ticker": tkr,
            "opened": when,
            "live": bool(live),
            "severity": "high" if live else "info",
            "detail": detail,
        })

    if entry is None or entry <= 0:
        # Every %-based alert, nudge and P&L number divides by entry.
        add("entry.missing", f"entry price is {pos.get(entry_key)!r} — all %-based "
                             f"alerts and P&L for this position are dead")
        return out                                   # nothing else is meaningful

    if tgt is not None and tgt <= entry:
        add("levels.target_below_entry",
            f"target ${tgt:,.2f} is at or below entry ${entry:,.2f} — this long "
            f"position cannot reach its target, so it can only ever close at a loss")
    if stop is not None and stop >= entry:
        add("levels.stop_above_entry",
            f"stop ${stop:,.2f} is at or above entry ${entry:,.2f} — born stopped-out; "
            f"the next check will close it immediately")
    if tgt is not None and stop is not None and tgt <= stop:
        add("levels.target_below_stop",
            f"target ${tgt:,.2f} is at or below stop ${stop:,.2f} — the levels are inverted")
    if live and shares is not None and shares <= 0:
        add("shares.non_positive",
            f"shares is {shares} — position holds nothing but still occupies the log")
    return out


def audit_account(chat_id: str, trade_log: dict, paper: dict | None = None) -> list[dict]:
    """All findings for one account. Pure — callers supply the data."""
    findings: list[dict] = []
    log = trade_log or {}
    for p in log.get("open", []) or []:
        findings += _check_one(p, chat_id, live=True)
    for p in log.get("closed", []) or []:
        findings += _check_one(p, chat_id, live=False)
    for p in (paper or {}).get("positions", []) or []:
        findings += _check_one(p, chat_id, live=True, entry_key="avg_price")
    return findings


def apply_dispositions(findings: list[dict], dispositions: dict) -> list[dict]:
    """Overlay the owner's resolved/ignored marks.

    A LIVE finding marked resolved that is STILL PRESENT is reopened: otherwise
    'resolved' silently means 'hidden', and the defect is still in production.
    Historical findings (closed trades) cannot be fixed retroactively, so an
    acknowledgement there is final.
    """
    out = []
    for f in findings:
        d = (dispositions or {}).get(f["id"]) or {}
        status = d.get("status", "open")
        f = dict(f)
        f["note"] = d.get("note", "")
        f["updated_at"] = d.get("updated_at", "")
        if status == "resolved" and f["live"]:
            f["status"] = "reopened"
            f["detail"] += "  ·  marked resolved but STILL PRESENT — not actually fixed"
        else:
            f["status"] = status
        out.append(f)
    # worst first: reopened, then open live, then open historical, then handled
    rank = {"reopened": 0, "open": 1, "ignored": 2, "resolved": 3}
    out.sort(key=lambda f: (rank.get(f["status"], 9), not f["live"], f["ticker"]))
    return out


def summarise(findings: list[dict]) -> dict:
    live_open = [f for f in findings
                 if f.get("status") in ("open", "reopened") and f.get("live")]
    return {
        "total": len(findings),
        "live_open": len(live_open),
        "reopened": sum(1 for f in findings if f.get("status") == "reopened"),
        "by_check": {c: sum(1 for f in findings if f["check"] == c)
                     for c in sorted({f["check"] for f in findings})},
    }
