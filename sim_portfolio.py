"""
sim_portfolio.py — the synthetic trader's BOOK: an equity curve and a SPY twin
that receives the SAME cash flows on the SAME days.

Why a twin and not "SPY over the period". The bot is only invested when it has
a position; comparing its equity to buy-and-hold SPY would credit or blame it
for time it spent in cash. The twin buys $C of SPY the moment the bot buys $C
of anything and sells that lot when the bot sells the position, so the only
difference between the two curves is WHAT was bought — which is the question.

    bot equity   = paper cash + Σ shares × live price
    twin equity  = twin cash  + Σ SPY units × SPY price

Both start equal. A position the bot held BEFORE the book started has no twin
lot: selling it moves the bot from position to cash at market (no equity
change) and the twin is untouched, so legacy positions neither help nor hurt.

Stored as ONE whole-blob document keyed by account (`config_manager
.SIM_PORTFOLIO_FILE`), so each future tournament arm gets its own book without
a new file. Everything here is pure except `load` / `update`, which go through
`mutate_gist_file` and therefore the same storage backend as the rest of the app.

MEASUREMENT, never INPUT: nothing in the pick engine may import this module.
"""
from __future__ import annotations

import math

MAX_SNAPSHOTS = 400     # ~1.5 years of trading days; the gist has a ~1 MB wall
MAX_FLOWS = 1500

CURVE_POINTS = 120      # what the /admin sparkline is handed (last N snapshots)


def _fin(x) -> float | None:
    """A real finite number or None. NaN/inf/bool/garbage never survive into
    the document — the same rule as `plausible_price`, for the same reason:
    NaN does not announce itself, it makes every later comparison false."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def new_doc(account: str, start_date: str, starting_equity: float) -> dict:
    eq = _fin(starting_equity)
    if eq is None or eq <= 0:
        raise ValueError("starting_equity must be a positive finite number")
    return {
        "account": str(account),
        "start_date": start_date,
        "starting_equity": round(eq, 2),
        "twin_cash": round(eq, 2),
        "twin_lots": {},          # ticker -> {"usd", "units", "date"}
        "flows": [],
        "snapshots": [],
    }


def record_buy(doc: dict, ticker: str, usd: float, spy_px: float, date: str) -> dict:
    """The bot spent `usd` on `ticker`; the twin spends the same on SPY."""
    usd, spy_px = _fin(usd), _fin(spy_px)
    if usd is None or usd <= 0 or spy_px is None or spy_px <= 0:
        return doc                      # unmeasurable — record nothing, invent nothing
    units = usd / spy_px
    t = ticker.upper()
    lot = doc.setdefault("twin_lots", {}).get(t)
    if lot:                             # a second buy of the same ticker adds to the lot
        lot["usd"] = round(lot["usd"] + usd, 2)
        lot["units"] = lot["units"] + units
    else:
        doc["twin_lots"][t] = {"usd": round(usd, 2), "units": units, "date": date}
    doc["twin_cash"] = round(doc.get("twin_cash", 0.0) - usd, 2)
    _flow(doc, {"date": date, "kind": "buy", "ticker": t,
                "usd": round(usd, 2), "spy_px": round(spy_px, 4)})
    return doc


def record_sell(doc: dict, ticker: str, proceeds_usd: float, spy_px: float,
                date: str, outcome: str = "") -> dict:
    """The bot sold `ticker` for `proceeds_usd`; the twin sells that lot's SPY
    units at today's SPY price. No lot means a pre-book position: the twin
    does nothing, deliberately."""
    spy_px = _fin(spy_px)
    t = ticker.upper()
    lot = doc.setdefault("twin_lots", {}).pop(t, None)
    if lot and spy_px and spy_px > 0:
        doc["twin_cash"] = round(doc.get("twin_cash", 0.0) + lot["units"] * spy_px, 2)
    elif lot:
        # SPY price unavailable: keep the lot rather than lose it. The next
        # snapshot marks it at whatever SPY is then; the twin stays whole.
        doc["twin_lots"][t] = lot
    _flow(doc, {"date": date, "kind": "sell", "ticker": t,
                "usd": round(_fin(proceeds_usd) or 0.0, 2),
                "spy_px": round(spy_px, 4) if spy_px else None,
                "outcome": outcome, "twin_lot": bool(lot)})
    return doc


def _flow(doc: dict, row: dict) -> None:
    flows = doc.setdefault("flows", [])
    flows.append(row)
    if len(flows) > MAX_FLOWS:
        del flows[: len(flows) - MAX_FLOWS]


def twin_equity(doc: dict, spy_px) -> float | None:
    spy_px = _fin(spy_px)
    lots = doc.get("twin_lots") or {}
    if lots and (spy_px is None or spy_px <= 0):
        return None                     # cannot mark the lots — say so, do not guess
    held = sum(l["units"] for l in lots.values()) * (spy_px or 0.0)
    return round((_fin(doc.get("twin_cash")) or 0.0) + held, 2)


def snapshot(doc: dict, date: str, bot_equity: float, spy_px, open_positions: int) -> dict:
    """Append (or overwrite for the same date) one point on both curves.

    Idempotent per date on purpose: a re-run of the open phase must not draw
    two points for one day, and a later run the same day carries the fresher
    mark. Nothing is written when either equity is unmeasurable."""
    be = _fin(bot_equity)
    te = twin_equity(doc, spy_px)
    if be is None or te is None:
        return doc
    start = _fin(doc.get("starting_equity")) or 0.0
    row = {
        "date": date,
        "bot": round(be, 2),
        "twin": round(te, 2),
        "spy_px": round(_fin(spy_px), 4) if _fin(spy_px) else None,
        "open": int(open_positions or 0),
        "bot_ret_pct": round((be - start) / start * 100, 2) if start > 0 else None,
        "twin_ret_pct": round((te - start) / start * 100, 2) if start > 0 else None,
    }
    snaps = doc.setdefault("snapshots", [])
    snaps[:] = [s for s in snaps if s.get("date") != date]
    snaps.append(row)
    snaps.sort(key=lambda s: s.get("date") or "")
    if len(snaps) > MAX_SNAPSHOTS:
        del snaps[: len(snaps) - MAX_SNAPSHOTS]
    return doc


def bot_equity(paper: dict, price_of) -> float | None:
    """Paper cash plus every open position at a LIVE price. Returns None when
    any position cannot be priced — a partial mark would read as a drawdown
    that never happened, the exact false-P&L class `plausible_price` exists
    to stop. Caller decides whether to fall back to cost basis and SAYS so."""
    from market_data import plausible_price
    total = _fin((paper or {}).get("cash")) or 0.0
    for p in (paper or {}).get("positions") or []:
        try:
            px = price_of(p["ticker"])
        except Exception:
            px = None
        ref = _fin(p.get("avg_price")) or _fin(p.get("entry_price"))
        if not plausible_price(px, ref if ref else px):
            return None
        total += float(px) * float(p.get("shares") or 0)
    return round(total, 2)


def max_drawdown_pct(values: list) -> float | None:
    """Largest peak-to-trough fall, in percent of the peak. Order-dependent by
    construction — feed it the curve in date order."""
    peak, worst = None, 0.0
    for v in values:
        v = _fin(v)
        if v is None:
            continue
        if peak is None or v > peak:
            peak = v
        elif peak > 0:
            worst = max(worst, (peak - v) / peak * 100)
    return round(worst, 2) if peak is not None else None


def summary(doc: dict | None) -> dict:
    """What /admin and the engine report render. NaN-free, never raises."""
    if not doc or not doc.get("snapshots"):
        return {"active": False,
                "note": ("No equity curve yet. It starts on the first open-phase "
                         "run after the book is reset.")}
    snaps = doc["snapshots"]
    last = snaps[-1]
    start = _fin(doc.get("starting_equity")) or 0.0
    bot, twin = _fin(last.get("bot")), _fin(last.get("twin"))
    bot_ret = _fin(last.get("bot_ret_pct"))
    twin_ret = _fin(last.get("twin_ret_pct"))
    alpha = (round(bot_ret - twin_ret, 2)
             if bot_ret is not None and twin_ret is not None else None)
    flows = doc.get("flows") or []
    return {
        "active": True,
        "account": doc.get("account"),
        "start_date": doc.get("start_date"),
        "as_of": last.get("date"),
        "days": len(snaps),
        "starting_equity": round(start, 2),
        "bot_equity": bot,
        "twin_equity": twin,
        "bot_ret_pct": bot_ret,
        "twin_ret_pct": twin_ret,
        "alpha_pct": alpha,
        "max_drawdown_pct": max_drawdown_pct([s.get("bot") for s in snaps]),
        "twin_max_drawdown_pct": max_drawdown_pct([s.get("twin") for s in snaps]),
        "open_positions": int(last.get("open") or 0),
        "buys": sum(1 for f in flows if f.get("kind") == "buy"),
        "sells": sum(1 for f in flows if f.get("kind") == "sell"),
        "curve": [[s.get("date"), s.get("bot"), s.get("twin")] for s in snaps[-CURVE_POINTS:]],
        # Honesty line, rendered beside the number: a few weeks of one book is
        # a direction, not a verdict. The tournament's gate is the same n>=30.
        "sample_warning": (None if len(snaps) >= 30 else
                           f"only {len(snaps)} trading day(s) — directional, not conclusive"),
    }


def standings(books: dict, labels: dict) -> list:
    """One row per arm, best first — the tournament table.

    `books` is the whole stored document ({account: doc}); `labels` maps an
    account to the arm name. Arms with no curve yet are reported as inactive
    rather than omitted: an arm that is silently missing looks like an arm that
    has not been built, which is the failure the empty-state rule exists for.

    Ranked on RETURN, not on alpha: every arm's twin receives that arm's own
    cash flows, so alpha is a statement about timing within an arm and is not
    comparable ACROSS arms. Return over the same days is.
    """
    rows = []
    for account, name in sorted(labels.items(), key=lambda kv: kv[1]):
        s = summary((books or {}).get(str(account)))
        rows.append({"arm": name, "account": str(account), **s})
    live = [r for r in rows if r.get("active")]
    idle = [r for r in rows if not r.get("active")]
    live.sort(key=lambda r: (r.get("bot_ret_pct") is None, -(r.get("bot_ret_pct") or 0)))
    return live + idle


def all_books() -> dict:
    """Every account's book in ONE read — they share a document on purpose, so
    the dashboard pays a single round trip however many arms are running."""
    from config_manager import _load_gist_file, SIM_PORTFOLIO_FILE
    return _load_gist_file(SIM_PORTFOLIO_FILE) or {}


# ── storage ─────────────────────────────────────────────────────────────────

def load(account: str) -> dict | None:
    from config_manager import _load_gist_file, SIM_PORTFOLIO_FILE
    return (_load_gist_file(SIM_PORTFOLIO_FILE) or {}).get(str(account))


def update(account: str, fn) -> dict | None:
    """Apply `fn(doc_or_None) -> doc` to this account's book under the storage
    lock. `fn` returning None declines the write."""
    from config_manager import mutate_gist_file, NO_WRITE, SIM_PORTFOLIO_FILE
    acct = str(account)

    def _mut(cur):
        cur = dict(cur or {})
        new = fn(cur.get(acct))
        if new is None:
            return NO_WRITE, cur.get(acct)
        cur[acct] = new
        return cur, new

    return mutate_gist_file(SIM_PORTFOLIO_FILE, _mut, default={})
