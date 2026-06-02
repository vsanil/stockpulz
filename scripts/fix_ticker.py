"""
One-time migration: rename a ticker in all users' open + closed trade logs.

Usage:
    python scripts/fix_ticker.py AVERY AVY

Reads from Gist, patches in memory, writes back. Dry-run by default — pass
--apply to actually save.
"""
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config_manager import get_allowed_users, load_user_trade_log, save_user_trade_log


def fix_ticker(old: str, new: str, apply: bool = False):
    old = old.upper()
    new = new.upper()
    users = get_allowed_users()
    print(f"Scanning {len(users)} user(s) for ticker '{old}' → '{new}'  "
          f"({'APPLY' if apply else 'DRY RUN'})")

    total = 0
    for uid in users:
        log     = load_user_trade_log(uid)
        changed = 0

        for trade in log.get("open", []):
            if trade.get("ticker") == old:
                trade["ticker"] = new
                changed += 1

        for trade in log.get("closed", []):
            if trade.get("ticker") == old:
                trade["ticker"] = new
                changed += 1

        if changed:
            print(f"  user {uid}: {changed} entry/entries updated")
            if apply:
                save_user_trade_log(uid, log)
                print(f"  user {uid}: saved.")
            total += changed
        else:
            print(f"  user {uid}: no entries found")

    if total == 0:
        print("Nothing to change.")
    elif not apply:
        print(f"\n{total} entries would be renamed. Re-run with --apply to save.")
    else:
        print(f"\nDone. {total} entries renamed.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_apply = "--apply" in sys.argv

    if len(args) != 2:
        print("Usage: python scripts/fix_ticker.py OLD_TICKER NEW_TICKER [--apply]")
        sys.exit(1)

    fix_ticker(args[0], args[1], apply=do_apply)
