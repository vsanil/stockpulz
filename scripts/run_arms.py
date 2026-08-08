#!/usr/bin/env python3
"""
run_arms.py — run engine VARIANTS side by side on the same market day.

Why this exists
---------------
Comparing "engine v1 in July" against "engine v2 in August" measures July vs
August. Market regime dominates any real difference between two variants, so a
sequential comparison is close to worthless. Arms remove that confound by
running every variant on the SAME day against the SAME market.

What arms do NOT fix is sample size: separating a 55% engine from a 60% one
needs ~1,500 picks per arm either way, because only the picks where arms
DISAGREE carry information. That is why the arms here are deliberately far
apart — breakout (momentum only) vs pullback (mean-reversion only) — the one
split with measured evidence behind it: on live data those two signals never
fire on the same candidate (5/5 anti-correlated).

Safety rules baked in
---------------------
  * PAPER ONLY. An arm never opens a real position. These are experiments.
  * Each arm trades its OWN account (config_manager.ARM_CHAT_IDS), which are
    registered test users — so no Telegram send is attempted, and they are
    absent from allowed_users, which is what actually keeps them out of
    community stats and the LLM pick prompt.
  * It NEVER writes picks.json. Production picks are untouched; this process
    cannot change what a real user receives.
  * Ledger rows are tagged `arm`, and the evaluator segments them away from the
    headline exactly like controls.

Cost note: each arm is a full screener pass plus one Claude call. Arms run
SEQUENTIALLY on purpose — firing three screeners at Yahoo/Finnhub at once
invites rate limiting, and an arm that silently got degraded data would be
measuring the API rather than the strategy.

Usage:
    python scripts/run_arms.py --dry-run          # no writes, prints what would happen
    python scripts/run_arms.py --arms breakout,pullback
"""
from __future__ import annotations

import os
import sys
import argparse
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_manager import (ARM_CHAT_IDS, et_today, get_config, is_test_user,
                            load_user_paper)
from screener import STRATEGIES

_PAPER_USD = 500.0          # per pick, per arm — arms must be comparable
_MIN_CASH  = 20_000.0       # top up below this so a broke arm never silently stops buying


def _pos(x) -> bool:
    try:
        return x is not None and float(x) > 0
    except (TypeError, ValueError):
        return False


def _ensure_cash(chat_id: str, dry: bool) -> float:
    """An arm that runs out of cash stops buying and quietly stops being an arm.
    That already happened once to the synthetic bot ($271 across 33 positions),
    where it looked like the paper path working rather than failing."""
    paper = load_user_paper(chat_id)
    cash  = float(paper.get("cash") or 0)
    if cash >= _MIN_CASH or dry:
        return cash
    from paper_trader import paper_add_cash
    paper_add_cash(_MIN_CASH - cash + 5_000.0, chat_id)
    return float(load_user_paper(chat_id).get("cash") or 0)


def run_arm(name: str, dry: bool) -> dict:
    """One arm: screen → analyse → paper-buy every pick → ledger it."""
    strat   = STRATEGIES[name]
    chat_id = ARM_CHAT_IDS[name]
    # Guard against an arm ever pointing at a REAL account. Note that checking
    # is_test_user() here would be circular — get_test_users() is DERIVED from
    # ARM_CHAT_IDS, so anything listed there is a "test user" by definition and
    # the assert could never fire. The allowlist is the independent fact: a real
    # user is someone who receives messages.
    from config_manager import get_allowed_users
    owner = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if str(chat_id) in set(get_allowed_users()) or str(chat_id) == owner:
        raise SystemExit(f"[arms] REFUSING: arm '{name}' points at real account "
                         f"{chat_id}. Arms paper-trade; they must never touch a "
                         f"live user's data.")

    out = {"arm": name, "chat_id": chat_id, "picks": 0, "bought": 0, "errors": []}
    cfg = get_config()

    from screener import run_screener
    from ai_analyzer import analyze_with_claude

    stock = run_screener(watchlist=cfg.get("watchlist", []),
                         excluded_sectors=cfg.get("excluded_sectors", []),
                         strategy=strat)
    picks = analyze_with_claude(stock, cfg, strategy=strat)

    flat = []
    for sec, key in (("stocks", "ticker"), ("crypto", "symbol"),
                     ("etfs", "ticker"), ("commodities", "ticker")):
        for tf in ("short_term", "long_term"):
            for p in (picks.get(sec, {}) or {}).get(tf, []) or []:
                t = (p.get(key) or p.get("ticker") or p.get("symbol") or "").upper()
                if t and _pos(p.get("entry_price")):
                    flat.append((t, p))
    out["picks"] = len(flat)

    if dry:
        out["tickers"] = [t for t, _ in flat]
        return out

    _ensure_cash(chat_id, dry)
    from paper_trader import paper_buy
    for t, p in flat:
        try:
            px = float(p["entry_price"])
            shares = round(_PAPER_USD / px, 6)
            msg = paper_buy(t, shares, chat_id, price=px,
                            stop_loss=p.get("stop_loss"),
                            target_price=p.get("target_price"))
            if msg.startswith("❌"):
                out["errors"].append(f"{t}: {msg[:60]}")
            else:
                out["bought"] += 1
        except Exception as exc:                      # one bad ticker never aborts an arm
            out["errors"].append(f"{t}: {type(exc).__name__} {exc}")

    # Ledger the arm's picks so the evaluator can score them independently.
    try:
        import importlib.util
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evaluate_picks.py")
        _s = importlib.util.spec_from_file_location("evaluate_picks", _p)
        ev = importlib.util.module_from_spec(_s); _s.loader.exec_module(ev)
        all_rows, shard, shard_name = ev._load_ledger()
        n = ev.record_picks(shard, picks, et_today().isoformat(), arm=name,
                            known={ev._key(r) for r in all_rows})
        if n and not ev._save_ledger(shard, shard_name):
            out["errors"].append("LEDGER SAVE FAILED — this arm's picks are not recorded")
        out["ledgered"] = n
    except Exception as exc:
        out["errors"].append(f"ledger: {type(exc).__name__} {exc}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="breakout,pullback",
                    help="comma-separated arm names from screener.STRATEGIES")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in names if a not in STRATEGIES or a not in ARM_CHAT_IDS]
    if bad:
        print(f"[arms] unknown arm(s): {bad}. known={sorted(set(STRATEGIES) & set(ARM_CHAT_IDS))}")
        return 2

    results, failed = [], False
    for name in names:                       # SEQUENTIAL — see the module docstring
        print(f"[arms] ── {name} ──")
        try:
            r = run_arm(name, args.dry_run)
        except Exception as exc:
            traceback.print_exc()
            r = {"arm": name, "picks": 0, "bought": 0, "errors": [f"CRASHED: {exc}"]}
        results.append(r)
        print(f"[arms] {name}: {r['picks']} picks, {r.get('bought',0)} bought, "
              f"{len(r['errors'])} error(s)")
        for e in r["errors"]:
            print(f"[arms]   ! {e}")
        failed = failed or bool(r["errors"])

    lines = [f"🧪 <b>A/B arms — {et_today().isoformat()}</b>",
             "<i>paper only · test accounts · production picks untouched</i>", ""]
    for r in results:
        lines.append(f"<b>{r['arm']}</b>: {r['picks']} picks · {r.get('bought',0)} bought"
                     + (f" · ⚠️ {len(r['errors'])} error(s)" if r["errors"] else ""))
    if not args.dry_run:
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
