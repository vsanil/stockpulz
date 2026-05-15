"""
cmd_settings.py — Settings panel + settings commands extracted from bot_commands.py.
"""

import os

from telegram_api import send_message, send_inline_keyboard
from config_manager import (
    get_user_config, update_user_config, update_user_config_multi, reset_user_config,
    get_config, load_picks, save_pending_state, clear_pending_state,
)
from formatters import _esc, format_daily_message


# Budget buckets: callback key → (label, stock_budget, crypto_budget)
_BUDGET_BUCKETS = {
    "lt500":    ("Under $500",   200,  50),
    "mid1k":    ("$500–$2K",     800, 200),
    "mid3k":    ("$2K–$5K",    2000, 500),
    "gt5k":     ("Over $5K",   5000, 1000),
}

# Risk options: callback key → (label, risk_profile value)
_RISK_OPTIONS = {
    "conservative": ("🛡 Conservative", "conservative"),
    "moderate":     ("⚖️ Moderate",     "moderate"),
    "aggressive":   ("🔥 Aggressive",   "aggressive"),
}

# Asset options: callback key → (label, show_crypto, show_etfs)
_ASSET_OPTIONS = {
    "stocks":         ("📈 Stocks only",          False, False),
    "stockscrypto":   ("📈+₿ Stocks + Crypto",    True,  False),
    "all":            ("🌐 Everything (+ ETFs)",   True,  True),
}


def _start_onboarding_wizard(chat_id: str) -> None:
    """Send the first wizard step (budget question) to a newly approved user."""
    send_message(
        "👋 <b>Welcome to StockPulz!</b>\n\n"
        "You're in. Let's set you up in 3 quick questions so your picks are tailored to you.\n\n"
        "<b>Question 1 of 3 — What's your rough budget per trade?</b>\n"
        "<i>(This determines how many shares/coins you'd allocate per pick.)</i>",
        chat_id=chat_id,
    )
    send_inline_keyboard(
        "",
        [[
            {"text": label, "callback_data": f"onboard_budget_{key}"}
            for key, (label, _, __) in _BUDGET_BUCKETS.items()
        ]],
        chat_id=chat_id,
    )


def _send_onboarding_complete(chat_id: str) -> None:
    """
    Send the final onboarding card: settings summary, daily rhythm,
    today's picks (if available), and the /bought nudge.
    """
    user_cfg  = get_user_config(chat_id)
    risk      = user_cfg.get("risk_profile", "moderate").capitalize()
    s_budget  = user_cfg.get("stock_budget", 200)
    c_budget  = user_cfg.get("crypto_budget", 50)
    crypto_on = user_cfg.get("show_crypto", True)
    etfs_on   = user_cfg.get("show_etfs", False)
    assets    = "Stocks + Crypto" if crypto_on and not etfs_on else ("Everything" if etfs_on else "Stocks only")

    send_message(
        f"✅ <b>You're all set!</b>\n\n"
        f"<b>Your settings:</b>\n"
        f"  • Risk: <b>{risk}</b>\n"
        f"  • Per-trade budget: <b>${s_budget} stocks</b>"
        + (f" · <b>${c_budget} crypto</b>" if crypto_on else "") +
        f"\n  • Assets: <b>{assets}</b>\n\n"
        f"You can change any of these anytime with /settings.\n\n"
        "─────────────────────\n"
        "<b>📅 Your daily schedule:</b>\n"
        "📬 <b>8:00 AM ET</b> — AI-curated picks with entry, target &amp; stop\n"
        "🕙 <b>10:30 AM ET</b> — Confirmation: enter, wait, or exit signal\n"
        "🔔 <b>Every 30 min</b> — Price alerts if a stop or target is hit\n"
        "📊 <b>4:15 PM ET</b> — End-of-day wrap: how picks moved\n"
        "📅 <b>Saturday</b> — Crypto picks + weekly P&amp;L recap\n"
        "🗓 <b>Sunday</b> — Week-ahead briefing (earnings, macro)\n\n"
        "─────────────────────\n"
        "⭐ <b>One thing that makes this powerful:</b>\n"
        "When you place a real trade, send <code>/bought TICKER</code> — e.g. <code>/bought NVDA</code>.\n"
        "That's how your win rate, expectancy, and P&amp;L get tracked in /stats. "
        "The more you log, the smarter your performance dashboard gets.\n\n"
        "📖 Type /guide anytime for a quick reference card.",
        chat_id=chat_id,
    )

    # Show today's picks if available
    picks = load_picks()
    if picks:
        try:
            global_cfg = get_config()
            full_cfg   = {**global_cfg, **user_cfg}
            picks_msg  = format_daily_message(picks, full_cfg)
            send_message(
                "Here are <b>today's picks</b> to get you started 👇\n\n" + picks_msg,
                chat_id=chat_id,
            )
        except Exception as exc:
            print(f"[onboarding] Could not send today's picks to {chat_id}: {exc}")


# ── Prompts for param commands ────────────────────────────────────────────────

_PARAM_PROMPTS: dict[str, str] = {
    "bought":   ("🛒 <b>What did you buy?</b>\n"
                 "<i>e.g.</i>  <code>apple</code>  ·  <code>AAPL 182.50</code>  ·  <code>AAPL 182.50 5</code>"),
    "sold":     ("💸 <b>What did you sell?</b>\n"
                 "<i>e.g.</i>  <code>apple</code>  ·  <code>AAPL 197.10</code>"),
    "cancel":   ("↩️ <b>Which trade to undo?</b>\n"
                 "<i>e.g.</i>  <code>apple</code>  ·  <code>AAPL</code>"),
    "explain":  ("💬 <b>What would you like to know?</b>\n"
                 "<i>e.g.</i>  <code>why is NVDA picked?</code>  ·  <code>apple thesis</code>"),
    "watch":    ("👀 <b>Which tickers to watch?</b>\n"
                 "<i>e.g.</i>  <code>NVDA TSLA</code>  ·  <code>nvidia tesla</code>"),
    "exclude":  ("🚫 <b>Which sector to exclude?</b>\n"
                 "<i>e.g.</i>  <code>energy</code>  ·  <code>oil stocks</code>"),
    "set_risk": ("⚖️ <b>Risk level?</b>\n"
                 "<code>conservative</code>   ·   <code>moderate</code>   ·   <code>aggressive</code>"),
    "set_st":    "💰 <b>Stock short-term budget per trade?</b>  <i>e.g.</i>  <code>30</code>",
    "set_lt":    "💰 <b>Stock long-term monthly budget?</b>  <i>e.g.</i>  <code>50</code>",
    "set_cst":   "💰 <b>Crypto short-term budget per trade?</b>  <i>e.g.</i>  <code>20</code>",
    "set_clt":   "💰 <b>Crypto long-term monthly budget?</b>  <i>e.g.</i>  <code>30</code>",
    "alert":     ("🔔 <b>Set a price alert</b>\n"
                  "<i>e.g.</i>  <code>NVDA 1000</code>  ·  <code>AAPL below 175</code>  ·  <code>TSLA above 300</code>"),
    "unalert":   ("🔕 <b>Remove which alert?</b>\n"
                  "<i>e.g.</i>  <code>NVDA</code>  (removes all NVDA alerts)  ·  <code>NVDA 1000</code>"),
    "paper_buy": ("📄 <b>Paper buy — what to simulate?</b>\n"
                  "<i>e.g.</i>  <code>AAPL 10</code>  ·  <code>AAPL 182.50 10</code>"),
    "paper_sell":("📄 <b>Paper sell — which position?</b>\n"
                  "<i>e.g.</i>  <code>AAPL</code>  ·  <code>AAPL 5</code>  (partial sell)"),
    "broadcast": ("📢 <b>Type your message to broadcast to all users:</b>\n"
                  "<i>e.g.</i>  <code>Picks will be delayed today — back tomorrow at 8:30 AM ET</code>"),
    "release":   ("🚀 <b>Type your release note to send to all users:</b>\n"
                  "<i>e.g.</i>  <code>New feature: price alerts now support crypto!</code>"),
    "chart":     ("📊 <b>Which ticker?</b>\n"
                  "<i>e.g.</i>  <code>AAPL</code>  ·  <code>NVDA</code>  ·  <code>BTC</code>"),
    "feedback":  ("💬 <b>What's on your mind?</b>\n"
                  "<i>Share any thoughts, suggestions, or issues — your feedback goes straight to the team.</i>"),
}


def _prompt_for_param(command: str, chat_id: str) -> None:
    """Save pending state and send the parameter-request prompt with a Cancel button."""
    prompt = _PARAM_PROMPTS.get(command, f"What value for /{command}?")
    save_pending_state(chat_id, command)
    send_inline_keyboard(
        prompt,
        [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
        chat_id=chat_id,
    )


def _send_settings_panel(chat_id: str) -> None:
    """Send the full interactive /settings panel with buttons for every preference."""
    cfg        = get_user_config(chat_id)
    global_cfg = get_config()

    # Values
    paused      = bool(cfg.get("paused", False))
    show_crypto = bool(cfg.get("show_crypto", True))
    risk        = cfg.get("risk_profile", "moderate")
    mode        = cfg.get("pick_mode", "both")
    sb          = cfg.get("stock_budget")
    cb          = cfg.get("crypto_budget")
    ms          = cfg.get("max_stock_picks")
    mc          = cfg.get("max_crypto_picks")
    sl_pct      = cfg.get("stop_loss_pct")   or global_cfg.get("stop_loss_pct",   7)
    tg_pct      = cfg.get("target_gain_pct") or global_cfg.get("target_gain_pct", 15)
    wl          = cfg.get("watchlist", [])
    ex          = cfg.get("excluded_sectors", [])
    skip_conf   = bool(cfg.get("skip_confirmation",     False))
    skip_eod    = bool(cfg.get("skip_eod",              False))
    skip_wl     = bool(cfg.get("skip_watchlist_alerts", False))
    skip_pre    = bool(cfg.get("skip_premarket",        False))

    # Portfolio sizing config
    portfolio_cfg  = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
    cap_size        = portfolio_cfg.get("portfolio_size")
    risk_per_trade  = portfolio_cfg.get("risk_per_trade_pct", 1.0)
    max_pos_pct     = portfolio_cfg.get("max_position_pct", 10.0)
    max_sec_pct     = portfolio_cfg.get("max_sector_pct",   35.0)
    cap_label       = f"${int(cap_size):,}" if cap_size else "not set"
    risk_pt_label   = f"{risk_per_trade}%"
    max_pos_label   = f"{max_pos_pct}%"
    max_sec_label   = f"{max_sec_pct}%"

    # Conviction threshold
    min_conv        = int(cfg.get("min_conviction", 4))
    min_conv_label  = "★" * min_conv

    # Labels
    risk_emoji  = {"conservative": "🛡", "moderate": "⚖️", "aggressive": "🔥"}.get(risk, "⚖️")
    mode_label  = {"st": "ST only", "lt": "LT only", "both": "Both"}.get(mode, mode)
    sb_label    = f"${int(sb)}" if sb else "not set"
    cb_label    = f"${int(cb)}" if cb else "not set"
    ms_label    = str(ms) if ms else "all"
    mc_label    = str(mc) if mc else "all"
    wl_label    = ", ".join(wl[:3]) + ("…" if len(wl) > 3 else "") if wl else "none"
    ex_label    = ", ".join(ex[:2]) + ("…" if len(ex) > 2 else "") if ex else "none"

    text = (
        "<b>⚙️ Settings</b>  —  tap any button to change\n\n"
        f"{'⏸' if paused else '✅'} Picks {'paused' if paused else 'active'}   "
        f"{'🔕' if not show_crypto else '🔔'} Crypto {'hidden' if not show_crypto else 'shown'}\n"
        f"{risk_emoji} Risk: <b>{risk}</b>   📊 Mode: <b>{mode_label}</b>\n"
        f"💰 Stock budget: <b>{sb_label}</b>   ₿ Crypto: <b>{cb_label}</b>\n"
        f"📈 Stock picks: <b>{ms_label}</b>   🪙 Crypto picks: <b>{mc_label}</b>\n"
        f"🛑 Stop loss: <b>{sl_pct}%</b>   🎯 Target: <b>{tg_pct}%</b>\n"
        f"👀 Watchlist: <b>{wl_label}</b>   🚫 Excluded: <b>{ex_label}</b>\n"
        f"💼 Portfolio capital: <b>{cap_label}</b>   ⚖️ Risk/trade: <b>{risk_pt_label}</b>   🎯 Max pos: <b>{max_pos_label}</b>   🏭 Max sector: <b>{max_sec_label}</b>\n"
        f"📨 10:30 confirm: <b>{'off' if skip_conf else 'on'}</b>   "
        f"🌅 EOD summary: <b>{'off' if skip_eod else 'on'}</b>   "
        f"👁 WL alerts: <b>{'off' if skip_wl else 'on'}</b>   "
        f"🌅 Pre-market: <b>{'off' if skip_pre else 'on'}</b>"
    )

    buttons = [
        # Row 1: toggles
        [
            {"text": "⏸ Pause" if not paused else "▶️ Resume",
             "callback_data": "settings_toggle|paused"},
            {"text": "🔕 Hide crypto" if show_crypto else "🔔 Show crypto",
             "callback_data": "settings_toggle|show_crypto"},
        ],
        # Row 2: risk + mode (open pickers)
        [
            {"text": f"{risk_emoji} Risk: {risk}",     "callback_data": "settings_open|risk"},
            {"text": f"📊 Mode: {mode_label}",          "callback_data": "settings_open|mode"},
        ],
        # Row 3: budgets (prompt)
        [
            {"text": f"💰 Stock budget: {sb_label}",   "callback_data": "settings_prompt|budget_stock"},
            {"text": f"₿ Crypto budget: {cb_label}",   "callback_data": "settings_prompt|budget_crypto"},
        ],
        # Row 4: pick counts (open pickers)
        [
            {"text": f"📈 Stock picks: {ms_label}",    "callback_data": "settings_open|picks_stock"},
            {"text": f"🪙 Crypto picks: {mc_label}",   "callback_data": "settings_open|picks_crypto"},
        ],
        # Row 5: thresholds (prompt)
        [
            {"text": f"🛑 Stop loss: {sl_pct}%",       "callback_data": "settings_prompt|stop"},
            {"text": f"🎯 Target gain: {tg_pct}%",     "callback_data": "settings_prompt|target"},
        ],
        # Row 6: watchlist + exclude (prompt)
        [
            {"text": f"👀 Watchlist: {wl_label}",      "callback_data": "settings_prompt|watchlist"},
            {"text": f"🚫 Exclude: {ex_label}",        "callback_data": "settings_prompt|exclude"},
        ],
        # Row 7: notification opt-outs (row a)
        [
            {"text": f"📨 10:30 AM {'✅' if not skip_conf else '🔕 off'}",
             "callback_data": "settings_toggle|skip_confirmation"},
            {"text": f"🌅 EOD {'✅' if not skip_eod else '🔕 off'}",
             "callback_data": "settings_toggle|skip_eod"},
        ],
        # Row 7b: more notification opt-outs
        [
            {"text": f"👁 WL alerts {'✅' if not skip_wl else '🔕 off'}",
             "callback_data": "settings_toggle|skip_watchlist_alerts"},
            {"text": f"🌅 Pre-market {'✅' if not skip_pre else '🔕 off'}",
             "callback_data": "settings_toggle|skip_premarket"},
        ],
        # Row 8a: portfolio sizing — capital + risk
        [
            {"text": f"💼 Capital: {cap_label}",        "callback_data": "settings_prompt|portfolio_size"},
            {"text": f"⚖️ Risk/trade: {risk_pt_label}",  "callback_data": "settings_prompt|portfolio_risk"},
        ],
        # Row 8b: portfolio caps — max position + max sector
        [
            {"text": f"🎯 Max pos: {max_pos_label}",    "callback_data": "settings_prompt|portfolio_max_pos"},
            {"text": f"🏭 Max sector: {max_sec_label}", "callback_data": "settings_prompt|portfolio_max_sector"},
        ],
        # Row 8c: conviction gate
        [
            {"text": f"🏆 Min conviction: {min_conv_label} ({min_conv}/5)",
             "callback_data": "settings_prompt|min_conviction"},
        ],
        # Row 9: reset
        [
            {"text": "🔄 Reset all settings", "callback_data": "settings_reset_ask"},
        ],
    ]

    send_inline_keyboard(text, buttons, chat_id=chat_id)


def _cmd_settings(text: str, original: str, chat_id: str) -> "str | None":
    """User preference / settings commands."""
    # /set_risk conservative | moderate | aggressive  (or natural language)
    if text == "SET RISK":
        _prompt_for_param("set_risk", chat_id)
        return ""

    if text.startswith("SET RISK "):
        from cmd_helpers import _nl_param
        raw     = text[len("SET RISK "):].strip().lower()
        profile = raw if raw in ("conservative", "moderate", "aggressive") else _nl_param("risk", raw).lower()
        if profile not in ("conservative", "moderate", "aggressive"):
            send_inline_keyboard(
                "⚖️ Choose your risk profile:",
                [[
                    {"text": "🛡 Conservative", "callback_data": "set_risk|conservative"},
                    {"text": "⚖️ Moderate",     "callback_data": "set_risk|moderate"},
                    {"text": "🔥 Aggressive",   "callback_data": "set_risk|aggressive"},
                ]],
                chat_id=chat_id,
            )
            return ""
        update_user_config(chat_id, "risk_profile", profile)
        descriptions = {
            "conservative": "Fewer picks, tighter stops, low-volatility sectors, reduced crypto.",
            "moderate":     "Balanced approach — default settings.",
            "aggressive":   "More picks, wider stops, all sectors, full crypto allocation.",
        }
        return f"✅ Risk profile → <b>{profile}</b>\n<i>{descriptions[profile]}</i>\nTakes effect tomorrow."

    # /mode st | /mode lt | /mode both — choose which sections appear in daily picks
    if text == "MODE":
        config = get_user_config(chat_id)
        current = config.get("pick_mode", "both")
        mode_desc = {
            "st":   "Short Term only (stocks + crypto, fast trades)",
            "lt":   "Long Term only (stocks + crypto, DCA positions)",
            "both": "Both short-term and long-term sections",
        }
        return (
            f"📊 <b>Pick Mode</b>\n"
            f"Current: <b>{current}</b> — {mode_desc.get(current, current)}\n\n"
            f"To change:\n"
            f"  /mode st   — short term only\n"
            f"  /mode lt   — long term only\n"
            f"  /mode both — show all sections (default)"
        )

    if text.startswith("MODE "):
        raw = text[len("MODE "):].strip().lower()
        if raw not in ("st", "lt", "both"):
            return "❌ Invalid mode. Use: /mode st, /mode lt, or /mode both"
        update_user_config(chat_id, "pick_mode", raw)
        labels = {
            "st":   "📈 Short Term only — fast trades (stock + crypto ST sections)",
            "lt":   "🏦 Long Term only — DCA positions (stock + crypto LT sections)",
            "both": "📊 Both — all sections shown (default)",
        }
        return f"✅ Pick mode → <b>{raw}</b>\n{labels[raw]}\nTakes effect tomorrow."

    # /exclude energy stocks  |  /exclude oil companies  |  /exclude none
    if text == "EXCLUDE":
        _prompt_for_param("exclude", chat_id)
        return ""

    if text.startswith("EXCLUDE "):
        import json as _json
        from cmd_helpers import _nl_param
        raw_query     = original.lstrip("/")
        sectors_input = raw_query.split(" ", 1)[1].strip() if " " in raw_query else ""
        if sectors_input.lower() in ("none", "clear", "reset", ""):
            update_user_config(chat_id, "excluded_sectors", [])
            return "✅ Sector exclusions cleared — all sectors eligible again."
        # Use Haiku to map natural language → proper sector names
        try:
            excluded = _json.loads(_nl_param("exclude", sectors_input))
        except Exception:
            excluded = [sectors_input.title()]
        update_user_config(chat_id, "excluded_sectors", excluded)
        return (f"✅ Excluding sectors: <b>{', '.join(excluded)}</b>\n"
                f"These sectors will be skipped in tomorrow's picks.\n"
                f"<i>To clear: /exclude none</i>")

    # /watch tesla/microsoft  |  /watch NVDA TSLA  |  /watch none
    if text == "WATCH":
        _prompt_for_param("watch", chat_id)
        return ""

    if text.startswith("WATCH "):
        import re as _re, json as _json
        from cmd_helpers import _nl_param
        raw_query     = original.lstrip("/")
        tickers_input = raw_query.split(" ", 1)[1].strip() if " " in raw_query else ""
        if tickers_input.upper() in ("NONE", "CLEAR", "RESET", ""):
            update_user_config(chat_id, "watchlist", [])
            return "✅ Watchlist cleared."
        # Split on spaces, commas, or slashes — support "tesla/microsoft", "NVDA, TSLA"
        parts = [p.strip() for p in _re.split(r"[,/\s]+", tickers_input) if p.strip()]
        # If all parts look like tickers (short, letters/hyphens only), use directly
        looks_like_tickers = all(len(p) <= 5 and _re.match(r"^[A-Za-z.\-]+$", p) for p in parts)
        if looks_like_tickers:
            tickers = [p.upper() for p in parts]
        else:
            # Natural language — resolve via Haiku
            try:
                tickers = _json.loads(_nl_param("watch", tickers_input))
            except Exception:
                tickers = [p.upper() for p in parts]
        update_user_config(chat_id, "watchlist", tickers)
        return (f"✅ Watchlist set: <b>{', '.join(tickers)}</b>\n"
                f"These tickers will always be evaluated in tomorrow's screener.\n"
                f"<i>To clear: /watch none</i>")

    # ── /share ───────────────────────────────────────────────────────────────
    if text == "SHARE":
        from telegram_api import _get_bot_username
        from cmd_helpers import _is_admin, _make_admin_invite_token
        import requests
        from telegram_api import TELEGRAM_API, _bot_token
        from config_manager import get_pending_users, remove_pending_user, get_allowed_users, add_pending_user
        bot_username = _get_bot_username()
        if not bot_username:
            return "⚠️ Could not fetch bot username — try again in a moment."

        # Admin share → cryptographically signed token, auto-approves the clicker.
        # Regular user share → still requires admin approval (normal pending flow).
        if _is_admin(chat_id):
            deep_link = _make_admin_invite_token()   # signed + time-limited (48h)
            footer    = "<i>(Anyone who taps this link is automatically approved ✅ — link valid 48 hours)</i>"
        else:
            deep_link = f"ref_{chat_id}"
            footer    = "<i>(Your friend will need admin approval — usually a few hours)</i>"

        bot_link = f"https://t.me/{bot_username}?start={deep_link}"

        return (
            f"📲 <b>Share StockPulz with friends:</b>\n\n"
            f"Hey! I'm using StockPulz — a personal AI stock advisor that sends daily stock &amp; crypto picks, "
            f"price alerts, and weekly performance recaps.\n\n"
            f'🌐 Learn more: <a href="https://stockpulz.com/">stockpulz.com</a>\n\n'
            f"📱 Join on Telegram 👇\n"
            f'<a href="{bot_link}">{bot_link}</a>\n\n'
            f"{footer}"
        )

    # ── /start (also handles deep links: /start admin_ref | /start ref_<id>) ───
    if text == "START" or text.startswith("START "):
        import requests
        from telegram_api import TELEGRAM_API, _bot_token
        from config_manager import get_pending_users, remove_pending_user, get_allowed_users, add_pending_user
        from cmd_helpers import _is_admin, _verify_admin_invite_token

        # Parse deep link parameter (everything after "START ")
        deep_param = text[6:].strip() if text.startswith("START ") else ""
        is_admin_invite = _verify_admin_invite_token(deep_param)

        # Known user — check if they've been through onboarding
        if _is_admin(chat_id) or chat_id in get_allowed_users():
            user_cfg = get_user_config(chat_id)
            if not user_cfg.get("onboarded") and not _is_admin(chat_id):
                if not user_cfg:
                    # Truly new user — no config at all — start wizard
                    _start_onboarding_wizard(chat_id)
                    return ""
                # Existing user with config but no flag — silently mark onboarded
                update_user_config(chat_id, "onboarded", True)
            return (
                "👋 <b>Welcome back to StockPulz!</b>\n\n"
                "/today — Today's stock &amp; crypto picks\n"
                "/positions — open trades with live P&amp;L\n"
                "/stats — your win rate &amp; P&amp;L breakdown\n"
                "/guide — quick reference card\n"
                "/settings — your preferences\n"
                "/help — all commands\n"
            )

        # ── Admin invite link → auto-approve immediately ──────────────────────
        if is_admin_invite:
            from config_manager import add_allowed_user
            add_allowed_user(chat_id)
            remove_pending_user(chat_id)
            # Notify admin silently
            owner = os.environ.get("TELEGRAM_CHAT_ID", "")
            if owner and owner != chat_id:
                send_message(f"🔔 <code>{chat_id}</code> auto-approved via invite link.", chat_id=owner)
            # Start the onboarding wizard — it handles the welcome
            _start_onboarding_wizard(chat_id)
            return ""   # wizard already sent above

        # ── Referral link → auto-approve immediately, notify referrer ───────────
        if deep_param.startswith("ref_"):
            referrer_id = deep_param[4:]
            # Fetch profile for welcome message
            first_name, username = "", ""
            try:
                resp = requests.get(
                    TELEGRAM_API.format(token=_bot_token(), method="getChat"),
                    params={"chat_id": chat_id}, timeout=5,
                ).json().get("result", {})
                first_name = resp.get("first_name", "")
                username   = resp.get("username", "")
            except Exception:
                pass
            # Auto-approve — no admin queue for referred users
            from config_manager import add_allowed_user
            add_allowed_user(chat_id)
            remove_pending_user(chat_id)
            # Track referral count on the referrer's profile
            try:
                ref_cfg   = get_user_config(referrer_id)
                ref_count = ref_cfg.get("referral_count", 0) + 1
                update_user_config(referrer_id, "referral_count", ref_count)
            except Exception:
                ref_count = 1
            # Notify referrer
            try:
                joiner_name = _esc(first_name or chat_id)
                send_message(
                    f"🎉 <b>Your invite worked!</b>\n\n"
                    f"{joiner_name} just joined StockPulz via your link.\n"
                    f"You've now referred <b>{ref_count}</b> friend{'s' if ref_count != 1 else ''}. "
                    f"Thanks for growing the community! 🙌",
                    chat_id=referrer_id,
                )
            except Exception:
                pass
            # Notify admin silently
            owner = os.environ.get("TELEGRAM_CHAT_ID", "")
            if owner and owner != chat_id:
                send_message(
                    f"🔔 <code>{chat_id}</code> auto-approved via referral from <code>{referrer_id}</code>.",
                    chat_id=owner,
                )
            # Start onboarding for the new user
            _start_onboarding_wizard(chat_id)
            return ""

        # ── Normal flow — add to pending, notify admin ────────────────────────
        pending = get_pending_users()
        if chat_id in pending:
            return (
                "⏳ <b>Your request is already pending.</b>\n"
                "You'll be notified as soon as you're approved — usually within a few hours."
            )
        # Fetch their Telegram profile for the admin notification
        first_name, username = "", ""
        try:
            resp = requests.get(
                TELEGRAM_API.format(token=_bot_token(), method="getChat"),
                params={"chat_id": chat_id}, timeout=5,
            ).json().get("result", {})
            first_name = resp.get("first_name", "")
            username   = resp.get("username", "")
        except Exception:
            pass
        add_pending_user(chat_id, first_name=first_name, username=username)
        # Notify admin with a one-tap approve button
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        admin_msg = (
            f"🔔 <b>New access request</b>\n\n"
            f"Name: <b>{_esc(first_name)}</b>"
            + (f"  (@{_esc(username)})" if username else "") +
            f"\nChat ID: <code>{chat_id}</code>\n\n"
            f"Tap to approve 👇"
        )
        if owner:
            send_inline_keyboard(
                admin_msg,
                [[
                    {"text": f"✅ Approve {_esc(first_name or chat_id)}",
                     "callback_data": f"approve_user|{chat_id}"},
                    {"text": "❌ Reject",
                     "callback_data": f"reject_user|{chat_id}"},
                ]],
                chat_id=owner,
            )
        return (
            "👋 <b>Welcome to StockPulz!</b>\n\n"
            "Your access request has been sent. You'll be notified as soon as you're approved — usually within a few hours.\n\n"
            "<i>StockPulz delivers daily AI-curated stock &amp; crypto picks, price alerts, and weekly performance recaps.</i>\n\n"
            "📖 <a href=\"https://stockpulz.com\">Learn more at stockpulz.com →</a>"
        )

    return None
