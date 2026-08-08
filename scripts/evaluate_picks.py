#!/usr/bin/env python3
"""
evaluate_picks.py — measures whether the daily pick recommendations are actually
BENEFICIAL, honestly.

This is deliberately SEPARATE from the pick-generation feedback loop. It does not
tune anything and is never injected into the LLM prompt. Its only job is to answer
one question with evidence:

    "If a user had followed these picks, would they have done better than simply
     buying SPY?"

How it works
    1. LEDGER — each run appends the day's picks to `pick_ledger.json` (Gist),
       deduped by (date, ticker). Signal quality is judged on EVERY pick, whether
       or not anyone traded it, so the sample is unbiased by execution.
    2. SCORE — a pick matures after _HORIZON_DAYS. We replay its daily bars:
       first touch of target = win, first touch of stop = loss, else mark-to-market
       at the horizon. SPY over the SAME window is the benchmark.
    3. REPORT — hit rate, average return, and ALPHA vs SPY, split by conviction /
       asset type / timeframe, with explicit sample-size honesty.

Why the honesty matters: with a handful of trades, any "edge" is noise. Tuning the
engine on that is overfitting — it makes the numbers look better while making the
picks worse. This script therefore refuses to call anything conclusive below
_MIN_N and reports a 95% confidence interval on the win rate.

Usage:
    python3 scripts/evaluate_picks.py              # record today + report
    python3 scripts/evaluate_picks.py --report-only
    python3 scripts/evaluate_picks.py --dry-run    # no Gist write, no Telegram
"""
from __future__ import annotations

import os
import sys
import json
import math
import argparse
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

_GID = os.environ.get("GIST_ID")
_TOK = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
_LEDGER_FILE = "pick_ledger.json"

_HORIZON_DAYS = 30      # calendar days a short-term pick gets to work
_MIN_N        = 30      # below this, NOTHING is called conclusive
_BENCH        = "SPY"


# ── ledger I/O ────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"Authorization": f"token {_TOK}"}


def _content(meta: dict) -> str:
    """GitHub omits file content past ~1 MB and sets truncated=True, handing back
    a raw_url. Reading `content` blindly then parses PARTIAL JSON — which on this
    script's swallowing path looks like an EMPTY ledger and would re-record every
    pick. Same guard as storage._gist_content."""
    if meta.get("truncated") and meta.get("raw_url"):
        r = requests.get(meta["raw_url"], headers=_headers(), timeout=25)
        r.raise_for_status()
        return r.text
    return meta.get("content") or "{}"


def _gist_file(name: str) -> dict:
    try:
        files = requests.get(f"https://api.github.com/gists/{_GID}",
                             headers=_headers(), timeout=20).json().get("files", {})
        return json.loads(_content(files.get(name, {})))
    except Exception as exc:
        print(f"[evaluate] read {name} failed: {exc}")
        return {}


# ── Year-sharded ledger ───────────────────────────────────────────────────────
# The ledger is append-only and kept forever, and a Gist file is unreadable past
# ~1 MB. Picks (~700 KB/yr) plus controls (~370 KB/yr) cross that inside ONE
# year, so a single file would silently truncate. Shard by year: reads merge
# every shard, writes touch only the current year, and the legacy
# `pick_ledger.json` is read-only history that is never rewritten (so it can
# never be clobbered).
def _shard_name(year: int) -> str:
    return f"pick_ledger_{year}.json"


def _load_ledger() -> tuple[list, dict, str]:
    """Return (all rows across shards, current-year shard, its filename)."""
    name = _shard_name(dt.date.today().year)
    try:
        files = requests.get(f"https://api.github.com/gists/{_GID}",
                             headers=_headers(), timeout=20).json().get("files", {})
    except Exception as exc:
        print(f"[evaluate] ledger read failed: {exc}")
        return [], {"picks": []}, name

    rows, shard = [], {"picks": []}
    for fname, meta in sorted(files.items()):
        if fname != _LEDGER_FILE and not fname.startswith("pick_ledger_"):
            continue
        try:
            data = json.loads(_content(meta))
        except Exception as exc:
            print(f"[evaluate] shard {fname} unreadable ({exc}) — skipped")
            continue
        rows.extend(data.get("picks", []) or [])
        if fname == name:
            shard = data
    shard.setdefault("picks", [])
    return rows, shard, name


def _save_ledger(led: dict, name: str = _LEDGER_FILE) -> bool:
    """Persist the shard, VERIFYING the write. This was fire-and-forget, which is
    worse here than anywhere else in the project: `record_today` only ever reads
    TODAY's picks.json, so a silently throttled PATCH (GitHub 409/403 on rapid
    successive writes) loses that day's picks PERMANENTLY — there is no next run
    that can recover them. And a lost day isn't random: it correlates with the
    busy morning-run window, which would quietly bias the sample the launch
    decision rests on. Retries with backoff and reports loudly."""
    import time
    body = {"files": {name: {"content": json.dumps(led, indent=2)}}}
    last = ""
    for attempt in range(1, 6):
        try:
            r = requests.patch(f"https://api.github.com/gists/{_GID}",
                               headers=_headers(), json=body, timeout=25)
            if r.status_code < 400:
                return True
            last = f"{r.status_code} {r.text[:80]}"
        except Exception as exc:
            last = str(exc)
        time.sleep(1.5 * attempt)
    print(f"[evaluate] 🚨 LEDGER SAVE FAILED ({name}): {last} — "
          f"today's picks are NOT recorded and cannot be recovered.")
    return False


def _pos(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def _key(r: dict) -> tuple:
    """Ledger identity. Includes the ARM so the same ticker picked on the same
    day by two different arms is two independent observations, not a duplicate —
    without this the second arm's pick would be silently dropped and the
    comparison would quietly measure nothing."""
    return (r["date"], r["ticker"], r.get("arm"))


def record_picks(led: dict, picks: dict, day: str, arm: str | None = None,
                 known: set | None = None) -> int:
    """Append one run's picks. `arm=None` is production; an arm name marks an
    A/B variant, which the report segments separately and NEVER mixes into the
    headline (same rule as controls)."""
    rows = led.setdefault("picks", [])
    seen = {_key(r) for r in rows} | (known or set())
    added = 0
    for sec, atype, key in (("stocks", "stock", "ticker"), ("crypto", "crypto", "symbol"),
                            ("etfs", "etf", "ticker"), ("commodities", "commodity", "ticker")):
        for tf in ("short_term", "long_term"):
            for p in (picks.get(sec, {}) or {}).get(tf, []) or []:
                t = (p.get(key) or p.get("ticker") or p.get("symbol") or "").upper()
                if not t or not _pos(p.get("entry_price")):
                    continue
                row = {"date": day, "ticker": t, "asset": atype, "timeframe": tf,
                       "entry": float(p["entry_price"]),
                       "target": p.get("target_price"), "stop": p.get("stop_loss"),
                       "conviction": p.get("conviction")}
                if arm:
                    row["arm"] = arm
                scr = p.get("_screen")
                if isinstance(scr, dict):
                    row["screen"] = scr
                if _key(row) in seen:
                    continue
                rows.append(row)
                seen.add(_key(row)); added += 1
    return added


def record_today(led: dict, known: set | None = None) -> int:
    """Append today's picks to the CURRENT shard. Deduped by (date, ticker) so
    re-runs and the hourly cadence never double-count a pick. `known` carries the
    keys already present in OTHER shards — without it, a pick recorded in last
    year's shard could be re-recorded after a year rollover."""
    picks = _gist_file("picks.json")
    day   = picks.get("_saved_date")
    if not day:
        return 0
    rows = led.setdefault("picks", [])
    seen = {_key(r) for r in rows} | (known or set())
    added = 0
    for sec, atype, key in (("stocks", "stock", "ticker"), ("crypto", "crypto", "symbol"),
                            ("etfs", "etf", "ticker"), ("commodities", "commodity", "ticker")):
        for tf in ("short_term", "long_term"):
            for p in (picks.get(sec, {}) or {}).get(tf, []) or []:
                t = (p.get(key) or p.get("ticker") or p.get("symbol") or "").upper()
                if not t or (day, t, None) in seen:
                    continue
                if not _pos(p.get("entry_price")):
                    continue
                row = {
                    "date":       day,
                    "ticker":     t,
                    "asset":      atype,
                    "timeframe":  tf,
                    "entry":      float(p["entry_price"]),
                    "target":     p.get("target_price"),
                    "stop":       p.get("stop_loss"),
                    "conviction": p.get("conviction"),
                }
                # Screener features that justified this candidate (ai_analyzer
                # attaches them as `_screen`). Recorded so the report can say
                # WHICH signal earned the win, not just that there was one —
                # these cannot be backfilled once the screener cache expires.
                scr = p.get("_screen")
                if isinstance(scr, dict):
                    row["screen"] = scr
                rows.append(row)
                seen.add((day, t, None)); added += 1

    # ── Control group: the runners-up we did NOT pick ─────────────────────────
    # The screener already surfaces the top few candidates that ranked but lost
    # the final cut (near_misses). Recording them answers the question the
    # headline cannot: did the LAST step — dedup + Claude's choice — actually
    # pick better than the alternatives it passed over? Without a control, a
    # 60% win rate is unreadable: it might just be the market that month.
    #
    # Scope limit worth remembering: this grades the SELECTION step only. It says
    # nothing about whether the 600-ticker screen surfaced the right pool.
    #
    # Compact schema on purpose — controls never need target/stop/conviction,
    # and this file is kept forever against a ~1 MB Gist wall.
    from screener import setup_type                    # one definition (rubric's owner)
    nm = (picks.get("stocks", {}) or {}).get("near_misses", {}) or {}
    for tf in ("short_term", "long_term"):
        for c in nm.get(tf, []) or []:
            t = (c.get("ticker") or "").upper()
            px = c.get("current_price")
            if not t or (day, t, None) in seen or not _pos(px):
                continue
            rows.append({
                "date":      day,
                "ticker":    t,
                "asset":     "stock",
                "timeframe": tf,
                "entry":     float(px),
                "control":   True,
                "screen":    {"score": c.get("score"), "setup_type": setup_type(c)},
            })
            seen.add((day, t, None)); added += 1
    return added


# ── scoring ───────────────────────────────────────────────────────────────────

def _yf_symbol(ticker: str, asset: str) -> str:
    if asset == "crypto":
        try:
            from price_checker import CRYPTO_SYMBOLS
            if ticker in CRYPTO_SYMBOLS:
                return f"{ticker}-USD"
        except Exception:
            pass
        return f"{ticker}-USD"
    return ticker


def _bars(symbol: str, start: str, end: str):
    """Daily OHLC between two dates, or None."""
    try:
        import yfinance as yf
        df = yf.download(symbol, start=start, end=end, progress=False,
                         auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        return df.dropna()
    except Exception:
        return None


def score_pick(row: dict) -> dict | None:
    """Replay a matured pick. Returns None while it is still within its horizon
    (unscored picks must never be counted — that would bias the sample toward
    fast winners)."""
    d0 = dt.date.fromisoformat(row["date"])
    end = d0 + dt.timedelta(days=_HORIZON_DAYS)
    if end > dt.date.today():
        return None                                   # not matured yet

    sym = _yf_symbol(row["ticker"], row.get("asset", "stock"))
    df  = _bars(sym, d0.isoformat(), (end + dt.timedelta(days=1)).isoformat())
    if df is None or len(df) < 2:
        return None

    entry = float(row["entry"])
    tgt   = float(row["target"]) if _pos(row.get("target")) else None
    stop  = float(row["stop"])   if _pos(row.get("stop"))   else None

    def _col(name):
        c = df[name]
        return c.iloc[:, 0] if hasattr(c, "columns") else c

    highs, lows, closes = _col("High"), _col("Low"), _col("Close")

    outcome, exit_px = "open", float(closes.iloc[-1])
    for i in range(len(df)):
        hi, lo = float(highs.iloc[i]), float(lows.iloc[i])
        # stop checked first — the conservative assumption when both are touched
        # intraday, so the engine is never flattered by ambiguous bars.
        if stop and lo <= stop:
            outcome, exit_px = "stop", stop
            break
        if tgt and hi >= tgt:
            outcome, exit_px = "target", tgt
            break

    ret = (exit_px - entry) / entry * 100.0

    bench = _bars(_BENCH, d0.isoformat(), (end + dt.timedelta(days=1)).isoformat())
    spy_ret = None
    if bench is not None and len(bench) >= 2:
        bc = bench["Close"]
        bc = bc.iloc[:, 0] if hasattr(bc, "columns") else bc
        spy_ret = (float(bc.iloc[-1]) - float(bc.iloc[0])) / float(bc.iloc[0]) * 100.0

    # Mark-to-market at the horizon, ALWAYS — ignoring target/stop. A pick can
    # exit early at its target; a control has no levels at all, so comparing
    # `ret_pct` between them would flatter whichever had levels. This is the one
    # number that means the same thing for both.
    mtm = (float(closes.iloc[-1]) - entry) / entry * 100.0

    return {
        **row,
        "outcome":  outcome,
        "exit":     round(exit_px, 4),
        "ret_pct":  round(ret, 2),
        "mtm_pct":  round(mtm, 2),
        "mtm_alpha_pct": (round(mtm - spy_ret, 2) if spy_ret is not None else None),
        "spy_pct":  round(spy_ret, 2) if spy_ret is not None else None,
        "alpha_pct": round(ret - spy_ret, 2) if spy_ret is not None else None,
    }


# ── reporting ─────────────────────────────────────────────────────────────────

def _wilson(wins: int, n: int) -> tuple[float, float]:
    """95% Wilson confidence interval for a win rate — honest about small N."""
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, (centre - half) * 100), min(100.0, (centre + half) * 100))


def _score_band(r: dict) -> str:
    """Bucket the screener's own score. The rubric's components sum well past
    100 and the top candidates bunch at 90-95, so if the score is doing real
    work the bands should separate — and if they don't, that is the finding."""
    sc = (r.get("screen") or {}).get("score")
    if not isinstance(sc, (int, float)):
        return "?"
    if sc >= 95: return "95+"
    if sc >= 85: return "85-94"
    if sc >= 70: return "70-84"
    return "<70"


def _agg(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = sum(1 for r in rows if r["ret_pct"] > 0)
    alphas = [r["alpha_pct"] for r in rows if r.get("alpha_pct") is not None]
    beat = sum(1 for a in alphas if a > 0)
    lo, hi = _wilson(wins, n)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "win_ci": (round(lo, 1), round(hi, 1)),
        "avg_ret": round(sum(r["ret_pct"] for r in rows) / n, 2),
        "avg_alpha": round(sum(alphas) / len(alphas), 2) if alphas else None,
        "beat_spy_pct": round(beat / len(alphas) * 100, 1) if alphas else None,
    }


def build_report(scored: list[dict]) -> str:
    # 🔴 Controls are the runners-up we did NOT pick. They exist only as a
    # baseline and must NEVER enter the headline, the slices, or any number a
    # user sees — mixing them in would dilute the engine's record with trades it
    # never recommended. Split first, before anything is aggregated.
    controls = [r for r in scored if r.get("control")]
    scored   = [r for r in scored if not r.get("control")]

    if not scored:
        return ("📊 <b>Pick evaluation</b>\n\n<i>No picks have matured yet "
                f"({_HORIZON_DAYS}-day horizon). Nothing to conclude — by design.</i>")

    o = _agg(scored)
    L = ["📊 <b>Pick evaluation — are the picks beneficial?</b>",
         f"<i>{o['n']} matured picks · {_HORIZON_DAYS}-day horizon · vs {_BENCH}</i>", ""]

    a = o["avg_alpha"]
    if a is None:
        bench_line = f"vs {_BENCH}: n/a"
    elif abs(a) < 0.05:
        bench_line = f"vs {_BENCH}: 🟡 <b>in line</b> ({a:+.2f}% per pick)"
    else:
        verdict = "🟢 <b>beating</b>" if a > 0 else "🔴 <b>trailing</b>"
        bench_line = f"vs {_BENCH}: {verdict} by <b>{abs(a):.2f}%</b> per pick"
    L += [f"<b>Overall</b>  win {o['win_rate']}%  ·  avg {o['avg_ret']:+.2f}%",
          bench_line,
          f"beat {_BENCH}: {o['beat_spy_pct']}% of picks" if o["beat_spy_pct"] is not None else "",
          f"95% CI on win rate: {o['win_ci'][0]}–{o['win_ci'][1]}%", ""]

    # ── the honesty gate ──────────────────────────────────────────────────────
    if o["n"] < _MIN_N:
        L += [f"⚠️ <b>NOT CONCLUSIVE</b> — {o['n']} picks is far below the ~{_MIN_N} "
              f"needed to distinguish skill from luck. The confidence interval above "
              f"spans {o['win_ci'][1] - o['win_ci'][0]:.0f} points. "
              f"<b>Do not tune the engine on this.</b>", ""]
    else:
        ci_lo = o["win_ci"][0]
        L += [f"✅ Sample is meaningful (n={o['n']}). "
              + (f"Win rate is above coin-flip even at the low end of the CI ({ci_lo}%)."
                 if ci_lo > 50 else
                 f"Win rate is NOT distinguishable from a coin flip (CI low end {ci_lo}%)."), ""]

    # Slices. `setup_type` and `score band` come from the screener features
    # recorded at pick time — they answer WHICH part of the engine is working,
    # which the overall number cannot. Every slice carries its own n and is
    # tagged (low n) below the gate: a 6-pick slice must never read as a finding.
    for label, keyfn in (("By conviction", lambda r: f"★{r.get('conviction') or '?'}"),
                         ("By asset", lambda r: r.get("asset", "?")),
                         ("By timeframe", lambda r: r.get("timeframe", "?")),
                         ("By setup", lambda r: (r.get("screen") or {}).get("setup_type", "?")),
                         ("By screener score", _score_band)):
        groups: dict[str, list] = {}
        for r in scored:
            groups.setdefault(keyfn(r), []).append(r)
        if set(groups) == {"?"}:          # nothing recorded yet — skip the section
            continue
        L.append(f"<b>{label}</b>")
        for k in sorted(groups):
            g = _agg(groups[k])
            tag = "" if g["n"] >= _MIN_N else "  <i>(low n)</i>"
            a = f"{g['avg_alpha']:+.2f}%" if g["avg_alpha"] is not None else "n/a"
            ci = f" · CI {g['win_ci'][0]:.0f}-{g['win_ci'][1]:.0f}%" if g["n"] >= _MIN_N else ""
            L.append(f"  {k}: n={g['n']} · win {g['win_rate']}% · α {a}{ci}{tag}")
        L.append("")

    # How much of the ledger predates the instrumentation — otherwise a slice
    # built on a subset silently looks like it covers everything.
    instrumented = sum(1 for r in scored if r.get("screen"))
    if instrumented < len(scored):
        L += [f"<i>Note: {instrumented}/{len(scored)} matured picks carry screener "
              f"features; earlier picks predate the instrumentation and are "
              f"excluded from the setup/score slices.</i>", ""]

    # ── Picked vs not-picked ─────────────────────────────────────────────────
    # Compared on mark-to-market only: controls have no target/stop, so this is
    # the one basis that means the same thing for both.
    if controls:
        def _mtm(rows):
            v = [r["mtm_pct"] for r in rows if r.get("mtm_pct") is not None]
            a = [r["mtm_alpha_pct"] for r in rows if r.get("mtm_alpha_pct") is not None]
            return (len(v),
                    round(sum(v) / len(v), 2) if v else None,
                    round(sum(a) / len(a), 2) if a else None)
        pn, pret, pa = _mtm(scored)
        cn, cret, ca = _mtm(controls)
        L.append("<b>Picked vs the runners-up we passed over</b>")
        if pret is None or cret is None:
            L.append("  <i>not enough matured rows on both sides yet</i>")
        else:
            L += [f"  picked:     n={pn} · {pret:+.2f}% (α {pa:+.2f}%)" if pa is not None
                  else f"  picked:     n={pn} · {pret:+.2f}%",
                  f"  not picked: n={cn} · {cret:+.2f}% (α {ca:+.2f}%)" if ca is not None
                  else f"  not picked: n={cn} · {cret:+.2f}%"]
            edge = pret - cret
            if min(pn, cn) < _MIN_N:
                L.append(f"  <i>edge {edge:+.2f}% — but below n={_MIN_N} on at least "
                         f"one side, so this is NOT a finding yet.</i>")
            elif edge > 0:
                L.append(f"  🟢 selection added <b>{edge:+.2f}%</b> over the alternatives.")
            else:
                L.append(f"  🔴 selection <b>cost</b> {abs(edge):.2f}% vs just taking "
                         f"the runners-up — the final pick step is not adding value.")
        L += ["  <i>Grades the SELECTION step only — says nothing about whether "
              "the screen surfaced the right pool.</i>", ""]

    L.append("<i>Signal quality on every pick, traded or not. Not financial advice.</i>")
    return "\n".join([x for x in L if x != ""] + [""])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="record + score but do NOT DM — the daily cadence. "
                         "Evidence accumulates every day; the verdict is only "
                         "sent weekly, so nobody is tempted to react to noise.")
    args = ap.parse_args()

    all_rows, shard, shard_name = _load_ledger()
    added = 0
    if not args.report_only:
        other = {_key(r) for r in all_rows}
        before = len(shard.get("picks", []))
        added = record_today(shard, known=other)
        if added and not args.dry_run:
            if not _save_ledger(shard, shard_name):
                # Loud, and a non-zero exit so the workflow goes red and
                # self-heal picks it up. A silent loss here is unrecoverable —
                # it must NEVER be swallowed the way a report failure can be.
                try:
                    from telegram_api import send_message
                    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
                    if owner:
                        send_message(
                            f"🚨 <b>Pick ledger save FAILED</b>\n{added} pick(s) from "
                            f"today were not recorded and cannot be recovered.",
                            chat_id=owner)
                except Exception as exc:
                    print(f"[evaluate] alert send failed: {exc}")
                return 1
        # newly appended rows are in the shard but not yet in all_rows
        all_rows = all_rows + shard["picks"][before:]
    rows = all_rows
    n_ctl = sum(1 for r in rows if r.get("control"))
    print(f"[evaluate] ledger={len(rows)} rows ({len(rows) - n_ctl} picks + "
          f"{n_ctl} controls, +{added} today) · shard={shard_name}")

    scored = []
    for r in rows:
        try:
            s = score_pick(r)
            if s:
                scored.append(s)
        except Exception as exc:
            print(f"[evaluate] score {r.get('ticker')} failed: {exc}")
    print(f"[evaluate] matured & scored: {len(scored)}")

    report = build_report(scored)
    print("\n" + report.replace("<b>", "").replace("</b>", "")
          .replace("<i>", "").replace("</i>", ""))

    if not args.dry_run and not args.quiet:
        try:
            from telegram_api import send_message
            owner = os.environ.get("TELEGRAM_CHAT_ID", "")
            if owner:
                send_message(report, chat_id=owner)
        except Exception as exc:
            print(f"[evaluate] send failed: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
