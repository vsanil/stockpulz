"""
traffic_tracker.py — when do people actually use this, and did they pay for a
cold server when they did?

Why this exists
---------------
Render's free plan spins the service down after ~15 min idle, and the keep-warm
window was narrowed to 10:00-03:59 UTC to stay under the 750 h/month account cap.
That trade was made on reasoning, not evidence: nobody knew whether anyone
touches the bot between 11 PM and 5 AM ET. This is the evidence, and it is the
input to a future "is $7/mo worth it" decision.

The metric that matters is NOT "hits during the cold window"
------------------------------------------------------------
A request at 04:30 UTC that lands on an already-warm server cost the user
nothing and argues for nothing. What justifies paying is a request that actually
paid the cold-start price — a 30-60s wait, or a dropped reply. So every hit is
recorded against the UTC hour, and separately flagged `cold` when the process
had only just booted to serve it.

Design constraints — all deliberate, do not "optimise" them away
----------------------------------------------------------------
  * 🔴 COLD HITS FLUSH IMMEDIATELY. The obvious design (count in memory, flush
    on a timer) loses exactly the data this module exists to collect: a lone
    3 AM tap wakes the server, increments a counter, and then Render spins the
    process down 15 min later and the count evaporates. Isolated cold-window
    events are the MOST likely to be lost and they are the whole point. Cold
    hits are rare by definition, so writing one out immediately is affordable.
  * Warm hits batch behind a time + count threshold. Gist writes are rate
    limited, and that limit has cost this project data before.
  * Aggregate only: a per-hour, per-user COUNT. No timestamps, no paths, no
    message content — enough to answer "does anyone use this at 3 AM, and who",
    not enough to reconstruct a session.
  * Failure is always silent. Telemetry must never degrade the app.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

# Set at import — i.e. when the process boots. Render kills the process on
# spin-down, so a fresh, tiny value here means this process booted to serve
# whatever request is being handled right now.
_PROC_START = time.time()

# A request arriving within this many seconds of boot was almost certainly the
# one that triggered the boot. Render cold starts run ~30-60s; the margin covers
# a slow boot without sweeping up ordinary traffic on a long-lived process.
COLD_START_SECS = 90.0

# Warm-hit flush policy. Either bound can trigger; whichever comes first.
_FLUSH_AFTER_SECS = 600.0
_FLUSH_AFTER_HITS = 25

# 🔴 Debounce for the immediate cold flush. `_PROC_START` makes EVERY request in
# the first COLD_START_SECS look cold, so a burst (full_sweep drives 60 paths
# in-process, the morning relay, a user tapping through tabs after a boot) fired
# one gist PATCH per request. Observed live 2026-08-12: 6 x "409 Conflict" on
# traffic_hours.json in a single sweep, plus collateral 409s on user_configs and
# a trade log.
#
# That matters beyond noise — rapid successive PATCHes to one gist are what
# tripped GitHub's secondary rate limit on 2026-07-03 and left the admin's
# alerts wiped when the RESTORE was the blocked call.
#
# The intent still holds: the FIRST cold hit flushes immediately, so a lone 3 AM
# tap survives the spin-down that follows. Only bursts are throttled — and a
# burst means the process is busy, so it is not about to idle out anyway.
_MIN_COLD_FLUSH_GAP = 20.0

TRAFFIC_FILE = "traffic_hours.json"

_lock = threading.Lock()
_last_cold_flush = 0.0
_pending: dict = {}            # {"YYYY-MM": {"HH": {"hits":n,"cold":n,"users":{uid:n}}}}
_pending_hits = 0
_last_flush = time.time()


def process_age() -> float:
    return time.time() - _PROC_START


def is_cold_start() -> bool:
    """True while this process is young enough that it booted for this request."""
    return process_age() < COLD_START_SECS


def _blank() -> dict:
    return {"hits": 0, "cold": 0, "users": {}}


def _copy(stored: dict) -> dict:
    """Deep-ish copy so merging pending counts for display can never mutate the
    caller's data (which is the freshly-read gist blob)."""
    return {m: {h: {"hits": int((r or {}).get("hits", 0)),
                    "cold": int((r or {}).get("cold", 0)),
                    "users": dict((r or {}).get("users") or {})}
                for h, r in (hours or {}).items()}
            for m, hours in (stored or {}).items()}


def _merge(dst: dict, src: dict) -> dict:
    """Fold `src` counts into `dst`. Used both to accumulate in memory and to
    merge our pending counts onto whatever is already stored."""
    for month, hours in (src or {}).items():
        d_month = dst.setdefault(month, {})
        for hour, rec in (hours or {}).items():
            d = d_month.setdefault(hour, _blank())
            d["hits"] = int(d.get("hits", 0)) + int((rec or {}).get("hits", 0))
            d["cold"] = int(d.get("cold", 0)) + int((rec or {}).get("cold", 0))
            for uid, n in ((rec or {}).get("users") or {}).items():
                d["users"][str(uid)] = int(d["users"].get(str(uid), 0)) + int(n)
    return dst


def record(chat_id: str | None = None, cold: bool | None = None,
           now: datetime | None = None) -> bool:
    """Count one real user interaction. Returns True if it flushed.

    `cold` is injectable for tests; in production it is derived from how long
    this process has been alive.
    """
    global _pending_hits
    try:
        cold = is_cold_start() if cold is None else bool(cold)
        now = now or datetime.now(timezone.utc)
        month, hour = now.strftime("%Y-%m"), now.strftime("%H")
        uid = str(chat_id) if chat_id else "anon"

        with _lock:
            rec = _pending.setdefault(month, {}).setdefault(hour, _blank())
            rec["hits"] += 1
            rec["cold"] += 1 if cold else 0
            rec["users"][uid] = rec["users"].get(uid, 0) + 1
            _pending_hits += 1
            # A cold hit still flushes immediately — but at most once per
            # _MIN_COLD_FLUSH_GAP, so a burst of "cold" requests cannot become a
            # burst of gist writes. Anything held back is picked up by the
            # count/time thresholds below, or by the next cold flush.
            global _last_cold_flush
            now_t = time.time()
            cold_due = cold and (now_t - _last_cold_flush) >= _MIN_COLD_FLUSH_GAP
            if cold_due:
                _last_cold_flush = now_t
            due = (cold_due
                   or _pending_hits >= _FLUSH_AFTER_HITS
                   or (now_t - _last_flush) >= _FLUSH_AFTER_SECS)
        if due:
            return flush()
    except Exception:
        pass                                                   # telemetry never breaks a request
    return False


def flush() -> bool:
    """Merge pending counts into the stored file. Silent on failure, and the
    counts are only cleared once the write is known to have landed — otherwise a
    transient gist error would silently discard them."""
    global _pending_hits, _last_flush
    with _lock:
        if not _pending:
            _last_flush = time.time()
            return False
        batch = {m: {h: {"hits": r["hits"], "cold": r["cold"], "users": dict(r["users"])}
                     for h, r in hours.items()}
                 for m, hours in _pending.items()}

    try:
        from config_manager import mutate_gist_file

        # 🔴 mutate_gist_file's contract is `mutator(current) -> (new_contents, result)`.
        # Returning a bare dict raises "not enough values to unpack", which this
        # function then SWALLOWS — so the tracker silently never wrote at all.
        def _mut(data):
            return _merge(dict(data or {}), batch), None

        mutate_gist_file(TRAFFIC_FILE, _mut, default={})
    except Exception as exc:
        print(f"[traffic] flush failed (counts kept for the next attempt): {exc}")
        return False

    with _lock:
        # Subtract exactly what was written rather than clearing outright — a
        # concurrent request may have counted a hit while the write was in
        # flight, and dropping it would lose a cold hit at the worst moment.
        for month, hours in batch.items():
            for hour, rec in hours.items():
                cur = _pending.get(month, {}).get(hour)
                if not cur:
                    continue
                cur["hits"] -= rec["hits"]
                cur["cold"] -= rec["cold"]
                for uid, n in rec["users"].items():
                    left = cur["users"].get(uid, 0) - n
                    if left > 0:
                        cur["users"][uid] = left
                    else:
                        cur["users"].pop(uid, None)
                if cur["hits"] <= 0 and not cur["users"]:
                    _pending[month].pop(hour, None)
            if not _pending.get(month):
                _pending.pop(month, None)
        _pending_hits = sum(r["hits"] for hs in _pending.values() for r in hs.values())
        _last_flush = time.time()
    return True


# ── reading it back ────────────────────────────────────────────────────────
def _warm_hours() -> set:
    """The keep-warm window, as UTC hours. Kept in ONE place so the card cannot
    drift from the schedule it is describing — the same drift that let the entry
    window be promised at 2% and enforced at 3%."""
    raw = os.environ.get("KEEPWARM_HOURS_UTC", "0,1,2,3,10,11,12,13,14,15,16,17,18,19,20,21,22,23")
    out = set()
    for part in raw.split(","):
        try:
            out.add(int(part.strip()))
        except ValueError:
            continue
    return out


def pending_snapshot() -> dict:
    """A copy of the counts not yet written out.

    The admin card merges these so it reflects what has actually happened rather
    than what happened to have been flushed. Warm hits batch behind a 25-hit /
    10-minute threshold, so at two users a real interaction could otherwise sit
    invisible for hours — and an under-reporting card is the specific failure
    mode this whole module is guarding against.
    """
    with _lock:
        return {m: {h: {"hits": r["hits"], "cold": r["cold"], "users": dict(r["users"])}
                    for h, r in hours.items()}
                for m, hours in _pending.items()}


def summarise(stored: dict, month: str | None = None,
              include_pending: bool = True) -> dict:
    """Shape the stored counts for the admin card."""
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    stored = _merge(_copy(stored), pending_snapshot()) if include_pending else (stored or {})
    hours = (stored or {}).get(month) or {}
    warm = _warm_hours()

    rows, users = [], {}
    tot = cold_tot = cold_window_hits = cold_window_cold = 0
    for h in range(24):
        rec = hours.get(f"{h:02d}") or {}
        hits, cold = int(rec.get("hits", 0)), int(rec.get("cold", 0))
        in_window = h in warm
        tot += hits
        cold_tot += cold
        if not in_window:
            cold_window_hits += hits
            cold_window_cold += cold
        for uid, n in (rec.get("users") or {}).items():
            u = users.setdefault(str(uid), {"hits": 0, "cold_window": 0})
            u["hits"] += int(n)
            if not in_window:
                u["cold_window"] += int(n)
        rows.append({"hour": h, "hits": hits, "cold": cold, "warm": in_window})

    return {
        "month": month,
        "total": tot,
        "cold_starts": cold_tot,
        "hours": rows,
        # The number the upgrade decision actually rests on: interactions that
        # happened while the service was scheduled to be asleep.
        "outside_window": cold_window_hits,
        "outside_window_cold": cold_window_cold,
        "peak_hour": max(rows, key=lambda r: r["hits"])["hour"] if tot else None,
        "users": sorted(
            ({"chat_id": k, **v} for k, v in users.items()),
            key=lambda u: (-u["cold_window"], -u["hits"])),
        # Never let an empty card read as "nobody uses it at night" when it may
        # simply be "we only started counting on Tuesday".
        "no_data": tot == 0,
    }
