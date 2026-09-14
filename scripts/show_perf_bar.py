#!/usr/bin/env python3
"""What does the morning performance bar ACTUALLY render for real users today?

READ-ONLY. No sends, no writes, no Telegram. Run it on CI, where SUPABASE_* is
configured — a laptop has neither var, so `get_storage_backend()` hands back the
GIST, which is the rollback copy, and every number below would describe a store
production stopped writing to in August. It prints the backend it resolved to
for exactly that reason.

WHY IT EXISTS. On 2026-09-13 the win-rate honesty floor was raised to
`_MIN_WIN_RATE_N = 30`, so the derived PERCENTAGE is withheld below 30 closed
trades while the counts, median and SPY comparison still render. That was
verified by rendering synthetic fixtures. It was NOT verified against the real
ledger, and this project's own record is full of features that passed their
tests and did something else in production — the usage counter that recorded
nothing for a week, the admin page that 500'd behind twenty green assertions.

So this drives the REAL renderer (`formatters.format_daily_message`) over the
REAL stats function, on the REAL store, and prints the line a user would read.
It does not re-implement the bar; a re-implementation could agree with itself
while disagreeing with production.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    import config_manager as cm
    from storage import get_storage_backend
    from performance_tracker import (get_recent_stats, build_community_stats,
                                     _MIN_RECENT_TRADES, _MIN_COMMUNITY_TRADES,
                                     _MIN_WIN_RATE_N)
    from formatters import format_daily_message

    backend = type(get_storage_backend()).__name__
    print(f"[store] resolved to {backend}")
    if backend != "SupabaseBackend":
        print("  ⚠️  NOT the production store. Numbers below describe the "
              "rollback copy and must not be reported as what users see.")
    print(f"[gates] recent>={_MIN_RECENT_TRADES}  community>={_MIN_COMMUNITY_TRADES} "
          f"  win-rate>={_MIN_WIN_RATE_N}\n")

    users = cm.get_allowed_users()
    print(f"[users] {len(users)} allowed user(s)")

    logs = []
    for uid in users:
        try:
            log = cm.load_user_trade_log(uid)
        except Exception as exc:
            print(f"  {uid}: UNREADABLE ({exc}) — excluded")
            continue
        closed = (log or {}).get("closed", []) or []
        human = cm.human_trades(closed)
        print(f"  {uid}: {len(closed)} closed, {len(human)} human "
              f"({len(closed) - len(human)} synthetic, excluded)")
        logs.append(log or {})

    # ---- exactly what agent.py does before building the morning message ----
    stats = get_recent_stats(logs)
    print("\n[morning perf bar]")
    if not stats:
        total_human = sum(len(cm.human_trades((l or {}).get('closed', []) or []))
                          for l in logs)
        print(f"  get_recent_stats -> None  ({total_human} human closed trades "
              f"in window, floor is {_MIN_RECENT_TRADES})")
        print("  RENDERED: no performance bar at all — the morning message "
              "simply omits it.")
    else:
        t = stats["total"]
        print(f"  n={t['count']}  win_rate={t.get('win_rate')}  "
              f"conclusive={t.get('win_rate_conclusive')}")
        msg = format_daily_message({}, cm.get_config() or {}, recent_stats=stats)
        bar = [l for l in msg.splitlines() if "📊" in l]
        print("  RENDERED: " + (bar[0] if bar else "(no 📊 line found!)"))
        if not t.get("win_rate_conclusive"):
            assert f"{t['win_rate']}%" not in (bar[0] if bar else ""), \
                "LEAK: an inconclusive win rate reached the rendered bar"
            print(f"  ✅ the {t['win_rate']}% figure is correctly WITHHELD "
                  f"(n={t['count']} < {_MIN_WIN_RATE_N})")
        else:
            print(f"  ✅ n={t['count']} clears the floor, so the rate is shown")

    comm = build_community_stats(logs)
    print("\n[community line]")
    if not comm:
        print(f"  build_community_stats -> None (floor {_MIN_COMMUNITY_TRADES}) "
              f"— /community renders its empty state.")
    else:
        print(f"  n={comm.get('total_trades')}  win_rate={comm.get('win_rate')}  "
              f"conclusive={comm.get('win_rate_conclusive')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
