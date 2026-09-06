"""
config_manager.py — Read/write agent config from a GitHub Gist (JSON store).
Falls back to hardcoded defaults if the Gist is unreachable.
"""
from __future__ import annotations

import os
import json
import time
import threading
import requests

# ── In-memory Gist read cache ─────────────────────────────────────────────────
# Interactive webhook commands fire 3-5 Gist reads each (pending_state, config,
# user_configs, allowed_users…). Without caching that's 1-2s of GitHub API latency
# before any logic runs.
#
# Strategy: cache reads for GIST_CACHE_TTL seconds. Writes always:
#   1. Go straight to GitHub (source of truth)
#   2. Immediately invalidate the relevant cache key
#
# TTL is intentionally short (20s) — stale reads only matter if two commands
# arrive within the window AND the first one changed data the second one reads.
# In practice this is rare and recovers automatically on the next cache miss.

_gist_read_cache: dict[str, tuple[object, float]] = {}
GIST_CACHE_TTL = 20   # seconds

# In-memory pending state — no Gist needed, survives only for process lifetime.
# Conversation state is transient; if the server restarts mid-conversation
# the user simply retypes the command. 5-minute TTL per user.
_PENDING_STATE_CACHE: dict[str, dict] = {}


def _cache_get(filename: str):
    entry = _gist_read_cache.get(filename)
    if entry:
        data, ts = entry
        if time.time() - ts < GIST_CACHE_TTL:
            return data
    return None


def _cache_set(filename: str, data) -> None:
    _gist_read_cache[filename] = (data, time.time())


def _cache_invalidate(filename: str) -> None:
    _gist_read_cache.pop(filename, None)

# ── Default config ────────────────────────────────────────────────────────────
# ── Global bot config (shared across all users) ───────────────────────────────
DEFAULT_CONFIG = {
    "max_short_picks": 2,
    "max_long_picks": 3,
    "stop_loss_pct": 7,
    "target_gain_pct": 15,
    "enabled": True,
    "timezone": "America/New_York",
    "crypto_enabled": True,
    "max_crypto_short_picks": 2,
    "max_crypto_long_picks": 2,
    # Multi-user pilot: list of chat_id strings allowed to receive messages
    "allowed_users": [],
    # Social: show how many members bought each pick (admin-only toggle)
    # Keep False until the user base is large enough to make counts meaningful
    "show_buy_counts": False,
}

# ── Per-user config (each user has their own copy of these) ───────────────────
DEFAULT_USER_CONFIG = {
    "risk_profile":      "moderate",   # conservative | moderate | aggressive
    "excluded_sectors":  [],           # e.g. ["Energy", "Utilities"]
    "watchlist":         [],           # e.g. ["NVDA", "TSLA"] — always evaluated
    "pick_mode":         "both",       # "st" | "lt" | "both"
    "stock_budget":      None,         # total daily $ for stocks (null = unset)
    "crypto_budget":     None,         # total daily $ for crypto (null = unset)
    "paused":            False,        # True = user opted out of daily picks
    "max_stock_picks":   None,         # cap total stock picks shown (None = use global defaults)
    "max_crypto_picks":  None,         # cap total crypto picks shown (None = use global defaults)
    "stop_loss_pct":     None,         # % below entry to auto-close (None = use global default of 7%)
    "target_gain_pct":   None,         # % above entry as default target (None = use global default of 15%)
    "show_crypto":       True,         # False = hide all crypto sections from daily picks
    "show_etfs":         True,         # False = hide ETF sections from daily picks
    "assets":            "both",       # "stocks" | "crypto" | "both" (mini-app asset filter)
    "min_conviction":    3,            # minimum conviction (1-5) for a pick to be shown
    "portfolio":         {},           # {portfolio_size, risk_per_trade_pct, max_position_pct, max_sector_pct}
    # Notification opt-outs (all default on)
    "skip_confirmation":     False,    # True = skip 10:30 AM confirmation message
    "skip_eod":              False,    # True = skip 3:30 PM EOD portfolio summary
    "skip_watchlist_alerts": False,    # True = skip watchlist RSI/MACD signal alerts
}

# Canonical fallback budgets when a user hasn't set one. Single source of truth —
# /size, onboarding, and settings all derive from these so a budget-unset user
# gets the same position size everywhere (was 500/100 in /size vs 200/50 elsewhere).
DEFAULT_STOCK_BUDGET  = 200
DEFAULT_CRYPTO_BUDGET = 50


def shows_crypto(cfg: dict) -> bool:
    """
    Single resolver for "should this user see crypto?" — reconciles the mini-app
    `assets` key ("stocks"|"crypto"|"both") with the bot's `show_crypto` toggle.
    These were read independently, so a user who set "stocks only" in the app
    still got crypto in the morning broadcast (which read show_crypto).
    """
    a = cfg.get("assets")
    if a == "stocks":
        return False
    if a == "crypto":
        return True
    return bool(cfg.get("show_crypto", True))


def shows_etfs(cfg: dict) -> bool:
    """Should this user see ETFs? Crypto-only hides them; else the show_etfs toggle."""
    if cfg.get("assets") == "crypto":
        return False
    return bool(cfg.get("show_etfs", True))


def et_today():
    """
    Today's date in US/Eastern — the trading day. Use for dedup keys, date
    stamps, and day-diffs. Bare `date.today()` is server-local (UTC on Render),
    so it rolls over at UTC midnight = 7–8 PM ET — making evening alerts double-
    fire or wrongly suppress, and disagreeing with the ET-based picks date.
    Returns a datetime.date.
    """
    import pytz
    from datetime import datetime as _dt
    return _dt.now(pytz.timezone("America/New_York")).date()


GIST_FILENAME          = "config.json"
PICKS_FILENAME         = "picks.json"           # Stores morning picks for 10:30 AM confirmation
WEEKLY_PICKS_FILENAME  = "weekly_picks.json"    # Accumulates Mon–Fri picks for Saturday recap
PENDING_STATE_FILENAME = "pending_state.json"   # Conversation state for multi-step commands
PENDING_USERS_FILE     = "pending_users.json"   # Users awaiting admin approval
PRICE_ALERTS_FILE      = "price_alerts.json"    # User price alerts (already per-user by chat_id)
SIGNAL_CACHE_FILE      = "signal_cache.json"    # Cached sentiment + insider signals (5-day TTL)
SCREENER_CACHE_FILE    = "screener_cache.json"  # Pre-scored candidates from midnight run
BUY_COUNTS_FILE        = "buy_counts.json"      # Social: how many members bought each pick today
MACRO_CACHE_FILE       = "macro_cache.json"     # Fear & Greed + FRED rates (4h TTL, shared across all users)
# ── Per-user data files (keyed by chat_id inside the JSON) ───────────────────
USER_CONFIGS_FILE      = "user_configs.json"    # Per-user settings (risk, watchlist, budget…)
USER_TRADES_FILE       = "user_trades.json"     # Per-user trade logs (open + closed)
USER_PAPER_FILE        = "user_paper.json"      # Per-user paper portfolios
FEEDBACK_FILE          = "feedback.json"        # User feedback submissions
BACKTEST_TRADES_FILE   = "backtest_trades.json" # Simulated trades seeded from backtester (per-user)
AUDIT_DISPOSITIONS_FILE = "audit_dispositions.json"  # Owner's resolved/ignored marks on audit findings

# Files whose TOP-LEVEL KEYS are chat_ids. On a row-capable backend these become
# one row PER USER (the whole point of the Supabase migration: writing user A can
# never clobber user B, and same-user writes get real compare-and-swap). On the
# Gist they stay single JSON blobs.
USER_KEYED_FILES = {
    USER_CONFIGS_FILE, USER_TRADES_FILE, USER_PAPER_FILE,
    PRICE_ALERTS_FILE, BACKTEST_TRADES_FILE, PENDING_USERS_FILE,
}

# ── User activity log (per-user, for admin troubleshooting) ──────────────────
_USER_LOG_MAX       = 150   # max events kept per user in Gist
_USER_LOG_FLUSH_AT  = 8     # flush in-memory buffer to Gist after this many events
_user_event_buffer: dict = {}  # {chat_id: [event, …]}


def _gist_headers() -> dict:
    token = os.environ.get("GH_GIST_TOKEN", "")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def _gist_id() -> str:
    gist_id = os.environ.get("GIST_ID", "")
    if not gist_id:
        raise EnvironmentError("GIST_ID environment variable is not set.")
    return gist_id


# ── Public API ────────────────────────────────────────────────────────────────

def get_config() -> dict:
    """Fetch config.json from Gist (cached). Falls back to DEFAULT_CONFIG on error."""
    cached = _cache_get(GIST_FILENAME)
    if cached is not None:
        return {**DEFAULT_CONFIG, **cached}
    try:
        url = f"https://api.github.com/gists/{_gist_id()}"
        resp = requests.get(url, headers=_gist_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        raw    = data["files"][GIST_FILENAME]["content"]
        config = json.loads(raw)
        _cache_set(GIST_FILENAME, config)
        return {**DEFAULT_CONFIG, **config}
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not fetch Gist config ({exc}). Using defaults.")
        return dict(DEFAULT_CONFIG)


def update_config(key: str, value) -> dict:
    """Patch a single key in config.json on the Gist. Returns updated config."""
    config = get_config()
    config[key] = value
    _write_config(config)
    return config


def update_config_multi(updates: dict) -> dict:
    """Patch multiple keys at once. Returns updated config."""
    config = get_config()
    config.update(updates)
    _write_config(config)
    return config


def reset_config() -> dict:
    """Restore config.json on the Gist to DEFAULT_CONFIG. Returns defaults."""
    _write_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


# ── Per-user config ───────────────────────────────────────────────────────────

def get_user_config(chat_id: str) -> dict:
    """Return config for a specific user, merged with DEFAULT_USER_CONFIG."""
    all_configs = _load_gist_file(USER_CONFIGS_FILE) or {}
    user = all_configs.get(str(chat_id), {})
    return {**DEFAULT_USER_CONFIG, **user}


def update_user_config(chat_id: str, key: str, value) -> dict:
    """Update a single key for a user. Returns updated user config.

    These two are the app's most-called writers (every settings toggle, budget
    change and preference flip routes through them). They used the forbidden
    `_load_gist_file(F) or {}` → `_write_gist_file(F, whole_file)` pattern:
    `_load_gist_file` SWALLOWS a transient read error and returns None, so the
    `or {}` turned one failed read into a write of {this_user: …} — erasing
    every other user's config. Routing through mutate_user_record gets
    read_strict (raises instead of clobbering), a fresh read inside the lock,
    and row-level CAS once the backend supports rows.
    """
    def _mut(record):
        record = dict(record or {})
        record[key] = value
        return record, None

    mutate_user_record(USER_CONFIGS_FILE, chat_id, _mut, default={})
    return get_user_config(chat_id)


def update_user_config_multi(chat_id: str, updates: dict) -> dict:
    """Update multiple keys for a user at once. Returns updated user config."""
    def _mut(record):
        record = dict(record or {})
        record.update(updates)
        return record, None

    mutate_user_record(USER_CONFIGS_FILE, chat_id, _mut, default={})
    return get_user_config(chat_id)


def save_user_config(chat_id: str, ucfg: dict) -> None:
    """Persist the full user config dict (replaces existing entry)."""
    _update_user_keyed_file(USER_CONFIGS_FILE, chat_id, ucfg)


def reset_user_config(chat_id: str) -> dict:
    """Reset a user's config to DEFAULT_USER_CONFIG."""
    _update_user_keyed_file(USER_CONFIGS_FILE, chat_id, dict(DEFAULT_USER_CONFIG))
    return dict(DEFAULT_USER_CONFIG)


# ── Per-user trade log ────────────────────────────────────────────────────────

def load_user_trade_log(chat_id: str) -> dict:
    """Load trade log for a specific user."""
    all_logs = _load_gist_file(USER_TRADES_FILE) or {}
    log = all_logs.get(str(chat_id), {"open": [], "closed": []})
    log.setdefault("open", [])
    log.setdefault("closed", [])
    return log


def save_user_trade_log(chat_id: str, log: dict) -> None:
    """Save trade log for a specific user (clobber-safe, concurrency-safe)."""
    _update_user_keyed_file(USER_TRADES_FILE, chat_id, log)


# ── Per-user paper portfolio ──────────────────────────────────────────────────

def load_user_paper(chat_id: str) -> dict:
    """Load paper portfolio for a specific user."""
    all_paper = _load_gist_file(USER_PAPER_FILE) or {}
    data = all_paper.get(str(chat_id), {})
    data.setdefault("positions", [])
    data.setdefault("history", [])
    data.setdefault("starting_cash", 10_000.0)
    data.setdefault("cash", data["starting_cash"])
    return data


def save_user_paper(chat_id: str, data: dict) -> None:
    """Save paper portfolio for a specific user (clobber-safe, concurrency-safe)."""
    _update_user_keyed_file(USER_PAPER_FILE, chat_id, data)


# ── Position-audit dispositions ───────────────────────────────────────────────
# Findings themselves are DERIVED fresh every run (see position_audit) — only the
# owner's disposition is stored. Persisting the findings instead would rot into a
# stale worklist, which is how such lists stop being read.

def load_audit_dispositions() -> dict:
    return _load_gist_file(AUDIT_DISPOSITIONS_FILE) or {}


ENGINE_FINDINGS_FILE = "engine_findings_state.json"


def get_finding_dispositions() -> dict:
    """Decisions on engine findings, keyed by finding id.

    🔴 Lives in STORAGE, not in the repo. The daily analysis writes the REPORT
    to analysis/ENGINE_FINDINGS.md and commits it, but the DECISIONS cannot live
    beside it: Render's filesystem is ephemeral, so an approval made on /admin
    would vanish on the next deploy and the GitHub Actions job would never see
    it. Same reasoning — and same mechanism — as audit_dispositions.
    """
    return _load_gist_file(ENGINE_FINDINGS_FILE) or {}


def set_finding_disposition(finding_id: str, status: str, note: str = "",
                            extra: dict | None = None) -> dict:
    """status: open | awaiting_approval | approved | acknowledged | wont_fix.

    `extra` carries the proposal (proposed_change, proposed_files) and the
    consent stamp (approved_on) so the whole lifecycle is one record.
    """
    from datetime import datetime, timezone
    fid = str(finding_id)[:96]

    def _mut(data):
        data = data or {}
        rec = dict(data.get(fid) or {})
        rec.update(extra or {})
        rec["status"] = status
        if note:
            rec["note"] = str(note)[:600]
        rec["updated_at"] = datetime.now(timezone.utc).isoformat()
        if data.get(fid) == rec:
            return NO_WRITE, rec
        data[fid] = rec
        return data, rec

    return mutate_gist_file(ENGINE_FINDINGS_FILE, _mut, default={})


def set_audit_disposition(finding_id: str, status: str, note: str = "") -> dict:
    """status: 'resolved' | 'ignored' | 'open' (which clears the mark)."""
    from datetime import datetime, timezone
    fid = str(finding_id)[:64]

    def _mut(data):
        data = data or {}
        if status == "open":
            if fid not in data:
                return NO_WRITE, {}
            data.pop(fid, None)
            return data, {}
        rec = {"status": status, "note": str(note or "")[:300],
               "updated_at": datetime.now(timezone.utc).isoformat()}
        if data.get(fid) == rec:
            return NO_WRITE, rec
        data[fid] = rec
        return data, rec

    return mutate_gist_file(AUDIT_DISPOSITIONS_FILE, _mut, default={})


# ── Multi-user allowlist helpers ─────────────────────────────────────────────

def get_allowed_users() -> list[str]:
    """Return list of allowed chat_ids. Always includes TELEGRAM_CHAT_ID (owner)."""
    config = get_config()
    users  = [str(u) for u in config.get("allowed_users", [])]
    owner  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if owner and owner not in users:
        users.insert(0, owner)
    return users


# ── Synthetic TEST accounts (single source of truth) ──────────────────────────
# The synthetic-user bot trades on a VIRTUAL chat_id so the owner's real account
# stays clean. That id is NOT a real Telegram user, so:
#   • it must stay OUT of get_allowed_users() → no broadcasts, and (deliberately)
#     excluded from community stats + the LLM pick-feedback loop, which both
#     iterate allowed users. A robot's mechanical fills must never steer real picks.
#   • any send to it must be skipped (telegram_api.is_test_user) — a dead chat_id
#     costs ~10s of retries per send and would spam "chat not found" forever,
#     masking a REAL user who blocked the bot.
DEFAULT_TEST_CHAT_ID = "900000001"


# A trade opened by the synthetic bot, not by a human. Tagged (never deleted) so
# the record stays complete while being EXCLUDED from every user-facing aggregate
# and from the LLM pick-feedback prompt — a robot's mechanical stop-outs must not
# be shown as a user track record, nor steer tomorrow's real picks.
SYNTHETIC_SOURCE = "synthetic_bot"


def is_synthetic_trade(trade: dict) -> bool:
    """True if this trade was opened by the synthetic bot (any account)."""
    if not isinstance(trade, dict):
        return False
    return str(trade.get("source") or "") == SYNTHETIC_SOURCE


def _one_year_after(d):
    """The one-year anniversary of a date. Feb 29 has no anniversary in a common
    year; Mar 1 is the conventional substitute."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, month=3, day=1)


def is_long_term_hold(opened, closed=None) -> bool | None:
    """Is this holding period long-term for US capital-gains purposes?

    🔴 The IRS test is held MORE THAN one year, and the holding period starts
    the day AFTER acquisition — so selling on the one-year anniversary is still
    SHORT-term; you need one more day.

    A day count cannot express that: one year is 365 days in a common year and
    366 in a leap year, so the `days >= 365` this replaces mislabelled the
    anniversary itself as long-term. That errs toward the LOWER rate (15% vs
    ~35%), i.e. it UNDER-states the tax owed — the harmful direction for anyone
    planning around it. Compare calendar dates instead.

    Returns None when either date is missing or unparseable, so callers can
    tell "not long-term" apart from "unknown".
    """
    from datetime import date as _d
    try:
        o = _d.fromisoformat(str(opened)[:10])
        c = _d.fromisoformat(str(closed)[:10]) if closed is not None else et_today()
    except (TypeError, ValueError):
        return None
    return c > _one_year_after(o)


def days_until_long_term(opened, today=None) -> int | None:
    """Days remaining before a still-open position qualifies as long-term.

    None when already long-term, or when the open date is unusable.
    """
    from datetime import date as _d, timedelta as _td
    try:
        o = _d.fromisoformat(str(opened)[:10])
        t = _d.fromisoformat(str(today)[:10]) if today is not None else et_today()
    except (TypeError, ValueError):
        return None
    first_lt_day = _one_year_after(o) + _td(days=1)
    return None if t >= first_lt_day else (first_lt_day - t).days


def human_trades(trades) -> list:
    """Drop synthetic-bot trades from a list. Use at every aggregation point."""
    return [t for t in (trades or []) if not is_synthetic_trade(t)]


# A/B arm accounts. Each arm paper-trades its own variant of the engine into its
# own account. They MUST be test users: a dead chat_id otherwise burns ~10s of
# Telegram retries per send, and they must never appear in community stats or
# the LLM pick prompt (they are also absent from allowed_users, which is what
# actually excludes them from both).
ARM_CHAT_IDS: dict[str, str] = {
    "breakout": "900000010",
    "pullback": "900000011",
}


def get_test_users() -> list[str]:
    """Virtual/synthetic chat_ids. Pure env+constant (no Gist read) so it is safe
    to call on the hot send path."""
    ids = {DEFAULT_TEST_CHAT_ID} | set(ARM_CHAT_IDS.values())
    env = (os.environ.get("SYNTHETIC_CHAT_ID") or "").strip()
    if env:
        ids.add(env)
    return sorted(ids)


def is_test_user(chat_id) -> bool:
    s = str(chat_id or "").strip()
    return bool(s) and s in set(get_test_users())


def add_allowed_user(chat_id: str) -> list[str]:
    """Add a chat_id to allowed_users. Returns updated list."""
    config = get_config()
    users  = [str(u) for u in config.get("allowed_users", [])]
    if str(chat_id) not in users:
        users.append(str(chat_id))
        update_config("allowed_users", users)
        print(f"[config_manager] Added user {chat_id} to allowlist.")
    return users


def remove_allowed_user(chat_id: str) -> list[str]:
    """Remove a chat_id from allowed_users. Returns updated list."""
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    if str(chat_id) == str(owner):
        raise ValueError("Cannot remove the bot owner from the allowlist.")
    config = get_config()
    users  = [str(u) for u in config.get("allowed_users", []) if str(u) != str(chat_id)]
    update_config("allowed_users", users)
    print(f"[config_manager] Removed user {chat_id} from allowlist.")
    return users


# ── Pending users (awaiting admin approval) ──────────────────────────────────

def get_pending_users() -> dict:
    """Return pending users dict: { chat_id: {first_name, username, requested_at} }

    Empty records are filtered out: on a ROW backend a removal is written as an
    empty record (rows are updated, not deleted), and a tombstone must not read
    back as somebody still waiting for approval.
    """
    data = _load_gist_file(PENDING_USERS_FILE) or {}
    return {uid: rec for uid, rec in data.items() if rec}


def add_pending_user(chat_id: str, first_name: str = "", username: str = "") -> None:
    """Add a user to the pending approval list."""
    from datetime import datetime, timezone
    record = {
        "first_name":    first_name,
        "username":      username,
        "requested_at":  datetime.now(timezone.utc).isoformat(),
    }
    # Was `get_pending_users() or {}` → write-whole-file: one transient read
    # error dropped everyone else waiting for approval.
    mutate_user_record(PENDING_USERS_FILE, chat_id,
                       lambda _rec: (record, None), default={})


def remove_pending_user(chat_id: str) -> None:
    """Remove a user from the pending list (after approval or rejection)."""
    def _mut(pending):
        pending = pending or {}
        if not pending.get(str(chat_id)):
            return NO_WRITE, None
        del pending[str(chat_id)]
        return pending, None

    mutate_gist_file(PENDING_USERS_FILE, _mut, default={})


# ── Picks storage (for 10:30 AM confirmation run) ────────────────────────────

def save_picks(picks: dict) -> None:
    """Save morning picks to Gist as picks.json for the confirmation run."""
    picks["_saved_date"] = et_today().isoformat()
    url = f"https://api.github.com/gists/{_gist_id()}"
    payload = {
        "files": {
            PICKS_FILENAME: {
                "content": json.dumps(picks, indent=2)
            }
        }
    }
    try:
        resp = requests.patch(url, headers=_gist_headers(), json=payload, timeout=10)
        resp.raise_for_status()
        print("[config_manager] Morning picks saved to Gist.")
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not save picks ({exc}).")


def load_picks() -> dict | None:
    """Load today's morning picks from Gist. Returns None if not found or stale.
    Date comparison uses US/Eastern timezone so picks stay valid until midnight ET
    (not midnight UTC which would expire them at 8 PM ET)."""
    today_et = et_today().isoformat()
    try:
        url = f"https://api.github.com/gists/{_gist_id()}"
        resp = requests.get(url, headers=_gist_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        files = data.get("files", {})
        if PICKS_FILENAME not in files:
            return None
        raw   = files[PICKS_FILENAME]["content"]
        picks = json.loads(raw)
        # Only return picks saved today (ET date)
        if picks.get("_saved_date") != today_et:
            print("[config_manager] Picks are from a previous day — skipping confirmation.")
            return None
        return picks
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not load picks ({exc}).")
        return None


# ── Weekly picks storage (for Saturday recap) ────────────────────────────────

def save_weekly_pick(picks: dict) -> None:
    """Append today's picks to weekly_picks.json in Gist. Clears stale weeks automatically."""
    from datetime import date, timedelta
    today = date.today().isoformat()

    # Load existing weekly data
    weekly = _load_gist_file(WEEKLY_PICKS_FILENAME) or {}

    # If the oldest entry is > 6 days old, it's a new week — start fresh
    if weekly:
        oldest = min(weekly.keys())
        try:
            if (date.today() - date.fromisoformat(oldest)).days > 6:
                weekly = {}
        except ValueError:
            weekly = {}

    weekly[today] = picks
    _write_gist_file(WEEKLY_PICKS_FILENAME, weekly)
    print(f"[config_manager] Weekly picks updated ({len(weekly)} days this week).")


def load_weekly_picks() -> dict:
    """Load this week's picks keyed by date string. Returns {} if empty or missing."""
    return _load_gist_file(WEEKLY_PICKS_FILENAME) or {}


# ── Dynamic pick counts ───────────────────────────────────────────────────────

def get_dynamic_pick_counts(config: dict) -> dict:
    """
    Compute pick counts from the two-bucket budgets.
    If budget is unset, fall back to the config's existing max_*_picks values.

    Stock budget split equally between ST and LT:
      $100 stock → 2 ST + 3 LT (defaults), $200 → up to 4 ST + 5 LT
    Crypto budget split equally between ST and LT.

    Min per pick: stocks $12, crypto $10. Max: 5 ST / 6 LT / 4 CST / 4 CLT.
    """
    def _count(budget: float | None, share: float, min_per_pick: float,
               max_picks: int, default: int) -> int:
        if not budget:
            return default
        allocated = budget * share   # rough half for ST, half for LT
        return max(2, min(max_picks, int(allocated / min_per_pick)))

    sb = config.get("stock_budget")
    cb = config.get("crypto_budget")

    counts = {
        "max_short_picks":        _count(sb, 0.4, 12.0, 5, config.get("max_short_picks", 2)),
        "max_long_picks":         _count(sb, 0.6, 15.0, 6, config.get("max_long_picks",  3)),
        "max_crypto_short_picks": _count(cb, 0.5, 10.0, 4, config.get("max_crypto_short_picks", 2)),
        "max_crypto_long_picks":  _count(cb, 0.5, 10.0, 4, config.get("max_crypto_long_picks",  2)),
    }

    # Honor the user's explicit total caps (were written but never consumed).
    # max_stock_picks / max_crypto_picks bound the ST+LT total; trim ST first.
    def _clamp_total(short_key, long_key, cap):
        if not cap:
            return
        cap = int(cap)
        if counts[short_key] + counts[long_key] > cap:
            counts[short_key] = max(0, min(counts[short_key], cap))
            counts[long_key]  = max(0, cap - counts[short_key])

    _clamp_total("max_short_picks", "max_long_picks", config.get("max_stock_picks"))
    _clamp_total("max_crypto_short_picks", "max_crypto_long_picks", config.get("max_crypto_picks"))
    return counts


# ── Signal cache (sentiment + insider, 5-day TTL) ────────────────────────────

SIGNAL_CACHE_TTL_DAYS = 5

# Bump when options_flow or other signal fields change shape so stale entries
# (missing new fields like nearest_otm_call) are automatically discarded.
SIGNAL_CACHE_SCHEMA_VERSION = 2   # v2: nearest_otm_call + nearest_call_expiry added


def load_signal_cache() -> dict:
    """
    Load the signal cache from Gist.
    Cache structure: { "_schema": int, ticker: { "sentiment": {...}, "insider": {...},
                                                  "cached_date": "YYYY-MM-DD" } }
    Returns {} if missing or schema mismatch.
    """
    data = _load_gist_file(SIGNAL_CACHE_FILE) or {}
    if data.get("_schema", 1) != SIGNAL_CACHE_SCHEMA_VERSION:
        print(
            f"[config_manager] Signal cache schema v{data.get('_schema', 1)} != "
            f"current v{SIGNAL_CACHE_SCHEMA_VERSION} — discarding."
        )
        return {}
    return data


def save_signal_cache(cache: dict) -> None:
    """Write signal cache to Gist, stamping the current schema version."""
    cache["_schema"] = SIGNAL_CACHE_SCHEMA_VERSION
    _write_gist_file(SIGNAL_CACHE_FILE, cache)
    # Subtract 1 for the "_schema" sentinel key when reporting ticker count
    ticker_count = max(0, len(cache) - 1)
    print(f"[config_manager] Signal cache saved ({ticker_count} ticker(s), schema v{SIGNAL_CACHE_SCHEMA_VERSION}).")


def get_cached_signal(cache: dict, ticker: str) -> dict | None:
    """
    Return cached signals for a ticker if still within TTL, else None.
    Caller is responsible for providing the loaded cache dict to avoid
    repeated Gist fetches.
    """
    from datetime import date
    if ticker == "_schema":
        return None   # sentinel key — not a ticker entry
    entry = cache.get(ticker)
    if not entry or not isinstance(entry, dict):
        return None
    try:
        cached_date = date.fromisoformat(entry.get("cached_date", ""))
        age_days    = (date.today() - cached_date).days
        if age_days <= SIGNAL_CACHE_TTL_DAYS:
            return entry
    except Exception:
        pass
    return None


def set_cached_signal(cache: dict, ticker: str, sentiment: dict | None,
                      insider: dict | None) -> None:
    """
    Upsert a ticker's signals in the cache dict (in-place).
    Call save_signal_cache(cache) after processing all tickers.
    """
    from datetime import date
    cache[ticker] = {
        "sentiment":   sentiment,
        "insider":     insider,
        "cached_date": date.today().isoformat(),
    }


# ── Screener cache (midnight pre-score, consumed by 8 AM morning run) ────────

SCREENER_CACHE_MAX_AGE_HOURS = 10   # midnight ET → 8 AM ET = 8h; 10h gives buffer

# Bump this integer whenever the screener scoring logic changes materially
# (new signals, changed thresholds, new fields) so stale caches are rejected.
SCREENER_CACHE_SCHEMA_VERSION = 2   # v2: LT quality gate + dynamic sector PE + vol guard


def save_screener_cache(stock_results: dict, crypto_results: dict) -> None:
    """
    Save pre-scored screener candidates from the midnight run.
    Stored as screener_cache.json in the Gist.
    """
    from datetime import datetime, timezone
    payload = {
        "schema_version": SCREENER_CACHE_SCHEMA_VERSION,
        "cached_at":      datetime.now(timezone.utc).isoformat(),
        "stocks":         stock_results,
        "crypto":         crypto_results,
    }
    _write_gist_file(SCREENER_CACHE_FILE, payload)
    print("[config_manager] Screener cache saved to Gist.")


# Why a screener-cache read came back empty. ONLY `CACHE_UNAVAILABLE` is worth
# retrying — every other verdict is computed from data we successfully fetched,
# so a second attempt is guaranteed to reach the same conclusion.
CACHE_HIT         = "hit"
CACHE_UNAVAILABLE = "unavailable"      # could not be fetched — a retry may help
CACHE_STALE       = "stale"            # fetched, older than the TTL
CACHE_SCHEMA      = "schema-mismatch"  # fetched, written by an older screener
CACHE_INVALID     = "invalid"          # fetched, unparseable


def load_screener_cache_verdict() -> tuple[dict | None, str]:
    """Load the midnight screener cache and say WHY if it is unusable.

    🔴 WHY THE REASON EXISTS. `agent._screener_cache_with_retry` retried three
    times with 4 s sleeps on ANY empty result, because a bare `None` could not
    tell "the Gist call failed" from "the cache is 16 hours old". Staleness
    cannot change between attempts, so a genuine miss slept 8 s on the MORNING
    DELIVERY PATH to re-derive a foregone conclusion — visible as the triple
    "too stale" line in any cache-miss log.
    🔎 It was 8 s and log noise, NOT three API calls: `_load_gist_file` memoises
    for `GIST_CACHE_TTL` (20 s) and the retry finishes inside that window, so
    attempts 2-3 were served from memory. Stated precisely because an earlier
    note here said "3 Gist reads", which was wrong.

    ⚠️ `CACHE_UNAVAILABLE` deliberately covers BOTH a transient fetch failure and
    a genuinely absent file. `_load_gist_file` swallows its exception and returns
    None for both, so this layer CANNOT distinguish them — and retrying an absent
    file is cheap and harmless, while not retrying a flaky fetch costs users
    their picks. Do not "tighten" this without first making the storage layer
    report transience.
    """
    from datetime import datetime, timedelta, timezone
    data = _load_gist_file(SCREENER_CACHE_FILE)
    if not data:
        return None, CACHE_UNAVAILABLE
    try:
        # Reject caches written by an older screener version — they may be
        # missing quality-gate fields (avg_dollar_vol, dynamic_sector_pe, etc.)
        cached_version = data.get("schema_version", 1)
        if cached_version != SCREENER_CACHE_SCHEMA_VERSION:
            print(
                f"[config_manager] Screener cache schema v{cached_version} != "
                f"current v{SCREENER_CACHE_SCHEMA_VERSION} — discarding."
            )
            return None, CACHE_SCHEMA

        cached_at = datetime.fromisoformat(data["cached_at"])
        age = datetime.now(timezone.utc) - cached_at
        if age > timedelta(hours=SCREENER_CACHE_MAX_AGE_HOURS):
            print(f"[config_manager] Screener cache is {age} old — too stale, ignoring.")
            return None, CACHE_STALE
        print(f"[config_manager] Screener cache hit — schema v{cached_version}, {age} old.")
        return data, CACHE_HIT
    except Exception as exc:
        print(f"[config_manager] Screener cache invalid ({exc}).")
        return None, CACHE_INVALID


def load_screener_cache() -> dict | None:
    """The midnight screener cache if it is fresh (< 10h) and schema-current.

    Unchanged signature for callers that only need the data (webhook.py,
    cmd_market.py). Anything deciding whether to RETRY must use
    `load_screener_cache_verdict()` instead.
    """
    return load_screener_cache_verdict()[0]


# ── Backtest trades (simulated track record seed) ───────────────────────────

def save_backtest_trades(chat_id: str, trades: list) -> None:
    """Persist simulated backtest trades for a user (used to seed Performance tab)."""
    mutate_user_record(BACKTEST_TRADES_FILE, chat_id,
                       lambda _rec: (trades, None), default=[])
    print(f"[config_manager] Saved {len(trades)} backtest trades for {chat_id}.")


def load_backtest_trades(chat_id: str) -> list:
    """Load simulated backtest trades for a user. Returns [] if none seeded."""
    all_data = _load_gist_file(BACKTEST_TRADES_FILE) or {}
    return all_data.get(str(chat_id), [])


# ── Buy counts (social feature, admin-gated) ─────────────────────────────────

def load_buy_counts() -> dict:
    """
    Load today's buy counts.
    Structure: {"date": "YYYY-MM-DD", "counts": {"NVDA": 3, "BTC": 2}}
    Auto-resets when the date changes — counts are always today-only.
    Returns empty counts dict if missing or stale.
    """
    # 🔴 MUST match increment_buy_count's clock. These were split on 2026-08-11
    # (writer moved to et_today, reader left on date.today) and on Render — where
    # local time IS UTC — the two disagree from 00:00-04:00 UTC every night, so
    # counts read as EMPTY for 4-5 hours daily. Same save/load-pair rule that
    # already exists for the screener and macro caches.
    today = et_today().isoformat()
    data  = _load_gist_file(BUY_COUNTS_FILE) or {}
    if data.get("date") != today:
        return {}   # new day — treat as empty (don't wipe Gist yet, lazy reset)
    return data.get("counts", {})


def increment_buy_count(ticker: str) -> int:
    """
    Increment the buy count for a ticker today.
    Creates or resets the file if it's a new day.
    Returns the new count.
    """
    # et_today(), not date.today(): this is a per-day counter with a reset
    # boundary, and a bare UTC date rolls over at 7-8 PM ET on Render — so the
    # day's counts were being wiped mid-evening while users were still trading,
    # and an evening buy started a "new day". Same class as the documented
    # dedup-key bug; every date stamp with a rollover belongs on the ET clock.
    today = et_today().isoformat()

    # Increment against a FRESH read — the old load→write pattern lost counts
    # whenever two users bought the same ticker at once.
    def _mut(data):
        data = data or {}
        if data.get("date") != today:
            data = {"date": today, "counts": {}}   # new day — reset counts
        data.setdefault("counts", {})
        data["counts"][ticker] = data["counts"].get(ticker, 0) + 1
        return data, data["counts"][ticker]

    count = mutate_gist_file(BUY_COUNTS_FILE, _mut, default={})
    print(f"[config_manager] Buy count for {ticker}: {count}")
    return count


# ── Macro cache (Fear & Greed + FRED rates, shared across all users) ─────────
# Fetched ONCE per morning run, cached in Gist with 4h TTL.
# User count never affects fetch volume — all users read from same cached data.

MACRO_CACHE_TTL_HOURS = 4


def load_macro_cache() -> dict | None:
    """
    Load macro signals cache from Gist.
    Returns None if missing or older than MACRO_CACHE_TTL_HOURS.
    Structure: { "cached_at": ISO str, "fear_greed": {...}, "rate_trend": {...} }
    """
    from datetime import datetime, timedelta, timezone
    data = _load_gist_file(MACRO_CACHE_FILE)
    if not data:
        return None
    try:
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now(timezone.utc) - cached_at > timedelta(hours=MACRO_CACHE_TTL_HOURS):
            print(f"[config_manager] Macro cache expired ({datetime.now(timezone.utc) - cached_at} old).")
            return None
        return data
    except Exception as exc:
        print(f"[config_manager] Macro cache invalid ({exc}).")
        return None


def save_macro_cache(
    fear_greed: dict | None,
    rate_trend: dict | None,
    sector_rotation: dict | None = None,
) -> None:
    """Save macro signals to Gist with current UTC timestamp."""
    from datetime import datetime, timezone
    payload = {
        "cached_at":       datetime.now(timezone.utc).isoformat(),
        "fear_greed":      fear_greed,
        "rate_trend":      rate_trend,
        "sector_rotation": sector_rotation,
    }
    try:
        _write_gist_file(MACRO_CACHE_FILE, payload)
        print("[config_manager] Macro cache saved.")
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not save macro cache ({exc}).")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _write_config(config: dict) -> None:
    """Write config dict directly to Gist as config.json via GitHub API.
    Must use the same path as get_config() reads from — NOT the storage backend
    (Supabase), which get_config() never reads."""
    _cache_invalidate(GIST_FILENAME)
    url = f"https://api.github.com/gists/{_gist_id()}"
    payload = {"files": {GIST_FILENAME: {"content": json.dumps(config, indent=2)}}}
    try:
        resp = requests.patch(url, headers=_gist_headers(), json=payload, timeout=10)
        resp.raise_for_status()
        _cache_set(GIST_FILENAME, config)
        print(f"[config_manager] Config updated: {config}")
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not write Gist config ({exc}).")


def _load_gist_file(filename: str) -> dict | None:
    """
    Fetch and parse a JSON file directly from Gist via GitHub API.
    Checks in-memory cache first — saves a round-trip on cache hits.
    Returns None on any error.

    Reads directly from GistBackend (not get_storage_backend()) because
    Supabase reads consistently return None on this instance, and the silent
    fallback was masking the failure. Symmetric with _write_gist_file.
    Same fix applied to _write_config and _write_gist_file (Jun 08-09).
    """
    cached = _cache_get(filename)
    if cached is not None:
        return cached
    try:
        from storage import get_storage_backend as get_backend   # Gist unless SUPABASE_* is set
        backend = get_backend()
        # On a row backend the per-user files live as one row PER USER, so
        # reassemble the {chat_id: record} mapping every caller still expects.
        if backend.supports_rows() and filename in USER_KEYED_FILES:
            result = backend.read_all_users(filename)
        else:
            result = backend.read(filename)
        if result is not None:
            _cache_set(filename, result)
        return result
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not load {filename} ({exc}).")
        return None


def _write_gist_file(filename: str, data: dict) -> None:
    """
    Write any dict as a JSON blob directly to Gist via GitHub API.
    Always invalidates the in-memory cache before writing.

    Must write to Gist directly (not get_storage_backend()) because
    _load_gist_file falls back to GistBackend on Supabase read failures —
    data written to Supabase is never found by the fallback read path.
    Same fix applied to _write_config (Jun 08).
    """
    _cache_invalidate(filename)   # invalidate before write so stale data is never served
    from storage import get_storage_backend as get_backend   # Gist unless SUPABASE_* is set
    get_backend().write(filename, data)


# ── Safe per-user write (concurrency + clobber protection) ───────────────────
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(filename: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        lk = _FILE_LOCKS.get(filename)
        if lk is None:
            lk = _FILE_LOCKS[filename] = threading.Lock()
        return lk


# ── Read-after-write lag guard (cross-user lost update) ───────────────────────
# The lock serializes our own writes, and read_strict RAISES on a fetch error, but
# neither protects against GitHub's read-after-write LAG: a fresh GET moments after
# a PATCH can still return the PRE-write copy. Writing user A then user B then
# re-read that stale copy for B's merge and wrote it back — silently erasing A.
# (Observed live: tagging the owner's 34 trades then the test account's 4 lost the
# owner's entirely.) Fix: remember what we just wrote per (file, uid) and re-apply
# any echo the server hasn't shown us yet. Costs no extra network calls.
_recent_user_writes: dict[str, dict[str, tuple]] = {}
_WRITE_ECHO_TTL = 30   # s — gist lag is normally <5s; keep the window tight so a
                       # genuinely newer write from another process isn't stomped.


def _record_write(filename: str, uid: str, value) -> None:
    _recent_user_writes.setdefault(filename, {})[uid] = (value, time.time())


def _reapply_recent_writes(filename: str, all_data: dict, skip_uid: str | None = None) -> None:
    """Re-apply our own very recent writes that the fresh read is missing."""
    echoes = _recent_user_writes.get(filename)
    if not echoes:
        return
    now = time.time()
    for uid, (value, ts) in list(echoes.items()):
        if now - ts > _WRITE_ECHO_TTL:
            echoes.pop(uid, None)                 # expired — server has caught up
            continue
        if uid == skip_uid:
            continue
        if all_data.get(uid) != value:            # server copy hasn't caught up yet
            all_data[uid] = value
            print(f"[config] read-after-write lag: re-applied {filename}[{uid}]")


def _update_user_keyed_file(filename: str, chat_id: str, value) -> None:
    """
    Safely set all_data[chat_id] = value in a chat_id-keyed multi-user file.

    Two protections that the old `load() or {} ; all[id]=v ; write()` lacked:
    1. Re-reads the file FRESH under a per-file lock and merges only this user's
       slot — so a concurrent save for a DIFFERENT user can't clobber it (with
       gunicorn at --workers 1, the lock serializes all web-side mutations).
    2. Uses read_strict, which RAISES on a fetch error — so a transient Gist
       failure aborts the write instead of persisting `{}` and erasing everyone.
    (A same-user concurrent mutation across processes is still last-writer-wins —
    the full fix is etag/If-Match or a row store; this kills the data-loss cases.)
    """
    from storage import get_storage_backend as get_backend   # Gist unless SUPABASE_* is set
    uid = str(chat_id)
    backend = get_backend()

    # Row-per-user backend: write ONLY this user's row. No whole-file rewrite, so
    # a concurrent write for a DIFFERENT user cannot be clobbered, and there is no
    # read-after-write lag to produce a stale merge base.
    if backend.supports_rows():
        _, version = backend.read_user(filename, uid)
        backend.write_user(filename, uid, value, version)
        _cache_invalidate(filename)
        return

    with _lock_for(filename):
        current  = backend.read_strict(filename)   # raises on fetch error → no clobber
        all_data = current if isinstance(current, dict) else {}
        _reapply_recent_writes(filename, all_data, skip_uid=uid)
        all_data[uid] = value
        _cache_invalidate(filename)
        backend.write(filename, all_data)
        _cache_set(filename, all_data)   # keep cache coherent with what we just wrote
        _record_write(filename, uid, value)


#: Returned by a mutator INSTEAD of a record to mean "nothing changed, don't write".
#: Reject paths (insufficient paper cash, position not found) must not burn a
#: storage write — on the Gist a wasted PATCH counts against GitHub's secondary
#: rate limit, which is what wiped data during the full-sweep incident.
NO_WRITE = object()


def mutate_user_record(filename: str, chat_id: str, mutator, default=None):
    """Read-modify-write ONE user's record with the read INSIDE the lock.

    Fixes the same-user CROSS-PROCESS race. The racy pattern is:

        log = load_user_trade_log(uid)      # ← process B writes here …
        log["open"].append(trade)
        save_user_trade_log(uid, log)       # ← … and this silently clobbers it

    Between the caller's load and save (often seconds — price fetches, LLM calls)
    another PROCESS can write the same user, and the save wipes it. GitHub's Gist
    API has NO compare-and-swap (verified: PATCH rejects If-Match with 400), so
    the fix is to shrink the window instead of locking: `mutator(record)` runs on
    a FRESH read taken microseconds before the write, not on a stale snapshot the
    caller took earlier.

    mutator(record) -> (new_record, result). `result` is returned to the caller.

    NOTE: this shrinks the race window from seconds to milliseconds; it cannot
    eliminate it without server-side CAS or row-level storage.
    """
    from storage import get_storage_backend as get_backend   # Gist unless SUPABASE_* is set
    uid = str(chat_id)
    backend = get_backend()

    # Row backend: TRUE compare-and-swap. write_user returns None when another
    # writer bumped the version first — we then re-run the mutator against the
    # fresh record instead of clobbering it. This is what the Gist could never
    # do (its PATCH rejects If-Match), and it closes the same-user cross-process
    # race rather than merely narrowing it.
    if backend.supports_rows():
        for attempt in range(1, 6):
            record, version = backend.read_user(filename, uid)
            if record is None:
                record = {} if default is None else default
            new_record, result = mutator(record)
            if new_record is NO_WRITE:
                return result
            if backend.write_user(filename, uid, new_record, version) is not None:
                _cache_invalidate(filename)
                return result
            print(f"[config] CAS conflict on {filename}[{uid}] — retry {attempt}")
        raise RuntimeError(f"mutate_user_record: CAS kept failing for {filename}[{uid}]")

    with _lock_for(filename):
        current  = backend.read_strict(filename)   # raises on fetch error → no clobber
        all_data = current if isinstance(current, dict) else {}
        _reapply_recent_writes(filename, all_data, skip_uid=uid)
        record = all_data.get(uid)
        if record is None:
            record = {} if default is None else default
        new_record, result = mutator(record)
        if new_record is NO_WRITE:
            return result
        all_data[uid] = new_record
        _cache_invalidate(filename)
        backend.write(filename, all_data)
        _cache_set(filename, all_data)
        _record_write(filename, uid, new_record)
        return result


def mutate_user_trade_log(chat_id: str, mutator):
    """mutate_user_record for the trade log — the highest-collision file
    (web 'I Bought This' vs a scheduled job closing a trade for the same user)."""
    return mutate_user_record(USER_TRADES_FILE, chat_id, mutator,
                              default={"open": [], "closed": [], "watchlist": []})


def mutate_gist_file(filename: str, mutator, default=None):
    """
    Atomically read-modify-write a whole Gist file under a per-file lock.

    `mutator(current)` receives the FRESH file contents (or `default` if the
    file doesn't exist) and returns (new_contents, result). The new contents are
    written and `result` is returned to the caller. Read failures RAISE (no
    clobber). Use for shared multi-user files (e.g. price_alerts) where the whole
    blob is rewritten so a naive load→mutate→save loses concurrent edits.

    A mutator may return NO_WRITE instead of new contents to mean "nothing
    changed" — the write is skipped and `result` is still returned.
    """
    from storage import get_storage_backend as get_backend   # Gist unless SUPABASE_* is set
    backend = get_backend()

    # A user-keyed file lives as ROWS on a row backend. Reading it back via
    # backend.read()/write() would hit the whole-blob `documents` table instead —
    # a different place than _load_gist_file reads from — so alerts written here
    # would be invisible to every reader. Reassemble from rows, let the mutator
    # work on the familiar {chat_id: value} mapping, then persist only the rows
    # it actually changed.
    if backend.supports_rows() and filename in USER_KEYED_FILES:
        with _lock_for(filename):
            import copy as _copy
            before  = backend.read_all_users(filename) or {}
            # 🔴 DEEP copy, not dict(). A shallow copy SHARES the nested lists,
            # so a mutator that appends in place — which add_alert does via
            # `alerts.setdefault(uid, []).append(entry)` — mutates the very
            # object `before` holds. The `before.get(uid) != val` guard below
            # then compares the list to ITSELF, sees no change, and SKIPS the
            # write. Measured 2026-08-22: every alert for a user who already had
            # a row vanished silently, with no error anywhere.
            current = _copy.deepcopy(before)
            new_contents, result = mutator(current)
            if new_contents is NO_WRITE:
                return result
            new_contents = new_contents if isinstance(new_contents, dict) else {}
            for uid, val in new_contents.items():
                if before.get(uid) != val:
                    _, ver = backend.read_user(filename, str(uid))
                    backend.write_user(filename, str(uid), val, ver)
            for uid in before:
                if uid not in new_contents:          # emptied, not deleted — same
                    _, ver = backend.read_user(filename, str(uid))
                    backend.write_user(filename, str(uid), [], ver)
            _cache_invalidate(filename)
            return result

    with _lock_for(filename):
        current = backend.read_strict(filename)
        if current is None:
            current = {} if default is None else default
        new_contents, result = mutator(current)
        if new_contents is NO_WRITE:
            return result
        _cache_invalidate(filename)
        backend.write(filename, new_contents)
        _cache_set(filename, new_contents)
        return result


# ── Pending conversation state (multi-step commands) ─────────────────────────

def load_pending_state(chat_id: str) -> dict | None:
    """
    Load pending conversation state for a chat_id.
    Returns None if not found or expired (5-minute TTL).
    Stored in-memory only — no Gist round-trip needed.
    """
    import time
    state = _PENDING_STATE_CACHE.get(str(chat_id))
    if not state:
        return None
    if time.time() > state.get("_expires_at", 0):
        del _PENDING_STATE_CACHE[str(chat_id)]
        return None
    return state


def save_pending_state(chat_id: str, command: str,
                       step: int = 1, data: dict | None = None) -> None:
    """Save pending state for a chat_id in-memory with a 5-minute expiry."""
    import time
    _PENDING_STATE_CACHE[str(chat_id)] = {
        "command":    command,
        "step":       step,
        "data":       data or {},
        "_expires_at": time.time() + 300,   # 5 minutes
    }


def clear_pending_state(chat_id: str) -> None:
    """Remove pending state for a chat_id."""
    _PENDING_STATE_CACHE.pop(str(chat_id), None)


# ── User feedback ────────────────────────────────────────────────────────────

def load_feedback() -> list:
    """Return list of feedback entries, newest first."""
    data = _load_gist_file(FEEDBACK_FILE)
    if not data:
        return []
    return data.get("entries", [])


def add_feedback(chat_id: str, text: str, username: str = "", first_name: str = "",
                 platform: str = "", page: str = "") -> None:
    """Append a new feedback entry with user + app context for admin triage."""
    from datetime import datetime, timezone
    data = _load_gist_file(FEEDBACK_FILE) or {"entries": []}
    data.setdefault("entries", [])
    data["entries"].insert(0, {
        "chat_id":      str(chat_id),
        "first_name":   first_name,
        "username":     username,
        "text":         text,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "read":         False,
        "platform":     platform,   # e.g. "ios", "android", "tdesktop"
        "page":         page,       # mini app tab active when feedback was sent
    })
    # Keep last 200 entries
    data["entries"] = data["entries"][:200]
    _write_gist_file(FEEDBACK_FILE, data)


def mark_feedback_read() -> None:
    """Mark all feedback entries as read."""
    data = _load_gist_file(FEEDBACK_FILE) or {"entries": []}
    for entry in data.get("entries", []):
        entry["read"] = True
    _write_gist_file(FEEDBACK_FILE, data)


def count_unread_feedback() -> int:
    """Return number of unread feedback entries."""
    entries = load_feedback()
    return sum(1 for e in entries if not e.get("read", False))


# ── User activity log ────────────────────────────────────────────────────────

def log_user_event(chat_id: str, event_type: str, detail: str, level: str = "info") -> None:
    """
    Append a structured event to the user's rolling activity log.
    Buffered in-memory and flushed to Gist every _USER_LOG_FLUSH_AT events.
    Levels: "info", "warn", "error"
    Types: "command", "error", "alert", "position", "settings", "system"
    """
    from datetime import datetime, timezone
    entry = {
        "ts":     datetime.now(timezone.utc).isoformat(),
        "level":  level,
        "type":   event_type,
        "detail": str(detail)[:300],
    }
    buf = _user_event_buffer.setdefault(str(chat_id), [])
    buf.append(entry)
    if len(buf) >= _USER_LOG_FLUSH_AT:
        _flush_user_logs(str(chat_id))


def _flush_user_logs(chat_id: str) -> None:
    """Merge buffer into Gist and clear buffer. Called automatically or by admin read."""
    buf = _user_event_buffer.get(str(chat_id), [])
    if not buf:
        return
    filename = f"user_log_{chat_id}.json"
    try:
        existing = _load_gist_file(filename) or []
        if not isinstance(existing, list):
            existing = []
        merged = existing + buf
        if len(merged) > _USER_LOG_MAX:
            merged = merged[-_USER_LOG_MAX:]
        _write_gist_file(filename, merged)
        _user_event_buffer[str(chat_id)] = []
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not flush logs for {chat_id}: {exc}")


def get_user_logs(chat_id: str, limit: int = 100) -> list:
    """
    Return the last N log events for a user, newest first.
    Merges persisted Gist data with any un-flushed in-memory buffer.
    """
    # Flush first so we get the freshest data
    _flush_user_logs(str(chat_id))
    filename = f"user_log_{chat_id}.json"
    logs = _load_gist_file(filename) or []
    if not isinstance(logs, list):
        return []
    return list(reversed(logs[-limit:]))


# ── Banned users ──────────────────────────────────────────────────────────────

def get_banned_users() -> list:
    """Return list of banned chat_ids."""
    config = get_config()
    return [str(u) for u in config.get("banned_users", [])]


def ban_user(chat_id: str) -> None:
    """Ban a user: remove from allowlist and add to banned list."""
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    if str(chat_id) == str(owner):
        raise ValueError("Cannot ban the bot owner.")
    config = get_config()
    banned = [str(u) for u in config.get("banned_users", [])]
    if str(chat_id) not in banned:
        banned.append(str(chat_id))
        update_config("banned_users", banned)
    try:
        remove_allowed_user(chat_id)
    except Exception:
        pass
    log_user_event(chat_id, "system", "User banned by admin", level="warn")


def unban_user(chat_id: str) -> None:
    """Remove a user from the banned list (does not re-approve, use add_allowed_user for that)."""
    config = get_config()
    banned = [str(u) for u in config.get("banned_users", []) if str(u) != str(chat_id)]
    update_config("banned_users", banned)


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pprint
    print("Current config:")
    pprint.pprint(get_config())
