"""
config_manager.py — Read/write agent config from a GitHub Gist (JSON store).
Falls back to hardcoded defaults if the Gist is unreachable.
"""

import os
import json
import time
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
    # Notification opt-outs (all default on)
    "skip_confirmation":     False,    # True = skip 10:30 AM confirmation message
    "skip_eod":              False,    # True = skip 3:30 PM EOD portfolio summary
    "skip_watchlist_alerts": False,    # True = skip watchlist RSI/MACD signal alerts
}

GIST_FILENAME          = "config.json"
PICKS_FILENAME         = "picks.json"           # Stores morning picks for 10:30 AM confirmation
WEEKLY_PICKS_FILENAME  = "weekly_picks.json"    # Accumulates Mon–Fri picks for Saturday recap
PENDING_STATE_FILENAME = "pending_state.json"   # Conversation state for multi-step commands
PENDING_USERS_FILE     = "pending_users.json"   # Users awaiting admin approval
PRICE_ALERTS_FILE      = "price_alerts.json"    # User price alerts (already per-user by chat_id)
SIGNAL_CACHE_FILE      = "signal_cache.json"    # Cached sentiment + insider signals (5-day TTL)
SCREENER_CACHE_FILE    = "screener_cache.json"  # Pre-scored candidates from midnight run
BUY_COUNTS_FILE        = "buy_counts.json"      # Social: how many members bought each pick today
# ── Per-user data files (keyed by chat_id inside the JSON) ───────────────────
USER_CONFIGS_FILE      = "user_configs.json"    # Per-user settings (risk, watchlist, budget…)
USER_TRADES_FILE       = "user_trades.json"     # Per-user trade logs (open + closed)
USER_PAPER_FILE        = "user_paper.json"      # Per-user paper portfolios
FEEDBACK_FILE          = "feedback.json"        # User feedback submissions


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
    """Update a single key for a user. Returns updated user config."""
    all_configs = _load_gist_file(USER_CONFIGS_FILE) or {}
    uid = str(chat_id)
    if uid not in all_configs:
        all_configs[uid] = {}
    all_configs[uid][key] = value
    _write_gist_file(USER_CONFIGS_FILE, all_configs)
    return get_user_config(uid)


def update_user_config_multi(chat_id: str, updates: dict) -> dict:
    """Update multiple keys for a user at once. Returns updated user config."""
    all_configs = _load_gist_file(USER_CONFIGS_FILE) or {}
    uid = str(chat_id)
    if uid not in all_configs:
        all_configs[uid] = {}
    all_configs[uid].update(updates)
    _write_gist_file(USER_CONFIGS_FILE, all_configs)
    return get_user_config(uid)


def reset_user_config(chat_id: str) -> dict:
    """Reset a user's config to DEFAULT_USER_CONFIG."""
    all_configs = _load_gist_file(USER_CONFIGS_FILE) or {}
    all_configs[str(chat_id)] = dict(DEFAULT_USER_CONFIG)
    _write_gist_file(USER_CONFIGS_FILE, all_configs)
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
    """Save trade log for a specific user."""
    all_logs = _load_gist_file(USER_TRADES_FILE) or {}
    all_logs[str(chat_id)] = log
    _write_gist_file(USER_TRADES_FILE, all_logs)


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
    """Save paper portfolio for a specific user."""
    all_paper = _load_gist_file(USER_PAPER_FILE) or {}
    all_paper[str(chat_id)] = data
    _write_gist_file(USER_PAPER_FILE, all_paper)


# ── Multi-user allowlist helpers ─────────────────────────────────────────────

def get_allowed_users() -> list[str]:
    """Return list of allowed chat_ids. Always includes TELEGRAM_CHAT_ID (owner)."""
    config = get_config()
    users  = [str(u) for u in config.get("allowed_users", [])]
    owner  = os.environ.get("TELEGRAM_CHAT_ID", "")
    if owner and owner not in users:
        users.insert(0, owner)
    return users


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
    """Return pending users dict: { chat_id: {first_name, username, requested_at} }"""
    return _load_gist_file(PENDING_USERS_FILE) or {}


def add_pending_user(chat_id: str, first_name: str = "", username: str = "") -> None:
    """Add a user to the pending approval list."""
    from datetime import datetime
    pending = get_pending_users()
    pending[str(chat_id)] = {
        "first_name":    first_name,
        "username":      username,
        "requested_at":  datetime.utcnow().isoformat(),
    }
    _write_gist_file(PENDING_USERS_FILE, pending)


def remove_pending_user(chat_id: str) -> None:
    """Remove a user from the pending list (after approval or rejection)."""
    pending = get_pending_users()
    if str(chat_id) in pending:
        del pending[str(chat_id)]
        _write_gist_file(PENDING_USERS_FILE, pending)


# ── Picks storage (for 10:30 AM confirmation run) ────────────────────────────

def save_picks(picks: dict) -> None:
    """Save morning picks to Gist as picks.json for the confirmation run."""
    from datetime import date
    picks["_saved_date"] = date.today().isoformat()
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
    """Load today's morning picks from Gist. Returns None if not found or stale."""
    from datetime import date
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
        # Only return picks saved today
        if picks.get("_saved_date") != date.today().isoformat():
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

    return {
        "max_short_picks":        _count(sb, 0.4, 12.0, 5, config.get("max_short_picks", 2)),
        "max_long_picks":         _count(sb, 0.6, 15.0, 6, config.get("max_long_picks",  3)),
        "max_crypto_short_picks": _count(cb, 0.5, 10.0, 4, config.get("max_crypto_short_picks", 2)),
        "max_crypto_long_picks":  _count(cb, 0.5, 10.0, 4, config.get("max_crypto_long_picks",  2)),
    }


# ── Signal cache (sentiment + insider, 5-day TTL) ────────────────────────────

SIGNAL_CACHE_TTL_DAYS = 5


def load_signal_cache() -> dict:
    """
    Load the signal cache from Gist.
    Cache structure: { ticker: { "sentiment": {...}, "insider": {...}, "cached_date": "YYYY-MM-DD" } }
    Returns {} if missing.
    """
    return _load_gist_file(SIGNAL_CACHE_FILE) or {}


def save_signal_cache(cache: dict) -> None:
    """Write signal cache to Gist."""
    _write_gist_file(SIGNAL_CACHE_FILE, cache)
    print(f"[config_manager] Signal cache saved ({len(cache)} ticker(s)).")


def get_cached_signal(cache: dict, ticker: str) -> dict | None:
    """
    Return cached signals for a ticker if still within TTL, else None.
    Caller is responsible for providing the loaded cache dict to avoid
    repeated Gist fetches.
    """
    from datetime import date
    entry = cache.get(ticker)
    if not entry:
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


def save_screener_cache(stock_results: dict, crypto_results: dict) -> None:
    """
    Save pre-scored screener candidates from the midnight run.
    Stored as screener_cache.json in the Gist.
    """
    from datetime import datetime
    payload = {
        "cached_at":  datetime.utcnow().isoformat(),
        "stocks":     stock_results,
        "crypto":     crypto_results,
    }
    _write_gist_file(SCREENER_CACHE_FILE, payload)
    print("[config_manager] Screener cache saved to Gist.")


def load_screener_cache() -> dict | None:
    """
    Load the midnight screener cache if it exists and is fresh (< 10h old).
    Returns the cache dict {cached_at, stocks, crypto} or None if stale/missing.
    """
    from datetime import datetime, timedelta
    data = _load_gist_file(SCREENER_CACHE_FILE)
    if not data:
        return None
    try:
        cached_at = datetime.fromisoformat(data["cached_at"])
        age = datetime.utcnow() - cached_at
        if age > timedelta(hours=SCREENER_CACHE_MAX_AGE_HOURS):
            print(f"[config_manager] Screener cache is {age} old — too stale, ignoring.")
            return None
        print(f"[config_manager] Screener cache hit — {age} old.")
        return data
    except Exception as exc:
        print(f"[config_manager] Screener cache invalid ({exc}).")
        return None


# ── Buy counts (social feature, admin-gated) ─────────────────────────────────

def load_buy_counts() -> dict:
    """
    Load today's buy counts.
    Structure: {"date": "YYYY-MM-DD", "counts": {"NVDA": 3, "BTC": 2}}
    Auto-resets when the date changes — counts are always today-only.
    Returns empty counts dict if missing or stale.
    """
    from datetime import date
    today = date.today().isoformat()
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
    from datetime import date
    today = date.today().isoformat()
    data  = _load_gist_file(BUY_COUNTS_FILE) or {}

    if data.get("date") != today:
        data = {"date": today, "counts": {}}   # new day — reset counts

    data["counts"][ticker] = data["counts"].get(ticker, 0) + 1
    _write_gist_file(BUY_COUNTS_FILE, data)
    print(f"[config_manager] Buy count for {ticker}: {data['counts'][ticker]}")
    return data["counts"][ticker]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _write_config(config: dict) -> None:
    """Write config dict to the Gist as config.json (also invalidates cache)."""
    _write_gist_file(GIST_FILENAME, config)   # _write_gist_file already invalidates
    print(f"[config_manager] Config updated: {config}")


def _load_gist_file(filename: str) -> dict | None:
    """
    Fetch and parse a JSON file from the Gist.
    Checks in-memory cache first — saves a GitHub round-trip on cache hits.
    Returns None on any error.
    """
    cached = _cache_get(filename)
    if cached is not None:
        return cached
    try:
        url  = f"https://api.github.com/gists/{_gist_id()}"
        resp = requests.get(url, headers=_gist_headers(), timeout=10)
        resp.raise_for_status()
        files = resp.json().get("files", {})
        if filename not in files:
            return None
        result = json.loads(files[filename]["content"])
        _cache_set(filename, result)
        return result
    except Exception as exc:
        print(f"[config_manager] WARNING: Could not load {filename} ({exc}).")
        return None


def _write_gist_file(filename: str, data: dict) -> None:
    """Write any dict as a JSON file to the Gist. Always invalidates the cache."""
    _cache_invalidate(filename)   # invalidate before write so stale data is never served
    url     = f"https://api.github.com/gists/{_gist_id()}"
    payload = {"files": {filename: {"content": json.dumps(data, indent=2)}}}
    resp    = requests.patch(url, headers=_gist_headers(), json=payload, timeout=10)
    resp.raise_for_status()


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


def add_feedback(chat_id: str, text: str, username: str = "", first_name: str = "") -> None:
    """Append a new feedback entry."""
    from datetime import datetime
    data = _load_gist_file(FEEDBACK_FILE) or {"entries": []}
    data.setdefault("entries", [])
    data["entries"].insert(0, {
        "chat_id":    str(chat_id),
        "first_name": first_name,
        "username":   username,
        "text":       text,
        "submitted_at": datetime.utcnow().isoformat(),
        "read":       False,
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


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pprint
    print("Current config:")
    pprint.pprint(get_config())
