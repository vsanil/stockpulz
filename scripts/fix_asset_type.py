#!/usr/bin/env python3
"""
fix_asset_type.py — one-time correction of coins stored with the wrong asset type.

Found 2026-11-15: a BNB position logged on 2026-05-26 is stored as
`asset_type: "stock"`. It predates the 2026-06-26 rule that types a position
from the canonical coin list, so nothing has corrected it since. Its PRICE is
right (every price lookup recognises BNB by name), but anything that groups by
type counts it as a stock: the mini-app's stock/crypto split and the portfolio
concentration check.

A trade is retyped only if ALL of these hold: it belongs to a REAL user (test
accounts are skipped), its ticker is in `price_checker.CRYPTO_SYMBOLS`, and its
stored type is something other than "crypto". Open and closed trades both. Each
change is printed, so a stock that happens to share a coin's ticker would be
visible in the dry run before anything is written.

Dry run by default; `--apply` writes through `mutate_user_trade_log` and reads
the result back. Supabase only.

    python scripts/fix_asset_type.py            # show
    python scripts/fix_asset_type.py --apply    # correct
"""
from __future__ import annotations

import argparse
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def retype(log: dict, crypto_symbols) -> tuple[dict, list]:
    """(corrected copy, [(where, ticker, old type)]) for one user's trade log."""
    out = copy.deepcopy(log or {})
    changes = []
    for where in ("open", "closed"):
        for t in out.get(where) or []:
            if not isinstance(t, dict):
                continue
            tick = str(t.get("ticker") or "").upper()
            if tick in crypto_symbols and t.get("asset_type") != "crypto":
                changes.append((where, tick, t.get("asset_type")))
                t["asset_type"] = "crypto"
    return out, changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from storage import get_storage_backend
    be = get_storage_backend()
    print(f"[fix_asset_type] store: {be.name()}")
    if be.name() != "supabase":
        print("[fix_asset_type] refusing: not the store production reads.")
        return 2

    import config_manager as cm
    from price_checker import CRYPTO_SYMBOLS

    all_logs = cm._load_gist_file(cm.USER_TRADES_FILE)
    if not isinstance(all_logs, dict) or not all_logs:
        print("[fix_asset_type] refusing: could not read the trade logs.")
        return 2

    plan = {}
    for uid, log in all_logs.items():
        if cm.is_test_user(str(uid)) or not isinstance(log, dict):
            continue
        _, changes = retype(log, CRYPTO_SYMBOLS)
        if changes:
            plan[str(uid)] = changes
            for where, tick, old in changes:
                print(f"  {uid}  {where:6}  {tick:6}  {old!r} -> 'crypto'")
    total = sum(len(c) for c in plan.values())
    print(f"[fix_asset_type] {total} trade(s) across {len(plan)} user(s) to retype")
    if not args.apply or not total:
        return 0

    def _fix(log):
        fixed, changes = retype(log, CRYPTO_SYMBOLS)
        if not changes:
            return cm.NO_WRITE, 0
        return fixed, len(changes)

    for uid in plan:
        n = cm.mutate_user_trade_log(uid, _fix)
        print(f"[fix_asset_type] {uid}: retyped {n}")

    cm._cache_invalidate(cm.USER_TRADES_FILE)     # read back from the STORE, not memory
    bad = 0
    for uid in plan:
        _, left = retype(cm.load_user_trade_log(uid), CRYPTO_SYMBOLS)
        if left:
            bad += len(left)
            print(f"[fix_asset_type] ❌ {uid}: still mistyped after write: {left}")
    if bad:
        return 1
    print("[fix_asset_type] ✅ read back: every listed trade is now 'crypto'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
