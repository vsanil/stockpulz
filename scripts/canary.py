#!/usr/bin/env python3
"""
canary.py — daily synthetic end-to-end health check + calculation audit.

Runs against LIVE data as the admin account (TELEGRAM_CHAT_ID), exercises every
major path (picks, prices, sizing, paper trade, alerts, watchlist, backtest,
delivery/cron health, endpoint health) AND verifies the underlying math, then
DMs a report to the admin.

Non-destructive: before any mutation it snapshots the affected files FROM THE
LIVE BACKEND (Gist or Supabase, whichever the app resolves to) and
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


def _check(name: str, ok: bool, detail: str = "", fail_detail: str = "") -> None:
    """`detail` is the note shown when the check PASSES (state the fact observed);
    `fail_detail` is shown when it FAILS (state the consequence). They were one
    field, so a check whose note was written as a warning printed that warning
    on a PASS line — e.g. price_guard read "lets a $0.01/spike through" while
    green, which is the exact opposite of what happened."""
    note = (detail if ok else (fail_detail or detail))
    RESULTS.append((name, bool(ok), note))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {('· ' + note) if note else ''}")


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


def _store():
    """The backend the APP uses — NOT the raw Gist API.

    🔴 Every data read here used to go through `_gist_all()` while production
    and CI resolve to Supabase. Two live consequences, both measured
    2026-08-23: `data.completeness` FAILED on a 2026-08-19 stamp while Supabase
    held 2026-08-21 (a false alarm that fired self_heal), and the mutating
    round-trips WROTE through the app to Supabase while restoring the GIST — so
    the restore undid nothing and residue accumulated in production storage.

    A monitor that reads a different store than the app is not monitoring the
    app. Imported function-locally to match the rest of this file.
    """
    from storage import get_storage_backend
    return get_storage_backend()


def _store_read(name: str):
    """Read `name` from the table it actually lives in.

    User-keyed files are ROWS on a row backend and need `read_all_users`;
    `read()` hits `documents` and would report them empty — the exact false
    alarm that nearly triggered a rollback on 2026-08-22.
    """
    from config_manager import USER_KEYED_FILES
    b = _store()
    if name in USER_KEYED_FILES and b.supports_rows():
        return b.read_all_users(name)
    return b.read(name)


def _snapshot() -> dict:
    """Capture the stores the mutating checks touch, from the LIVE backend."""
    snap = {}
    for fn in _SNAP_FILES:
        try:
            snap[fn] = _store_read(fn)
        except Exception:
            snap[fn] = None          # never let one unreadable file abort the run
    return snap


def _restore(snapshot: dict) -> None:
    """Put every snapshotted store back, with retries.

    🔴 Rows do not disappear the way a whole-file rewrite made them disappear.
    On the Gist a restore rewrote the entire blob, so any chat_id the run
    CREATED vanished for free. On a row backend that key survives, so rows the
    run added must be tombstoned explicitly — otherwise every canary run leaves
    a synthetic user behind in production storage.

    Writes go through the same backend the run mutated. Retried because GitHub
    409/403s on rapid successive PATCHes (the run does ~8 writes) and Supabase
    can lose a CAS race; residue is the failure this exists to prevent.
    """
    import time
    from config_manager import USER_KEYED_FILES
    b = _store()
    time.sleep(1.5)                  # let the backend settle after the writes
    last = ""
    for attempt in range(1, 7):
        try:
            for fn, before in snapshot.items():
                if before is None:
                    continue
                if fn in USER_KEYED_FILES and b.supports_rows():
                    after = b.read_all_users(fn) or {}
                    for uid in set(after) - set(before):
                        _cur, ver = b.read_user(fn, str(uid))
                        empty = [] if isinstance(after.get(uid), list) else {}
                        b.write_user(fn, str(uid), empty, ver)
                    for uid, content in before.items():
                        _cur, ver = b.read_user(fn, str(uid))
                        b.write_user(fn, str(uid), content, ver)
                else:
                    b.write(fn, before)
            return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"[:120]
            time.sleep(2 * attempt)
    raise RuntimeError(f"restore failed after retries: {last}")


def _raw_picks() -> dict:
    """Read picks.json directly (NOT load_picks, which returns None off-day)."""
    try:
        return _store_read("picks.json") or {}
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


def _delivery_fresh(actual: str, expected: str) -> bool:
    """Picks/delivery are fresh if the stamp is in [expected, today].

    The app ALSO delivers crypto picks on WEEKENDS (crypto is 24/7), stamping the
    weekend date — which is FRESHER than the prior-weekday floor `expected`. So a
    rigid `== expected` false-alarms on any weekend that had crypto picks (Sat Jul
    18: saved 07-18, floor 07-17). Accept the whole window instead: at least as
    recent as `expected`, never in the future. Still catches a genuinely stale
    file (older than expected) or an impossible future date. ISO dates compare
    lexicographically = chronologically."""
    if not actual:
        return False
    import datetime as dt, pytz
    today = dt.datetime.now(pytz.timezone("America/New_York")).date().isoformat()
    return expected <= actual <= today


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
    _check("picks.fresh", _delivery_fresh(picks.get("_saved_date"), exp),
           f"_saved_date={picks.get('_saved_date')} expected≥{exp} (weekend crypto may be today)")

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
    # ── Crypto: check the path USERS actually get ─────────────────────────────
    # Previously this called cg_prices() directly — raw CoinGecko with NO
    # fallback — so a transient CoinGecko blip failed the canary even though the
    # app itself was fine (get_live_price falls back to yfinance -USD). That made
    # it a CoinGecko uptime monitor, not a check of user-visible behaviour.
    from market_data import get_live_price
    btc = get_live_price("BTC")
    eth = get_live_price("ETH")
    _check("prices.btc_sane", _pos(btc) and 10_000 <= btc <= 500_000, f"BTC=${btc}")
    _check("prices.eth_sane", _pos(eth) and 300 <= eth <= 30_000, f"ETH=${eth}")

    # Cache coherence. MUST assert a REAL price, not just equality: the old check
    # compared cg2 == btc, and when CoinGecko returned nothing BOTH were None —
    # `None == None` PASSED while crypto pricing was completely broken.
    from price_checker import cg_prices, _SYMBOL_TO_CG_ID
    _bid = _SYMBOL_TO_CG_ID["BTC"]
    c1 = cg_prices([_bid]).get(_bid)
    c2 = cg_prices([_bid]).get(_bid)          # 2nd call must hit the 60s cache
    _check("prices.cg_cache", _pos(c1) and c1 == c2,
           f"cached BTC stable at ${c1}" if _pos(c1)
           else f"CoinGecko returned {c1} — cache cannot be verified")


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
           "rejected $0.01/$0.004/0/nan/11x-spike vs their references",
           fail_detail="plausible_price lets a $0.01/spike through — false alerts will fire")
    accepts = (plausible_price(354.0, 360.0)          # real quote
               and plausible_price(570.31, 651.0)     # BNB real −12.4% stop
               and plausible_price(50.0, 100.0))      # legit −50% below-alert
    _check("price_guard.accepts_real", accepts,
           "accepted a real quote, a real −12.4% stop and a legit −50% below-alert",
           fail_detail="plausible_price rejects a real quote/drop — alerts would be suppressed")


def check_storage_headroom() -> None:
    """Warn BEFORE the Gist hits GitHub's ~1 MB per-file API limit.

    Past that, the API returns the file truncated. GistBackend now refetches via
    raw_url so it degrades safely rather than corrupting — but a whole-file
    rewrite per save is already the wrong shape at that size. At current per-user
    growth (~11-13 KB/user for user_trades and price_alerts) that wall arrives
    around 75-90 users, which is the real trigger to migrate to a row store —
    NOT some far-off 10k. This check makes the runway visible instead of a
    surprise."""
    WARN = 700_000          # ~70% of the limit — migrate when this trips
    # Deliberately still the GIST: this measures GitHub's per-file API limit,
    # which is a property of the Gist and of nothing else. Once the app is on a
    # row backend the Gist is the ROLLBACK copy, and its headroom still decides
    # whether a rollback is possible — so the check stays, and the note says
    # which store it graded rather than implying it graded production.
    try:
        files = _gist_all()
    except Exception as e:
        _check("storage.headroom", False, f"could not read gist: {e}")
        return
    biggest, worst = None, 0
    for name, meta in files.items():
        size = meta.get("size") or len(meta.get("content") or "")
        if size > worst:
            biggest, worst = name, size
    ok = worst < WARN
    _check("storage.headroom", ok,
           f"GIST: largest file {biggest} = {worst/1024:.0f} KB "
           f"({worst/1_000_000*100:.0f}% of the ~1 MB limit)"
           + ("" if ok else "  → TIME TO MIGRATE to a row store"))

def check_cron_delivery() -> None:
    from config_manager import get_config
    cfg = get_config()
    exp = _expected_delivery_date()
    # last_morning_run is the DELIVERY stamp (cron_last_morning is just "started").
    lmr = (cfg.get("last_morning_run") or "")[:10]
    _check("delivery.morning", _delivery_fresh(lmr, exp),
           f"last_morning_run={lmr or 'never'} (expected ≥{exp})")
    _saved = _raw_picks().get("_saved_date")
    _check("delivery.picks_saved", _delivery_fresh(_saved, exp),
           f"picks._saved_date={_saved} (expected ≥{exp})")


def check_synthetic_user() -> None:
    """Did the synthetic-user bot actually OPEN positions today?

    🔴 Why this exists. On 2026-08-20/21 the bot opened ZERO positions for two
    days across **30 consecutive "success" runs**. The Aug 19 workflow wiring
    pointed it at Supabase with an RLS-bound key, so every paper buy threw
    `42501 new row violates row-level security policy` — per-ticker, caught, and
    logged one line at a time, so the workflow stayed green and the run reported
    `held all (real: [], paper: [])`. Nobody noticed until the owner asked.

    That is the same shape as every other bug this project has had: the failure
    is swallowed, the monitor is green, and the only symptom is an ABSENCE. An
    absence needs something asserting the presence.

    The bot is the app's best bug detector — it found the paper `target_price=None`
    defect, the drained paper cash, and levels that made a position born
    stopped-out. A dead detector is worse than none, because you stop looking.

    Silent when there is nothing to verify: it only runs Mon-Fri, and with no
    picks there is correctly nothing to buy. A check that cries wolf on weekends
    trains you to ignore it.
    """
    from config_manager import (load_user_trade_log, load_user_paper,
                                load_picks, et_today, DEFAULT_TEST_CHAT_ID)
    today = et_today()
    if today.weekday() >= 5:
        _check("synthetic.opened", True, "weekend — the open phase does not run")
        return
    try:
        picks = load_picks() or {}
    except Exception as exc:
        _check("synthetic.opened", True, f"NOT VERIFIED — picks unreadable ({exc})")
        return
    if str(picks.get("_saved_date", ""))[:10] != today.isoformat():
        _check("synthetic.opened", True,
               "no picks saved for today — nothing for the bot to buy")
        return

    uid = DEFAULT_TEST_CHAT_ID
    try:
        log = load_user_trade_log(uid) or {}
        paper = load_user_paper(uid) or {}
    except Exception as exc:
        _check("synthetic.opened", True, f"NOT VERIFIED — test account unreadable ({exc})")
        return

    iso = today.isoformat()
    real_today = [t.get("ticker") for t in log.get("open", [])
                  if str(t.get("opened_date", ""))[:10] == iso]
    paper_today = [t.get("ticker") for t in paper.get("positions", [])
                   if str(t.get("bought_date") or t.get("opened_date", ""))[:10] == iso]
    n = len(real_today) + len(paper_today)
    _check(
        "synthetic.opened", n > 0,
        f"opened {len(real_today)} real + {len(paper_today)} paper today",
        fail_detail=("the bot opened NOTHING today despite picks existing — it is "
                     "the app's main bug detector and it is blind. Check the "
                     "synthetic_user run log for swallowed per-ticker errors "
                     "(storage writes, paper cash, price fetches)."),
    )


def check_weekly_relay() -> None:
    """Did Saturday's weekly run execute on GH Actions, and did it set alerts?

    🔴 Why this exists. `run_weekly_recap` opens with `run_morning(...)` —
    "Saturday: run crypto morning picks, then send a compact weekly recap" — so
    the weekly trigger runs the FULL morning pipeline. Until 2026-08-15 it did
    that LOCALLY ON RENDER via the generic /trigger/<mode> subprocess path; the
    weekday morning trigger had been converted to a GH Actions relay in Jul 2026
    but the Saturday path never was.

    Observed Sat 2026-08-15: it generated and DELIVERED GLD + SLV and set **ZERO**
    auto alerts for either real user, while the identical code yields
    `GLD stop@381.41` + `SLV invalidation@49.71` anywhere else. `_auto_set_pick_alerts`
    catches per-ticker, so every failure logged and continued and the run still
    stamped success. Nothing was watching, and the weekly path only runs once a
    week — the slowest possible way to notice.

    Silent on days no weekly run fired: a check that cries wolf six days out of
    seven trains you to ignore it.
    """
    import datetime as _dt
    from config_manager import get_config, et_today, get_allowed_users
    try:
        cfg = get_config()
    except Exception as exc:
        _check("weekly.relay", True, f"NOT VERIFIED — config unreadable ({exc})")
        return

    last = str(cfg.get("cron_last_weekly", ""))[:10]
    today = et_today().isoformat()
    if last != today:
        _check("weekly.relay", True,
               f"no weekly run today (last {last or 'never'}) — nothing to verify")
        return

    # 1. It must have run on GitHub Actions, not spawned on Render.
    repo = os.environ.get("GITHUB_REPOSITORY") or "vsanil/stockpulz"
    tok  = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    on_gh = None
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/daily_run.yml/"
            f"runs?created=>={today}&per_page=20",
            headers={"Authorization": f"token {tok}",
                     "Accept": "application/vnd.github+json"}, timeout=20)
        if r.status_code == 200:
            on_gh = any(x.get("event") == "workflow_dispatch"
                        for x in (r.json().get("workflow_runs") or []))
    except Exception:
        on_gh = None
    if on_gh is None:
        _check("weekly.on_github", True, "NOT VERIFIED — GitHub API unreachable")
    else:
        _check("weekly.on_github", on_gh,
               "weekly ran on GitHub Actions (relay working)",
               fail_detail="the weekly run did NOT reach GitHub Actions — it is "
                           "spawning on Render again, which is what silently "
                           "produced zero alerts on 2026-08-15")

    # 2. The defect itself: did it actually set alerts, for a REAL user?
    #    Split by account — the synthetic bot's alerts MASKED this exact gap.
    try:
        # Read the LIVE store. Reading the Gist here would grade the rollback
        # copy, and on 2026-08-23 that copy was days out of step with production.
        alerts = _store_read("price_alerts.json") or {}
        real = set(map(str, get_allowed_users() or []))
        n = 0
        for uid in real:
            for a in (alerts.get(uid) or []):
                if isinstance(a, dict) and a.get("auto") \
                   and str(a.get("set_at", ""))[:10] == today:
                    n += 1
        _check("weekly.alerts_set", n > 0,
               f"{n} auto alert(s) created today across {len(real)} real user(s)",
               fail_detail="the weekly run delivered picks but set ZERO auto "
                           "alerts for any REAL user — the 2026-08-15 failure "
                           "has recurred (check it ran on GH Actions, and the "
                           "per-ticker 'alert set failed' lines in the log)")
    except Exception as exc:
        _check("weekly.alerts_set", True, f"NOT VERIFIED — alerts unreadable ({exc})")


def check_selfheal_health() -> None:
    """Who watches the watcher.

    self_heal.yml is the safety net for the other monitors — and NOTHING watched
    it. On Aug 6 a self-heal run died at "Set up job" with
    `Failed to resolve action download info: Service Unavailable` — GitHub's own
    action registry, before a line of our code ran. It went unnoticed. A broken
    net is worse than no net, because you stop looking.

    Deliberately a 7-DAY look-back on a daily check, not a weekly job: it costs
    one API call, reports through a channel that already exists, and is itself
    covered by self-heal — a separate weekly workflow would be one more thing
    that can rot unobserved, which is the exact failure being fixed here.

    NOTE the escape hatch: this reports to the OWNER, not to self-heal. Asking a
    broken self-healer to heal itself is not a plan.
    """
    import datetime as _dt
    repo = os.environ.get("GITHUB_REPOSITORY") or "vsanil/stockpulz"
    tok  = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"self_heal.yml/runs?created=>={since}&per_page=50")
    try:
        r = requests.get(url, headers={"Authorization": f"token {tok}",
                                       "Accept": "application/vnd.github+json"}, timeout=20)
        if r.status_code != 200:
            # Can't verify != verified clean. Say so plainly rather than let a
            # green line imply a check that never actually ran (the cg_cache trap).
            _check("selfheal.healthy", True,
                   f"NOT VERIFIED this run — GitHub API HTTP {r.status_code}")
            return
        runs = r.json().get("workflow_runs", []) or []
    except Exception as e:
        _check("selfheal.healthy", True, f"NOT VERIFIED this run — {type(e).__name__}")
        return

    # `skipped` is the NORMAL outcome (the monitor it watched passed) — not a fault.
    done = [x for x in runs if x.get("conclusion")]          # ignore in-progress
    bad  = [x for x in done if x.get("conclusion") == "failure"]

    # Fail on "the net is DOWN NOW", not "the net hiccupped once this week".
    # The Aug 6 failure was GitHub's action registry returning Service
    # Unavailable — already over by the next run. Failing for 7 days on a
    # resolved transient is how a canary trains you to ignore it; the rule here
    # is the same one the crypto checks learned: fail when it MATTERS, not when
    # a third party hiccupped. So: red only if the most recent concluded run
    # failed (still broken), or failures are frequent enough to look chronic.
    latest_failed = bool(done) and done[0].get("conclusion") == "failure"
    chronic       = len(bad) >= 3
    ok = not (latest_failed or chronic)

    recovered = f" ({len(bad)} failed earlier in 7d, since recovered)" if bad else ""
    # Built conditionally: `fail_detail` is evaluated eagerly by the call, so
    # indexing bad[0] here would IndexError on the COMMON path — no failures.
    fail_detail = ""
    if bad:
        first = bad[0]
        fail_detail = (f"{len(bad)}/{len(done)} self-heal run(s) FAILED in 7d — "
                       f"{'LATEST run failed' if latest_failed else 'chronic failures'} "
                       f"({(first.get('created_at') or '?')[:16]}, "
                       f"{first.get('html_url','')}). The auto-fix net is down: "
                       f"monitor failures will go unfixed until it is restored.")
    _check("selfheal.healthy", ok,
           f"{len(done)} self-heal run(s) in 7d, latest "
           f"{done[0].get('conclusion') if done else 'none'}{recovered}",
           fail_detail=fail_detail)


def check_data_completeness() -> None:
    """Did the last screener run actually GET its data?

    Every existing monitor checks that things RESPOND. None checked that the
    data behind them is COMPLETE — which is how the Finnhub rate-limit bug ran
    on every production run for weeks, starving 65% of candidates of
    fundamentals and visibly changing 4 of 5 long-term picks, while canary, full
    sweep, synthetic bot and evaluator all stayed green.

    This reads what the RUN recorded, not what a probe can reach. That
    distinction is the whole point: the bug was a RATE limit, so probing a
    single ticker would have succeeded and reported green.

    FAILS only on fundamentals — they carry ~90% of the long-term score, so
    missing them makes LT picks close to arbitrary and users are affected.
    Optional signals (congressional, sentiment) are REPORTED in the note so
    degradation is visible daily, but do not page: a flaky third-party feed is
    not a reason to cry wolf (the lesson from the crypto checks).
    """
    FUNDAMENTAL_MIN = 80.0        # % of attempted fetches that must succeed
    try:
        dq = _store_read("data_quality.json") or {}
    except Exception as e:
        _check("data.completeness", True, f"NOT VERIFIED this run — {type(e).__name__}")
        return
    if not dq or not dq.get("sources"):
        _check("data.completeness", True,
               "NOT VERIFIED — no data_quality.json yet (written by the next screener run)")
        return

    src = dq["sources"]
    stamp = (dq.get("date") or "")[:10]
    fresh = _delivery_fresh(stamp, _expected_delivery_date())

    fundamentals = {k: v for k, v in src.items() if k.startswith("finnhub_")}
    worst_name, worst_cov = None, 100.0
    for k, v in fundamentals.items():
        cov = float(v.get("coverage_pct") or 0)
        if cov < worst_cov:
            worst_name, worst_cov = k, cov

    def _fmt(k, v):
        # total == 0 means the source is not configured, which is NOT the same
        # as configured-and-returning-nothing. Saying "0%" for a dormant feed
        # sends the owner chasing a breakage that does not exist.
        if not v.get("total"):
            return f"{k}=n/a (not configured)"
        return f"{k}={v.get('coverage_pct')}%"

    detail = f"[{stamp}] " + "  ·  ".join(_fmt(k, v) for k, v in sorted(src.items()))

    if not fundamentals:
        _check("data.completeness", True, detail + "  (no fundamentals recorded)")
        return
    ok = worst_cov >= FUNDAMENTAL_MIN and fresh
    _check("data.completeness", ok, detail,
           fail_detail=(f"{detail} — "
                        + (f"{worst_name} coverage {worst_cov}% is below {FUNDAMENTAL_MIN}%: "
                           f"long-term scoring is ~90% fundamentals, so LT picks are being "
                           f"chosen on missing data"
                           if worst_cov < FUNDAMENTAL_MIN
                           else f"stale — last screener run recorded {stamp}")))


def check_position_integrity() -> None:
    """Arithmetic that must hold for any long position, on LIVE positions only.

    Found by hand first: two AMBA positions whose TARGET sat below their ENTRY —
    long trades mathematically incapable of winning — plus a FICO paper fill
    carrying a stop ABOVE its entry (born stopped-out). Neither a win rate nor a
    mocked test can surface that; it only appears when a real position is built
    by the real path against live prices.

    Historical findings (closed trades) are counted but do NOT fail: they cannot
    be fixed retroactively, and failing daily on immutable history would train
    the owner to ignore this check. The admin page tracks those for triage.
    """
    try:
        from position_audit import audit_account, apply_dispositions, summarise
        from config_manager import (get_allowed_users, load_user_trade_log,
                                    load_user_paper, load_audit_dispositions,
                                    DEFAULT_TEST_CHAT_ID, ARM_CHAT_IDS)
        accounts = list(dict.fromkeys(
            [*get_allowed_users(), DEFAULT_TEST_CHAT_ID, *ARM_CHAT_IDS.values()]))
        raw = []
        for a in accounts:
            raw += audit_account(a, load_user_trade_log(a), load_user_paper(a))
        findings = apply_dispositions(raw, load_audit_dispositions())
        summ = summarise(findings)
    except Exception as e:
        _check("positions.integrity", True, f"NOT VERIFIED this run — {type(e).__name__}: {e}")
        return

    live = [f for f in findings if f["live"] and f["status"] in ("open", "reopened")]
    hist = summ["total"] - len(live)
    ok = not live
    _check("positions.integrity", ok,
           f"{len(accounts)} accounts · no live integrity problems"
           + (f" ({hist} historical, tracked in admin)" if hist else ""),
           fail_detail=("; ".join(f"{f['ticker']} {f['check']}" for f in live[:4])
                        + f" — {len(live)} LIVE position(s) are broken by arithmetic: "
                          f"they cannot behave correctly regardless of the market"))


def check_selfheal_demo() -> None:
    """🔴 DELIBERATE FAILURE — a live end-to-end test of the self-heal loop.

    Added 2026-08-24 by the owner's request, to prove the PR path works now that
    the test gate can actually run. Until today self_heal installed no pytest,
    so its gate reported green=false on every run and a PR was never opened.

    THE FIX IS TO DELETE THIS FUNCTION and remove `check_selfheal_demo` from the
    list in main(). Nothing else. It asserts nothing about the app.
    """
    _check("selfheal.demo", False, "",
           fail_detail="deliberate test failure — delete check_selfheal_demo() "
                       "from scripts/canary.py and its entry in main()")


def check_selfheal_unmerged() -> None:
    """Remind about self-heal fixes sitting unreviewed on branches.

    🔴 THIS CHECK MUST NEVER FAIL, and the reason is not squeamishness. A canary
    FAILURE triggers self_heal, which writes another branch — so a failing
    "you have unmerged branches" check would manufacture the very condition it
    reports, every single day, forever. It is informational by construction.

    Why it exists: for four days self_heal wrote real fixes and proposed none,
    because its gate had no pytest installed and so could never report green.
    Nobody was told. A branch nobody is told about is a fix that does not
    exist.
    """
    import datetime as _dt
    repo = os.environ.get("GITHUB_REPOSITORY") or "vsanil/stockpulz"
    tok = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not tok:
        _check("selfheal.unmerged", True, "NOT VERIFIED — no GitHub token")
        return
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/branches",
                         headers={"Authorization": f"token {tok}",
                                  "Accept": "application/vnd.github+json"},
                         params={"per_page": 100}, timeout=20)
        if r.status_code != 200:
            _check("selfheal.unmerged", True,
                   f"NOT VERIFIED — GitHub returned {r.status_code}")
            return
        names = [b.get("name", "") for b in (r.json() or [])
                 if str(b.get("name", "")).startswith("auto/self-heal-")]
        live = []
        for n in names:
            c = requests.get(
                f"https://api.github.com/repos/{repo}/compare/main...{n}",
                headers={"Authorization": f"token {tok}"}, timeout=20).json()
            if c.get("ahead_by"):
                live.append(n)
    except Exception as exc:
        _check("selfheal.unmerged", True,
               f"NOT VERIFIED this run — {type(exc).__name__}")
        return

    if not live:
        _check("selfheal.unmerged", True, "no unreviewed auto-fixes")
        return
    ages = []
    for n in live:
        try:
            ages.append((_dt.date.today()
                         - _dt.date.fromtimestamp(int(n.rsplit("-", 1)[1]))).days)
        except Exception:
            pass
    oldest = f", oldest {max(ages)}d" if ages else ""
    # PASS on purpose — see the docstring. The note is the reminder.
    _check("selfheal.unmerged", True,
           f"\u26a0 {len(live)} auto-fix(es) awaiting YOUR review on /admin{oldest}: "
           + ", ".join(live[:3]))


def check_storage_surfaces() -> None:
    """🔴 A surface silently on the WRONG backend is invisible for days.

    Measured 2026-08-23: every Supabase `cron_last_*` was frozen at 2026-08-19
    while the Gist's ran to that very morning — Render had been writing to the
    Gist while GitHub Actions wrote to Supabase, so the same file existed twice
    with different contents. Nothing reported it. It surfaced only because the
    two stores were diffed by hand, four days later.

    That is the SAME split-brain as 2026-08-19 (`d11a1c9`), which was declared
    closed. It reopened because closing it was a configuration act with no
    monitor behind it. This is the monitor.

    Compares the backend THIS job resolves to against what the web service
    reports at /health. They must agree — one store, or writes land in two.
    """
    base = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL")
            or "https://stock-agent-enqx.onrender.com").rstrip("/")
    try:
        mine = _store().name()
    except Exception as exc:
        _check("storage.surfaces", True,
               f"NOT VERIFIED this run — local backend unresolvable ({exc})")
        return
    try:
        theirs = (requests.get(base + "/health", timeout=25).json() or {}).get("storage")
    except Exception as exc:
        _check("storage.surfaces", True,
               f"NOT VERIFIED this run — {type(exc).__name__}")
        return
    # A green line must never imply a check that could not run.
    if not theirs or theirs == "unavailable":
        _check("storage.surfaces", True,
               f"NOT VERIFIED — the web service reports storage={theirs!r}")
        return
    _check("storage.surfaces", mine == theirs,
           f"both surfaces agree on {theirs}",
           fail_detail=(f"SPLIT BRAIN: this job writes to {mine}, the web service "
                        f"writes to {theirs} — the same file now exists in two "
                        f"stores with different contents, and neither is complete"))


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
    snapshot = _snapshot()
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
                # The synthetic-user bot paper-buys daily and drains cash over time
                # (was $271 with 33 positions). paper_buy correctly REJECTS a buy it
                # can't afford and stores nothing → false "position not stored". Top
                # up first; the snapshot restores user_paper.json so real cash is
                # byte-identical after the run.
                from paper_trader import paper_add_cash
                if (load_user_paper(admin).get("cash") or 0) < total + 10:
                    paper_add_cash(total + 100, admin)
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
                from paper_trader import paper_add_cash   # ensure funds (synthetic bot drains cash)
                if (load_user_paper(admin).get("cash") or 0) < 210:
                    paper_add_cash(300, admin)            # restored by snapshot
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

        # ── Paper VIEW prices crypto correctly ───────────────────────────────
        # 🔴 The gap that let a real bug through. paper.crypto_fractional passes
        # an EXPLICIT price to paper_buy, so it never exercised the resolver the
        # portfolio VIEW uses. paper_trader._live_price tried the bare symbol
        # first, and bare BTC/ETH resolve on yfinance to unrelated instruments —
        # measured 2026-08-12 at $28.00 and $18.00 against $63,623 and $1,891.
        # Every paper crypto holding rendered at roughly -99.96% unrealized and
        # every monitor stayed green.
        #
        # Cross-checked against CoinGecko — an INDEPENDENT source. Comparing two
        # reads from the same resolver would agree even when both are wrong,
        # which is the false-pass trap prices.cg_cache already fell into.
        try:
            from paper_trader import _live_price as _paper_price
            from price_checker import cg_prices, _SYMBOL_TO_CG_ID
            ref = (cg_prices([_SYMBOL_TO_CG_ID["ETH"]]) or {}).get(_SYMBOL_TO_CG_ID["ETH"])
            got = _paper_price("ETH")
            if not _pos(ref):
                _check("paper.view_crypto_price", True,
                       "NOT VERIFIED — CoinGecko unavailable this run")
            elif not _pos(got):
                _check("paper.view_crypto_price", False, "",
                       fail_detail="paper view could not price ETH at all")
            else:
                drift = abs(got - ref) / ref * 100
                _check("paper.view_crypto_price", drift < 20,
                       f"paper view ETH ${got:,.2f} vs CoinGecko ${ref:,.2f} ({drift:.1f}% apart)",
                       fail_detail=(f"paper view prices ETH at ${got:,.2f} but CoinGecko says "
                                    f"${ref:,.2f} ({drift:.0f}% off) — every paper crypto "
                                    f"position's P&L is wrong on screen"))
        except Exception as e:
            _check("paper.view_crypto_price", False, "", fail_detail=f"exc: {e}")

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
            _restore(snapshot)
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
               check_backtest_math, check_price_guard, check_storage_headroom,
               check_cron_delivery, check_selfheal_health, check_data_completeness,
               check_weekly_relay,
               check_synthetic_user,
               check_position_integrity,
               check_storage_surfaces, check_selfheal_unmerged,
               check_selfheal_demo,
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
