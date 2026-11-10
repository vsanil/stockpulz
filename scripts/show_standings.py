#!/usr/bin/env python3
"""The tournament's EQUITY CURVES, read from the live store. READ-ONLY.

Same family as show_perf_bar.py: no sends, no writes, no Telegram. Run it on CI
(.github/workflows/show_standings.yml), where SUPABASE_* is set — a laptop
resolves to the GIST, the rollback copy, and would report stale books as live.
It prints the backend it resolved to for exactly that reason.

It does not re-implement anything: rows come from `sim_portfolio.standings`,
the same function the /admin "Tournament standings" card renders, over the same
account labels, plus the synthetic user's book (the LIVE engine's picks, traded
by the same trader).

Ranked on RETURN, not alpha — each arm's SPY twin receives that arm's own cash
flows, so alpha compares timing WITHIN an arm and is not comparable across arms.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _f(x, fmt="{:+.2f}%"):
    return "—" if x is None else fmt.format(x)


def main():
    import sim_portfolio as sp
    from config_manager import ARM_CHAT_IDS, DEFAULT_TEST_CHAT_ID
    from storage import get_storage_backend

    backend = type(get_storage_backend()).__name__
    print(f"[store] resolved to {backend}")
    if backend != "SupabaseBackend":
        print("  ⚠️  NOT the production store — these books are the rollback copy.")

    labels = {cid: name for name, cid in ARM_CHAT_IDS.items()}
    labels[DEFAULT_TEST_CHAT_ID] = "live (synthetic user)"
    rows = sp.standings(sp.all_books(), labels)

    print("\n[standings] ranked by return since each book started\n")
    print(f"{'arm':<22}{'start':<12}{'as of':<12}{'days':>5}{'equity':>12}"
          f"{'return':>9}{'SPY twin':>10}{'alpha':>9}{'max DD':>8}{'twin DD':>9}"
          f"{'open':>6}{'buys':>6}{'sells':>6}")
    for r in rows:
        if not r.get("active"):
            print(f"{r['arm']:<22}(no curve) {r.get('note', '')}")
            continue
        print(f"{r['arm']:<22}{r['start_date'] or '?':<12}{r['as_of'] or '?':<12}"
              f"{r['days']:>5}{_f(r['bot_equity'], '${:,.0f}'):>12}"
              f"{_f(r['bot_ret_pct']):>9}{_f(r['twin_ret_pct']):>10}"
              f"{_f(r['alpha_pct']):>9}{_f(r['max_drawdown_pct'], '{:.1f}%'):>8}"
              f"{_f(r['twin_max_drawdown_pct'], '{:.1f}%'):>9}"
              f"{r['open_positions']:>6}{r['buys']:>6}{r['sells']:>6}")
        if r.get("sample_warning"):
            print(f"{'':<22}⚠️ {r['sample_warning']}")

    print("\n[curves] weekly samples: date  bot equity / twin equity")
    for r in rows:
        if not r.get("active"):
            continue
        curve = r.get("curve") or []
        pts = curve[::5] + ([curve[-1]] if curve and (len(curve) - 1) % 5 else [])
        print(f"  {r['arm']}: " + "  ".join(
            f"{d[5:]} {b:,.0f}/{t:,.0f}" for d, b, t in pts
            if b is not None and t is not None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
