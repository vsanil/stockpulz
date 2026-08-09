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
                  paper_sell).

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

Usage: python3 scripts/synthetic_user.py --phase open|manage [--dry-run]
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
_MAX_PAPER = 20     # paper-buy EVERY pick — max independent samples for evaluation
_REAL_USD = 1000.0
_PAPER_USD = 500.0


def _pos(x) -> bool:
    try:
        import math
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


_FALLBACK_STOP_PCT   = 5.0
_FALLBACK_TARGET_PCT = 8.0


def _levels_for(px: float, stop, target) -> tuple[float, float]:
    """Levels that actually bracket the price we FILLED at.

    A pick's stop/target are relative to the pick's entry. If the live price has
    since moved past one of them, inheriting them blindly creates a position born
    already stopped-out (or already at target) — the next manage run closes it
    instantly and books a fabricated loss/gain. Seen live: a paper FICO filled at
    $1,177.74 carrying the pick's $1,290 stop. Fall back to a % of the real fill."""
    px = float(px)
    s = float(stop) if _pos(stop) else None
    t = float(target) if _pos(target) else None
    if s is None or s >= px:
        s = round(px * (1 - _FALLBACK_STOP_PCT / 100), 4)
    if t is None or t <= px:
        t = round(px * (1 + _FALLBACK_TARGET_PCT / 100), 4)
    return s, t


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
                                "atype": atype})
    return out


def phase_open(admin: str, dry: bool) -> list[str]:
    from market_data import get_live_price
    from trade_logger import add_holding, load_user_trade_log
    from paper_trader import paper_buy, load_user_paper
    from price_alert_manager import add_alert
    import webhook as wh

    uni = _universe(_raw_picks())
    if not uni:
        return ["no picks today — nothing to open"]
    st = _state(admin)
    opened = set(st.get("real", [])) | set(st.get("paper", []))
    acts, new_real, new_paper, watch = [], [], [], []

    real_cands = [u for u in uni if u["atype"] == "stock" and u["t"] not in opened][:_MAX_REAL]
    # Paper-buy EVERY pick (all asset types). Paper costs nothing and each pick is
    # one independent sample — this is what makes the pick-quality evaluation
    # statistically meaningful over time, instead of 2 arbitrary picks a day.
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
                shares = round(_REAL_USD / px, 4)
                _s, _t = _levels_for(px, u.get("stop"), u.get("target"))
                if not dry:
                    add_holding(u["t"], admin, entry_override=float(px),
                                stop_override=_s, target_override=_t,
                                shares_override=shares, asset_type_override=u["atype"],
                                source=SYNTHETIC_SOURCE)   # tag provenance; never counted as a user trade
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

        # Keep the paper-buy path exercisable: the bot accumulates positions and
        # drains cash over time (paper_buy rejects a buy it can't afford). Top up
        # when low so daily paper buys don't silently start failing. paper_add_cash
        # bumps starting_cash too, so the paper return % stays honest.
        if not dry:
            try:
                from paper_trader import paper_add_cash
                _cash = load_user_paper(admin).get("cash") or 0
                _day  = len(paper_cands) * _PAPER_USD           # today's intended buys
                if _cash < _day + 500:
                    _target = max(_day * 3, 10_000)             # ~3 days of headroom
                    paper_add_cash(round(_target - _cash, 2), admin)
                    acts.append(f"💵 paper cash topped up (was ${_cash:.0f} → ${_target:,.0f})")
            except Exception as e:
                acts.append(f"   ⚠️ paper top-up skipped: {e}")

        held_paper = {x["ticker"] for x in load_user_paper(admin).get("positions", [])}
        for u in paper_cands:
            try:
                px = get_live_price(u["t"])
                if not _pos(px) or u["t"] in held_paper:
                    continue
                shares = round(_PAPER_USD / px, 8)
                if not dry:
                    # Pass the pick's levels — WITHOUT them the position stores
                    # target_price/stop_loss = None, so manage's paper branch
                    # (`if tgt and px >= tgt`) can never fire: paper positions
                    # would accumulate forever, never exercise paper_sell, and
                    # drain the paper cash (38 stale positions before this fix).
                    _ps, _pt = _levels_for(px, u.get("stop"), u.get("target"))
                    paper_buy(u["t"], shares, admin, price=float(px),
                              stop_loss=_ps, target_price=_pt)
                new_paper.append(u["t"]); watch.append(u["t"])
                acts.append(f"📄 PAPER {u['t']} @ ${px:.2f} · {shares} sh")
            except Exception as e:
                acts.append(f"   ⚠️ paper {u['t']} skipped: {e}")

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
            for a in _manage_account(_OWNER_ID, dry):
                acts.append(f"[owner wind-down] {a}")
    return acts


def _manage_account(admin: str, dry: bool) -> list[str]:
    from market_data import get_live_price
    from trade_logger import load_user_trade_log, close_trade
    from paper_trader import load_user_paper, paper_sell

    st = _state(admin)
    acts = []

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
            continue
        try:
            px = get_live_price(t)
            if not _pos(px):
                still_paper.append(t); continue
            tgt, stop = pos.get("target_price"), pos.get("stop_loss")
            if _pos(tgt) and px >= float(tgt):
                if not dry:
                    paper_sell(t, admin, price=float(px))
                acts.append(f"🎯 paper-sold {t} @ ${px:.2f} (target)")
            elif _pos(stop) and px <= float(stop):
                if not dry:
                    paper_sell(t, admin, price=float(px))
                acts.append(f"🔴 paper-sold {t} @ ${px:.2f} (stop)")
            else:
                still_paper.append(t)
        except Exception as e:
            still_paper.append(t)
            acts.append(f"⚠️ manage PAPER {t} errored: {e}")

    if not dry:
        st["real"], st["paper"] = still_real, still_paper
        if not _save_state(admin, st):
            # Surface it — a silent failure leaves sold tickers "tracked" and, on
            # the open path, can orphan a live position.
            acts.append(f"⚠️ state save FAILED for {admin} — tracking may be stale")
    # NOTE: returns ONLY actionable events (sells/cuts/errors). An empty list
    # means "nothing to do" — main() then stays silent (no Telegram spam).
    return acts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["open", "manage"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not _TRADE_ID:
        print("no trade account resolved (SYNTHETIC_CHAT_ID)"); return 2

    # TRADES on the virtual test account; REPORTS to the real owner.
    acts = (phase_open(_TRADE_ID, args.dry_run) if args.phase == "open"
            else phase_manage(args.dry_run))

    # manage runs hourly during market hours — only DM when it actually did
    # something (sold/cut/error), else stay silent so the reports don't become
    # noise. open always reports (it opens positions every time).
    actionable = bool(acts)
    if not acts:  # manage with nothing to do — log a held summary to stdout only
        st = _state(_TRADE_ID)
        acts = [f"held all (real: {st.get('real')}, paper: {st.get('paper')})"]
    body = "\n".join(f"  {a}" for a in acts)
    print(f"[synthetic_user] phase={args.phase} dry={args.dry_run} "
          f"trade_account={_TRADE_ID}\n{body}")

    should_send = (not args.dry_run) and (args.phase == "open" or actionable)
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
