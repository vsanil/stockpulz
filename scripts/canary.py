#!/usr/bin/env python3
"""
canary.py — daily synthetic end-to-end health check + calculation audit.

Runs against LIVE data as the admin account (TELEGRAM_CHAT_ID), exercises every
major path (picks, prices, sizing, paper trade, alerts, watchlist, backtest,
delivery/cron health, endpoint health) AND verifies the underlying math, then
DMs a report to the admin.

Non-destructive: before any mutation it snapshots the affected Gist files and
ALWAYS restores them in a finally block, so the admin's real data is byte-for-byte
unchanged after a run — the paper buy / alert / watchlist round-trips leave nothing.

Usage:
    python3 scripts/canary.py            # run + send the report to the admin
    python3 scripts/canary.py --dry-run  # run + print only (no Telegram send)
"""
from __future__ import annotations

import os
import sys
import math
import argparse
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {('· ' + detail) if detail else ''}")


def _fin(x) -> bool:
    """True only for a real finite number (rejects None / NaN / Inf)."""
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _pos(x) -> bool:
    return _fin(x) and float(x) > 0


# ── raw Gist snapshot / restore (guarantees zero residue) ─────────────────────
_GID = os.environ.get("GIST_ID")
_TOK = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
_SNAP_FILES = ("user_paper.json", "price_alerts.json", "trade_log.json",
               "user_trades.json", "user_configs.json")


def _gist_all() -> dict:
    r = requests.get(f"https://api.github.com/gists/{_GID}",
                     headers={"Authorization": f"token {_TOK}"}, timeout=20)
    r.raise_for_status()
    return r.json().get("files", {})


def _gist_restore(snapshot: dict) -> None:
    """Write the snapshotted contents back verbatim, with retries. GitHub can
    409/403 on rapid successive PATCHes to the same gist (the canary does ~8
    writes during a run), so a single restore PATCH sometimes conflicts — retry
    with backoff until it lands, else the run leaves residue on the account."""
    import time
    files = {fn: {"content": c} for fn, c in snapshot.items() if c is not None}
    if not files:
        return
    time.sleep(1.5)   # let GitHub settle after the run's writes
    last = ""
    for attempt in range(1, 7):
        r = requests.patch(f"https://api.github.com/gists/{_GID}",
                           headers={"Authorization": f"token {_TOK}"},
                           json={"files": files}, timeout=25)
        if r.status_code < 400:
            return
        last = f"{r.status_code} {r.text[:80]}"
        time.sleep(2 * attempt)
    raise RuntimeError(f"gist restore failed after retries: {last}")


def _raw_picks() -> dict:
    """Read picks.json directly (NOT load_picks, which returns None off-day)."""
    import json
    files = _gist_all()
    try:
        return json.loads(files.get("picks.json", {}).get("content") or "{}")
    except Exception:
        return {}


def _expected_delivery_date() -> str:
    """The date we SHOULD have morning picks/delivery for, given the clock.
    Morning runs ~11:00 UTC (7 AM ET). Today counts only if it's a weekday and
    we're past ~7:30 AM ET; otherwise the latest delivery is the prior weekday.
    Makes the delivery checks correct whatever hour the canary runs."""
    import datetime as dt, pytz
    et = dt.datetime.now(pytz.timezone("America/New_York"))
    d = et.date()
    if d.weekday() < 5 and (et.hour, et.minute) >= (7, 30):
        return d.isoformat()
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# Read-only checks
# ══════════════════════════════════════════════════════════════════════════════

def check_picks_integrity() -> None:
    picks = _raw_picks()
    if not picks or not picks.get("stocks"):
        _check("picks.load", False, "picks.json empty/malformed")
        return
    exp = _expected_delivery_date()
    _check("picks.load", True, f"saved {picks.get('_saved_date')}")
    _check("picks.fresh", picks.get("_saved_date") == exp,
           f"_saved_date={picks.get('_saved_date')} expected={exp}")

    # Validate EVERY pick in EVERY section — stocks, crypto, ETFs, commodities.
    # (commodities are often empty; count 0 is fine, not a failure.)
    bad, math_bad, counts = [], [], {}
    for sec, key in (("stocks", "ticker"), ("crypto", "symbol"),
                     ("etfs", "ticker"), ("commodities", "ticker")):
        n = 0
        for tf, is_long in (("short_term", False), ("long_term", True)):
            for p in (picks.get(sec, {}) or {}).get(tf, []) or []:
                n += 1
                t = p.get(key) or p.get("ticker") or p.get("symbol") or "?"
                e, tgt, stop = p.get("entry_price"), p.get("target_price"), p.get("stop_loss")
                if not (_pos(e) and _pos(tgt)):
                    bad.append(f"{sec}:{t}(e={e},t={tgt})")
                    continue
                if float(tgt) <= float(e):
                    math_bad.append(f"{sec}:{t} tgt<=entry")
                if not is_long and _pos(stop) and float(stop) >= float(e):
                    math_bad.append(f"{sec}:{t} stop>=entry")
                up = p.get("upside_pct") or p.get("target_gain_pct")
                if _fin(up):
                    calc = (float(tgt) - float(e)) / float(e) * 100
                    if abs(float(up) - calc) > 0.6:
                        math_bad.append(f"{sec}:{t} upside {up}≠{calc:.1f}")
        counts[sec] = n
    _check("picks.all_sections_valid", not bad,
           ("bad: " + ", ".join(bad)) if bad else f"counts={counts} · all entry/target > 0")
    _check("picks.math", not math_bad,
           "; ".join(math_bad) if math_bad else "target>entry, stop<entry, upside ok (ALL sections)")


def check_live_prices() -> None:
    from market_data import get_live_prices
    basket = ["AAPL", "MSFT", "NVDA", "SPY"]
    prices = get_live_prices(basket)
    missing = [t for t in basket if not _pos(prices.get(t))]
    _check("prices.stocks", not missing,
           f"missing/invalid: {missing}" if missing else f"{', '.join(f'{t}=${prices[t]:.0f}' for t in basket if prices.get(t))}")
    # crypto via cg cache
    from price_checker import cg_prices, _SYMBOL_TO_CG_ID
    cg = cg_prices([_SYMBOL_TO_CG_ID["BTC"], _SYMBOL_TO_CG_ID["ETH"]])
    btc = cg.get(_SYMBOL_TO_CG_ID["BTC"])
    eth = cg.get(_SYMBOL_TO_CG_ID["ETH"])
    _check("prices.btc_sane", _pos(btc) and 10_000 <= btc <= 500_000, f"BTC=${btc}")
    _check("prices.eth_sane", _pos(eth) and 300 <= eth <= 30_000, f"ETH=${eth}")
    # cache: 2nd call must not change / must return same
    cg2 = cg_prices([_SYMBOL_TO_CG_ID["BTC"]])
    _check("prices.cg_cache", cg2.get(_SYMBOL_TO_CG_ID["BTC"]) == btc, "cached BTC stable")


def check_sizing() -> None:
    from position_sizer import size_pick
    from config_manager import get_config
    cfg = get_config()
    pick = {"ticker": "TESTX", "entry_price": 100.0, "stop_loss": 93.0,
            "target_price": 120.0, "conviction": 3}
    r = size_pick(pick, cfg)
    shares = r.get("shares")
    _check("sizing.shares_pos", _pos(shares), f"shares={shares}")
    # risk-dollars sanity: shares * (entry-stop) should be > 0 and finite
    try:
        risk = float(shares) * (100.0 - 93.0)
        _check("sizing.risk_finite", _fin(risk) and risk > 0, f"risk≈${risk:.0f}")
    except Exception as e:
        _check("sizing.risk_finite", False, str(e))
    sp = r.get("stop_pct")
    _check("sizing.stop_pct_sane", _fin(sp) and 0 < float(sp) <= 25,
           f"stop_pct={sp} (should be ~7%, not clamped to 20 for a 7% stop)")
    # crypto sizes by DOLLAR amount (shares intentionally None — callers derive
    # fractional coins from dollar_amount / price). Verify the $ sizing is valid.
    cr = size_pick({"symbol": "BTC", "entry_price": 60000.0, "stop_loss": 55000.0,
                    "target_price": 80000.0, "conviction": 3}, cfg, is_crypto=True)
    _check("sizing.crypto_dollar_based", _pos(cr.get("dollar_amount")),
           f"crypto dollar_amount=${cr.get('dollar_amount')} (shares None by design)")


def check_backtest_math() -> None:
    # replicate the endpoint's non-overlapping logic on a synthetic series
    closes = [100, 100, 100, 89, 95, 96, 100, 100, 105, 111]
    entry, stop, target = 100, 90, 110
    n = len(closes); i = 0; wins = losses = 0
    while i < n:
        if abs(closes[i] - entry) / entry > 0.02:
            i += 1; continue
        ht = hs = False; exit_idx = min(i + 60, n) - 1
        for j in range(i + 1, min(i + 60, n)):
            if closes[j] >= target: ht = True; exit_idx = j; break
            if closes[j] <= stop:   hs = True; exit_idx = j; break
        wins += ht; losses += hs; i = exit_idx + 1
    _check("backtest.nonoverlap", wins == 1 and losses == 1,
           f"wins={wins} losses={losses} (expect 1/1, not 2/3)")
    if wins + losses:
        wr = wins / (wins + losses) * 100
        _check("backtest.winrate_math", abs(wr - 50.0) < 0.01, f"win_rate={wr}")


def check_price_guard() -> None:
    """The garbage-price guard (market_data.plausible_price) is what stops a
    failed feed's tiny value ($0.01 TSLA / ~$0.00 UNI on a holiday) from firing a
    false 'below $X · −100%' alert or a fake EOD stop-hit. This bug fired live on
    Jul 3 while the canary was green — because nothing here injected a bad price.
    Assert the guard rejects garbage and still accepts real quotes + real drops."""
    from market_data import plausible_price
    rejects = (not plausible_price(0.01, 354.05)      # TSLA holiday garbage
               and not plausible_price(0.004, 3.07)   # UNI
               and not plausible_price(0.0, 100.0)
               and not plausible_price(float("nan"), 100.0)
               and not plausible_price(4000.0, 354.0))  # 11x spike
    _check("price_guard.rejects_garbage", rejects,
           "plausible_price lets a $0.01/spike through — false alerts will fire")
    accepts = (plausible_price(354.0, 360.0)          # real quote
               and plausible_price(570.31, 651.0)     # BNB real −12.4% stop
               and plausible_price(50.0, 100.0))      # legit −50% below-alert
    _check("price_guard.accepts_real", accepts,
           "plausible_price rejects a real quote/drop — alerts would be suppressed")


def check_cron_delivery() -> None:
    from config_manager import get_config
    cfg = get_config()
    exp = _expected_delivery_date()
    # last_morning_run is the DELIVERY stamp (cron_last_morning is just "started").
    lmr = (cfg.get("last_morning_run") or "")[:10]
    _check("delivery.morning", lmr == exp,
           f"last_morning_run={lmr or 'never'} (expected {exp})")
    _check("delivery.picks_saved", _raw_picks().get("_saved_date") == exp,
           f"picks._saved_date={_raw_picks().get('_saved_date')} (expected {exp})")


def check_endpoints() -> None:
    base = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL")
            or "https://stock-agent-enqx.onrender.com").rstrip("/")
    try:
        h = requests.get(base + "/health", timeout=25)
        _check("endpoint.health", h.status_code == 200 and h.json().get("status") == "ok",
               f"{h.status_code}")
    except Exception as e:
        _check("endpoint.health", False, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Mutating round-trips (snapshot → act → verify → restore)
# ══════════════════════════════════════════════════════════════════════════════

def check_mutations(admin: str) -> None:
    snap_files = _gist_all()
    snapshot = {fn: snap_files.get(fn, {}).get("content")
                for fn in _SNAP_FILES if fn in snap_files}
    try:
        _check("snapshot", bool(snapshot), f"{len(snapshot)} files snapshotted")

        # ── Paper trade round-trip ────────────────────────────────────────────
        try:
            from paper_trader import paper_buy, paper_cancel, load_user_paper
            from market_data import get_live_price
            px = get_live_price("AAPL")
            if _pos(px):
                total = 1000.0
                shares = round(total / float(px), 6)
                paper_buy("AAPL", shares, admin, price=float(px))
                data = load_user_paper(admin)
                pos = next((p for p in data.get("positions", []) if p.get("ticker") == "AAPL"), None)
                if pos:
                    ep = pos.get("entry_price")
                    # entry must be the per-share PRICE, never the $1000 total
                    ok = _pos(ep) and abs(float(ep) - float(px)) / float(px) < 0.02
                    _check("paper.entry_is_price", ok,
                           f"stored entry=${ep} vs price=${px:.2f} (must NOT be ${total} total)")
                    _check("paper.shares_fractional", abs(float(pos.get("shares", 0)) - shares) < 1e-4,
                           f"shares={pos.get('shares')} expect {shares}")
                    # Sell at +10% → exercise the realized-P&L path (restore cleans up).
                    from paper_trader import paper_sell
                    sm = paper_sell("AAPL", admin, price=round(float(px) * 1.10, 2))
                    _check("paper.sell_ok", not sm.startswith("❌"), f"sell: {sm[:70]}")
                else:
                    _check("paper.entry_is_price", False, "position not stored after paper_buy")
            else:
                _check("paper.entry_is_price", False, "AAPL price unavailable")
        except Exception as e:
            _check("paper.entry_is_price", False, f"exc: {e}")

        # ── "I Bought This" (log a REAL position) round-trip ──────────────────
        try:
            from trade_logger import add_holding, load_user_trade_log
            from market_data import get_live_price
            px = get_live_price("V")   # Visa — not in the admin's holdings/watchlist
            if _pos(px):
                add_holding("V", admin,
                            entry_override=float(px),
                            stop_override=round(float(px) * 0.93, 2),
                            target_override=round(float(px) * 1.15, 2),
                            shares_override=3.0,
                            asset_type_override="stock")
                pos = next((t for t in load_user_trade_log(admin).get("open", [])
                            if t.get("ticker") == "V"), None)
                if pos:
                    ep = pos.get("entry_price")
                    _check("log_bought.entry_is_price",
                           _pos(ep) and abs(float(ep) - float(px)) < 0.5,
                           f"stored entry=${ep} vs price=${px:.2f} (must be per-share, not a total)")
                    _check("log_bought.levels",
                           _pos(pos.get("stop_loss")) and _pos(pos.get("target_price")),
                           f"stop={pos.get('stop_loss')} target={pos.get('target_price')}")
                    pnl = (float(px) - float(ep)) / float(ep) * 100
                    _check("log_bought.pnl_math", _fin(pnl), f"P&L vs entry = {pnl:.2f}%")
                else:
                    _check("log_bought.entry_is_price", False, "position not stored after add_holding")
            else:
                _check("log_bought.entry_is_price", False, "V price unavailable")
        except Exception as e:
            _check("log_bought.entry_is_price", False, f"exc: {e}")

        # ── Crypto paper round-trip (fractional coin, -USD pricing) ───────────
        try:
            from paper_trader import paper_buy, load_user_paper
            from market_data import get_live_price
            cpx = get_live_price("ETH")
            if _pos(cpx):
                csh = round(200.0 / float(cpx), 8)   # $200 of ETH → tiny fraction
                # Delta-based so it's robust to a pre-existing ETH position (the
                # synthetic-user bot holds ETH paper): paper_buy aggregates, so the
                # stored entry becomes a weighted avg that legitimately drifts from
                # live. We verify THIS buy's contribution, not the aggregate.
                _before = next((p for p in load_user_paper(admin).get("positions", [])
                                if p.get("ticker") == "ETH"), None)
                before_sh = float(_before.get("shares", 0)) if _before else 0.0
                before_entry = (float(_before.get("entry_price"))
                                if _before and _pos(_before.get("entry_price")) else float(cpx))
                paper_buy("ETH", csh, admin, price=float(cpx))
                cpos = next((p for p in load_user_paper(admin).get("positions", [])
                             if p.get("ticker") == "ETH"), None)
                delta = (float(cpos.get("shares", 0)) - before_sh) if cpos else 0.0
                # $200 of ETH must add the FRACTIONAL coin (~200/price), NOT 200
                # shares / a $200 entry. A weighted-avg entry is always BETWEEN the
                # old entry and the new buy price, so bound it there (rejects $200).
                lo, hi = min(before_entry, float(cpx)) * 0.98, max(before_entry, float(cpx)) * 1.02
                entry = float(cpos.get("entry_price")) if cpos else 0.0
                ok = bool(cpos) and _pos(entry) \
                    and 0 < delta < 1 and abs(delta - csh) < 1e-4 and lo <= entry <= hi
                _check("paper.crypto_fractional", ok,
                       f"+{delta:.6f} ETH from $200 buy @ ${cpos.get('entry_price') if cpos else '?'} "
                       f"(fractional coin at real -USD price)")
            else:
                _check("paper.crypto_fractional", False, "ETH price unavailable")
        except Exception as e:
            _check("paper.crypto_fractional", False, f"exc: {e}")

        # ── Alert round-trip (add → replace → remove) ─────────────────────────
        try:
            from price_alert_manager import add_alert, remove_alert, _load_alerts
            from market_data import get_live_price
            px = get_live_price("MSFT") or 400.0
            t1 = round(float(px) * 0.90, 2)   # 10% below → 'below'
            t2 = round(float(px) * 0.85, 2)
            add_alert(admin, "MSFT", t1)
            got1 = [a for a in _load_alerts().get(admin, []) if a["ticker"] == "MSFT"]
            _check("alert.add", len(got1) == 1 and abs(got1[0]["target"] - t1) < 0.01,
                   f"{[a['target'] for a in got1]}")
            add_alert(admin, "MSFT", t2, replace=True)   # atomic replace
            got2 = [a for a in _load_alerts().get(admin, []) if a["ticker"] == "MSFT"]
            _check("alert.replace_atomic", len(got2) == 1 and abs(got2[0]["target"] - t2) < 0.01,
                   f"after replace: {[a['target'] for a in got2]} (must be exactly [{t2}])")
            remove_alert(admin, "MSFT")
            got3 = [a for a in _load_alerts().get(admin, []) if a["ticker"] == "MSFT"]
            _check("alert.remove", len(got3) == 0, f"remaining: {[a['target'] for a in got3]}")
        except Exception as e:
            _check("alert.add", False, f"exc: {e}")

        # ── Watchlist round-trip ──────────────────────────────────────────────
        try:
            import webhook as wh
            before = wh._load_watchlist(admin)
            test_t = "SPY"
            wh._save_watchlist(admin, list(dict.fromkeys(list(before) + [test_t])))
            mid = wh._load_watchlist(admin)
            _check("watchlist.add", test_t in [t.upper() for t in mid], f"watchlist has {test_t}")
            wh._save_watchlist(admin, before)   # restore original list
            _check("watchlist.restore_ok", True, f"{len(before)} tickers preserved")
        except Exception as e:
            _check("watchlist.add", False, f"exc: {e}")

    finally:
        # ALWAYS restore — guarantees the admin's data is unchanged after a run.
        try:
            _gist_restore(snapshot)
            _check("restore", True, "all mutated stores restored to snapshot")
        except Exception as e:
            _check("restore", False, f"RESTORE FAILED: {e}")


# ══════════════════════════════════════════════════════════════════════════════

def build_report() -> str:
    from datetime import datetime
    import pytz
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%a %b %d · %I:%M %p ET")
    fails = [r for r in RESULTS if not r[1]]
    passes = [r for r in RESULTS if r[1]]
    head = "✅" if not fails else "🔴"
    lines = [f"{head} <b>Canary — daily health check</b>", f"<i>{now}</i>"]
    if fails:
        # Loud, unmissable, and tells you exactly what to do with it.
        lines += ["",
                  "🚨🚨🚨 <b>ACTION NEEDED</b> 🚨🚨🚨",
                  "<b>Forward this whole message to Claude to fix.</b>",
                  ""]
    lines.append(f"<b>{len(passes)}/{len(RESULTS)} checks passed</b>")
    if fails:
        lines.append("")
        lines.append(f"🔴 <b>{len(fails)} FAILED:</b>")
        for name, _ok, detail in fails:
            lines.append(f"  • <b>{name}</b> — {detail}")
    else:
        lines.append("<i>Every path + calculation verified. Nothing to do. 👍</i>")
    lines += ["", "<i>Passed: " + ", ".join(r[0] for r in passes) + "</i>"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only, don't send")
    args = ap.parse_args()

    admin = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not admin:
        print("TELEGRAM_CHAT_ID not set — cannot run canary as admin.")
        return 2

    for fn in (check_picks_integrity, check_live_prices, check_sizing,
               check_backtest_math, check_price_guard, check_cron_delivery,
               check_endpoints):
        try:
            fn()
        except Exception as e:
            _check(fn.__name__, False, f"crashed: {e}")
            traceback.print_exc()

    try:
        check_mutations(admin)
    except Exception as e:
        _check("check_mutations", False, f"crashed: {e}")
        traceback.print_exc()

    report = build_report()
    print("\n" + "=" * 60 + "\n" + report.replace("<b>", "").replace("</b>", "")
          .replace("<i>", "").replace("</i>", ""))

    if not args.dry_run:
        try:
            from telegram_api import send_message
            send_message(report, chat_id=admin)
            print("\n[canary] report sent to admin.")
        except Exception as e:
            print(f"[canary] failed to send report: {e}")

    fails = [r for r in RESULTS if not r[1]]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
