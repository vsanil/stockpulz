#!/usr/bin/env python3
"""
synthetic_user.py — an automated "active user" for stabilization testing.

Runs a few times a day as the ADMIN account and performs the full user lifecycle
so real-usage bugs (P&L drift over days, alerts firing, position tracking) surface:

  --phase open    (once, ~after the morning picks): from today's picks, LOG a
                  couple of REAL "I Bought This" positions + PAPER-buy a couple,
                  set target alerts, and add them to the watchlist.
  --phase manage  (midday + near close): check the bot's own positions and SELL
                  winners at target / cut at stop (real via close_trade, paper via
                  paper_sell).

SAFETY: the bot tracks the tickers IT opened in a state file (synthetic_state.json)
and ONLY manages those — it never touches the owner's real pre-existing positions.
By design this LOGS REAL positions (owner will reset the account before real use).

Usage: python3 scripts/synthetic_user.py --phase open|manage [--dry-run]
"""
from __future__ import annotations

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

_GID = os.environ.get("GIST_ID")
_TOK = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
_STATE_FILE = "synthetic_state.json"
_MAX_REAL = 2      # real "I Bought This" positions the bot opens per day
_MAX_PAPER = 2     # paper positions per day
_REAL_USD = 1000.0
_PAPER_USD = 500.0


def _pos(x) -> bool:
    try:
        import math
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def _state() -> dict:
    try:
        files = requests.get(f"https://api.github.com/gists/{_GID}",
                             headers={"Authorization": f"token {_TOK}"}, timeout=20
                             ).json().get("files", {})
        return json.loads(files.get(_STATE_FILE, {}).get("content") or "{}")
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    requests.patch(f"https://api.github.com/gists/{_GID}",
                   headers={"Authorization": f"token {_TOK}"},
                   json={"files": {_STATE_FILE: {"content": json.dumps(st, indent=2)}}},
                   timeout=20)


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
    st = _state()
    opened = set(st.get("real", [])) | set(st.get("paper", []))
    acts, new_real, new_paper, watch = [], [], [], []

    real_cands  = [u for u in uni if u["atype"] == "stock" and u["t"] not in opened][:_MAX_REAL]
    paper_cands = [u for u in uni if u["atype"] in ("crypto", "etf") and u["t"] not in opened][:_MAX_PAPER] \
                  or [u for u in uni if u["t"] not in opened][:_MAX_PAPER]

    def _persist():
        """Save state so a logged position is NEVER orphaned, even if a later
        best-effort step (alert/watchlist) throws. manage tracks only what's here."""
        if dry:
            return
        st.setdefault("real", []).extend(new_real)
        st.setdefault("paper", []).extend(new_paper)
        st["real"] = list(dict.fromkeys(st["real"]))
        st["paper"] = list(dict.fromkeys(st["paper"]))
        _save_state(st)

    try:
        held_real = {x["ticker"] for x in load_user_trade_log(admin).get("open", [])}
        for u in real_cands:
            try:
                px = get_live_price(u["t"])
                if not _pos(px) or u["t"] in held_real:
                    continue
                shares = round(_REAL_USD / px, 4)
                if not dry:
                    add_holding(u["t"], admin, entry_override=float(px),
                                stop_override=u["stop"], target_override=u["target"],
                                shares_override=shares, asset_type_override=u["atype"])
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

        held_paper = {x["ticker"] for x in load_user_paper(admin).get("positions", [])}
        for u in paper_cands:
            try:
                px = get_live_price(u["t"])
                if not _pos(px) or u["t"] in held_paper:
                    continue
                shares = round(_PAPER_USD / px, 8)
                if not dry:
                    paper_buy(u["t"], shares, admin, price=float(px))
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
        acts.append(f"👁 watchlisted {watch}")
    finally:
        _persist()
    return acts


def phase_manage(admin: str, dry: bool) -> list[str]:
    from market_data import get_live_price
    from trade_logger import load_user_trade_log, close_trade
    from paper_trader import load_user_paper, paper_sell

    st = _state()
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
                    close_trade(t, admin, exit_price=float(px))
                acts.append(f"🎯 SOLD REAL {t} @ ${px:.2f} (target hit)")
            elif _pos(stop) and px <= float(stop):
                if not dry:
                    close_trade(t, admin, exit_price=float(px))
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
        _save_state(st)
    # NOTE: returns ONLY actionable events (sells/cuts/errors). An empty list
    # means "nothing to do" — main() then stays silent (no Telegram spam).
    return acts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["open", "manage"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    admin = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not admin:
        print("TELEGRAM_CHAT_ID not set"); return 2

    acts = phase_open(admin, args.dry_run) if args.phase == "open" else phase_manage(admin, args.dry_run)

    # manage runs hourly during market hours — only DM when it actually did
    # something (sold/cut/error), else stay silent so the reports don't become
    # noise. open always reports (it opens positions every time).
    actionable = bool(acts)
    if not acts:  # manage with nothing to do — log a held summary to stdout only
        st = _state()
        acts = [f"held all (real: {st.get('real')}, paper: {st.get('paper')})"]
    body = "\n".join(f"  {a}" for a in acts)
    print(f"[synthetic_user] phase={args.phase} dry={args.dry_run}\n{body}")

    should_send = (not args.dry_run) and (args.phase == "open" or actionable)
    if should_send:
        try:
            from telegram_api import send_message
            send_message(f"🤖 <b>Synthetic user — {args.phase}</b>\n{body}", chat_id=admin)
        except Exception as e:
            print(f"[synthetic_user] report send failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
