#!/usr/bin/env python3
"""
synthetic_user.py — an automated "active user" for stabilization testing.

Runs several times a day on a dedicated VIRTUAL TEST account and performs the full
user lifecycle, so real-usage bugs (P&L drift over days, alerts firing, position
tracking) surface — without polluting the owner's real account:

  --phase open    (once, ~after the morning picks): from today's picks, LOG REAL
                  "I Bought This" positions + PAPER-buy EVERY pick, set target
                  alerts, and add them to the watchlist.
  --phase manage  (hourly, market hours): check the bot's own positions and SELL
                  winners at target / cut at stop (real via close_trade, paper via
                  paper_sell) — or EXPIRE them at the time stop.
  --phase reset   (once, by hand): liquidate the paper book, set it to
                  _BOOK_START_USD and start a fresh equity curve. History is KEPT.

THE PAPER BOOK IS A TRADER, NOT A SAMPLER (Phase 1 of the tournament, 2026-09-19).
Until then it bought a flat $500 of every pick, never exited on time, and kept
no equity curve — a bug detector, not a model of a user. Now:
  • every paper buy is SIZED by the app's own `position_sizer.size_pick` against
    the book's current equity, exactly as the morning message sizes it;
  • the portfolio rules `apply_portfolio_sizing` only WARNS about are HARD here
    (max positions, deployed cap, total open risk) — a rule that never blocks a
    trade is a comment;
  • a long-term pick's stop is the app's own invalidation level
    (agent.LT_INVALIDATION_PCT), recorded under its own `levels_source`, not the
    ±5% short-term fallback;
  • every position has a TIME STOP (30 d short-term / 180 d long-term, matching
    the evaluator's horizons) so every trade RESOLVES and the exit mix stops
    being a survivor sample;
  • no scale-in, no free cash top-ups: the book is closed, so the curve is real;
  • `sim_portfolio` snapshots equity daily beside a SPY twin that receives the
    SAME cash flows on the SAME days. That curve is the Phase-1 headline.
The REAL "I Bought This" positions are unchanged: flat $1,000, four a day. They
exercise the real-position and alert paths and are NOT part of the curve.

ACCOUNTS (do not conflate):
  • _TRADE_ID  — the VIRTUAL test account it trades on (SYNTHETIC_CHAT_ID). Kept
    OUT of allowed_users on purpose, so it gets no broadcasts and is excluded from
    community stats + the LLM pick-feedback loop (a robot's mechanical fills must
    never steer real users' picks). Sends to it are skipped in telegram_api.
  • _OWNER_ID  — the real admin (TELEGRAM_CHAT_ID). Receives the REPORTS only.

SAFETY: the bot tracks the tickers IT opened in a PER-ACCOUNT state file and ONLY
manages those — it never touches positions a human opened. The pre-split owner
state (synthetic_state.json) is wound down, never added to: `manage` keeps selling
those legacy positions at target/stop so the account switch cannot orphan them.

Usage: python3 scripts/synthetic_user.py --phase open|manage|reset [--dry-run]
"""
from __future__ import annotations

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

from config_manager import DEFAULT_TEST_CHAT_ID, SYNTHETIC_SOURCE

_GID = os.environ.get("GIST_ID")
_TOK = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")

_TRADE_ID = (os.environ.get("SYNTHETIC_CHAT_ID") or DEFAULT_TEST_CHAT_ID).strip()
_OWNER_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

_LEGACY_STATE_FILE = "synthetic_state.json"   # owner's pre-split state — wind-down only
_MAX_REAL = 4       # real "I Bought This" positions opened per day (test account)
_MAX_PAPER = 20     # paper candidates considered per day; the BOOK RULES do the capping
_REAL_USD = 1000.0  # real positions stay flat — they are not part of the equity curve

# ── The paper BOOK ──────────────────────────────────────────────────────────
_BOOK_START_USD = 10_000.0
# Time stops. Pinned by test to the evaluator's own horizons
# (evaluate_picks._HORIZON_DAYS, backtest_longterm.LT_HORIZON_DAYS) rather than
# imported: both modules drag in pandas/yfinance and one a 65 MB SEC cache.
_ST_HORIZON_DAYS = 30
_LT_HORIZON_DAYS = 180
# A long-term pick carries no stop by design. The app's own answer for "the
# thesis may be broken" is agent.LT_INVALIDATION_PCT — pinned by test, not
# imported (agent costs ~121 MB to import). It is NOT a stop and the
# levels_source says so, so the exit mix can tell the two apart.
_LT_INVALIDATION_PCT = 15.0
# The rules position_sizer.apply_portfolio_sizing only WARNS about, applied
# HARD to the running book. Same numbers as its warnings: >8 positions, >80%
# deployed, >5% of equity at risk across open stops.
_BOOK_RULES = {"max_positions": 8, "max_deployed_pct": 80.0, "max_total_risk_pct": 5.0}
_SIZER_DEFAULTS = {"risk_per_trade_pct": 1.0, "max_position_pct": 10.0, "max_risk_pct": 1.5}


def _pos(x) -> bool:
    try:
        import math
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


_FALLBACK_STOP_PCT   = 5.0
_FALLBACK_TARGET_PCT = 8.0


def _levels_for(px: float, stop, target, lt: bool = False) -> tuple[float, float, str]:
    """Levels that actually bracket the price we FILLED at, plus their SOURCE.

    LONG-TERM (`lt=True`): a missing stop is BY DESIGN, and substituting the
    short-term ±5% fallback made every LT position a 5%-stop trade the engine
    never published. It now gets the app's own invalidation level
    (_LT_INVALIDATION_PCT below the fill) under source "invalidation" — so the
    exit mix can separate "the thesis was invalidated" from "a fallback stop
    was hit" from "the pick's own stop was hit". A target is still filled from
    the +8% fallback when absent, and the source says so ("invalidation+target").

    A pick's stop/target are relative to the pick's entry. If the live price has
    since moved past one of them, inheriting them blindly creates a position born
    already stopped-out (or already at target) — the next manage run closes it
    instantly and books a fabricated loss/gain. Seen live: a paper FICO filled at
    $1,177.74 carrying the pick's $1,290 stop. Fall back to a % of the real fill.

    Returns (stop, target, source), where `source` records WHICH leg came from
    the pick and which was substituted:

        "pick"  both inherited        "stop"    the stop was substituted
        "both"  both substituted      "target"  the target was substituted

    🔴 Why the source matters. Without it the exit-reason mix is CONFOUNDED: a
    stop-out on a SUBSTITUTED stop says nothing about the engine's published
    levels, only about the ±5%/8% fallback. Recording it lets the analysis
    separate "did OUR levels get hit" from "did the fallback get hit" — the
    difference between a measurement and a number.
    """
    px = float(px)
    s = float(stop) if _pos(stop) else None
    t = float(target) if _pos(target) else None
    sub_s = s is None or s >= px
    sub_t = t is None or t <= px
    if sub_s:
        pct = _LT_INVALIDATION_PCT if lt else _FALLBACK_STOP_PCT
        s = round(px * (1 - pct / 100), 4)
    if sub_t:
        t = round(px * (1 + _FALLBACK_TARGET_PCT / 100), 4)
    if lt and sub_s:
        source = "invalidation+target" if sub_t else "invalidation"
    else:
        source = ("both" if sub_s and sub_t else
                  "stop" if sub_s else "target" if sub_t else "pick")
    return s, t, source


def _horizon_days(lt: bool) -> int:
    return _LT_HORIZON_DAYS if lt else _ST_HORIZON_DAYS


def _age_days(opened: str | None, today) -> int | None:
    """Calendar days since `opened` (ISO date) on the ET clock, or None when
    the open date is unusable — an undatable position is never expired."""
    import datetime as _dt
    try:
        return (today - _dt.date.fromisoformat(str(opened)[:10])).days
    except (TypeError, ValueError):
        return None


def _size(u: dict, px: float, stop: float, equity: float) -> dict:
    """Size ONE paper buy with the app's own sizer against the book's equity.

    Returns {"shares", "usd", "risk_usd", "sizing"}. Stocks/ETFs/commodities
    take the sizer's whole-share count; crypto takes its dollar amount as a
    fractional quantity, exactly as the morning message and /size do. Risk is
    computed from the position's ACTUAL stop, not the sizer's clamped one, so
    the book's risk rule sees what the position can really lose.
    """
    from position_sizer import size_pick
    pick = {"entry_price": float(px), "stop_loss": float(stop),
            "conviction": u.get("conviction") or 3,
            "suggested_stop_pct": u.get("suggested_stop_pct"),
            "atr_pct": u.get("atr_pct")}
    cfg = {"portfolio": dict(_SIZER_DEFAULTS, portfolio_size=float(equity))}
    is_crypto = u.get("atype") == "crypto"
    sz = size_pick(pick, cfg, is_crypto=is_crypto)
    usd = float(sz.get("dollar_amount") or 0)
    if is_crypto:
        shares = round(usd / float(px), 8) if usd > 0 else 0.0
    else:
        shares = float(sz.get("shares") or 0)
        usd = round(shares * float(px), 2)
    risk = round(max(float(px) - float(stop), 0.0) * shares, 2)
    return {"shares": shares, "usd": usd, "risk_usd": risk, "sizing": sz}


def _book_block(book: dict, add_usd: float, add_risk: float) -> str | None:
    """The portfolio rules as HARD rules. `book` is the running tally for this
    run: {"equity", "n_open", "deployed", "risk"}. Returns the reason a buy is
    refused, or None. A rule that only warns is a comment — and the old bot
    proved it by holding 33 positions at once."""
    eq = float(book.get("equity") or 0)
    if eq <= 0:
        return "book equity unknown"
    if book.get("n_open", 0) >= _BOOK_RULES["max_positions"]:
        return f"max_positions ({_BOOK_RULES['max_positions']} open)"
    if (book.get("deployed", 0.0) + add_usd) / eq * 100 > _BOOK_RULES["max_deployed_pct"]:
        return f"deployed cap ({_BOOK_RULES['max_deployed_pct']:.0f}% of equity)"
    if (book.get("risk", 0.0) + add_risk) / eq * 100 > _BOOK_RULES["max_total_risk_pct"]:
        return f"risk cap ({_BOOK_RULES['max_total_risk_pct']:.0f}% of equity at risk)"
    return None


def _book_tally(paper: dict, price_of) -> dict:
    """Where the book stands NOW: equity (live marks), open count, dollars
    deployed and dollars at risk across open stops. `equity_source` says
    whether the marks were live or cost basis — a fallback must be visible."""
    import sim_portfolio as sp
    positions = paper.get("positions") or []
    deployed = sum(float(p.get("avg_price") or 0) * float(p.get("shares") or 0)
                   for p in positions)
    risk = 0.0
    for p in positions:
        stop, avg, sh = p.get("stop_loss"), p.get("avg_price"), p.get("shares")
        if _pos(stop) and _pos(avg) and _pos(sh) and float(stop) < float(avg):
            risk += (float(avg) - float(stop)) * float(sh)
    eq = sp.bot_equity(paper, price_of)
    src = "live"
    if eq is None:
        eq = round(float(paper.get("cash") or 0) + deployed, 2)
        src = "cost basis (a position could not be priced)"
    return {"equity": eq, "equity_source": src, "n_open": len(positions),
            "deployed": round(deployed, 2), "risk": round(risk, 2)}


def _state_file(chat_id: str) -> str:
    """State is PER-ACCOUNT. Before the test-account split the bot kept one global
    file for the owner; reusing that name for a different account would make
    `manage` look up the owner's tickers in the test account's log, not find them,
    and silently erase them from state — permanently orphaning real positions that
    would then never be sold at target or stop."""
    return _LEGACY_STATE_FILE if chat_id == _OWNER_ID else f"synthetic_state_{chat_id}.json"


def _state(chat_id: str) -> dict:
    try:
        files = requests.get(f"https://api.github.com/gists/{_GID}",
                             headers={"Authorization": f"token {_TOK}"}, timeout=20
                             ).json().get("files", {})
        return json.loads(files.get(_state_file(chat_id), {}).get("content") or "{}")
    except Exception:
        return {}


def _save_state(chat_id: str, st: dict) -> bool:
    """Persist state, VERIFYING the write. GitHub rate-limits (409/403) rapid
    successive PATCHes to the same gist, and a run does several writes
    (close_trade → paper_sell → state × 2 accounts). The old fire-and-forget
    version ignored the response, so a throttled save failed SILENTLY: a sold
    ticker stayed 'tracked', and — far worse — a freshly opened position could
    fail to persist and be orphaned (never sold at target or stop)."""
    import time
    body = {"files": {_state_file(chat_id): {"content": json.dumps(st, indent=2)}}}
    last = ""
    for attempt in range(1, 6):
        try:
            r = requests.patch(f"https://api.github.com/gists/{_GID}",
                               headers={"Authorization": f"token {_TOK}"},
                               json=body, timeout=25)
            if r.status_code < 400:
                return True
            last = f"{r.status_code} {r.text[:80]}"
        except Exception as exc:
            last = str(exc)
        time.sleep(1.5 * attempt)
    print(f"[synthetic_user] STATE SAVE FAILED for {chat_id}: {last}")
    return False


def _raw_picks() -> dict:
    try:
        files = requests.get(f"https://api.github.com/gists/{_GID}",
                             headers={"Authorization": f"token {_TOK}"}, timeout=20
                             ).json().get("files", {})
        return json.loads(files.get("picks.json", {}).get("content") or "{}")
    except Exception:
        return {}


def _universe(picks: dict) -> list[dict]:
    out, seen = [], set()
    for sec, atype, key in (("stocks", "stock", "ticker"), ("crypto", "crypto", "symbol"),
                            ("etfs", "etf", "ticker"), ("commodities", "commodity", "ticker")):
        for tf in ("short_term", "long_term"):
            for p in (picks.get(sec, {}) or {}).get(tf, []) or []:
                t = (p.get(key) or p.get("ticker") or p.get("symbol") or "").upper()
                if t and t not in seen:
                    seen.add(t)
                    out.append({"t": t, "entry": p.get("entry_price"),
                                "stop": p.get("stop_loss"), "target": p.get("target_price"),
                                "atype": atype, "lt": tf == "long_term",
                                # what the sizer reads — conviction scales the
                                # size, the ATR fields are its stop fallback
                                "conviction": p.get("conviction"),
                                "atr_pct": p.get("atr_pct") or (p.get("_screen") or {}).get("atr_pct"),
                                "suggested_stop_pct": p.get("suggested_stop_pct"),
                                "sector": p.get("sector")})
    return out


def _entry_breach(px, u: dict):
    """How far ABOVE the published entry this fill would land, in percent — or
    None when it is inside the window the morning message promised.

    🔴 Why the bot must not buy these. The message says "enter within X% — skip
    if above $Y". The bot bought anyway, so it modelled a user who IGNORES the
    instruction: `actionability` measured 10 of 98 fills breaching, up to 11.22%
    (DOT 2026-09-08) and 8.98% (NVDA 2026-08-27). Those are real overnight
    moves, not a pricing bug, so the honest fix is to OBEY our own rule rather
    than widen it — widening would legitimise the bad fill.

    Filling BELOW entry is never a breach (a cheaper fill is a better one), so
    only a positive excess counts. An unusable entry returns None: the window is
    then unmeasurable, and unmeasurable is never a violation — the same stance
    as a stop with no ATR. Never re-hardcode 2 or 3; the window has ONE
    definition (`formatters.entry_window_pct`) and it has drifted before.
    """
    from formatters import entry_window_pct
    try:
        entry = float(u.get("entry"))
    except (TypeError, ValueError):
        return None
    if not _pos(px) or entry <= 0:
        return None
    window = entry_window_pct(is_long_term=bool(u.get("lt")),
                              is_crypto=u.get("atype") == "crypto")
    slip = (float(px) - entry) / entry * 100.0
    return round(slip, 2) if slip > window else None


def phase_open(admin: str, dry: bool, picks: dict | None = None,
               open_real: bool = True) -> list[str]:
    """Open the day's positions for ONE account.

    🔴 EVERY TOURNAMENT ARM TRADES THROUGH THIS FUNCTION. That is the point: if
    an arm had its own buying code, the standings would compare execution as
    much as selection, and the whole experiment would be confounded. Arms differ only
    in the `picks` handed in — sizing, the hard book rules, the entry-window
    obedience, the levels and the SPY twin are identical for all of them.

    picks      — inject an arm's picks; defaults to production's picks.json.
    open_real  — arms are PAPER ONLY and pass False. An arm must never open a
                 real position; these are experiments, not recommendations.
    """
    from market_data import get_live_price
    from trade_logger import add_holding, load_user_trade_log
    from paper_trader import paper_buy, load_user_paper
    from price_alert_manager import add_alert
    import webhook as wh
    import sim_portfolio as sp

    uni = _universe(_raw_picks() if picks is None else picks)
    if not uni:
        return ["no picks today — nothing to open"]
    st = _state(admin)
    opened = set(st.get("real", [])) | set(st.get("paper", []))
    acts, new_real, new_paper, watch = [], [], [], []
    _skips: list[dict] = []        # entry-window breaches — observations, persisted
    _holds: list[dict] = []        # book-rule refusals — the trader said no
    _flows: list[tuple] = []       # (ticker, usd) buys for the SPY twin, ONE write at the end
    # ET, per the one-clock rule — actionability joins these to picks by DATE,
    # and a UTC stamp rolls over at 7-8 PM ET and would join to the wrong day.
    from config_manager import et_today
    _today = et_today().isoformat()

    real_cands = ([u for u in uni if u["atype"] == "stock" and u["t"] not in opened][:_MAX_REAL]
                  if open_real else [])
    # Consider EVERY pick (all asset types); the BOOK RULES decide how many the
    # book can actually carry. Each fill is one independent sample for the
    # evaluation, and each refusal is recorded as what a sized trader would do.
    paper_cands = [u for u in uni if u["t"] not in opened][:_MAX_PAPER]

    def _persist():
        """Save state so a logged position is NEVER orphaned, even if a later
        best-effort step (alert/watchlist) throws. manage tracks only what's here."""
        if dry:
            return
        st.setdefault("real", []).extend(new_real)
        st.setdefault("paper", []).extend(new_paper)
        st["real"] = list(dict.fromkeys(st["real"]))
        st["paper"] = list(dict.fromkeys(st["paper"]))
        # 🔴 A SKIP IS AN OBSERVATION, not an absence — persist it.
        # `actionability` measures reachability from the bot's FILLS, so simply
        # not buying a breached pick would make the breach rate fall toward 0%
        # while nothing improved. That is the documented failure that let a
        # closed position erase the worst breach on record (COHR, 15%->8.7%):
        # "a metric about a PROMISE must not get quieter". Recording the skip
        # keeps the denominator whole AND is strictly better evidence — it is
        # the obedient user's outcome, which is the thing being measured.
        # Deduped on (date, ticker): both loops can log the same pick.
        if _skips:
            prev = st.get("skipped") or []
            seen = {(r.get("date"), r.get("t")) for r in prev}
            for r in _skips:
                if (r["date"], r["t"]) not in seen:
                    seen.add((r["date"], r["t"]))
                    prev.append(r)
            st["skipped"] = prev[-500:]      # bounded: the gist has a ~1MB wall
        if _holds:
            # A refusal by the book rules is a different fact from a window
            # breach — the pick was reachable, the TRADER had no room. Kept
            # separately so neither metric borrows the other's count.
            st["held"] = ((st.get("held") or []) + _holds)[-200:]
        if not _save_state(admin, st):
            acts.append("🚨 STATE SAVE FAILED — a position just opened may be "
                        "orphaned (never sold at target/stop). Check the log.")

    try:
        held_real = {x["ticker"] for x in load_user_trade_log(admin).get("open", [])}
        for u in real_cands:
            try:
                px = get_live_price(u["t"])
                if not _pos(px) or u["t"] in held_real:
                    continue
                _slip = _entry_breach(px, u)
                if _slip is not None:
                    _skips.append({"t": u["t"], "date": _today, "entry": u.get("entry"),
                                   "would_pay": round(float(px), 6), "slippage_pct": _slip,
                                   "atype": u["atype"], "lt": bool(u.get("lt"))})
                    acts.append(f"⛔ SKIP {u['t']} — ${px:.2f} is {_slip:+.2f}% above the "
                                f"published entry ${u.get('entry')}; the message said skip it")
                    continue
                shares = round(_REAL_USD / px, 4)
                _s, _t, _src = _levels_for(px, u.get("stop"), u.get("target"), lt=bool(u.get("lt")))
                if not dry:
                    add_holding(u["t"], admin, entry_override=float(px),
                                stop_override=_s, target_override=_t,
                                shares_override=shares, asset_type_override=u["atype"],
                                # the horizon the time stop reads back in manage
                                timeframe_override="long_term" if u.get("lt") else "short_term",
                                source=SYNTHETIC_SOURCE, levels_source=_src)   # tag provenance; never counted as a user trade
                # record in state IMMEDIATELY — the position is now real
                new_real.append(u["t"]); watch.append(u["t"])
                acts.append(f"🟢 REAL {u['t']} @ ${px:.2f} · {shares} sh · target ${u['target']}")
                if not dry and _pos(u["target"]):
                    try:                              # alert is best-effort, not critical
                        add_alert(admin, u["t"], float(u["target"]), replace=True)
                    except Exception as e:
                        acts.append(f"   ⚠️ alert for {u['t']} skipped: {e}")
            except Exception as e:
                acts.append(f"   ⚠️ real {u['t']} skipped: {e}")

        # ── The paper BOOK: sized, rule-bound, closed ─────────────────────────
        # No cash top-up any more. The old bot refilled itself whenever it ran
        # low, which is why 33 positions could coexist; a book that prints its
        # own money has no equity curve worth reading.
        paper = load_user_paper(admin)
        held_paper = {x["ticker"] for x in paper.get("positions", [])}
        book = _book_tally(paper, get_live_price)
        cash = float(paper.get("cash") or 0)
        equity0 = book["equity"]
        acts.append(f"📒 book: equity ${equity0:,.2f} ({book['equity_source']}) · "
                    f"{book['n_open']} open · ${book['deployed']:,.0f} deployed · "
                    f"${book['risk']:,.0f} at risk · ${cash:,.0f} cash")
        spy_px = get_live_price("SPY")
        if not _pos(spy_px):
            spy_px = None
            acts.append("⚠️ SPY price unavailable — the SPY twin and today's snapshot "
                        "are NOT updated (nothing is invented)")
        for u in paper_cands:
            try:
                px = get_live_price(u["t"])
                if not _pos(px) or u["t"] in held_paper:
                    continue
                _slip = _entry_breach(px, u)
                if _slip is not None:
                    # The REAL loop above may have logged this same ticker today;
                    # _record_skips dedupes on (date, ticker).
                    _skips.append({"t": u["t"], "date": _today, "entry": u.get("entry"),
                                   "would_pay": round(float(px), 6), "slippage_pct": _slip,
                                   "atype": u["atype"], "lt": bool(u.get("lt"))})
                    acts.append(f"⛔ SKIP paper {u['t']} — {_slip:+.2f}% above published entry")
                    continue
                # Levels FIRST (they bracket the fill), then size off the real
                # stop — the sizer's risk arithmetic needs the stop it will
                # actually be stopped at, and an LT pick's is the invalidation.
                _ps, _pt, _psrc = _levels_for(px, u.get("stop"), u.get("target"),
                                              lt=bool(u.get("lt")))
                sz = _size(u, px, _ps, book["equity"])
                if sz["shares"] <= 0 or sz["usd"] <= 0:
                    acts.append(f"⏭ {u['t']} — sizer returned nothing: "
                                f"{(sz['sizing'] or {}).get('note')}")
                    continue
                why = _book_block(book, sz["usd"], sz["risk_usd"])
                if why is None and sz["usd"] > cash:
                    why = f"insufficient cash (${cash:,.0f} free, needs ${sz['usd']:,.0f})"
                if why:
                    _holds.append({"t": u["t"], "date": _today, "usd": sz["usd"], "why": why})
                    acts.append(f"⏸ HOLD {u['t']} — {why}")
                    continue
                if not dry:
                    # Pass the levels — WITHOUT them the position stores
                    # target_price/stop_loss = None, so manage's paper branch
                    # (`if tgt and px >= tgt`) can never fire: paper positions
                    # would accumulate forever, never exercise paper_sell, and
                    # drain the paper cash (38 stale positions before this fix).
                    msg = paper_buy(u["t"], sz["shares"], admin, price=float(px),
                                    stop_loss=_ps, target_price=_pt, levels_source=_psrc)
                    if str(msg).startswith("❌"):
                        acts.append(f"   ⚠️ paper {u['t']} refused: {str(msg)[:90]}")
                        continue
                # the running tally is what the NEXT candidate is judged against
                book["n_open"] += 1
                book["deployed"] = round(book["deployed"] + sz["usd"], 2)
                book["risk"] = round(book["risk"] + sz["risk_usd"], 2)
                cash = round(cash - sz["usd"], 2)
                st.setdefault("book", {})[u["t"]] = {
                    "lt": bool(u.get("lt")), "atype": u["atype"], "opened": _today,
                    "usd": sz["usd"], "shares": sz["shares"], "levels_source": _psrc,
                    "capped_by": (sz["sizing"] or {}).get("capped_by")}
                _flows.append((u["t"], sz["usd"]))
                new_paper.append(u["t"]); watch.append(u["t"])
                cap = (sz["sizing"] or {}).get("capped_by")
                acts.append(f"📄 PAPER {u['t']} @ ${px:.2f} · {sz['shares']:g} sh = "
                            f"${sz['usd']:,.0f} · stop ${_ps:g} ({_psrc}) · risk "
                            f"${sz['risk_usd']:,.0f}" + (f" · capped: {cap}" if cap else ""))
            except Exception as e:
                acts.append(f"   ⚠️ paper {u['t']} skipped: {e}")

        # ── One write: today's buys into the twin, then the daily snapshot ────
        # Equity is unchanged by the buys (cash became position at the same
        # price), so the pre-buy mark IS today's mark.
        if spy_px is not None:
            def _sim(d, _flows=list(_flows), _eq=equity0, _n=book["n_open"]):
                d = d or sp.new_doc(admin, _today, _eq)
                for t, usd in _flows:
                    sp.record_buy(d, t, usd, spy_px, _today)
                return sp.snapshot(d, _today, _eq, spy_px, _n)
            if dry:
                acts.append(f"📈 (dry) would snapshot {_today}: bot ${equity0:,.2f}, "
                            f"{len(_flows)} twin buy(s) at SPY ${spy_px:.2f}")
            else:
                try:
                    doc = sp.update(admin, _sim)
                    last = (doc or {}).get("snapshots", [])[-1:]
                    if last:
                        l = last[0]
                        acts.append(f"📈 {_today}: bot ${l['bot']:,.2f} ({l['bot_ret_pct']:+.2f}%) "
                                    f"vs SPY twin ${l['twin']:,.2f} ({l['twin_ret_pct']:+.2f}%)")
                except Exception as e:
                    acts.append(f"   ⚠️ equity snapshot failed: {e}")

        if watch and not dry:
            try:
                before = wh._load_watchlist(admin)
                wh._save_watchlist(admin, list(dict.fromkeys([x.upper() for x in before] + watch)))
            except Exception as e:
                acts.append(f"   ⚠️ watchlist skipped: {e}")
        acts.append(f"👁 watchlisted {list(dict.fromkeys(watch))}")
    finally:
        _persist()
    return acts


def phase_manage(dry: bool) -> list[str]:
    """Manage the TEST account, then wind down any legacy owner positions.

    The owner's pre-split state is managed (sold at target/stop) but never added
    to, so the account switch cannot orphan the positions the bot already opened
    there. Once that state empties, the legacy pass is a no-op forever."""
    acts = _manage_account(_TRADE_ID, dry)
    if _OWNER_ID and _OWNER_ID != _TRADE_ID:
        legacy = _state(_OWNER_ID)
        if legacy.get("real") or legacy.get("paper"):
            for a in _manage_account(_OWNER_ID, dry, sim=False):
                acts.append(f"[owner wind-down] {a}")
    return acts


def _manage_account(admin: str, dry: bool, sim: bool = True) -> list[str]:
    """Sell at target, cut at stop, or EXPIRE at the time stop.

    The time stop is what makes every position RESOLVE. Without it a long-term
    pick that never reached either level sat in the book forever, so the exit
    mix counted only the trades that happened to finish — a survivor sample.
    `sim=False` for the legacy owner wind-down: those positions predate the
    book and have no twin lots."""
    from market_data import get_live_price
    from trade_logger import load_user_trade_log, close_trade
    from paper_trader import load_user_paper, paper_sell
    from config_manager import et_today
    import sim_portfolio as sp

    today = et_today()
    today_s = today.isoformat()
    st = _state(admin)
    book_meta = st.get("book") or {}
    acts = []
    sold: list[tuple] = []           # (ticker, proceeds, outcome) for the twin

    # Only manage the bot's OWN real tickers (never the owner's real positions).
    real_open = {x["ticker"]: x for x in load_user_trade_log(admin).get("open", [])}
    still_real = []
    for t in st.get("real", []):
        pos = real_open.get(t)
        if not pos:
            continue                      # already closed / gone — drop from state
        try:
            px = get_live_price(t)
            if not _pos(px):
                still_real.append(t); continue
            tgt, stop = pos.get("target_price"), pos.get("stop_loss")
            lt = pos.get("timeframe") == "long_term"
            age = _age_days(pos.get("opened_date"), today)
            if _pos(tgt) and px >= float(tgt):
                if not dry:
                    # Record WHY. Without this every bot exit was tagged
                    # "manual" and stop-vs-target was unmeasurable.
                    close_trade(t, admin, exit_price=float(px), outcome="target")
                acts.append(f"🎯 SOLD REAL {t} @ ${px:.2f} (target hit)")
            elif _pos(stop) and px <= float(stop):
                if not dry:
                    close_trade(t, admin, exit_price=float(px), outcome="stop")
                acts.append(f"🔴 SOLD REAL {t} @ ${px:.2f} (stop hit)")
            elif age is not None and age >= _horizon_days(lt):
                if not dry:
                    close_trade(t, admin, exit_price=float(px), outcome="expired")
                acts.append(f"⏳ SOLD REAL {t} @ ${px:.2f} (time stop: {age}d ≥ {_horizon_days(lt)}d)")
            else:
                still_real.append(t)
        except Exception as e:
            still_real.append(t)          # keep tracking; surface the error
            acts.append(f"⚠️ manage REAL {t} errored: {e}")

    paper_open = {x["ticker"]: x for x in load_user_paper(admin).get("positions", [])}
    still_paper = []
    for t in st.get("paper", []):
        pos = paper_open.get(t)
        if not pos:
            book_meta.pop(t, None)
            continue
        try:
            px = get_live_price(t)
            if not _pos(px):
                still_paper.append(t); continue
            tgt, stop = pos.get("target_price"), pos.get("stop_loss")
            lt = bool((book_meta.get(t) or {}).get("lt"))
            age = _age_days(pos.get("bought_date"), today)
            outcome = None
            if _pos(tgt) and px >= float(tgt):
                outcome = "target"; note = "target"
            elif _pos(stop) and px <= float(stop):
                outcome = "stop"; note = "stop"
            elif age is not None and age >= _horizon_days(lt):
                outcome = "expired"; note = f"time stop: {age}d ≥ {_horizon_days(lt)}d"
            if outcome is None:
                still_paper.append(t)
                continue
            if not dry:
                paper_sell(t, admin, price=float(px), outcome=outcome)
            proceeds = round(float(px) * float(pos.get("shares") or 0), 2)
            sold.append((t, proceeds, outcome))
            book_meta.pop(t, None)
            glyph = {"target": "🎯", "stop": "🔴", "expired": "⏳"}[outcome]
            acts.append(f"{glyph} paper-sold {t} @ ${px:.2f} ({note})")
        except Exception as e:
            still_paper.append(t)
            acts.append(f"⚠️ manage PAPER {t} errored: {e}")

    # The twin sells the matching SPY lots — one write for the whole run.
    if sim and sold and not dry:
        spy_px = get_live_price("SPY")
        if not _pos(spy_px):
            spy_px = None
            acts.append("⚠️ SPY price unavailable — twin lots kept until the next mark")

        def _sim(d, _sold=list(sold)):
            if not d:
                return None               # no book yet: nothing to match against
            for t, usd, oc in _sold:
                sp.record_sell(d, t, usd, spy_px, today_s, outcome=oc)
            return d
        try:
            sp.update(admin, _sim)
        except Exception as e:
            acts.append(f"⚠️ twin update failed: {e}")

    if not dry:
        st["real"], st["paper"], st["book"] = still_real, still_paper, book_meta
        if not _save_state(admin, st):
            # Surface it — a silent failure leaves sold tickers "tracked" and, on
            # the open path, can orphan a live position.
            acts.append(f"⚠️ state save FAILED for {admin} — tracking may be stale")
    # NOTE: returns ONLY actionable events (sells/cuts/expiries/errors). An empty
    # list means "nothing to do" — main() then stays silent (no Telegram spam).
    return acts


def manage_account(admin: str, dry: bool) -> list[str]:
    """Public name for the ONE manage implementation.

    The tournament arms exit through exactly this function, so target, stop and
    the time stop mean the same thing in every arm. Reaching into the private
    `_manage_account` from another module would invite a second copy of the
    exit rules, and the standings would then compare execution as much as
    selection.
    """
    return _manage_account(admin, dry)


def phase_reset(admin: str, dry: bool) -> list[str]:
    """Start the book: liquidate every open paper position at market, set cash
    to _BOOK_START_USD, and begin a fresh equity curve with the SPY twin at
    the same starting equity.

    HISTORY IS KEPT. Closed paper trades feed the reachability metric, and a
    metric about a promise must not get quieter because the book restarted.
    The liquidations are recorded with outcome "liquidated" so the exit mix
    never reads them as stop-outs.

    All-or-nothing: if any position cannot be priced the reset REFUSES, because
    a book at $10k cash with a stray position is not a book at $10k."""
    from market_data import get_live_price
    from paper_trader import load_user_paper, paper_sell, _mutate_paper
    from config_manager import et_today
    import sim_portfolio as sp

    today = et_today().isoformat()
    paper = load_user_paper(admin)
    positions = list(paper.get("positions") or [])
    acts = []
    marks = {}
    for p in positions:
        px = get_live_price(p["ticker"])
        if not _pos(px):
            acts.append(f"🚫 {p['ticker']} could not be priced — reset REFUSED, nothing changed")
            return acts
        marks[p["ticker"]] = float(px)
    for p in positions:
        px = marks[p["ticker"]]
        if not dry:
            paper_sell(p["ticker"], admin, price=px, outcome="liquidated")
        acts.append(f"🧹 liquidated {p['ticker']} × {float(p.get('shares') or 0):g} @ ${px:.2f}")
    if not dry:
        def _mut(d):
            if d.get("positions"):
                return d, {"left": len(d["positions"])}      # something reappeared — do not zero it
            d["cash"] = _BOOK_START_USD
            d["starting_cash"] = _BOOK_START_USD
            return d, {"left": 0}
        res = _mutate_paper(admin, _mut)
        if res.get("left"):
            acts.append(f"🚫 {res['left']} position(s) still open after liquidation — cash NOT reset")
            return acts
        st = _state(admin)
        st["paper"], st["book"] = [], {}
        if not _save_state(admin, st):
            acts.append("⚠️ state save FAILED — old paper tickers may stay tracked")
        sp.update(admin, lambda _d: sp.new_doc(admin, today, _BOOK_START_USD))
    acts.append(f"💰 book set to ${_BOOK_START_USD:,.0f} cash · history kept "
                f"({len(paper.get('history') or [])} closed rows) · curve starts {today}")
    return acts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["open", "manage", "reset"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not _TRADE_ID:
        print("no trade account resolved (SYNTHETIC_CHAT_ID)"); return 2

    # TRADES on the virtual test account; REPORTS to the real owner.
    if args.phase == "open":
        acts = phase_open(_TRADE_ID, args.dry_run)
    elif args.phase == "reset":
        acts = phase_reset(_TRADE_ID, args.dry_run)
    else:
        acts = phase_manage(args.dry_run)

    # manage runs hourly during market hours — only DM when it actually did
    # something (sold/cut/error), else stay silent so the reports don't become
    # noise. open and reset always report.
    actionable = bool(acts)
    if not acts:  # manage with nothing to do — log a held summary to stdout only
        st = _state(_TRADE_ID)
        acts = [f"held all (real: {st.get('real')}, paper: {st.get('paper')})"]
    body = "\n".join(f"  {a}" for a in acts)
    print(f"[synthetic_user] phase={args.phase} dry={args.dry_run} "
          f"trade_account={_TRADE_ID}\n{body}")

    should_send = (not args.dry_run) and (args.phase in ("open", "reset") or actionable)
    if should_send and _OWNER_ID:
        try:
            from telegram_api import send_message
            send_message(f"🤖 <b>Synthetic user — {args.phase}</b>  "
                         f"<i>(test acct {_TRADE_ID})</i>\n{body}", chat_id=_OWNER_ID)
        except Exception as e:
            print(f"[synthetic_user] report send failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
