#!/usr/bin/env python3
"""
run_arms.py — THE TOURNAMENT. Engine variants traded side by side, every day,
against each other and against the index.

Why it exists
-------------
Comparing "engine v1 in July" with "engine v2 in August" measures July against
August. Arms remove that confound by running every variant on the SAME day
against the SAME market. What arms cannot fix is sample size — only the picks
where arms DISAGREE carry information — which is why the arms are now disjoint
BY CONSTRUCTION rather than by hope: `screener.eligible_candidates` filters each
technical arm to a different `setup_type`, and that function returns exactly one
label per candidate. `quality` trades the long-term pool only, so it cannot
intersect either of them. Before this they were weight variants of one pool and
overlapped the default 44-63%.

The four rules this file exists to keep
---------------------------------------
1. **ONE TRADER.** Every arm opens and manages through
   `synthetic_user.phase_open` / `_manage_account` — the same sizing, the same
   hard book rules, the same entry-window obedience, the same time stops, the
   same SPY twin. If an arm had its own buying code the standings would compare
   execution as much as selection, and the experiment would be worthless.
2. **ONLY THE LIVE ARM CALLS CLAUDE** (owner's Option B, 2026-09-19). Arms here
   are selected deterministically by `arm_selector` from the screener's own
   ranking. That is ~$60/month instead of ~$200, and it isolates the one thing
   never measured: what the model's selection adds over taking the top N.
3. **PAPER ONLY, OWN ACCOUNT, NEVER picks.json.** An arm cannot touch a real
   user's data or change what anyone is recommended.
4. **THE BENCHMARK IS NOT HANDICAPPED.** `spy_hold` deploys its whole book into
   the index, because capping it the way a diversified stock book is capped
   would flatter every other arm. The owner accepted in advance that this arm
   winning is a legitimate outcome.

Cost note: each screener arm is a full 600-ticker screen and NO Claude call.
Arms run SEQUENTIALLY — three concurrent screens invite rate limiting, and an
arm running on degraded data measures the API rather than the strategy.

Usage:
    python scripts/run_arms.py --phase open   [--arms breakout,pullback] [--dry-run]
    python scripts/run_arms.py --phase manage [--dry-run]
    python scripts/run_arms.py --phase reset  --arms breakout   # deliberate restart
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_manager import ARM_CHAT_IDS, et_today, get_config, get_allowed_users
from screener import STRATEGIES

PASSIVE_ARM = "spy_hold"
BENCHMARK = "SPY"

_SU_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthetic_user.py")


def _su():
    """The ONE trader. Loaded by path because it is a script, not a package."""
    spec = importlib.util.spec_from_file_location("synthetic_user", _SU_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ev():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evaluate_picks.py")
    spec = importlib.util.spec_from_file_location("evaluate_picks", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pos(x) -> bool:
    try:
        import math
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def _account_for(name: str) -> str:
    """Resolve an arm's account, refusing point-blank to aim at a real one.

    ⚠️ Checking `is_test_user()` here would be CIRCULAR — `get_test_users()` is
    DERIVED from ARM_CHAT_IDS, so anything listed is a test user by definition
    and the guard could never fire. The allowlist is the independent fact: a
    real user is someone who receives messages.
    """
    chat_id = ARM_CHAT_IDS[name]
    owner = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if str(chat_id) in set(get_allowed_users()) or str(chat_id) == owner:
        raise SystemExit(f"[arms] REFUSING: arm '{name}' points at real account "
                         f"{chat_id}. Arms paper-trade; they must never touch a "
                         f"live user's data.")
    return str(chat_id)


# ── the passive arm ──────────────────────────────────────────────────────────

def open_passive(chat_id: str, dry: bool) -> list[str]:
    """Buy the index once with the whole book, then hold it forever.

    This is the alternative every other arm has to beat for the product to mean
    anything, so it is deliberately NOT subjected to the diversified-book rules:
    an 80% deployment cap on a one-position index arm would handicap the
    benchmark and flatter everything measured against it.

    Idempotent — once it holds SPY it does nothing, which is the whole strategy.
    """
    from market_data import get_live_price
    from paper_trader import load_user_paper, paper_buy
    import sim_portfolio as sp

    today = et_today().isoformat()
    paper = load_user_paper(chat_id)
    if any(p.get("ticker") == BENCHMARK for p in paper.get("positions") or []):
        return []                       # already invested — holding IS the strategy

    px = get_live_price(BENCHMARK)
    if not _pos(px):
        return [f"⚠️ {BENCHMARK} unpriceable — the passive arm did NOT buy; "
                f"nothing was invented"]
    cash = float(paper.get("cash") or 0)
    if cash <= 0:
        return [f"⚠️ passive arm has no cash (${cash:,.2f}) — nothing bought"]

    shares = round(cash / float(px), 8)
    equity0 = round(cash, 2)
    if not dry:
        # No stop, no target: buy-and-hold has neither, and giving it one would
        # turn the benchmark into a different strategy.
        msg = paper_buy(BENCHMARK, shares, chat_id, price=float(px))
        if str(msg).startswith("❌"):
            return [f"⚠️ passive buy refused: {str(msg)[:90]}"]

        def _sim(d):
            d = d or sp.new_doc(chat_id, today, equity0)
            sp.record_buy(d, BENCHMARK, equity0, float(px), today)
            return sp.snapshot(d, today, equity0, float(px), 1)
        try:
            sp.update(chat_id, _sim)
        except Exception as exc:
            return [f"⚠️ passive book update failed: {exc}"]
    verb = "would buy" if dry else "bought"
    return [f"🅢 PASSIVE {verb} {shares:g} {BENCHMARK} @ ${px:,.2f} = "
            f"${equity0:,.2f} — held from here, never sold"]


def snapshot_passive(chat_id: str, dry: bool) -> list[str]:
    """Mark the passive arm daily so its curve has the same points as the others.

    It never trades, so without this its equity curve would be a single dot and
    could not be read beside arms that snapshot every day.
    """
    from market_data import get_live_price
    from paper_trader import load_user_paper
    import sim_portfolio as sp

    today = et_today().isoformat()
    paper = load_user_paper(chat_id)
    px = get_live_price(BENCHMARK)
    if not _pos(px):
        return [f"⚠️ {BENCHMARK} unpriceable — passive arm NOT marked today"]
    eq = sp.bot_equity(paper, get_live_price)
    if eq is None or dry:
        return [] if dry else ["⚠️ passive arm could not be marked (unpriceable position)"]
    try:
        sp.update(chat_id, lambda d: sp.snapshot(d, today, eq, float(px),
                                                 len(paper.get("positions") or []))
                  if d else None)
    except Exception as exc:
        return [f"⚠️ passive snapshot failed: {exc}"]
    return []


# ── screener arms ────────────────────────────────────────────────────────────

def open_arm(name: str, dry: bool) -> dict:
    """One arm's day: screen -> select (no Claude) -> trade -> ledger."""
    out = {"arm": name, "picks": 0, "acts": [], "errors": [], "ledgered": 0}
    chat_id = _account_for(name)
    out["chat_id"] = chat_id

    if name == PASSIVE_ARM:
        out["acts"] = open_passive(chat_id, dry) + snapshot_passive(chat_id, dry)
        return out

    import arm_selector
    from screener import run_screener

    cfg = get_config()
    screen = run_screener(watchlist=cfg.get("watchlist", []),
                          excluded_sectors=cfg.get("excluded_sectors", []),
                          strategy=STRATEGIES[name])
    picks = arm_selector.select(screen, STRATEGIES[name])
    flat = picks["stocks"]["short_term"] + picks["stocks"]["long_term"]
    out["picks"] = len(flat)
    out["tickers"] = [p["ticker"] for p in flat]
    if not flat:
        out["acts"] = [f"no eligible candidates today — this arm sat out, "
                       f"which is a market fact, not an error"]
        return out

    # THE ONE TRADER. open_real=False: an arm never opens a real position.
    out["acts"] = _su().phase_open(chat_id, dry, picks=picks, open_real=False)

    if dry:
        return out
    try:
        ev = _ev()
        all_rows, shard, shard_name = ev._load_ledger()
        n = ev.record_picks(shard, picks, et_today().isoformat(), arm=name,
                            known={ev._key(r) for r in all_rows})
        if n and not ev._save_ledger(shard, shard_name):
            out["errors"].append("LEDGER SAVE FAILED — this arm's picks are not recorded")
        out["ledgered"] = n
    except Exception as exc:
        out["errors"].append(f"ledger: {type(exc).__name__} {exc}")
    return out


def manage_arm(name: str, dry: bool) -> dict:
    """Exit at target, stop or the time stop — through the ONE trader."""
    out = {"arm": name, "acts": [], "errors": []}
    chat_id = _account_for(name)
    out["chat_id"] = chat_id
    if name == PASSIVE_ARM:
        # Buy-and-hold never exits. It is still MARKED daily so its curve is
        # comparable with arms that trade.
        out["acts"] = snapshot_passive(chat_id, dry)
        return out
    out["acts"] = _su().manage_account(chat_id, dry)
    return out


def reset_arm(name: str, dry: bool) -> dict:
    out = {"arm": name, "acts": [], "errors": []}
    chat_id = _account_for(name)
    out["chat_id"] = chat_id
    out["acts"] = _su().phase_reset(chat_id, dry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["open", "manage", "reset"], default="open")
    ap.add_argument("--arms", default=",".join(sorted(ARM_CHAT_IDS)),
                    help="comma-separated arm names; defaults to every arm")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = [a.strip() for a in args.arms.split(",") if a.strip()]
    known = set(ARM_CHAT_IDS) & (set(STRATEGIES) | {PASSIVE_ARM})
    bad = [a for a in names if a not in known]
    if bad:
        print(f"[arms] unknown arm(s): {bad}. known={sorted(known)}")
        return 2

    runner = {"open": open_arm, "manage": manage_arm, "reset": reset_arm}[args.phase]
    results, failed = [], False
    for name in names:                       # SEQUENTIAL — see the module docstring
        print(f"[arms] ── {name} ({args.phase}) ──")
        try:
            r = runner(name, args.dry_run)
        except SystemExit:
            raise
        except Exception as exc:
            traceback.print_exc()
            r = {"arm": name, "acts": [], "errors": [f"CRASHED: {exc}"]}
        results.append(r)
        for a in r.get("acts", []):
            print(f"[arms]   {a}")
        for e in r["errors"]:
            print(f"[arms]   ! {e}")
        failed = failed or bool(r["errors"])

    lines = [f"🏁 <b>Tournament arms — {args.phase} — {et_today().isoformat()}</b>",
             "<i>paper only · test accounts · production picks untouched</i>", ""]
    for r in results:
        bits = [f"<b>{r['arm']}</b>"]
        if args.phase == "open":
            bits.append(f"{r.get('picks', 0)} picks")
            if r.get("ledgered"):
                bits.append(f"{r['ledgered']} ledgered")
        acted = [a for a in r.get("acts", []) if a.strip()]
        bits.append(f"{len(acted)} event(s)")
        if r["errors"]:
            bits.append(f"⚠️ {len(r['errors'])} error(s)")
        lines.append(" · ".join(bits))

    # `manage` runs hourly: stay silent unless something actually happened, so
    # the reports do not become noise nobody reads.
    actionable = any(r.get("acts") or r["errors"] for r in results)
    if not args.dry_run and (args.phase != "manage" or actionable):
        try:
            from telegram_api import send_message
            owner = os.environ.get("TELEGRAM_CHAT_ID", "")
            if owner:
                send_message("\n".join(lines), chat_id=owner)
        except Exception as exc:
            print(f"[arms] report send failed: {exc}")
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
