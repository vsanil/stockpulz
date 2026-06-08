"""
webhook.py — Flask app to receive Telegram bot updates via webhook.
Deploy to Render.com free tier. After deploying, register the webhook URL once:

    python webhook.py --set-webhook https://your-render-url.onrender.com/webhook

Or call the /register endpoint manually.
"""
from __future__ import annotations

import os
import sys
import time
import threading
import secrets as _sec
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from functools import wraps as _wraps

import requests
from flask import Flask, request, jsonify, session, redirect, send_from_directory

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[FlaskIntegration()],
        send_default_pii=False,
        traces_sample_rate=0.1,
        environment=os.environ.get("RENDER_EXTERNAL_URL", "development"),
    )

from config_manager import get_config, get_allowed_users
from telegram_notifier import handle_incoming_command, handle_callback_query, set_webhook, send_typing_action, typing_until_done, send_message

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or _sec.token_hex(32)

# ── Admin magic-link token store (Gist-backed, survives Render restarts) ──────
_ADMIN_TOKEN_FILE = "admin_tokens.json"

def _load_admin_tokens() -> dict:
    """Load {token: expiry_ts} dict from Gist/storage. Returns {} on any error."""
    try:
        from config_manager import _load_gist_file
        data = _load_gist_file(_ADMIN_TOKEN_FILE)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_admin_tokens(tokens: dict) -> None:
    """Persist token dict to Gist/storage."""
    try:
        from config_manager import _write_gist_file
        _write_gist_file(_ADMIN_TOKEN_FILE, tokens)
    except Exception as e:
        print(f"[admin] WARNING: Could not save admin tokens: {e}")


# ── Keep-alive (prevents Render free tier cold starts) ────────────────────────

def _keep_alive_loop():
    """
    Ping /health every 14 minutes so Render doesn't spin down the service.
    Render free tier idles after 15 minutes of inactivity — first request after
    idle takes 15-20s. This keeps the process warm at zero extra cost.
    Requires RENDER_EXTERNAL_URL env var (set automatically by Render).
    """
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        print("[webhook] RENDER_EXTERNAL_URL not set — keep-alive disabled.")
        return
    ping_url = f"{url}/health"
    print(f"[webhook] Keep-alive started — pinging {ping_url} every 14 min.")
    while True:
        time.sleep(14 * 60)
        try:
            resp = requests.get(ping_url, timeout=10)
            print(f"[webhook] Keep-alive ping → {resp.status_code}")
        except Exception as exc:
            print(f"[webhook] Keep-alive ping failed: {exc}")

threading.Thread(target=_keep_alive_loop, daemon=True).start()


# ── External cron trigger (cron-job.org → Render) ────────────────────────────

@app.route("/trigger/morning", methods=["POST", "GET"])
def trigger_morning():
    """
    Called by cron-job.org at 11:00 AM UTC (7 AM EDT) on weekdays.
    Spawns the morning picks run in a background subprocess and returns immediately.
    Protected by CRON_SECRET env var — include as ?secret=XXX in the cron URL.
    """
    secret   = os.environ.get("CRON_SECRET", "")
    provided = request.args.get("secret", "") or (request.get_json(silent=True) or {}).get("secret", "")
    if not secret or provided != secret:
        return jsonify({"error": "unauthorized"}), 403

    # Duplicate-run guard — skip if morning already ran today (ET)
    # Pass ?force=true to bypass (admin/testing only — secret still required)
    force = request.args.get("force", "").lower() in ("1", "true")
    if not force:
        try:
            import pytz as _tz
            from datetime import datetime as _dt
            from config_manager import get_config as _gcfg
            _cfg     = _gcfg()
            _last    = _cfg.get("cron_last_morning", "")
            _today   = _dt.now(_tz.timezone("America/New_York")).date().isoformat()
            if _last and _last[:10] >= _today:
                print(f"[trigger_morning] Already ran today ({_last[:16]}) — skipping.")
                return jsonify({"ok": True, "skipped": True, "reason": "already ran today"}), 200
        except Exception as _e:
            print(f"[trigger_morning] Duplicate check failed (non-critical): {_e}")

    # Spawn morning run as a detached subprocess — start_new_session=True ensures
    # agent.py survives gunicorn worker restarts (new process group, not killed
    # by SIGTERM to the parent worker).
    owner_only  = request.args.get("owner_only", "").lower() in ("1", "true")
    extra_env   = {"RUN_MODE": "morning"}
    if owner_only:
        extra_env["OWNER_ONLY"] = "1"
    import subprocess, sys as _sys
    subprocess.Popen(
        [_sys.executable, "agent.py"],
        env={**os.environ, **extra_env},
        cwd=os.path.dirname(os.path.abspath(__file__)),
        start_new_session=True,   # detach from gunicorn worker process group
    )
    print(f"[trigger_morning] Morning run spawned (owner_only={owner_only}).")
    return jsonify({"ok": True, "triggered": True, "owner_only": owner_only}), 200


@app.route("/trigger/prescreener", methods=["POST", "GET"])
def trigger_prescreener():
    """
    Called by cron-job.org at 3:00 AM UTC (11 PM EDT) on weekdays.
    Runs the overnight prescreener that caches stock scores for the morning run.
    Protected by CRON_SECRET env var.
    """
    secret   = os.environ.get("CRON_SECRET", "")
    provided = request.args.get("secret", "") or (request.get_json(silent=True) or {}).get("secret", "")
    if not secret or provided != secret:
        return jsonify({"error": "unauthorized"}), 403

    # Duplicate guard — skip if prescreener already ran tonight
    try:
        import pytz as _tz
        from datetime import datetime as _dt
        from config_manager import get_config as _gcfg
        _cfg   = _gcfg()
        _last  = _cfg.get("cron_last_prescreener", "")
        _today = _dt.now(_tz.timezone("America/New_York")).date().isoformat()
        if _last and _last[:10] >= _today:
            print(f"[trigger_prescreener] Already ran today ({_last[:16]}) — skipping.")
            return jsonify({"ok": True, "skipped": True, "reason": "already ran today"}), 200
    except Exception as _e:
        print(f"[trigger_prescreener] Duplicate check failed (non-critical): {_e}")

    import subprocess, sys as _sys
    subprocess.Popen(
        [_sys.executable, "agent.py"],
        env={**os.environ, "RUN_MODE": "prescreener"},
        cwd=os.path.dirname(os.path.abspath(__file__)),
        start_new_session=True,
    )
    print("[trigger_prescreener] Prescreener spawned.")
    return jsonify({"ok": True, "triggered": True}), 200


@app.route("/trigger/<mode>", methods=["POST", "GET"])
def trigger_mode(mode: str):
    """
    Generic trigger endpoint for any agent run mode.
    Called by cron-job.org for all scheduled jobs.
    Protected by CRON_SECRET env var.

    Supported modes: confirmation, weekly, close_check, eod_summary,
    week_ahead, premarket, digest, friday_wrap, macro_alert, midday_check,
    vix_check, news_check, pre_earnings, monthly_commentary, watchdog, tax_harvest.

    ?owner_only=true  — send only to admin (for testing)
    ?secret=XXX       — required
    """
    _VALID_MODES = {
        "confirmation", "weekly", "close_check", "eod_summary",
        "week_ahead", "premarket", "digest", "friday_wrap",
        "macro_alert", "midday_check", "vix_check", "news_check",
        "pre_earnings", "monthly_commentary", "watchdog", "tax_harvest",
        "morning", "prescreener",   # kept for completeness; dedicated endpoints take priority
        "price_alerts",             # runs every 30 min during market hours
    }
    if mode not in _VALID_MODES:
        return jsonify({"error": f"unknown mode: {mode}"}), 400

    secret   = os.environ.get("CRON_SECRET", "")
    provided = request.args.get("secret", "") or (request.get_json(silent=True) or {}).get("secret", "")
    if not secret or provided != secret:
        return jsonify({"error": "unauthorized"}), 403

    # Per-mode duplicate guard — skip if this mode already ran today (ET).
    # price_alerts runs every 30 min during market hours — skip the daily guard for it.
    _MULTI_RUN_MODES = {"price_alerts"}
    if mode not in _MULTI_RUN_MODES:
        try:
            import pytz as _tz
            from datetime import datetime as _dt
            from config_manager import get_config as _gcfg
            _cfg   = _gcfg()
            _key   = f"cron_last_{mode}"
            _last  = _cfg.get(_key, "")
            _today = _dt.now(_tz.timezone("America/New_York")).date().isoformat()
            if _last and _last[:10] >= _today:
                print(f"[trigger/{mode}] Already ran today ({_last[:16]}) — skipping.")
                return jsonify({"ok": True, "skipped": True, "reason": "already ran today"}), 200
        except Exception as _e:
            print(f"[trigger/{mode}] Duplicate check failed (non-critical): {_e}")

    owner_only = request.args.get("owner_only", "").lower() in ("1", "true")
    extra_env  = {"RUN_MODE": mode}
    if owner_only:
        extra_env["OWNER_ONLY"] = "1"

    import subprocess, sys as _sys
    subprocess.Popen(
        [_sys.executable, "agent.py"],
        env={**os.environ, **extra_env},
        cwd=os.path.dirname(os.path.abspath(__file__)),
        start_new_session=True,   # detach from gunicorn worker process group
    )
    print(f"[trigger/{mode}] Spawned (owner_only={owner_only}).")
    return jsonify({"ok": True, "triggered": True, "mode": mode, "owner_only": owner_only}), 200


# ── Telegram webhook receiver ─────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive Telegram update (message from user to bot)."""
    data = request.get_json(silent=True) or {}

    # Extract message text and chat_id from Telegram update format
    # ── Inline keyboard button tap ────────────────────────────────────────────
    callback_query = data.get("callback_query")
    if callback_query:
        cq_chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        if cq_chat_id and cq_chat_id not in get_allowed_users():
            return jsonify({"status": "ok", "access": "denied"}), 200
        with typing_until_done(cq_chat_id or None):
            handle_callback_query(callback_query)
        return jsonify({"status": "ok", "type": "callback_query"}), 200

    # ── Regular message ───────────────────────────────────────────────────────
    message = data.get("message") or data.get("edited_message", {})
    if not message:
        return jsonify({"status": "ignored", "reason": "no message"}), 200

    text       = message.get("text", "").strip()
    chat_id    = str(message.get("chat", {}).get("id", ""))
    _from      = message.get("from", {})
    first_name = _from.get("first_name", "")
    username   = _from.get("username", "")

    if not text or not chat_id:
        return jsonify({"status": "ignored", "reason": "empty text or chat_id"}), 200

    print(f"[webhook] Received from {chat_id}: {text!r}")

    # Persist display name so morning picks can greet the user by name
    if first_name or username:
        def _persist_name():
            try:
                from config_manager import update_user_config_multi, get_user_config
                cur = get_user_config(chat_id)
                updates = {}
                if first_name and cur.get("first_name") != first_name:
                    updates["first_name"] = first_name
                if username and cur.get("username") != username:
                    updates["username"] = username
                if updates:
                    update_user_config_multi(chat_id, updates)
            except Exception:
                pass
        threading.Thread(target=_persist_name, daemon=True).start()

    # ── Access control ────────────────────────────────────────────────────────
    owner   = os.environ.get("TELEGRAM_CHAT_ID", "")
    allowed = get_allowed_users()   # always includes owner

    # Any /start message (plain or with deep-link param like /start adminref_xxx)
    # must reach handle_incoming_command — it contains the HMAC verification logic
    # for admin invite links. Matching on the prefix covers all variants.
    text_lower = text.strip().lower()
    is_start = text_lower.startswith("/start") or text_lower == "start"

    if chat_id not in allowed:
        if is_start:
            # Let handle_incoming_command deal with it — it handles pending flow,
            # admin invite auto-approval, and welcome messages.
            try:
                with typing_until_done(chat_id):
                    handle_incoming_command(text, chat_id=chat_id)
            except Exception as exc:
                print(f"[webhook] Error handling /start for {chat_id}: {exc}")
                send_message("⚠️ Something went wrong — please try again.", chat_id=chat_id)
        else:
            send_message(
                "🔒 You don't have access yet. Send /start to request access.",
                chat_id=chat_id,
            )
        return jsonify({"status": "ok", "access": "denied"}), 200

    try:
        with typing_until_done(chat_id):
            reply = handle_incoming_command(text, chat_id=chat_id)
    except Exception as exc:
        print(f"[webhook] Error handling {text!r}: {exc}")
        send_message("⚠️ Something went wrong — please try again.", chat_id=chat_id)
        return jsonify({"status": "error", "detail": str(exc)}), 200

    if reply:
        pass   # handle_incoming_command already sent via send_message for inline flows
    return jsonify({"status": "ok", "reply": reply}), 200


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check — returns current config."""
    try:
        config = get_config()
        return jsonify({"status": "ok", "config": config}), 200
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 500


# ── One-time webhook registration ─────────────────────────────────────────────

@app.route("/register", methods=["GET"])
def register():
    """
    Call this once after deploying to Render to register the Telegram webhook.
    e.g. https://your-app.onrender.com/register?url=https://your-app.onrender.com/webhook
    """
    webhook_url = request.args.get("url", "")
    if not webhook_url:
        host = request.host_url.rstrip("/")
        webhook_url = f"{host}/webhook"
    ok = set_webhook(webhook_url)
    return jsonify({"registered": ok, "webhook_url": webhook_url}), 200 if ok else 500


@app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "Stock Agent Telegram Webhook", "status": "running"}), 200


# ── Admin auth helpers ────────────────────────────────────────────────────────

def _require_admin(f):
    """Decorator: redirect to login if no valid admin session."""
    @_wraps(f)
    def _inner(*a, **kw):
        if not session.get("admin"):
            return redirect("/admin/login")
        return f(*a, **kw)
    return _inner


# ── Admin login page ──────────────────────────────────────────────────────────

_ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockPulz Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;background:#080a0f;color:#eef0f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#0f1117;border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:40px 36px;width:100%;max-width:380px;text-align:center}
h1{font-size:22px;font-weight:700;margin-bottom:8px}
.sub{color:#7c8899;font-size:14px;margin-bottom:28px}
.gh-btn{display:flex;align-items:center;justify-content:center;gap:10px;background:#238636;color:#fff;border:none;border-radius:10px;padding:13px 24px;font-size:15px;font-weight:600;cursor:pointer;width:100%;text-decoration:none;transition:opacity .15s}
.gh-btn:hover{opacity:.88}
.gh-btn svg{flex-shrink:0}
.msg{margin-top:16px;font-size:13px;color:#f87171;min-height:20px}
</style>
</head>
<body>
<div class="card">
  <h1>📈 StockPulz Admin</h1>
  <p class="sub">Sign in with your GitHub account.</p>
  <a class="gh-btn" href="/admin/github">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
    Login with GitHub
  </a>
  <p class="msg" id="msg"></p>
</div>
<script>
const p=new URLSearchParams(location.search);
if(p.get('error')){
  document.getElementById('msg').textContent=
    p.get('error')==='forbidden'?'Access denied — wrong GitHub account.':'Authentication failed — try again.';
}
</script>
</body>
</html>"""


# ── Admin dashboard HTML ──────────────────────────────────────────────────────

_ADMIN_DASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockPulz Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080a0f;--bg2:#0f1117;--bg3:#161b26;--bg4:#1c2233;
  --border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.12);
  --text:#eef0f5;--muted:#7c8899;--muted2:#a0aab8;
  --accent:#4f8ef7;--green:#34d399;--amber:#fbbf24;--red:#f87171;--purple:#a78bfa;
  --r:12px;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;background:var(--bg);color:var(--text);line-height:1.5;padding:20px 24px;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--border)}
.topbar-title{font-size:18px;font-weight:700;display:flex;align-items:center;gap:8px}
.live{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block}
.topbar-right{display:flex;align-items:center;gap:12px;font-size:12px;color:var(--muted)}
.btn-sm{background:var(--bg3);border:1px solid var(--border2);color:var(--muted2);padding:5px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-family:inherit}
.btn-sm:hover{border-color:var(--accent);color:var(--accent)}
.btn-danger{background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.3);color:var(--red);padding:5px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-family:inherit}
.btn-danger:hover{background:rgba(248,113,113,.2)}
.btn-success{background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.3);color:var(--green);padding:5px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-family:inherit}
.btn-success:hover{background:rgba(52,211,153,.2)}
.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px}
.m-label{font-size:11px;color:var(--muted);margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em}
.m-val{font-size:28px;font-weight:700}
.m-sub{font-size:11px;color:var(--muted);margin-top:3px}
.m-pending .m-val{color:var(--amber)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px}
.card-title{font-size:11px;font-weight:600;color:var(--muted);margin-bottom:12px;text-transform:uppercase;letter-spacing:.06em;display:flex;align-items:center;justify-content:space-between}
.row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;cursor:pointer;transition:background .1s;border-radius:6px;padding:8px 6px}
.row:last-child{border-bottom:none}
.row:hover{background:var(--bg3)}
.avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}
.av-blue{background:#1c2e4a;color:var(--accent)}
.av-green{background:#1a2e1a;color:var(--green)}
.av-red{background:#2e1a1a;color:var(--red)}
.u-info{flex:1;min-width:0}
.u-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.u-meta{font-size:11px;color:var(--muted);margin-top:1px}
.pill{font-size:10px;padding:2px 8px;border-radius:20px;font-weight:600;flex-shrink:0}
.p-active{background:rgba(52,211,153,.12);color:var(--green)}
.p-paused{background:rgba(251,191,36,.12);color:var(--amber)}
.p-idle{background:rgba(124,136,153,.12);color:var(--muted)}
.p-banned{background:rgba(248,113,113,.12);color:var(--red)}
.p-pending{background:rgba(167,139,250,.12);color:var(--purple)}
.cron-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px}
.cron-row:last-child{border-bottom:none}
.cron-left{display:flex;align-items:center;gap:8px}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.d-green{background:var(--green)}.d-amber{background:var(--amber)}.d-muted{background:var(--muted);opacity:.4}
.cron-time{color:var(--muted);font-size:11px;text-align:right}
.tabs{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap}
.tab{padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;border:1px solid var(--border);color:var(--muted);background:transparent;font-family:inherit}
.tab.on{background:var(--bg4);border-color:var(--border2);color:var(--text)}
.pick-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
.pick-row:last-child{border-bottom:none}
.ticker{font-weight:700;font-size:13px;min-width:52px;color:var(--text)}
.detail{color:var(--muted);flex:1;font-size:11px}
.fb-row{padding:8px 0;border-bottom:1px solid var(--border)}
.fb-row:last-child{border-bottom:none}
.fb-meta{font-size:11px;color:var(--muted);margin-bottom:3px}
.fb-text{font-size:12px;color:var(--text);line-height:1.45}
.unread{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);margin-right:5px;vertical-align:middle}
.empty{color:var(--muted);font-size:12px;text-align:center;padding:20px 0}
.loading{color:var(--muted);font-size:13px;text-align:center;padding:60px 0}
.pending-banner{background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.2);border-radius:var(--r);padding:12px 16px;margin-bottom:12px}
.pending-title{font-size:12px;font-weight:600;color:var(--purple);margin-bottom:8px}
.pending-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:12px}
.pending-row:last-child{border-bottom:none}
.broadcast-bar{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:12px 16px;margin-bottom:12px;display:flex;gap:8px;align-items:center}
.broadcast-bar input{flex:1;background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:7px 12px;color:var(--text);font-size:13px;font-family:inherit;outline:none}
.broadcast-bar input:focus{border-color:var(--accent)}
/* ── User detail drawer ── */
#drawer{position:fixed;top:0;right:-480px;width:480px;height:100vh;background:var(--bg2);border-left:1px solid var(--border2);z-index:1000;transition:right .25s ease;overflow-y:auto;display:flex;flex-direction:column}
#drawer.open{right:0}
#overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:999;display:none}
#overlay.open{display:block}
.drawer-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-shrink:0}
.drawer-body{flex:1;overflow-y:auto;padding:16px 20px}
.drawer-close{margin-left:auto;background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;line-height:1}
.drawer-close:hover{color:var(--text)}
.d-tabs{display:flex;gap:4px;margin-bottom:14px;border-bottom:1px solid var(--border);padding-bottom:8px}
.d-tab{padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer;border:none;background:transparent;color:var(--muted);font-family:inherit}
.d-tab.on{background:var(--bg4);color:var(--text)}
.kv-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:12px}
.kv-row:last-child{border-bottom:none}
.kv-key{color:var(--muted)}
.kv-val{font-weight:500;text-align:right;max-width:60%}
.pos-row{padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
.pos-row:last-child{border-bottom:none}
.log-row{padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:11px;display:flex;gap:8px}
.log-row:last-child{border-bottom:none}
.log-ts{color:var(--muted);flex-shrink:0;font-size:10px;padding-top:1px}
.log-type{flex-shrink:0;width:60px;font-weight:600;font-size:10px;text-transform:uppercase}
.lt-command{color:var(--accent)}.lt-error{color:var(--red)}.lt-alert{color:var(--amber)}.lt-position{color:var(--green)}.lt-system{color:var(--muted)}.lt-settings{color:var(--purple)}
.log-detail{color:var(--muted2);word-break:break-word}
.action-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.msg-area{width:100%;background:var(--bg3);border:1px solid var(--border2);border-radius:8px;padding:8px 12px;color:var(--text);font-size:13px;font-family:inherit;outline:none;resize:vertical;min-height:60px;margin-top:10px}
.msg-area:focus{border-color:var(--accent)}
.section-hd{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:14px 0 6px}
</style>
</head>
<body>
<div id="overlay" onclick="closeDrawer()"></div>
<div id="drawer">
  <div class="drawer-head">
    <div class="avatar av-blue" id="dav">??</div>
    <div><div id="dname" style="font-weight:700;font-size:15px"></div><div id="dmeta" style="font-size:11px;color:var(--muted);margin-top:2px"></div></div>
    <button class="drawer-close" onclick="closeDrawer()">✕</button>
  </div>
  <div class="drawer-body">
    <div class="d-tabs">
      <button class="d-tab on" onclick="dTab('overview')">Overview</button>
      <button class="d-tab" onclick="dTab('positions')">Positions</button>
      <button class="d-tab" onclick="dTab('alerts')">Alerts</button>
      <button class="d-tab" onclick="dTab('logs')">Logs</button>
      <button class="d-tab" onclick="dTab('actions')">Actions</button>
    </div>
    <div id="dpanel"><p class="loading">Loading…</p></div>
  </div>
</div>
<div class="topbar">
  <div class="topbar-title">📈 StockPulz Admin <span class="live"></span></div>
  <div class="topbar-right">
    <span id="ts"></span>
    <button class="btn-sm" onclick="load()">↻ Refresh</button>
    <a href="/admin/logout" class="btn-sm">Logout</a>
  </div>
</div>
<div id="root"><p class="loading">Loading…</p></div>
<script>
var picks={},activeTab='',_dData=null,_dTab='overview';

function age(iso){
  if(!iso)return'—';
  try{
    var d=new Date(iso+(iso.endsWith('Z')?'':'Z')),now=new Date();
    var m=Math.round((now-d)/60000);
    if(m<2)return'just now';if(m<60)return m+'m ago';
    if(m<1440)return Math.round(m/60)+'h ago';return Math.round(m/1440)+'d ago';
  }catch(e){return iso.slice(0,16).replace('T',' ');}
}
function ini(a,b){return((a||b||'??').slice(0,2)).toUpperCase();}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function metrics(s){
  var pend=s.pending_count||0;
  var pendCls=pend?'m-pending':'';
  return'<div class="metrics">'
    +'<div class="metric"><div class="m-label">Total users</div><div class="m-val">'+s.total_users+'</div><div class="m-sub">'+s.active_today+' active today</div></div>'
    +'<div class="metric '+pendCls+'"><div class="m-label">Pending</div><div class="m-val">'+pend+'</div><div class="m-sub">awaiting approval</div></div>'
    +'<div class="metric"><div class="m-label">Picks today</div><div class="m-val">'+s.total_picks+'</div><div class="m-sub">stocks · crypto · ETFs</div></div>'
    +'<div class="metric"><div class="m-label">Open positions</div><div class="m-val">'+s.open_positions+'</div><div class="m-sub">across all users</div></div>'
    +'<div class="metric"><div class="m-label">Unread feedback</div><div class="m-val">'+s.unread_feedback+'</div><div class="m-sub">&nbsp;</div></div>'
    +'</div>';
}

function pendingSection(list){
  if(!list||!list.length)return'';
  var rows=list.map(function(p){
    var name=p.first_name||(p.username?'@'+p.username:'')||('user_'+String(p.chat_id).slice(-4));
    return'<div class="pending-row">'
      +'<div style="flex:1"><b>'+esc(name)+'</b>'+(p.username?' <span style="color:var(--muted)">@'+esc(p.username)+'</span>':'')
      +'<span style="font-size:10px;color:var(--muted);margin-left:6px">requested '+age(p.requested_at)+'</span></div>'
      +'<button class="btn-success" style="padding:3px 10px;font-size:11px" onclick="approve(\\''+p.chat_id+'\\',this)">Approve</button>'
      +' <button class="btn-danger" style="padding:3px 10px;font-size:11px" onclick="reject(\\''+p.chat_id+'\\',this)">Reject</button>'
      +'</div>';
  }).join('');
  return'<div class="pending-banner"><div class="pending-title">⏳ '+list.length+' pending approval</div>'+rows+'</div>';
}

function broadcastBar(){
  return'<div class="broadcast-bar">'
    +'<input id="bcmsg" placeholder="Broadcast to all users…" onkeydown="if(event.key===\\'Enter\\')broadcast()">'
    +'<button class="btn-sm" onclick="broadcast()">Send all</button>'
    +'</div>';
}

function usersList(list){
  if(!list.length)return'<p class="empty">No users yet</p>';
  return list.map(function(u){
    var name=u.first_name||u.username||('user_'+u.id.slice(-4));
    var tag=u.is_admin?' <span style="color:var(--muted);font-weight:400;font-size:11px">(admin)</span>':'';
    var pill=u.paused?'<span class="pill p-paused">paused</span>':u.is_active?'<span class="pill p-active">active</span>':'<span class="pill p-idle">idle</span>';
    var avcls=u.is_admin?'av-green':'av-blue';
    return'<div class="row" onclick="openUser(\\''+u.id+'\\')">'+
      '<div class="avatar '+avcls+'">'+ini(u.first_name,u.username)+'</div>'
      +'<div class="u-info"><div class="u-name">'+esc(name)+tag+'</div>'
      +'<div class="u-meta">'+u.open_positions+' pos &middot; last seen '+age(u.last_seen)+'</div></div>'
      +pill+'<span style="color:var(--muted);font-size:14px;margin-left:2px">›</span></div>';
  }).join('');
}

var SCHED={morning:'7:00 AM ET',premarket:'8:45 AM ET',confirmation:'10:30 AM ET',
  close_check:'3:30 PM ET',eod_summary:'4:15 PM ET',prescreener:'11:00 PM ET',
  price_alerts:'every 30 min',weekly:'Sat 8AM',week_ahead:'Sun 8AM'};

function cron(c,lastMorning){
  return Object.keys(SCHED).map(function(k){
    var last=c[k]||(k==='morning'?lastMorning:'');
    var dc=last?'d-green':'d-muted';
    var runBtn='<button class="btn-sm" style="padding:2px 8px;font-size:11px" data-mode="'+k+'" data-label="&#9654; Run" onclick="runAgent(this,false)">&#9654; Run</button>';
    var mockBtn=(k==='morning'||k==='weekly')?'<button class="btn-sm" style="padding:2px 8px;font-size:11px;margin-left:4px" data-mode="'+k+'" data-label="&#9654; Mock" onclick="runAgent(this,true)">&#9654; Mock</button>':'';
    return'<div class="cron-row"><span class="cron-left"><span class="dot '+dc+'"></span>'+k.replace(/_/g,' ')+'</span>'
      +'<span class="cron-time">'+(last?age(last):'not yet')+' &middot; '+SCHED[k]+'</span>'
      +'<span style="margin-left:8px">'+runBtn+mockBtn+'</span></div>';
  }).join('');
}

async function runAgent(btn,mock){
  var mode=btn.dataset.mode;
  if(!confirm((mock?'[MOCK] ':'')+'Run '+mode+' now?'))return;
  // Disable all run buttons while in progress
  var allBtns=document.querySelectorAll('[data-mode]');
  allBtns.forEach(function(b){b.disabled=true;b.style.opacity='0.45';});
  var secs=mock?30:180;
  var iv=setInterval(function(){
    secs--;
    btn.textContent='⏳ '+secs+'s';
    if(secs<=0){
      clearInterval(iv);
      allBtns.forEach(function(b){b.disabled=false;b.style.opacity='';b.textContent=b.dataset.label;});
    }
  },1000);
  try{
    var r=await fetch('/admin/run_agent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode,mock:mock})});
    var d=await r.json();
    if(!d.ok){clearInterval(iv);allBtns.forEach(function(b){b.disabled=false;b.style.opacity='';b.textContent=b.dataset.label;});alert('Error: '+(d.error||'unknown'));}
  }catch(e){clearInterval(iv);allBtns.forEach(function(b){b.disabled=false;b.style.opacity='';b.textContent=b.dataset.label;});alert('Request failed: '+e);}
}

function pickRows(arr,isCrypto){
  if(!arr||!arr.length)return'<p class="empty">No picks</p>';
  return arr.map(function(p){
    var sym=isCrypto?(p.symbol||p.ticker||''):(p.ticker||p.symbol||'');
    var det='entry $'+(p.entry_price||'—')+' &middot; tgt $'+(p.target_price||'—')+(p.stop_loss?' &middot; stop $'+p.stop_loss:'');
    return'<div class="pick-row"><span class="ticker">'+esc(sym)+'</span><span class="detail">'+det+'</span></div>';
  }).join('');
}

var TABS=[['stocks_st','ST Stocks',false],['stocks_lt','LT Stocks',false],
  ['crypto_st','Crypto ST',true],['crypto_lt','Crypto LT',true],['etfs_st','ETF ST',false],['etfs_lt','ETF LT',false]];

function picksPanel(p){
  picks=p;
  var avail=TABS.filter(function(t){return p[t[0]]&&p[t[0]].length;});
  if(!avail.length)return'<p class="empty">No picks today</p>';
  if(!avail.find(function(t){return t[0]===activeTab;}))activeTab=avail[0][0];
  var tabsHtml=avail.map(function(t){
    return'<button class="tab'+(t[0]===activeTab?' on':'')+'" onclick="switchTab(\\''+t[0]+'\\')">'
      +t[1]+' ('+p[t[0]].length+')</button>';
  }).join('');
  return'<div class="tabs">'+tabsHtml+'</div><div id="pcontent">'+pickRows(picks[activeTab],activeTab.startsWith('crypto'))+'</div>';
}

function switchTab(k){
  activeTab=k;
  document.querySelectorAll('.tab').forEach(function(b){
    var m=b.getAttribute('onclick').match(/'([^']+)'/);
    b.className='tab'+(m&&m[1]===k?' on':'');
  });
  document.getElementById('pcontent').innerHTML=pickRows(picks[k],k.startsWith('crypto'));
}

function feedback(list){
  if(!list.length)return'<p class="empty">No feedback yet</p>';
  return list.slice(0,15).map(function(f){
    var name=f.first_name||(f.username?'@'+f.username:'')||('user_'+String(f.chat_id).slice(-4));
    var dot=f.read?'':'<span class="unread"></span>';
    var plat=f.platform?'<span style="font-size:10px;background:rgba(255,255,255,.08);padding:1px 5px;border-radius:4px;margin-left:4px">'+esc(f.platform)+'</span>':'';
    var pg=f.page?'<span style="font-size:10px;color:var(--muted)"> · on '+esc(f.page)+'</span>':'';
    var cid=f.chat_id||'';
    var copyBtn='<button onclick="navigator.clipboard.writeText(\\''+cid+'\\');this.textContent=\\'✓\\';setTimeout(()=>this.textContent=\\''+cid+'\\',1200)" '
      +'style="font-size:10px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:inherit;border-radius:4px;padding:2px 6px;cursor:pointer;margin-top:4px">'+cid+'</button>';
    return'<div class="fb-row">'
      +'<div class="fb-meta">'+dot+'<b onclick="openUser(\\''+cid+'\\')" style="cursor:pointer;text-decoration:underline">'+esc(name)+'</b>'+plat+pg+' &middot; '+age(f.submitted_at)+'</div>'
      +'<div class="fb-text">'+esc(f.text)+'</div>'
      +'<div>'+copyBtn+' <span style="font-size:10px;color:var(--muted)">↑ Render log filter</span></div>'
      +'</div>';
  }).join('');
}

/* ── Pending actions ── */
async function approve(cid,btn){
  btn.disabled=true;btn.textContent='…';
  await fetch('/admin/user/'+cid+'/approve',{method:'POST'});
  load();
}
async function reject(cid,btn){
  btn.disabled=true;btn.textContent='…';
  await fetch('/admin/user/'+cid+'/reject',{method:'POST'});
  load();
}
async function broadcast(){
  var t=document.getElementById('bcmsg');
  var msg=t.value.trim();if(!msg)return;
  t.disabled=true;
  var r=await fetch('/admin/broadcast',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:msg})});
  var d=await r.json();
  t.value='';t.disabled=false;
  alert('Sent to '+d.sent+' users'+(d.failed?' ('+d.failed+' failed)':'')+'.');
}

/* ── User detail drawer ── */
async function openUser(cid){
  _dTab='overview';
  document.getElementById('overlay').className='open';
  document.getElementById('drawer').className='open';
  document.getElementById('dname').textContent='Loading…';
  document.getElementById('dmeta').textContent='';
  document.getElementById('dpanel').innerHTML='<p class="loading">Loading…</p>';
  var r=await fetch('/admin/user/'+cid);
  if(!r.ok){document.getElementById('dpanel').innerHTML='<p class="empty">Error loading user</p>';return;}
  _dData=await r.json();
  renderDrawerHead();
  renderDrawerTab('overview');
}

function renderDrawerHead(){
  var d=_dData;
  var name=(d.first_name||d.username||('user_'+String(d.chat_id).slice(-4)));
  document.getElementById('dav').textContent=ini(d.first_name,d.username);
  document.getElementById('dname').textContent=name+(d.is_admin?' (admin)':'')+(d.is_banned?' 🚫':'');
  document.getElementById('dmeta').textContent=(d.username?'@'+d.username+' · ':'')+d.chat_id+' · last seen '+age(d.last_seen);
}

function dTab(t){
  _dTab=t;
  document.querySelectorAll('.d-tab').forEach(function(b){
    b.className='d-tab'+(b.getAttribute('onclick').includes("'"+t+"'")?' on':'');
  });
  renderDrawerTab(t);
}

function renderDrawerTab(t){
  var d=_dData;
  if(!d){return;}
  var h='';
  if(t==='overview'){
    var cfg=d.config||{};
    h='<div class="section-hd">Config</div>'
      +'<div class="kv-row"><span class="kv-key">Risk profile</span><span class="kv-val">'+esc(cfg.risk_profile||'—')+'</span></div>'
      +'<div class="kv-row"><span class="kv-key">Budget</span><span class="kv-val">$'+esc(cfg.budget||0)+'</span></div>'
      +'<div class="kv-row"><span class="kv-key">Goal return</span><span class="kv-val">'+esc(cfg.goal_pct||0)+'%</span></div>'
      +'<div class="kv-row"><span class="kv-key">Paused</span><span class="kv-val">'+esc(cfg.paused?'Yes':'No')+'</span></div>'
      +'<div class="kv-row"><span class="kv-key">Referral code</span><span class="kv-val">'+esc(cfg.referral_code||'—')+'</span></div>'
      +'<div class="section-hd">Summary</div>'
      +'<div class="kv-row"><span class="kv-key">Open positions</span><span class="kv-val">'+d.open_positions.length+'</span></div>'
      +'<div class="kv-row"><span class="kv-key">Closed trades</span><span class="kv-val">'+d.closed_positions_count+'</span></div>'
      +'<div class="kv-row"><span class="kv-key">Active alerts</span><span class="kv-val">'+d.alerts.length+'</span></div>'
      +'<div class="kv-row"><span class="kv-key">Watchlist items</span><span class="kv-val">'+d.watchlist.length+'</span></div>';
    if(d.watchlist.length){
      h+='<div class="section-hd">Watchlist</div><div style="font-size:12px;color:var(--muted2)">'+d.watchlist.map(function(w){return esc(w.ticker||w);}).join(', ')+'</div>';
    }
  }else if(t==='positions'){
    if(!d.open_positions.length){h='<p class="empty">No open positions</p>';}
    else{h=d.open_positions.map(function(p){
      var ticker=p.ticker||p.symbol||'?';
      var pnl=p.pnl_pct!=null?' · P&L: '+(p.pnl_pct>0?'+':'')+p.pnl_pct.toFixed(1)+'%':'';
      return'<div class="pos-row"><b>'+esc(ticker)+'</b>'+pnl
        +'<br><span style="color:var(--muted)">entry $'+(p.entry_price||'—')+' · stop $'+(p.stop_loss||'—')+' · tgt $'+(p.target_price||'—')+'</span></div>';
    }).join('');}
  }else if(t==='alerts'){
    if(!d.alerts.length){h='<p class="empty">No active alerts</p>';}
    else{h=d.alerts.map(function(a){
      var dir=a.direction==='above'?'↑':'↓';
      return'<div class="pos-row">'+dir+' <b>'+esc(a.ticker)+'</b> @ $'+a.price
        +(a.auto?'<span style="font-size:10px;background:rgba(255,255,255,.08);padding:1px 5px;border-radius:4px;margin-left:6px">auto</span>':'')+'</div>';
    }).join('');}
  }else if(t==='logs'){
    if(!d.logs.length){h='<p class="empty">No activity logged yet</p>';}
    else{
      h='<div style="font-size:11px;color:var(--muted);margin-bottom:8px">'+d.logs.length+' events (newest first)</div>';
      h+=d.logs.map(function(l){
        var lvlColor=l.level==='error'?'color:var(--red)':l.level==='warn'?'color:var(--amber)':'';
        var typeCls='lt-'+(l.type||'command');
        return'<div class="log-row" style="'+lvlColor+'">'
          +'<span class="log-ts">'+age(l.ts)+'</span>'
          +'<span class="log-type '+typeCls+'">'+esc(l.type||'')+'</span>'
          +'<span class="log-detail">'+esc(l.detail)+'</span>'
          +'</div>';
      }).join('');
    }
    if(d.feedback.length){
      h+='<div class="section-hd">Their feedback</div>';
      h+=d.feedback.map(function(f){
        return'<div class="fb-row"><div class="fb-meta">'+age(f.submitted_at)+'</div><div class="fb-text">'+esc(f.text)+'</div></div>';
      }).join('');
    }
  }else if(t==='actions'){
    var banBtn=d.is_banned
      ?'<button class="btn-success" onclick="doAction(\\'unban\\')">Unban user</button>'
      :'<button class="btn-danger" onclick="doAction(\\'ban\\')">Ban user</button>';
    h='<div class="section-hd">Send message</div>'
      +'<textarea class="msg-area" id="dmsg" placeholder="Type a message to this user…"></textarea>'
      +'<div class="action-row"><button class="btn-sm" onclick="doAction(\\'message\\')">Send message</button>'+banBtn+'</div>'
      +'<div id="daction-status" style="font-size:12px;color:var(--green);margin-top:8px"></div>'
      +'<div class="section-hd" style="margin-top:16px">User IDs</div>'
      +'<div class="kv-row"><span class="kv-key">chat_id</span>'
      +'<button onclick="navigator.clipboard.writeText(\\''+d.chat_id+'\\');this.textContent=\\'✓ Copied\\';setTimeout(()=>this.textContent=\\''+d.chat_id+'\\',2000)" '
      +'style="font-size:11px;background:rgba(255,255,255,.07);border:1px solid var(--border2);color:var(--text);border-radius:6px;padding:3px 8px;cursor:pointer">'+d.chat_id+'</button></div>';
  }
  document.getElementById('dpanel').innerHTML=h;
}

async function doAction(act){
  var cid=_dData.chat_id;
  var status=document.getElementById('daction-status');
  if(act==='message'){
    var txt=document.getElementById('dmsg').value.trim();
    if(!txt){status.style.color='var(--red)';status.textContent='Enter a message first.';return;}
    var r=await fetch('/admin/user/'+cid+'/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:txt})});
    status.style.color='var(--green)';status.textContent='Message sent!';
    document.getElementById('dmsg').value='';
  }else if(act==='ban'||act==='unban'){
    if(!confirm((act==='ban'?'Ban':'Unban')+' this user?'))return;
    await fetch('/admin/user/'+cid+'/'+act,{method:'POST'});
    status.style.color='var(--amber)';status.textContent='Done — reload to refresh.';
    openUser(cid);
  }
}

function closeDrawer(){
  document.getElementById('drawer').className='';
  document.getElementById('overlay').className='';
  _dData=null;
}

async function load(){
  var r;
  try{r=await fetch('/admin/data');}
  catch(e){document.getElementById('root').innerHTML='<p class="empty">Network error</p>';return;}
  if(r.status===302||r.status===401){window.location='/admin/login';return;}
  if(!r.ok){document.getElementById('root').innerHTML='<p class="empty">Error '+r.status+'</p>';return;}
  var d=await r.json();
  document.getElementById('root').innerHTML=
    metrics(d.stats)
    +pendingSection(d.pending)
    +broadcastBar()
    +'<div class="grid2">'
      +'<div class="card"><div class="card-title">Users <span style="font-weight:400;color:var(--muted)">(click to inspect)</span></div>'+usersList(d.users)+'</div>'
      +'<div class="card"><div class="card-title">Cron health</div>'+cron(d.cron,d.last_morning_run)+'</div>'
    +'</div>'
    +'<div class="grid2">'
      +'<div class="card"><div class="card-title">Today&rsquo;s picks</div>'+picksPanel(d.picks)+'</div>'
      +'<div class="card"><div class="card-title">Feedback</div>'+feedback(d.feedback)+'</div>'
    +'</div>';
  document.getElementById('ts').textContent='Updated '+new Date().toLocaleTimeString();
}

load();
setInterval(load,60000);
</script>
</body>
</html>"""


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route("/admin/login")
def admin_login():
    return _ADMIN_LOGIN_HTML, 200


@app.route("/admin/github")
def admin_github_redirect():
    """Kick off GitHub OAuth — redirect browser to GitHub authorization page."""
    client_id = os.environ.get("GITHUB_CLIENT_ID", "")
    if not client_id:
        return "GITHUB_CLIENT_ID not configured", 500
    # state token prevents CSRF
    state = _sec.token_urlsafe(16)
    session["oauth_state"] = state
    params = f"client_id={client_id}&scope=read:user&state={state}"
    return redirect(f"https://github.com/login/oauth/authorize?{params}")


@app.route("/admin/callback")
def admin_github_callback():
    """GitHub redirects here after user authorizes. Exchange code → token → verify username."""
    error = request.args.get("error")
    if error:
        return redirect("/admin/login?error=oauth")

    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    # CSRF check
    if not state or state != session.pop("oauth_state", None):
        return redirect("/admin/login?error=oauth")

    client_id     = os.environ.get("GITHUB_CLIENT_ID", "")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return "GitHub OAuth not configured", 500

    # Exchange code for access token
    import requests as _req
    token_resp = _req.post(
        "https://github.com/login/oauth/access_token",
        json={"client_id": client_id, "client_secret": client_secret, "code": code},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    access_token = token_resp.json().get("access_token", "")
    if not access_token:
        return redirect("/admin/login?error=oauth")

    # Fetch GitHub user info
    user_resp = _req.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=10,
    )
    github_username = user_resp.json().get("login", "")

    # Check against allowed admin username
    allowed = os.environ.get("GITHUB_ADMIN_USERNAME", "")
    if not allowed or github_username.lower() != allowed.lower():
        return redirect("/admin/login?error=forbidden")

    session["admin"] = True
    session.permanent = False
    return redirect("/admin")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")


@app.route("/admin")
@_require_admin
def admin_dashboard():
    return _ADMIN_DASH_HTML, 200


def _enrich_feedback(entries: list, users: list) -> list:
    """Merge live user stats into feedback entries for admin triage."""
    user_map = {str(u["id"]): u for u in users}
    result = []
    for f in entries:
        u = user_map.get(str(f.get("chat_id", "")), {})
        result.append({
            **f,
            "open_positions": u.get("open_positions", "?"),
            "last_seen":      u.get("last_seen", ""),
            "is_active":      u.get("is_active", False),
        })
    return result


@app.route("/admin/data")
@_require_admin
def admin_data():
    """Return JSON payload for the dashboard."""
    from config_manager import (
        get_allowed_users, get_user_config, load_picks,
        load_user_trade_log, load_feedback, get_config,
        count_unread_feedback, mark_feedback_read,
    )
    # Viewing the admin panel counts as reading — clear unread flag
    try:
        mark_feedback_read()
    except Exception:
        pass

    cfg     = get_config()
    owner   = os.environ.get("TELEGRAM_CHAT_ID", "")
    now_utc = _dt.now(_tz.utc)
    users   = []
    total_open   = 0
    active_today = 0

    for uid in get_allowed_users():
        try:
            ucfg = get_user_config(uid)
            log  = load_user_trade_log(uid)
            open_pos  = len(log.get("open", []))
            total_open += open_pos
            last_seen  = ucfg.get("last_seen", "")
            is_active  = False
            if last_seen:
                try:
                    delta = (now_utc - _dt.fromisoformat(last_seen)).total_seconds()
                    is_active = delta < 86400
                except Exception:
                    pass
            if is_active:
                active_today += 1
            users.append({
                "id":            uid,
                "first_name":    ucfg.get("first_name", ""),
                "username":      ucfg.get("username", ""),
                "last_seen":     last_seen,
                "paused":        bool(ucfg.get("paused")),
                "open_positions": open_pos,
                "is_admin":      uid == owner,
                "is_active":     is_active,
            })
        except Exception as exc:
            print(f"[admin/data] user {uid} failed: {exc}")

    picks_raw = load_picks() or {}
    stocks = picks_raw.get("stocks",      {})
    crypto = picks_raw.get("crypto",      {})
    etfs   = picks_raw.get("etfs",        {})
    comms  = picks_raw.get("commodities", {})
    st      = stocks.get("short_term", [])
    lt      = stocks.get("long_term",  [])
    cst     = crypto.get("short_term", [])
    clt     = crypto.get("long_term",  [])
    est     = etfs.get("short_term",   [])
    elt     = etfs.get("long_term",    [])
    comm_st = comms.get("short_term",  [])
    comm_lt = comms.get("long_term",   [])
    opts    = picks_raw.get("options_plays", [])
    total_picks = len(st) + len(lt) + len(cst) + len(clt) + len(est) + len(elt) + len(comm_st) + len(comm_lt)

    cron_keys = [
        "prescreener", "premarket", "morning", "confirmation",
        "close_check", "eod_summary", "weekly", "week_ahead", "price_alerts",
    ]
    cron = {k: cfg.get(f"cron_last_{k}", "") for k in cron_keys}

    # Pending users awaiting approval
    from config_manager import get_pending_users
    pending_raw = get_pending_users()
    pending = [
        {"chat_id": cid, **info}
        for cid, info in pending_raw.items()
    ]

    return jsonify({
        "stats": {
            "total_users":      len(users),
            "active_today":     active_today,
            "total_picks":      total_picks,
            "open_positions":   total_open,
            "unread_feedback":  count_unread_feedback(),
            "pending_count":    len(pending),
        },
        "users":   users,
        "pending": pending,
        "picks": {
            "stocks_st":      st,      "stocks_lt": lt,
            "crypto_st":      cst,     "crypto_lt": clt,
            "etfs_st":        est,     "etfs_lt":   elt,
            "commodities_st": comm_st, "commodities_lt": comm_lt,
            "options_plays":  opts,
        },
        "feedback":         _enrich_feedback(load_feedback()[:20], users),
        "cron":             cron,
        "last_morning_run": cfg.get("last_morning_run", ""),
    })


# ── Admin action routes ───────────────────────────────────────────────────────

@app.route("/admin/user/<chat_id>")
@_require_admin
def admin_user_detail(chat_id):
    """Full user snapshot for the detail drawer."""
    from config_manager import (
        get_user_config, load_user_trade_log, get_user_logs,
        load_feedback, get_allowed_users, get_banned_users,
    )
    ucfg  = get_user_config(chat_id)
    log   = load_user_trade_log(chat_id)
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")

    alerts = []
    try:
        from price_alert_manager import PriceAlertManager
        alerts = PriceAlertManager(chat_id).list_alerts()
    except Exception:
        pass

    all_fb   = load_feedback()
    user_fb  = [f for f in all_fb if str(f.get("chat_id", "")) == str(chat_id)]
    user_logs = get_user_logs(chat_id, limit=100)

    return jsonify({
        "chat_id":    chat_id,
        "first_name": ucfg.get("first_name", ""),
        "username":   ucfg.get("username", ""),
        "last_seen":  ucfg.get("last_seen", ""),
        "is_admin":   chat_id == owner,
        "is_banned":  chat_id in get_banned_users(),
        "is_allowed": chat_id in get_allowed_users(),
        "config": {
            "risk_profile":  ucfg.get("risk_profile", "moderate"),
            "budget":        ucfg.get("budget", 0),
            "paused":        bool(ucfg.get("paused")),
            "notifications": ucfg.get("notifications", {}),
            "goal_pct":      ucfg.get("goal_pct", 0),
            "referral_code": ucfg.get("referral_code", ""),
        },
        "open_positions":        log.get("open", []),
        "closed_positions_count": len(log.get("closed", [])),
        "alerts":    alerts,
        "watchlist": ucfg.get("watchlist", []),
        "feedback":  user_fb[:10],
        "logs":      user_logs,
    })


@app.route("/admin/user/<chat_id>/approve", methods=["POST"])
@_require_admin
def admin_approve_user(chat_id):
    from config_manager import add_allowed_user, remove_pending_user
    add_allowed_user(chat_id)
    remove_pending_user(chat_id)
    send_message(
        "✅ <b>Access approved!</b>\n\nWelcome to StockPulz. Send /start to begin.",
        chat_id=chat_id,
    )
    return jsonify({"ok": True})


@app.route("/admin/user/<chat_id>/reject", methods=["POST"])
@_require_admin
def admin_reject_user(chat_id):
    from config_manager import remove_pending_user
    remove_pending_user(chat_id)
    send_message(
        "Sorry, your access request was not approved at this time.",
        chat_id=chat_id,
    )
    return jsonify({"ok": True})


@app.route("/admin/user/<chat_id>/ban", methods=["POST"])
@_require_admin
def admin_ban_user(chat_id):
    from config_manager import ban_user
    try:
        ban_user(chat_id)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/admin/user/<chat_id>/unban", methods=["POST"])
@_require_admin
def admin_unban_user(chat_id):
    from config_manager import unban_user, add_allowed_user
    unban_user(chat_id)
    add_allowed_user(chat_id)
    return jsonify({"ok": True})


@app.route("/admin/user/<chat_id>/message", methods=["POST"])
@_require_admin
def admin_message_user(chat_id):
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No message text"}), 400
    send_message(f"📣 <b>Message from admin:</b>\n\n{text}", chat_id=chat_id)
    return jsonify({"ok": True})


@app.route("/admin/broadcast", methods=["POST"])
@_require_admin
def admin_broadcast():
    from config_manager import get_allowed_users
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "No message text"}), 400
    sent, failed = 0, 0
    for uid in get_allowed_users():
        try:
            send_message(f"📣 <b>Announcement:</b>\n\n{text}", chat_id=uid)
            sent += 1
        except Exception:
            failed += 1
    return jsonify({"ok": True, "sent": sent, "failed": failed})


@app.route("/admin/run_agent", methods=["POST"])
@_require_admin
def admin_run_agent():
    """Trigger any agent run mode on demand from the admin dashboard."""
    import threading, subprocess, sys as _sys
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", "morning")).strip()
    mock = bool(body.get("mock", False))
    valid = {"morning","confirmation","close_check","eod_summary","prescreener",
             "premarket","digest","weekly","week_ahead","price_alerts",
             "vix_check","news_check","macro_alert","midday_check","watchdog"}
    if mode not in valid:
        return jsonify({"error": f"unknown mode: {mode}"}), 400
    env = {**os.environ, "RUN_MODE": mode}
    if mock:
        env["MOCK_DATA"] = "true"
    def _run():
        try:
            subprocess.run([_sys.executable, "agent.py"], env=env,
                           cwd=os.path.dirname(os.path.abspath(__file__)),
                           timeout=600)
        except Exception as exc:
            print(f"[admin_run_agent] {mode} error: {exc}")
    threading.Thread(target=_run, daemon=True).start()
    print(f"[admin_run_agent] Spawned mode={mode} mock={mock}")
    return jsonify({"ok": True, "mode": mode, "mock": mock})


@app.route("/admin/fix_ticker", methods=["POST"])
@_require_admin
def admin_fix_ticker():
    """Rename a ticker across all users' open + closed trade logs.
    Body: {"from": "AVERY", "to": "AVY"}
    One-time migration tool — idempotent if run again."""
    from config_manager import get_allowed_users, load_user_trade_log, save_user_trade_log
    body    = request.get_json(silent=True) or {}
    old_t   = str(body.get("from", "")).upper().strip()
    new_t   = str(body.get("to",   "")).upper().strip()
    if not old_t or not new_t:
        return jsonify({"error": "both 'from' and 'to' are required"}), 400

    # Include owner (TELEGRAM_CHAT_ID) who may not be in allowed_users list
    owner    = os.environ.get("TELEGRAM_CHAT_ID", "")
    all_uids = list({*get_allowed_users(), *([ owner] if owner else [])})

    total = 0
    for uid in all_uids:
        log     = load_user_trade_log(uid)
        changed = 0
        for trade in log.get("open", []) + log.get("closed", []):
            if trade.get("ticker") == old_t:
                trade["ticker"] = new_t
                changed += 1
        if changed:
            save_user_trade_log(uid, log)
            total += changed

    return jsonify({"ok": True, "renamed": total, "from": old_t, "to": new_t})


# ── Mini App routes ───────────────────────────────────────────────────────────

_MINIAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp")


@app.route("/miniapp")
def miniapp_index():
    """Serve the Telegram Mini App HTML."""
    return send_from_directory(_MINIAPP_DIR, "index.html")


@app.route("/miniapp/manifest.json")
def miniapp_manifest():
    """Serve the PWA web app manifest."""
    return send_from_directory(_MINIAPP_DIR, "manifest.json",
                               mimetype="application/manifest+json")


def _miniapp_auth() -> str | None:
    """
    Extract and validate chat_id from the Mini App request.
    Returns chat_id string if authorised, None otherwise.

    Priority:
    1. chat_id param passed explicitly from JS (initDataUnsafe.user.id)
    2. Parse user.id out of the raw init_data string (URL-encoded, Telegram-signed)
    """
    if request.method == "POST":
        body      = request.get_json(silent=True) or {}
        # JS api() helper always appends auth to the URL; body.get() is primary,
        # request.args fallback handles bodyless POSTs (e.g. clear_alerts).
        chat_id   = str(body.get("chat_id", "") or request.args.get("chat_id", "")).strip()
        init_data = body.get("init_data", "") or request.args.get("init_data", "")
    else:
        chat_id   = str(request.args.get("chat_id", "")).strip()
        init_data = request.args.get("init_data", "")

    # Fallback: parse user id from initData if chat_id missing/zero
    if (not chat_id or chat_id == "0") and init_data:
        try:
            from urllib.parse import parse_qs
            import json as _json
            params    = parse_qs(init_data)
            user_json = params.get("user", ["{}"])[0]
            user_obj  = _json.loads(user_json)
            chat_id   = str(user_obj.get("id", "")).strip()
        except Exception as exc:
            print(f"[miniapp_auth] init_data parse failed: {exc}")

    print(f"[miniapp_auth] chat_id={chat_id!r}")

    if not chat_id or chat_id == "0":
        return None

    allowed = get_allowed_users()
    owner   = os.environ.get("TELEGRAM_CHAT_ID", "")
    if chat_id not in allowed and chat_id != owner:
        print(f"[miniapp_auth] {chat_id} not in allowed list {allowed}")
        return None
    return chat_id


_picks_mem_cache = {"data": None, "ts": 0}
_PICKS_CACHE_TTL = 300  # 5 minutes — picks only change once per day
_bot_username_cache = os.environ.get("TELEGRAM_BOT_USERNAME", "")  # set in Render env to skip API call

@app.route("/api/miniapp/picks")
def miniapp_picks():
    """Return today's picks for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    try:
        import time as _time
        import pytz
        from datetime import datetime as _dt
        from config_manager import load_picks, get_user_config

        # Serve from in-process memory cache if fresh (avoids Gist/Supabase round-trip)
        now_mono = _time.monotonic()
        if _picks_mem_cache["data"] is not None and (now_mono - _picks_mem_cache["ts"]) < _PICKS_CACHE_TTL:
            picks = {k: v for k, v in _picks_mem_cache["data"].items() if k != "_meta"}
        else:
            picks = load_picks() or {}
            _picks_mem_cache["data"] = picks
            _picks_mem_cache["ts"]   = now_mono

        ucfg  = get_user_config(chat_id)

        # Filter assets based on user preference
        assets = ucfg.get("assets", "both")
        if assets == "stocks":
            picks.pop("crypto", None)
        elif assets == "crypto":
            picks.pop("stocks", None)
            picks.pop("etfs", None)

        # Attach meta so the frontend can distinguish weekends/no-picks from errors
        now_et     = _dt.now(pytz.timezone("US/Eastern"))
        is_weekend = now_et.weekday() >= 5
        has_picks  = any(picks.get(k) for k in ("stocks", "crypto", "etfs"))

        # Determine picks date from nested entry timestamps (used for stale banner)
        picks_date = None
        try:
            for section_key in ("stocks", "crypto", "etfs"):
                section = picks.get(section_key, {})
                for tf in ("short_term", "long_term"):
                    for p in (section.get(tf) or []):
                        ts = p.get("generated_at") or p.get("date") or p.get("created_at")
                        if ts and (picks_date is None or ts > picks_date):
                            picks_date = ts[:10]  # keep YYYY-MM-DD only
        except Exception:
            pass

        # Include bought tickers so the frontend can restore boughtSet across sessions
        try:
            from config_manager import load_user_trade_log
            _log = load_user_trade_log(chat_id)
            bought_tickers = [t["ticker"] for t in _log.get("open", []) if t.get("ticker")]
        except Exception:
            bought_tickers = []

        picks["_meta"] = {
            "weekend":        is_weekend,
            "has_picks":      has_picks,
            "day":            now_et.strftime("%A"),
            "picks_date":     picks_date,       # ISO date string or None
            "today":          now_et.strftime("%Y-%m-%d"),
            "bought_tickers": bought_tickers,   # open positions — restores boughtSet on reload
        }

        return jsonify(picks)

    except Exception as exc:
        import traceback
        print(f"[miniapp_picks] ERROR: {exc}")
        traceback.print_exc()
        return jsonify({"error": "server_error", "detail": str(exc), "_meta": {"weekend": False, "has_picks": False}}), 500


@app.route("/api/miniapp/picks/history")
def miniapp_picks_history():
    """Return this week's past picks keyed by date, excluding today."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    try:
        from datetime import date
        from config_manager import load_weekly_picks
        weekly = load_weekly_picks()
        today  = date.today().isoformat()
        # Exclude today (served by /api/miniapp/picks) and return sorted newest-first
        history = {k: v for k, v in weekly.items() if k != today}
        sorted_history = dict(sorted(history.items(), reverse=True))
        return jsonify({"history": sorted_history})
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "server_error", "detail": str(exc)}), 500


@app.route("/api/miniapp/positions")
def miniapp_positions():
    """Return open positions with live P&L for the Mini App.

    Prices are fetched in a single batch call (one HTTP round-trip to Alpaca
    regardless of position count) instead of a sequential per-ticker loop.
    Response shape is identical to the previous implementation.
    """
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_user_trade_log
    from datetime import date as _date
    from market_data import get_live_prices as _prices_batch

    log      = load_user_trade_log(chat_id)
    open_pos = log.get("open", [])

    # One batch call for all tickers — O(1) network round-trips regardless of count
    all_syms = [t["ticker"] for t in open_pos]
    live     = _prices_batch(all_syms) if all_syms else {}

    result = []
    today  = _date.today()
    for t in open_pos:
        sym   = t["ticker"]
        entry = t.get("entry_price")
        cur   = live.get(sym)
        pnl   = None
        if entry and cur:
            pnl = round((cur - float(entry)) / float(entry) * 100, 2)
        days = None
        try:
            days = (today - _date.fromisoformat(t.get("opened_date", ""))).days
        except Exception:
            pass
        shares  = t.get("shares")
        pnl_usd = None
        if pnl is not None and entry and shares:
            pnl_usd = round(float(shares) * float(entry) * pnl / 100, 2)
        result.append({
            "ticker":        sym,
            "symbol":        sym,
            "asset_type":    t.get("asset_type", "stock"),
            "timeframe":     t.get("timeframe"),
            "entry_price":   entry,
            "current_price": round(cur, 4) if cur else None,
            "target_price":  t.get("target_price"),
            "stop_loss":     t.get("stop_loss"),
            "pnl_pct":       pnl,
            "pnl_usd":       pnl_usd,
            "shares":        float(shares) if shares is not None else None,
            "days_held":     days,
            "notes":         t.get("notes") or "",
        })

    return jsonify({"positions": result})


@app.route("/api/miniapp/seed_backtest", methods=["POST"])
def miniapp_seed_backtest():
    """
    Admin endpoint: run the backtest and seed simulated trades for a user.
    Runs in a background thread — returns immediately.
    POST body: {"chat_id": "optional — defaults to caller", "days_back": 60}
    """
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    # Only admin can seed for other users; regular users can only seed themselves
    body      = request.get_json(silent=True) or {}
    target_id = str(body.get("chat_id", chat_id))
    days_back = int(body.get("days_back", 60))

    def _run():
        try:
            from backtester import generate_dated_trades
            from config_manager import save_backtest_trades
            trades = generate_dated_trades(days_back=days_back, picks_per_period=3)
            if trades:
                save_backtest_trades(target_id, trades)
                print(f"[webhook] Seeded {len(trades)} backtest trades for {target_id}.")
            else:
                print(f"[webhook] Backtest generated no trades for {target_id}.")
        except Exception as exc:
            print(f"[webhook] seed_backtest error: {exc}")

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": f"Backtest seeding started for {target_id} ({days_back}d). Check Performance tab in ~2 minutes."})


@app.route("/api/miniapp/log_bought", methods=["POST"])
def miniapp_log_bought():
    """Log that the user bought a pick (same as /bought command)."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    body       = request.get_json(silent=True) or {}
    ticker     = str(body.get("ticker", "")).upper().strip()
    if not ticker:
        return jsonify({"error": "missing ticker"}), 400

    from market_data import validate_ticker
    if not validate_ticker(ticker):
        return jsonify({"error": f"'{ticker}' is not a recognised ticker — check the symbol and try again"}), 400

    entry_override     = body.get("entry_price")
    stop_override      = body.get("stop_loss")
    target_override    = body.get("target_price")
    shares_override    = body.get("shares")
    timeframe_override = body.get("timeframe")  # 'short_term' | 'long_term' | None

    from trade_logger import add_holding
    from config_manager import load_picks
    picks = load_picks() or {}
    trade, existed = add_holding(ticker, chat_id, picks=picks,
                                 entry_override=entry_override,
                                 stop_override=stop_override,
                                 target_override=target_override,
                                 shares_override=shares_override,
                                 timeframe_override=timeframe_override)

    # Auto-create a stop-loss alert if a stop is set on the new position
    if not existed:
        stop_for_alert = trade.get("stop_loss")
        if stop_for_alert:
            try:
                from price_alert_manager import add_alert
                add_alert(str(chat_id), ticker, float(stop_for_alert), direction="below", auto=True)
            except ValueError:
                pass  # alert already exists — skip silently
            except Exception as exc:
                print(f"[webhook] auto-stop alert failed (non-critical): {exc}")

    return jsonify({"ok": True, "existed": existed, "trade": trade})


@app.route("/api/miniapp/unlog_bought", methods=["POST"])
def miniapp_unlog_bought():
    """Remove a ticker from the user's bought list / open positions."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    body   = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker", "")).upper().strip()
    if not ticker:
        return jsonify({"error": "missing ticker"}), 400

    from trade_logger import remove_holding
    removed = remove_holding(ticker, chat_id)
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/miniapp/settings")
def miniapp_settings():
    """Return the user's settings for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import get_user_config
    ucfg = get_user_config(chat_id)
    # Notification preference defaults (all on except auto_stop_alerts)
    _notif_defaults = {
        "notif_target_hit":       True,
        "notif_target_approach":  True,
        "notif_watchlist_move":   True,
        "notif_weekly_recap":     True,
        "notif_morning_picks":    True,
        "auto_stop_alerts":       False,
    }
    notif_prefs = {k: ucfg.get(k, v) for k, v in _notif_defaults.items()}
    return jsonify({"settings": {
        "risk_profile":    ucfg.get("risk_profile", "moderate"),
        "assets":          ucfg.get("assets", "both"),
        "stock_budget":    ucfg.get("stock_budget"),
        "crypto_budget":   ucfg.get("crypto_budget"),
        "paused":          ucfg.get("paused", False),
        "stop_loss_pct":   ucfg.get("stop_loss_pct"),
        "target_gain_pct": ucfg.get("target_gain_pct"),
        "watchlist":       ucfg.get("watchlist", []),
        "excluded_sectors": ucfg.get("excluded_sectors", []),
        "onboarded":       ucfg.get("onboarded", False),
        **notif_prefs,
    }})


@app.route("/api/miniapp/settings/update", methods=["POST"])
def miniapp_settings_update():
    """Update a single user config key from the Mini App settings panel."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    from config_manager import update_user_config
    body  = request.get_json(silent=True) or {}
    key   = (body.get("key") or "").strip()
    value = body.get("value")
    # Allowlist of keys the Mini App is allowed to set
    _allowed = {
        "notif_target_hit", "notif_target_approach", "notif_watchlist_move",
        "notif_weekly_recap", "notif_morning_picks", "auto_stop_alerts",
        "risk_profile", "stock_budget", "crypto_budget",
        "stop_loss_pct", "target_gain_pct",
        "quiet_hours_enabled", "quiet_from", "quiet_to",
        "display_name",
    }
    if not key or key not in _allowed:
        return jsonify({"error": f"key '{key}' not allowed"}), 400
    update_user_config(chat_id, key, value)
    return jsonify({"ok": True, "key": key, "value": value})


_CHART_CRYPTO = {
    "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","MATIC",
    "LINK","UNI","ATOM","LTC","BCH","ALGO","XLM","VET","ICP","FIL",
    "TRX","NEAR","OP","ARB","SUI","APT","INJ","SEI","TIA","HYPE",
}


@app.route("/api/miniapp/chart/<ticker>")
def miniapp_chart(ticker):
    """Return daily OHLCV data for the Mini App chart overlay. period: 5d|1mo|3mo|1y"""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    ticker     = ticker.upper()
    asset_type = request.args.get("asset_type", "")
    period     = request.args.get("period", "3mo")
    days_map   = {"5d": 7, "1mo": 35, "3mo": 92, "1y": 370}
    days       = days_map.get(period, 92)
    is_crypto  = asset_type == "crypto" or ticker in _CHART_CRYPTO

    try:
        from market_data import get_ohlcv
        data = get_ohlcv(ticker, days=days)

        # No data — try resolving company name (e.g. "AMAZON" → "AMZN")
        if not data and not is_crypto:
            try:
                from cmd_helpers import _resolve_ticker_candidates
                candidates = _resolve_ticker_candidates(ticker)
                if candidates:
                    resolved = candidates[0]["ticker"].upper()
                    if resolved != ticker:
                        data   = get_ohlcv(resolved)
                        ticker = resolved
            except Exception:
                pass

        if not data:
            return jsonify({"error": "no data", "ticker": ticker}), 404

        # Fetch live intraday price separately so the chart footer shows today's
        # real price, not yesterday's close (daily bars lag by one session).
        live_price = None
        try:
            from market_data import get_live_price as _glp
            live_price = _glp(ticker)
        except Exception:
            pass

        return jsonify({"ticker": ticker, "data": data, "live_price": live_price})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/miniapp/define")
def miniapp_define():
    """Plain-English definition of a financial term via Haiku."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    term = request.args.get("term", "").strip()
    if not term or len(term) > 80:
        return jsonify({"error": "invalid term"}), 400

    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=(
                "You are a friendly financial educator. Explain the term in 2-3 short "
                "sentences of plain English — zero jargon, zero disclaimers. "
                "First sentence = one-line definition. "
                "Second sentence = why it matters for trading. "
                "Third sentence (optional) = a simple real-world example with numbers. "
                "End with one short line starting 'StockPulz uses this to:'"
            ),
            messages=[{"role": "user", "content": f"Explain this financial term: {term}"}],
        )
        return jsonify({"term": term, "explanation": resp.content[0].text.strip()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/miniapp/close_position", methods=["POST"])
def miniapp_close_position():
    """Close (sell) an open position from the Mini App, recording P&L."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    body   = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker", "")).upper().strip()
    price  = body.get("price")
    if not ticker:
        return jsonify({"error": "missing ticker"}), 400

    exit_price = float(price) if price else None

    from trade_logger import close_trade
    result = close_trade(ticker, chat_id, exit_price=exit_price)
    if result is None:
        return jsonify({"ok": False, "error": "position not found"}), 404
    return jsonify({"ok": True, "trade": result})


@app.route("/api/miniapp/remove_position", methods=["POST"])
def miniapp_remove_position():
    """Remove a position without recording a trade (for test entries, mistakes, etc.)."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    body   = request.get_json(silent=True) or {}
    ticker = str(body.get("ticker", "")).upper().strip()
    if not ticker:
        return jsonify({"error": "missing ticker"}), 400
    from trade_logger import remove_holding
    ok = remove_holding(ticker, chat_id)
    return jsonify({"ok": ok})


@app.route("/api/miniapp/toggle_paused", methods=["POST"])
def miniapp_toggle_paused():
    """Toggle the user's paused state from the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import get_user_config, save_user_config
    body   = request.get_json(silent=True) or {}
    paused = bool(body.get("paused", False))
    ucfg   = get_user_config(chat_id)
    ucfg["paused"] = paused
    save_user_config(chat_id, ucfg)
    return jsonify({"ok": True, "paused": paused})


@app.route("/api/miniapp/feedback", methods=["POST"])
def miniapp_feedback():
    """Submit feedback from the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty feedback"}), 400

    platform = (body.get("platform") or "").strip()[:32]   # e.g. "ios", "android", "tdesktop"
    page     = (body.get("page")     or "").strip()[:64]   # mini app tab active at send time

    from config_manager import add_feedback
    # Fetch user profile for context
    username, first_name = "", ""
    try:
        import requests as _req
        r = _req.get(
            f"{os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')}/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}/getChat",
            params={"chat_id": chat_id}, timeout=5,
        )
        result     = r.json().get("result", {})
        first_name = result.get("first_name", "")
        username   = result.get("username", "")
    except Exception:
        pass

    add_feedback(chat_id, text, username=username, first_name=first_name,
                 platform=platform, page=page)
    # Feedback is stored and surfaced in the admin mini app inbox — no Telegram alert sent.

    return jsonify({"ok": True})


@app.route("/api/miniapp/history")
def miniapp_history():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log, load_backtest_trades
    log    = load_user_trade_log(chat_id)
    closed = sorted(log.get("closed", []), key=lambda t: t.get("closed_date",""), reverse=True)

    # Merge simulated backtest trades when user has fewer than 5 real trades
    has_simulated = False
    if len(closed) < 5:
        bt = load_backtest_trades(chat_id)
        if bt:
            merged = sorted(closed + bt, key=lambda t: t.get("closed_date",""), reverse=True)
            has_simulated = True
            return jsonify({"ok": True, "trades": merged, "has_simulated": True})

    return jsonify({"ok": True, "trades": closed, "has_simulated": False})


@app.route("/api/miniapp/pnl_history")
def miniapp_pnl_history():
    """Return daily cumulative P&L from closed trades for portfolio chart."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log, load_backtest_trades
    log    = load_user_trade_log(chat_id)
    closed = log.get("closed", [])

    # Merge backtest if few real trades
    if len(closed) < 5:
        bt = load_backtest_trades(chat_id)
        if bt:
            closed = sorted(closed + bt, key=lambda t: t.get("closed_date", ""))

    # Build daily cumulative P&L (sum gain_usd by close date)
    from collections import defaultdict
    daily = defaultdict(float)
    for t in closed:
        d = t.get("closed_date", "")
        g = float(t.get("gain_usd") or t.get("return_pct") or 0)
        if d:
            daily[d] += g

    # Build sorted cumulative series
    points = []
    cum = 0.0
    for d in sorted(daily.keys()):
        cum += round(daily[d], 2)
        points.append({"date": d, "cumulative": round(cum, 2), "daily": round(daily[d], 2)})

    total_pnl  = round(cum, 2)
    total_trades = len(closed)
    wins = len([t for t in closed if float(t.get("return_pct") or 0) > 0])
    win_rate = round(wins / total_trades * 100) if total_trades else 0

    # Compute current win/loss streak from most recent trades
    current_streak = 0
    if closed:
        sorted_closed = sorted(closed, key=lambda t: t.get("closed_date", ""))
        last_outcome = None
        for t in reversed(sorted_closed):
            is_win = float(t.get("return_pct") or 0) > 0
            if last_outcome is None:
                last_outcome = is_win
                current_streak = 1 if is_win else -1
            elif is_win == last_outcome:
                current_streak = current_streak + 1 if is_win else current_streak - 1
            else:
                break

    return jsonify({"ok": True, "points": points, "total_pnl": total_pnl,
                    "total_trades": total_trades, "win_rate": win_rate,
                    "current_streak": current_streak})


@app.route("/api/miniapp/alerts")
def miniapp_alerts():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from price_alert_manager import _load_alerts
    all_alerts = _load_alerts()
    alerts  = all_alerts.get(str(chat_id), [])
    history = all_alerts.get(f"_history_{chat_id}", [])
    return jsonify({"ok": True, "alerts": alerts, "triggered_history": history})


@app.route("/api/miniapp/alerts", methods=["POST"])
def miniapp_alerts_post():
    """Add or remove a price alert from the Mini App watchlist."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    body   = request.get_json(silent=True) or {}
    action = body.get("action", "add")   # "add" | "remove"
    ticker = str(body.get("ticker", "")).strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    try:
        if action == "remove":
            from price_alert_manager import remove_alert
            target_raw = body.get("target")
            target     = float(target_raw) if target_raw is not None else None
            msg = remove_alert(str(chat_id), ticker, target_price=target)
            return jsonify({"ok": True, "message": msg})
        else:
            from price_alert_manager import add_alert
            target_raw = body.get("target")
            if target_raw is None:
                return jsonify({"error": "target price required"}), 400
            direction = body.get("direction", "auto")
            recurring = bool(body.get("recurring", False))
            msg = add_alert(str(chat_id), ticker, float(target_raw),
                            direction=direction, recurring=recurring)
            return jsonify({"ok": True, "message": msg})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Quote cache: 60-second TTL, avoids double yfinance round-trips ────────────
_quote_cache: dict = {}   # ticker → {"price", "change_pct", "ts"}
_QUOTE_TTL = 60           # seconds

def _fast_quote(ticker: str, crypto_set) -> tuple[float | None, float | None]:
    """Single yfinance fast_info call that returns (price, change_pct).
    Cached for _QUOTE_TTL seconds to avoid repeated slow lookups."""
    now = time.time()
    hit = _quote_cache.get(ticker)
    if hit and now - hit["ts"] < _QUOTE_TTL:
        return hit["price"], hit["change_pct"]
    is_crypto = ticker in crypto_set

    # ── Crypto: always use CoinGecko (better coverage than yfinance) ──────────
    if is_crypto:
        try:
            sr = requests.get("https://api.coingecko.com/api/v3/search",
                              params={"query": ticker}, timeout=8)
            if sr.ok:
                coins = sr.json().get("coins", [])
                coin  = next((c for c in coins[:10] if c.get("symbol", "").upper() == ticker), None)
                if coin:
                    pr = requests.get("https://api.coingecko.com/api/v3/simple/price",
                                      params={"ids": coin["id"], "vs_currencies": "usd",
                                              "include_24hr_change": "true"}, timeout=8)
                    if pr.ok:
                        data   = pr.json().get(coin["id"], {})
                        price  = data.get("usd")
                        change = data.get("usd_24h_change")
                        if price:
                            price  = float(price)
                            change = round(float(change), 2) if change else None
                            _quote_cache[ticker] = {"price": price, "change_pct": change, "ts": now}
                            return price, change
        except Exception:
            pass
        return None, None

    # ── Stocks/ETFs/commodities: use yfinance ─────────────────────────────────
    try:
        import yfinance as _yf
        fi    = _yf.Ticker(ticker).fast_info
        price = getattr(fi, "last_price", None)
        prev  = getattr(fi, "previous_close", None)
        if price:
            price  = float(price)
            change = round((price - float(prev)) / float(prev) * 100, 2) if prev and float(prev) > 0 else None
            _quote_cache[ticker] = {"price": price, "change_pct": change, "ts": now}
            return price, change
    except Exception:
        pass
    # Unknown ticker — try CoinGecko as last resort (catches exotic crypto not in crypto_set)
    try:
        sr = requests.get("https://api.coingecko.com/api/v3/search",
                          params={"query": ticker}, timeout=8)
        if sr.ok:
            coins = sr.json().get("coins", [])
            coin  = next((c for c in coins[:10] if c.get("symbol", "").upper() == ticker), None)
            if coin:
                pr = requests.get("https://api.coingecko.com/api/v3/simple/price",
                                  params={"ids": coin["id"], "vs_currencies": "usd",
                                          "include_24hr_change": "true"}, timeout=8)
                if pr.ok:
                    data   = pr.json().get(coin["id"], {})
                    price  = data.get("usd")
                    change = data.get("usd_24h_change")
                    if price:
                        price  = float(price)
                        change = round(float(change), 2) if change else None
                        _quote_cache[ticker] = {"price": price, "change_pct": change, "ts": now}
                        return price, change
    except Exception:
        pass
    return None, None


@app.route("/api/miniapp/names")
def miniapp_names():
    """Batch company name lookup. ?tickers=AMZN,BNB,FE  → {AMZN: "Amazon.com", BNB: "Binance Coin", ...}
    Uses market_data.get_company_names which handles crypto via static dict (instant)
    and stocks via yfinance with a process-level cache (no repeated HTTP calls).
    """
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    raw     = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()][:25]
    if not tickers: return jsonify({}), 200

    from market_data import get_company_names
    result = get_company_names(tickers)
    return jsonify(result)


@app.route("/api/miniapp/quote")
def miniapp_quote():
    """Return current price for a single ticker — used by the New Alert form.
    If the raw input isn't a valid ticker (e.g. 'AMAZON'), resolves via Haiku
    and returns the resolved ticker so the frontend can update the input field.
    Single yfinance call with 60-second cache avoids the previous double-fetch lag.
    """
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker: return jsonify({"error": "ticker required"}), 400
    try:
        from price_alert_manager import _CRYPTO_SYMBOLS as _ALERT_CRYPTO
        def _get_name(sym):
            """Return shortName for a ticker, with a short cache.
            Uses fast_info (no .info call — avoids 401s on cloud IPs)."""
            hit = _quote_cache.get(f"__name_{sym}")
            if hit and time.time() - hit["ts"] < 3600:
                return hit["v"]
            try:
                import yfinance as _yf2
                fi = _yf2.Ticker(sym).fast_info
                # fast_info doesn't have shortName; use display_name or fall back to sym
                n = getattr(fi, "display_name", None) or ""
                _quote_cache[f"__name_{sym}"] = {"v": n, "ts": time.time()}
                return n
            except Exception:
                return ""

        # Static name→ticker fast path — works offline, covers most common inputs
        _COMMON_NAMES: dict[str, str] = {
            "TESLA": "TSLA", "APPLE": "AAPL", "AMAZON": "AMZN", "GOOGLE": "GOOGL",
            "ALPHABET": "GOOGL", "MICROSOFT": "MSFT", "META": "META", "FACEBOOK": "META",
            "NVIDIA": "NVDA", "NETFLIX": "NFLX", "DISNEY": "DIS", "UBER": "UBER",
            "LYFT": "LYFT", "COINBASE": "COIN", "PALANTIR": "PLTR", "SNOWFLAKE": "SNOW",
            "SALESFORCE": "CRM", "SHOPIFY": "SHOP", "SQUARE": "SQ", "BLOCK": "SQ",
            "TWITTER": "X", "PAYPAL": "PYPL", "ROBINHOOD": "HOOD", "ROBLOX": "RBLX",
            "AMD": "AMD", "INTEL": "INTC", "QUALCOMM": "QCOM", "BROADCOM": "AVGO",
            "TSMC": "TSM", "SAMSUNG": "005930.KS", "SONY": "SONY", "TOYOTA": "TM",
            "ALIBABA": "BABA", "BAIDU": "BIDU", "TENCENT": "TCEHY",
            "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "CARDANO": "ADA",
            "DOGECOIN": "DOGE", "RIPPLE": "XRP", "POLKADOT": "DOT", "AVALANCHE": "AVAX",
            "CHAINLINK": "LINK", "LITECOIN": "LTC", "BINANCE": "BNB", "UNISWAP": "UNI",
            "SHIBA": "SHIB", "SHIBAINUTOKEN": "SHIB",
            "GOLD": "GLD", "SILVER": "SLV", "OIL": "USO", "SPY": "SPY", "QQQ": "QQQ",
            "JPMORGAN": "JPM", "BANKOFAMERICA": "BAC", "WELLSFARGO": "WFC",
            "GOLDMAN": "GS", "MORGAN": "MS", "VISA": "V", "MASTERCARD": "MA",
            "JOHNSON": "JNJ", "PFIZER": "PFE", "MODERNA": "MRNA", "ABBVIE": "ABBV",
            "EXXON": "XOM", "CHEVRON": "CVX", "BOEING": "BA", "CATERPILLAR": "CAT",
        }
        # Normalise: strip spaces so "BANK OF AMERICA" → "BANKOFAMERICA"
        _normalised = ticker.replace(" ", "").replace(".", "").replace("&", "")
        if _normalised in _COMMON_NAMES:
            ticker = _COMMON_NAMES[_normalised]

        price, change_pct = _fast_quote(ticker, _ALERT_CRYPTO)
        if price is not None:
            return jsonify({"ticker": ticker, "price": round(price, 6),
                            "change_pct": change_pct, "name": _get_name(ticker)})
        # No price — try resolving as a company name via Haiku
        try:
            from cmd_helpers import _resolve_ticker_candidates
            candidates = _resolve_ticker_candidates(ticker)
            if candidates:
                resolved = candidates[0]["ticker"].upper()
                if resolved != ticker:
                    price, change_pct = _fast_quote(resolved, _ALERT_CRYPTO)
                    if price is not None:
                        return jsonify({"ticker": resolved, "price": round(price, 6),
                                        "change_pct": change_pct, "name": _get_name(resolved)})
        except Exception:
            pass
        return jsonify({"error": f"No price found for {ticker}"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/news/<ticker>")
def miniapp_news(ticker: str):
    """Return up to 5 recent news headlines for a ticker via yfinance."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    ticker = ticker.strip().upper()
    if not ticker: return jsonify({"error": "ticker required"}), 400
    try:
        import yfinance as _yf
        from price_alert_manager import _CRYPTO_SYMBOLS as _ALERT_CRYPTO
        yf_sym = f"{ticker}-USD" if ticker in _ALERT_CRYPTO else ticker
        raw    = _yf.Ticker(yf_sym).news or []
        items  = []
        for n in raw[:5]:
            title = n.get("title") or n.get("content", {}).get("title", "")
            url   = n.get("link") or n.get("content", {}).get("canonicalUrl", {}).get("url", "")
            pub   = n.get("providerPublishTime") or n.get("content", {}).get("pubDate", "")
            src   = n.get("publisher") or n.get("content", {}).get("provider", {}).get("displayName", "")
            if title and url:
                items.append({"title": title, "url": url, "published": pub, "source": src})
        return jsonify({"ticker": ticker, "news": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/add_alert", methods=["POST"])
def miniapp_add_alert():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from price_alert_manager import add_alert
    body = request.get_json(silent=True) or {}
    ticker    = (body.get("ticker") or "").strip().upper()
    target    = body.get("target")
    direction = body.get("direction", "auto")
    recurring = bool(body.get("recurring", False))
    if not ticker or target is None:
        return jsonify({"error": "ticker and target required"}), 400
    try:
        msg = add_alert(chat_id, ticker, float(target), direction, recurring=recurring)
        return jsonify({"ok": True, "message": msg})
    except ValueError as e:
        # add_alert raises ValueError for invalid ticker or duplicate — return 400
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/remove_alert", methods=["POST"])
def miniapp_remove_alert():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from price_alert_manager import remove_alert
    body = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    target = body.get("target")
    if not ticker: return jsonify({"error": "ticker required"}), 400
    try:
        msg = remove_alert(chat_id, ticker, float(target) if target is not None else None)
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/clear_alerts", methods=["POST"])
def miniapp_clear_alerts():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from price_alert_manager import clear_alerts
    count = clear_alerts(chat_id)
    return jsonify({"ok": True, "removed": count})


@app.route("/api/miniapp/paper")
def miniapp_paper():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_paper
    data = load_user_paper(chat_id)
    # Enrich positions with live prices
    from market_data import get_live_prices
    positions = data.get("positions", [])
    tickers   = list({p["ticker"] for p in positions})
    prices    = get_live_prices(tickers) if tickers else {}
    for p in positions:
        p["live_price"] = prices.get(p["ticker"])
        if p.get("live_price") and p.get("entry_price"):
            p["pnl_pct"] = round((p["live_price"] - p["entry_price"]) / p["entry_price"] * 100, 2)
        else:
            p["pnl_pct"] = None
    history = sorted(data.get("history", []), key=lambda t: t.get("closed_date",""), reverse=True)
    return jsonify({
        "ok": True,
        "positions": positions,
        "history":   history,
        "cash":      data.get("cash", 0),
        "starting_cash": data.get("starting_cash", 10000),
    })


@app.route("/api/miniapp/update_settings", methods=["POST"])
def miniapp_update_settings():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import get_user_config, save_user_config
    body = request.get_json(silent=True) or {}
    ucfg = get_user_config(chat_id)
    allowed_risk = ("conservative", "moderate", "aggressive", "degen")
    allowed_assets = ("stocks", "crypto", "both")
    if "risk_profile" in body and body["risk_profile"] in allowed_risk:
        ucfg["risk_profile"] = body["risk_profile"]
    if "assets" in body and body["assets"] in allowed_assets:
        ucfg["assets"] = body["assets"]
    if "stock_budget" in body:
        try: ucfg["stock_budget"] = float(body["stock_budget"])
        except: pass
    if "crypto_budget" in body:
        try: ucfg["crypto_budget"] = float(body["crypto_budget"])
        except: pass
    if "stop_loss_pct" in body:
        try: ucfg["stop_loss_pct"] = float(body["stop_loss_pct"])
        except: pass
    if "target_gain_pct" in body:
        try: ucfg["target_gain_pct"] = float(body["target_gain_pct"])
        except: pass
    if "onboarded" in body:
        ucfg["onboarded"] = bool(body["onboarded"])
    save_user_config(chat_id, ucfg)
    return jsonify({"ok": True})


@app.route("/api/miniapp/live_prices")
def miniapp_live_prices():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403

    # When ?tickers= is provided (e.g. from watchlist chips), fetch those
    # specific tickers and return a plain {TICKER: price} dict so the
    # frontend can do direct key lookups without parsing a list.
    raw_tickers = request.args.get("tickers", "").strip()
    if raw_tickers:
        from market_data import get_live_prices
        tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
        prices  = get_live_prices(tickers)
        return jsonify({"ok": True, "prices": prices})

    from config_manager import load_picks
    picks_data = load_picks()
    if not picks_data:
        return jsonify({"ok": True, "prices": []})

    # Collect all tickers from st_picks, lt_picks, crypto_picks, etf_picks, commodities
    all_picks = []
    stocks = picks_data.get("stocks", {})
    for p in stocks.get("short_term", []):
        ticker = (p.get("ticker") or p.get("symbol", "")).upper()
        if ticker:
            all_picks.append({"ticker": ticker, "entry": p.get("entry_price") or p.get("price"), "type": "st_picks"})
    for p in stocks.get("long_term", []):
        ticker = (p.get("ticker") or p.get("symbol", "")).upper()
        if ticker:
            all_picks.append({"ticker": ticker, "entry": p.get("entry_price") or p.get("price"), "type": "lt_picks"})
    etfs = picks_data.get("etfs", {})
    for p in etfs.get("short_term", []) + etfs.get("long_term", []):
        ticker = (p.get("ticker") or p.get("symbol", "")).upper()
        if ticker:
            all_picks.append({"ticker": ticker, "entry": p.get("entry_price") or p.get("price"), "type": "etf_picks"})
    comms = picks_data.get("commodities", {})
    for p in comms.get("short_term", []) + comms.get("long_term", []):
        ticker = (p.get("ticker") or p.get("symbol", "")).upper()
        if ticker:
            all_picks.append({"ticker": ticker, "entry": p.get("entry_price") or p.get("price"), "type": "commodities"})
    crypto = picks_data.get("crypto", {})
    for p in crypto.get("short_term", []) + crypto.get("long_term", []):
        ticker = (p.get("ticker") or p.get("symbol", "")).upper()
        if ticker:
            all_picks.append({"ticker": ticker, "entry": p.get("entry_price") or p.get("price"), "type": "crypto"})

    if not all_picks:
        return jsonify({"ok": True, "prices": []})

    tickers = [p["ticker"] for p in all_picks]
    from market_data import get_live_prices
    prices  = get_live_prices(tickers)

    result = []
    for p in all_picks:
        t = p["ticker"]
        live = prices.get(t)
        entry = p.get("entry")
        pct = None
        if live and entry:
            try: pct = round((live - float(entry)) / float(entry) * 100, 2)
            except: pass
        result.append({"ticker": t, "entry": entry, "live": live, "pct": pct, "type": p["type"]})

    return jsonify({"ok": True, "prices": result})


_MARKETS_CACHE: dict = {}          # {"data": {...}, "ts": float}
_MARKETS_TTL  = 5 * 60            # 5-minute server-side cache

# All instruments the front-end knows about, grouped by section.
# yf_sym  = symbol passed to yfinance;  label = what the UI sees
_MARKETS_INSTRUMENTS = [
    # ── Indices ────────────────────────────────────────────────────
    ("SPY",   "indices",     "SPY"),
    ("QQQ",   "indices",     "QQQ"),
    ("IWM",   "indices",     "IWM"),
    ("DIA",   "indices",     "DIA"),
    # ── Crypto ────────────────────────────────────────────────────
    ("BTC-USD", "crypto",   "BTC"),
    ("ETH-USD", "crypto",   "ETH"),
    ("BNB-USD", "crypto",   "BNB"),
    ("SOL-USD", "crypto",   "SOL"),
    ("XRP-USD", "crypto",   "XRP"),
    # ── Commodities & Bonds ────────────────────────────────────────
    ("GLD",   "commodities", "GLD"),
    ("SLV",   "commodities", "SLV"),
    ("USO",   "commodities", "USO"),
    ("UNG",   "commodities", "UNG"),
    ("WEAT",  "commodities", "WEAT"),
    ("CORN",  "commodities", "CORN"),
    ("TLT",   "commodities", "TLT"),
    # ── Macro indicators ──────────────────────────────────────────
    ("^DXY",  "macro",       "DXY"),
    ("^VIX",  "macro",       "VIX"),
]


@app.route("/api/miniapp/markets")
def miniapp_markets():
    """Markets overview: indices + crypto + commodities prices with daily %,
    plus Fear & Greed, rates, and sector rotation from the cached regime.
    Results are cached server-side for 5 minutes to avoid hammering yfinance."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403

    import time as _time
    import yfinance as yf
    from market_regime import get_market_regime

    # ── Serve from cache if fresh ────────────────────────────────────────────
    cached = _MARKETS_CACHE.get("data")
    if cached and (_time.time() - _MARKETS_CACHE.get("ts", 0)) < _MARKETS_TTL:
        return jsonify(cached)

    # ── Regime (fast — reads from existing Gist cache) ───────────────────────
    try:
        regime = get_market_regime()
    except Exception:
        regime = {}

    # ── Fetch all prices in one yf.download() call ───────────────────────────
    yf_syms = [yf_sym for yf_sym, _, _ in _MARKETS_INSTRUMENTS]
    prices: dict = {}
    try:
        raw    = yf.download(yf_syms, period="5d", interval="1d",
                             auto_adjust=True, progress=False, threads=True)
        closes = raw["Close"] if "Close" in raw.columns else raw
        for sym in yf_syms:
            try:
                col  = closes[sym] if hasattr(closes, "columns") and sym in closes.columns else closes
                vals = col.dropna()
                if len(vals) >= 2:
                    p    = float(vals.iloc[-1])
                    prev = float(vals.iloc[-2])
                    prices[sym] = {"price": round(p, 4),
                                   "chg_pct": round((p - prev) / prev * 100, 2)}
                elif len(vals) == 1:
                    prices[sym] = {"price": round(float(vals.iloc[-1]), 4), "chg_pct": None}
            except Exception:
                pass
    except Exception:
        pass

    def _row(yf_sym: str, label: str) -> dict:
        d = prices.get(yf_sym, {})
        p = d.get("price")
        # Round prices appropriately: crypto can be very small or very large
        if p is not None:
            p = round(p, 2) if p >= 1 else round(p, 6)
        return {"ticker": label, "price": p, "chg_pct": d.get("chg_pct")}

    # Group rows by section
    sections: dict = {"indices": [], "crypto": [], "commodities": [], "macro": []}
    for yf_sym, section, label in _MARKETS_INSTRUMENTS:
        sections.setdefault(section, []).append(_row(yf_sym, label))

    result = {
        "ok":          True,
        "regime":      regime,
        "indices":     sections["indices"],
        "crypto":      sections["crypto"],
        "commodities": sections["commodities"],
        "macro":       sections["macro"],
    }

    _MARKETS_CACHE["data"] = result
    _MARKETS_CACHE["ts"]   = _time.time()

    return jsonify(result)


_regime_cache: dict = {}
_REGIME_TTL = 900  # 15 minutes

@app.route("/api/miniapp/regime")
def miniapp_regime():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    now = time.time()
    if _regime_cache.get("ts") and now - _regime_cache["ts"] < _REGIME_TTL:
        return jsonify({"ok": True, "regime": _regime_cache["data"]})
    from market_regime import get_market_regime
    try:
        r = get_market_regime()
        _regime_cache["data"] = r
        _regime_cache["ts"]   = now
        return jsonify({"ok": True, "regime": r})
    except Exception as e:
        # Return stale cache rather than error if available
        if _regime_cache.get("data"):
            return jsonify({"ok": True, "regime": _regime_cache["data"]})
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/miniapp/update_position", methods=["POST"])
def miniapp_update_position():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log, save_user_trade_log
    body = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker: return jsonify({"error": "ticker required"}), 400
    log = load_user_trade_log(chat_id)
    updated = False
    for t in log.get("open", []):
        if t.get("ticker") == ticker:
            if "stop_loss" in body:
                val = body["stop_loss"]
                t["stop_loss"] = float(val) if val not in (None, "", "null") else None
            if "target_price" in body:
                val = body["target_price"]
                t["target_price"] = float(val) if val not in (None, "", "null") else None
            if "entry_price" in body:
                val = body["entry_price"]
                t["entry_price"] = float(val) if val not in (None, "", "null") else None
            if "notes" in body:
                t["notes"] = str(body["notes"]).strip()[:500]  # cap at 500 chars
            if "shares" in body:
                val = body["shares"]
                t["shares"] = float(val) if val not in (None, "", "null") else None
            updated = True
            break
    if not updated:
        return jsonify({"error": "position not found"}), 404
    save_user_trade_log(chat_id, log)
    return jsonify({"ok": True})


@app.route("/api/miniapp/community")
def miniapp_community():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import get_allowed_users, load_user_trade_log
    from performance_tracker import build_community_stats
    users = get_allowed_users()
    owner = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    if owner and owner not in users:
        users = list(users) + [owner]
    logs = [load_user_trade_log(uid) for uid in users]
    try:
        stats = build_community_stats(logs)
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/miniapp/my_performance")
def miniapp_my_performance():
    """Return the user's REAL closed trade performance — no simulated data."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_user_trade_log
    log    = load_user_trade_log(chat_id)
    closed = [t for t in log.get("closed", []) if t.get("source") != "backtest"]

    if not closed:
        return jsonify({"ok": True, "trades": [], "summary": {}})

    # ── Per-trade enrichment ────────────────────────────────────────────────
    trades_out = []
    total_usd  = 0.0
    peak_cum   = 0.0
    max_dd     = 0.0
    cum        = 0.0
    wins = losses = 0

    for t in sorted(closed, key=lambda x: x.get("closed_date", "")):
        ret_pct  = float(t.get("return_pct") or 0)
        gain_usd = float(t.get("gain_usd") or 0)
        entry    = float(t.get("entry_price") or 0)
        exit_p   = float(t.get("exit_price") or t.get("sell_price") or 0)
        shares   = float(t.get("shares") or 0)

        # Infer gain_usd if not stored but we have entry/exit/shares
        if gain_usd == 0 and entry > 0 and exit_p > 0 and shares > 0:
            gain_usd = round((exit_p - entry) * shares, 2)

        if ret_pct > 0:
            wins += 1
        else:
            losses += 1

        total_usd += gain_usd
        cum       += gain_usd
        if cum > peak_cum:
            peak_cum = cum
        dd = peak_cum - cum
        if dd > max_dd:
            max_dd = dd

        trades_out.append({
            "ticker":      t.get("ticker", ""),
            "closed_date": t.get("closed_date", t.get("sold_date", "")),
            "entry_price": round(entry, 2) if entry else None,
            "exit_price":  round(exit_p, 2) if exit_p else None,
            "shares":      shares if shares else None,
            "return_pct":  round(ret_pct, 2),
            "gain_usd":    round(gain_usd, 2),
            "outcome":     t.get("outcome", ""),
        })

    trades_out.reverse()  # most recent first for display
    total_trades = len(closed)
    win_rate     = round(wins / total_trades * 100) if total_trades else 0
    avg_ret      = round(sum(t["return_pct"] for t in trades_out) / total_trades, 2) if total_trades else 0
    best  = max(trades_out, key=lambda t: t["return_pct"], default=None)
    worst = min(trades_out, key=lambda t: t["return_pct"], default=None)

    return jsonify({
        "ok":     True,
        "trades": trades_out,
        "summary": {
            "total_trades":  total_trades,
            "wins":          wins,
            "losses":        losses,
            "win_rate":      win_rate,
            "avg_return":    avg_ret,
            "total_pnl_usd": round(total_usd, 2),
            "max_drawdown":  round(max_dd, 2),
            "best":  {"ticker": best["ticker"],  "return_pct": best["return_pct"],  "gain_usd": best["gain_usd"]}  if best  else None,
            "worst": {"ticker": worst["ticker"], "return_pct": worst["return_pct"], "gain_usd": worst["gain_usd"]} if worst else None,
        },
    })


@app.route("/api/miniapp/economic_calendar")
def miniapp_economic_calendar():
    """Upcoming high-impact macro events — Fed, CPI, jobs, GDP, PCE."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    import requests as _req
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    from datetime import date, timedelta
    today   = date.today()
    end     = today + timedelta(days=60)
    events  = []

    if finnhub_key:
        try:
            r = _req.get(
                "https://finnhub.io/api/v1/calendar/economic",
                params={"from": today.isoformat(), "to": end.isoformat(), "token": finnhub_key},
                timeout=8,
            )
            if r.ok:
                HIGH_IMPACT = {"fomc", "fed", "cpi", "nonfarm", "payroll", "gdp", "pce",
                               "unemployment", "pmi", "retail", "inflation", "interest rate"}
                for ev in r.json().get("economicCalendar", []):
                    name_lc = (ev.get("event") or "").lower()
                    impact  = (ev.get("impact") or "").lower()
                    if impact not in ("high", "medium") and not any(k in name_lc for k in HIGH_IMPACT):
                        continue
                    ev_date = ev.get("time", ev.get("date", ""))[:10]
                    if not ev_date:
                        continue
                    days_until = (date.fromisoformat(ev_date) - today).days
                    if days_until < 0:
                        continue
                    events.append({
                        "date":       ev_date,
                        "days_until": days_until,
                        "event":      ev.get("event", ""),
                        "country":    ev.get("country", "US"),
                        "impact":     impact or "medium",
                        "actual":     ev.get("actual"),
                        "estimate":   ev.get("estimate"),
                        "previous":   ev.get("prev"),
                    })
        except Exception as exc:
            print(f"[econ_cal] {exc}")

    # Fallback: hardcoded major upcoming US events (always shown if Finnhub fails)
    if not events:
        hardcoded = [
            {"event": "FOMC Meeting",              "impact": "high"},
            {"event": "CPI (Consumer Price Index)","impact": "high"},
            {"event": "Nonfarm Payrolls",           "impact": "high"},
            {"event": "GDP (Preliminary)",          "impact": "high"},
            {"event": "PCE Price Index",            "impact": "high"},
        ]
        events = [{"date": "", "days_until": None, "country": "US",
                   "actual": None, "estimate": None, "previous": None, **e}
                  for e in hardcoded]

    events.sort(key=lambda e: e.get("days_until") or 999)
    return jsonify({"ok": True, "events": events[:20]})


@app.route("/api/miniapp/analyst_ratings")
def miniapp_analyst_ratings():
    """Recent analyst upgrades/downgrades for watchlist tickers."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    raw     = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()][:20]
    if not tickers:
        return jsonify({"ok": True, "ratings": []})

    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    from datetime import date, timedelta
    import requests as _req
    since = (date.today() - timedelta(days=90)).isoformat()
    results = []

    def _fetch_ratings(sym):
        if not finnhub_key:
            return []
        try:
            r = _req.get(
                "https://finnhub.io/api/v1/stock/recommendation",
                params={"symbol": sym, "token": finnhub_key},
                timeout=6,
            )
            if not r.ok:
                return []
            recs = r.json() or []
            # Finnhub returns monthly aggregates; show latest 2
            return [{"ticker": sym, "period": rec.get("period",""),
                     "buy": rec.get("buy",0), "hold": rec.get("hold",0),
                     "sell": rec.get("sell",0), "strongBuy": rec.get("strongBuy",0),
                     "strongSell": rec.get("strongSell",0)} for rec in recs[:2]]
        except Exception:
            return []

    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=6) as pool:
        for sym_results in pool.map(_fetch_ratings, tickers):
            results.extend(sym_results)

    return jsonify({"ok": True, "ratings": results})


@app.route("/api/miniapp/volume_spikes")
def miniapp_volume_spikes():
    """Detect unusual volume vs 20-day avg for given tickers."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    raw     = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()][:20]
    if not tickers:
        return jsonify({"ok": True, "spikes": []})

    import yfinance as _yf
    import concurrent.futures as _cf

    def _check_volume(sym):
        try:
            hist = _yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=True)
            if hist.empty or len(hist) < 5:
                return None
            avg_vol  = float(hist["Volume"].iloc[:-1].mean())
            today_vol = float(hist["Volume"].iloc[-1])
            ratio     = today_vol / avg_vol if avg_vol > 0 else 0
            if ratio >= 1.5:
                return {"ticker": sym, "today_volume": int(today_vol),
                        "avg_volume": int(avg_vol), "ratio": round(ratio, 1)}
            return None
        except Exception:
            return None

    spikes = []
    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(_check_volume, tickers):
            if result:
                spikes.append(result)

    spikes.sort(key=lambda x: x["ratio"], reverse=True)
    return jsonify({"ok": True, "spikes": spikes})


# Performance endpoint cache — closed trade stats are historical and can't change
# mid-session, so recomputing them (+ a live SPY yfinance call) on every tab visit
# is pure waste. 5-minute TTL; cache is per-user so everyone gets their own data.
_PERF_CACHE: dict = {}         # chat_id → {"payload": dict, "ts": float}
_PERF_CACHE_TTL = 5 * 60       # 5 minutes


@app.route("/api/miniapp/performance")
def miniapp_performance():
    """Return bot track record for the Mini App performance tab.

    Cached per-user for 5 minutes — closed trade P&L is historical and can't
    change while the user is in-session. Response shape is unchanged.
    """
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403

    now = time.time()
    cached = _PERF_CACHE.get(chat_id)
    if cached and now - cached["ts"] < _PERF_CACHE_TTL:
        return jsonify(cached["payload"])

    from config_manager import get_allowed_users, load_user_trade_log
    from performance_tracker import build_community_stats
    from performance_context import get_performance_context, get_performance_stats
    try:
        users = get_allowed_users()
        owner = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
        if owner and owner not in users:
            users = list(users) + [owner]
        logs  = [load_user_trade_log(uid) for uid in users]
        stats = build_community_stats(logs) or {}
        ctx30 = get_performance_context(lookback_days=30)
        period_stats = {
            "30d": get_performance_stats(30),
            "60d": get_performance_stats(60),
            "90d": get_performance_stats(90),
        }
        payload = {
            "ok":           True,
            "stats":        stats,
            "context_30d":  ctx30,
            "period_stats": period_stats,
        }
        _PERF_CACHE[chat_id] = {"payload": payload, "ts": now}
        return jsonify(payload)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/miniapp/dividends")
def miniapp_dividends():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log
    from dividends_checker import get_dividend_info
    log = load_user_trade_log(chat_id)
    stock_tickers = [
        t["ticker"] for t in log.get("open", [])
        if t.get("asset_type", "stock") == "stock"
    ]
    if not stock_tickers:
        return jsonify({"ok": True, "dividends": []})
    try:
        divs = get_dividend_info(list(set(stock_tickers)))
        paying = [d for d in divs if d.get("pays_dividend")]
        return jsonify({"ok": True, "dividends": paying})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/miniapp/tax_lots")
def miniapp_tax_lots():
    """Tax lot summary — realized ST vs LT P&L breakdown + FIFO/LIFO guidance for open positions."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_user_trade_log
    from datetime import date
    from collections import defaultdict

    log    = load_user_trade_log(chat_id)
    closed = [t for t in log.get("closed", []) if t.get("source") != "backtest"]
    opens  = log.get("open", [])
    today  = date.today()

    # ── Realized ST vs LT gains ────────────────────────────────────────────────
    st_gain = lt_gain = 0.0
    st_count = lt_count = 0
    realized_lots = []

    for t in sorted(closed, key=lambda x: x.get("closed_date", ""), reverse=True):
        opened_s   = t.get("opened_date") or t.get("bought_date", "")
        closed_s   = t.get("closed_date") or t.get("sold_date", "")
        days_held  = None
        is_lt      = False
        if opened_s and closed_s:
            try:
                days_held = (date.fromisoformat(closed_s) - date.fromisoformat(opened_s)).days
                is_lt = days_held >= 365
            except Exception:
                pass

        gain = float(t.get("gain_usd") or 0)
        entry  = float(t.get("entry_price") or 0)
        exit_p = float(t.get("exit_price") or t.get("sell_price") or 0)
        shares = float(t.get("shares") or 0)
        if gain == 0 and entry > 0 and exit_p > 0 and shares > 0:
            gain = round((exit_p - entry) * shares, 2)

        if is_lt:
            lt_gain  += gain
            lt_count += 1
        else:
            st_gain  += gain
            st_count += 1

        realized_lots.append({
            "ticker":      t.get("ticker", ""),
            "closed_date": closed_s,
            "days_held":   days_held,
            "is_lt":       is_lt,
            "gain_usd":    round(gain, 2),
        })

    # ── Open positions — FIFO / LIFO guidance ─────────────────────────────────
    open_lots = []
    for t in sorted(opens, key=lambda x: x.get("opened_date", "")):
        opened = t.get("opened_date", "")
        days_held = None
        is_lt     = False
        if opened:
            try:
                days_held = (today - date.fromisoformat(opened)).days
                is_lt = days_held >= 365
            except Exception:
                pass
        days_to_lt = (365 - days_held) if (days_held is not None and not is_lt) else None
        open_lots.append({
            "ticker":       t.get("ticker", ""),
            "opened_date":  opened,
            "shares":       float(t.get("shares") or 0),
            "entry_price":  float(t.get("entry_price") or 0),
            "days_held":    days_held,
            "is_lt":        is_lt,
            "days_to_lt":   days_to_lt,
        })

    # Group by ticker for per-ticker FIFO/LIFO tip
    by_ticker = defaultdict(list)
    for lot in open_lots:
        by_ticker[lot["ticker"]].append(lot)

    fifo_lifo = []
    for ticker, lots in by_ticker.items():
        lt_lots = [l for l in lots if l["is_lt"]]
        st_lots = [l for l in lots if not l["is_lt"]]
        tax_tip = ""
        if lt_lots:
            tax_tip = f"{len(lt_lots)} LT lot(s) — sell LT first for lower rate"
        elif st_lots:
            nearest = min(st_lots, key=lambda l: l.get("days_to_lt") or 999)
            d2lt = nearest.get("days_to_lt")
            if d2lt and d2lt <= 60:
                tax_tip = f"Turns LT in {d2lt}d — consider waiting"
        fifo_lifo.append({
            "ticker":   ticker,
            "lots":     lots,
            "lt_count": len(lt_lots),
            "st_count": len(st_lots),
            "tax_tip":  tax_tip,
        })

    # Rough federal tax estimates (guidance only, ~35% ST / ~15% LT)
    st_tax_est = round(st_gain * 0.35, 2) if st_gain > 0 else 0.0
    lt_tax_est = round(lt_gain * 0.15, 2) if lt_gain > 0 else 0.0

    return jsonify({
        "ok": True,
        "realized": {
            "st_gain":    round(st_gain, 2),
            "lt_gain":    round(lt_gain, 2),
            "st_count":   st_count,
            "lt_count":   lt_count,
            "st_tax_est": st_tax_est,
            "lt_tax_est": lt_tax_est,
            "total_gain": round(st_gain + lt_gain, 2),
        },
        "lots":           realized_lots[:20],
        "open_fifo_lifo": fifo_lifo,
    })


@app.route("/api/miniapp/screener_data")
def miniapp_screener_data():
    """Return current screener candidates (from overnight cache) for Mini App filter UI."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_screener_cache
    cache = load_screener_cache()
    if not cache:
        return jsonify({"ok": True, "candidates": [], "sectors": [],
                        "total": 0, "cached_at": "", "stale": True})

    candidates = []
    stocks = cache.get("stocks", {})
    for c in stocks.get("short_term", []):
        candidates.append({
            "ticker":     c.get("ticker", ""),
            "name":       c.get("company", c.get("name", "")),
            "sector":     c.get("sector", ""),
            "price":      c.get("current_price"),
            "score":      c.get("score"),
            "rs_vs_spy":  c.get("rs_vs_spy"),
            "pct_below_52w_high": c.get("pct_below_52w_high"),
            "pattern":    c.get("pattern", ""),
            "type":       "ST",
        })
    for c in stocks.get("long_term", []):
        candidates.append({
            "ticker":      c.get("ticker", ""),
            "name":        c.get("company", c.get("name", "")),
            "sector":      c.get("sector", ""),
            "price":       c.get("current_price"),
            "score":       c.get("score"),
            "pe_ratio":    c.get("pe_ratio"),
            "eps_surprise":c.get("eps_surprise_pct"),
            "rs_vs_spy":   c.get("rs_vs_spy"),
            "type":        "LT",
        })

    crypto = cache.get("crypto", {})
    for c in list(crypto.get("short_term", [])) + list(crypto.get("long_term", [])):
        candidates.append({
            "ticker": c.get("ticker", ""),
            "name":   c.get("company", c.get("name", c.get("ticker", ""))),
            "sector": "Crypto",
            "price":  c.get("current_price"),
            "score":  c.get("score"),
            "type":   "Crypto",
        })

    # Deduplicate by ticker — prefer ST entry
    seen: set = set()
    deduped = []
    for c in candidates:
        if c["ticker"] not in seen:
            seen.add(c["ticker"])
            deduped.append(c)

    sectors = sorted({c["sector"] for c in deduped if c.get("sector")})

    return jsonify({
        "ok":        True,
        "candidates": deduped,
        "sectors":   sectors,
        "total":     len(deduped),
        "cached_at": cache.get("cached_at", ""),
    })


@app.route("/api/miniapp/earnings_surprise")
def miniapp_earnings_surprise():
    """Return recent earnings surprises for the user's watchlist tickers.

    Checks yfinance quarterly earnings data and returns actual vs estimate
    for the most recent quarter. Designed for display on the Watch tab.
    """
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    raw     = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tickers:
        return jsonify({"ok": True, "surprises": []})

    import yfinance as _yf
    from concurrent.futures import ThreadPoolExecutor

    def _get_surprise(ticker: str) -> dict | None:
        try:
            tk   = _yf.Ticker(ticker)
            hist = tk.earnings_history
            if hist is None or (hasattr(hist, "empty") and hist.empty):
                return None
            # earnings_history is a DataFrame with columns EPS Estimate, Reported EPS, Surprise(%)
            # Most recent row first (or last) — sort by index descending
            if hasattr(hist, "sort_index"):
                hist = hist.sort_index(ascending=False)
            row = hist.iloc[0]
            est  = row.get("EPS Estimate")
            rep  = row.get("Reported EPS")
            surp = row.get("Surprise(%)")
            if est is None and rep is None:
                return None
            quarter = str(hist.index[0])[:7] if len(hist) > 0 else ""
            beat    = None
            if surp is not None:
                beat = float(surp) > 0
            elif rep is not None and est is not None:
                try:
                    beat = float(rep) > float(est)
                except Exception:
                    pass
            return {
                "ticker":   ticker,
                "quarter":  quarter,
                "est_eps":  round(float(est), 2) if est is not None else None,
                "rep_eps":  round(float(rep), 2) if rep is not None else None,
                "surprise_pct": round(float(surp), 1) if surp is not None else None,
                "beat":     beat,
            }
        except Exception:
            return None

    stock_tickers = [t for t in tickers if "/" not in t][:12]  # cap at 12
    with ThreadPoolExecutor(max_workers=min(len(stock_tickers), 6)) as ex:
        results = list(ex.map(_get_surprise, stock_tickers))

    surprises = [r for r in results if r is not None]
    # Sort: beats first, then by |surprise_pct| desc
    surprises.sort(key=lambda r: (
        0 if r.get("beat") else 1,
        -abs(r.get("surprise_pct") or 0),
    ))

    return jsonify({"ok": True, "surprises": surprises})


@app.route("/api/miniapp/run_command", methods=["POST"])
def miniapp_run_command():
    """Execute a bot command from the Mini App and return the reply inline.

    Runs the command in a thread and waits up to 22 seconds for it to
    finish so the result can be shown directly in the Mini App UI.
    If the command takes longer (e.g. /today on a cold start) it falls
    back to Telegram delivery and returns async=True.
    """
    import threading

    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    body    = request.get_json(silent=True) or {}
    command = (body.get("command") or "").strip()
    if not command:
        return jsonify({"error": "missing command"}), 400

    if not command.startswith("/"):
        command = "/" + command

    result_box = {"reply": None, "error": None}

    def _run():
        try:
            result_box["reply"] = handle_incoming_command(command, chat_id=chat_id) or ""
        except Exception as exc:
            result_box["error"] = str(exc)
            print(f"[run_command] error running {command}: {exc}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=22)

    if t.is_alive():
        # Still running — result will arrive via Telegram
        return jsonify({"ok": True, "async": True,
                        "message": "This one takes a moment — result sent to your Telegram chat."})

    if result_box["error"]:
        return jsonify({"error": result_box["error"]}), 500

    return jsonify({"ok": True, "reply": result_box["reply"]})


@app.route("/api/miniapp/share_link")
def miniapp_share_link():
    """Return a shareable invite link for the current user."""
    global _bot_username_cache
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    bot_username = _bot_username_cache
    if not bot_username:
        try:
            import requests as _req
            r = _req.get(
                f"{os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')}/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}/getMe",
                timeout=5,
            )
            bot_username = r.json().get("result", {}).get("username", "")
            if bot_username:
                _bot_username_cache = bot_username  # cache for all future requests
        except Exception:
            pass

    if not bot_username:
        return jsonify({"error": "could not fetch bot username"}), 500

    deep_link = f"ref_{chat_id}"
    bot_link  = f"https://t.me/{bot_username}?start={deep_link}"
    share_text = (
        "Hey! I'm using StockPulz — a personal AI stock advisor that sends daily stock & crypto picks, "
        "price alerts, and weekly performance recaps.\n\nJoin here 👇\n" + bot_link
    )
    return jsonify({"ok": True, "url": bot_link, "text": share_text})


_earnings_cache: dict = {"data": None, "date": ""}   # process-lifetime date-keyed cache


@app.route("/api/miniapp/earnings")
def miniapp_earnings():
    """Return upcoming earnings for the user's watchlist tickers.

    Uses a single bulk Finnhub calendar call (one HTTP request for all tickers)
    and caches the full calendar per calendar-date so repeat loads are instant.
    """
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    raw = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tickers:
        return jsonify({"ok": True, "earnings": []})

    from datetime import date, timedelta
    import requests as _req
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    today    = date.today()
    today_s  = today.isoformat()
    end_date = today + timedelta(days=90)

    # ── Serve from cache if same calendar day ────────────────────────────────
    if _earnings_cache["date"] == today_s and _earnings_cache["data"] is not None:
        calendar = _earnings_cache["data"]
    else:
        calendar = {}   # symbol → event dict
        if finnhub_key:
            try:
                # ONE bulk call — no symbol filter, returns every company's next event
                r = _req.get(
                    "https://finnhub.io/api/v1/calendar/earnings",
                    params={"from": today_s, "to": end_date.isoformat(), "token": finnhub_key},
                    timeout=10,
                )
                if r.ok:
                    for ev in r.json().get("earningsCalendar", []):
                        sym = (ev.get("symbol") or "").upper()
                        if not sym:
                            continue
                        # Keep only the earliest event per ticker
                        if sym not in calendar:
                            calendar[sym] = ev
            except Exception as exc:
                print(f"[earnings] Finnhub bulk fetch failed: {exc}")
        _earnings_cache["data"] = calendar
        _earnings_cache["date"] = today_s

    # ── Filter down to the user's tickers ────────────────────────────────────
    results = []
    ticker_set = set(tickers[:20])
    for ticker in ticker_set:
        try:
            ev = calendar.get(ticker)
            if ev:
                ed_str = ev.get("date", "")
                if not ed_str:
                    continue
                ed_date    = date.fromisoformat(ed_str)
                days_until = (ed_date - today).days
                if days_until < -1 or days_until > 90:
                    continue
                hour       = (ev.get("hour") or "").lower()
                timing_str = "Before open" if hour == "bmo" else "After close" if hour == "amc" else ""
                eps_est    = ev.get("epsEstimate")
                eps_act    = ev.get("epsActual")
                rev_est    = ev.get("revenueEstimate")
                results.append({
                    "ticker":       ticker,
                    "date":         ed_date.strftime("%b %d"),
                    "days_until":   max(0, days_until),
                    "timing":       timing_str,
                    "eps_estimate": round(eps_est, 2) if eps_est is not None else None,
                    "eps_actual":   round(eps_act, 2) if eps_act is not None else None,
                    "rev_estimate": int(rev_est) if rev_est else None,
                })
                continue

            # Finnhub returned nothing for this ticker — yfinance fallback
            import yfinance as yf
            from datetime import datetime as _dt
            info = yf.Ticker(ticker).fast_info
            ed   = getattr(info, "earnings_date", None)
            if ed is None:
                full_info = yf.Ticker(ticker).info
                ed = full_info.get("earningsTimestamp") or full_info.get("earningsDate")
            if ed:
                ed_date = _dt.utcfromtimestamp(ed).date() if isinstance(ed, (int, float)) \
                          else _dt.fromisoformat(str(ed)).date()
                days_until = (ed_date - today).days
                if -1 <= days_until <= 90:
                    results.append({
                        "ticker": ticker, "date": ed_date.strftime("%b %d"),
                        "days_until": max(0, days_until), "timing": "",
                        "eps_estimate": None, "eps_actual": None, "rev_estimate": None,
                    })
        except Exception:
            pass

    results.sort(key=lambda x: x["days_until"])

    # ── Enrich with analyst consensus (parallel yfinance, small set) ─────────
    if results:
        import yfinance as _yf_e
        import concurrent.futures as _cf_e

        def _fetch_analyst(sym):
            try:
                inf = _yf_e.Ticker(sym).info or {}
                rating = inf.get("recommendationKey") or ""
                target = inf.get("targetMeanPrice")
                count  = inf.get("numberOfAnalystOpinions")
                name   = inf.get("shortName") or ""
                # cache name while we have it
                _quote_cache[f"__name_{sym}"] = {"v": name, "ts": time.time()}
                return sym, {
                    "analyst_rating": rating,
                    "analyst_target": round(target, 2) if target else None,
                    "analyst_count":  int(count) if count else None,
                    "name":           name,
                }
            except Exception:
                return sym, {}

        analyst_map = {}
        with _cf_e.ThreadPoolExecutor(max_workers=5) as pool:
            for sym, ad in pool.map(_fetch_analyst, [r["ticker"] for r in results]):
                analyst_map[sym] = ad

        for r in results:
            ad = analyst_map.get(r["ticker"], {})
            r["analyst_rating"] = ad.get("analyst_rating", "")
            r["analyst_target"] = ad.get("analyst_target")
            r["analyst_count"]  = ad.get("analyst_count")
            r["name"]           = ad.get("name", "")

    return jsonify({"ok": True, "earnings": results})


@app.route("/api/miniapp/watchlist")
def miniapp_watchlist():
    """Return user's watchlist tickers with asset_type classification."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log
    from price_checker import _SYMBOL_TO_CG_ID
    log = load_user_trade_log(chat_id)
    tickers = log.get("watchlist", [])
    _crypto_syms = {s.upper() for s in _SYMBOL_TO_CG_ID}
    asset_types  = {t: ("crypto" if t.upper() in _crypto_syms else "stock") for t in tickers}
    return jsonify({"ok": True, "tickers": tickers, "asset_types": asset_types})


@app.route("/api/miniapp/sparkline/<ticker>")
def miniapp_sparkline(ticker: str):
    """Return closing prices for a sparkline chart. period query param: 7d (default) or 30d."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    ticker = ticker.strip().upper()
    from flask import request as _req
    period = _req.args.get("period", "7d")
    if period not in ("7d", "1mo", "3mo"):
        period = "7d"
    max_bars = {"7d": 7, "1mo": 30, "3mo": 90}.get(period, 7)
    try:
        import yfinance as _yf
        from price_checker import _SYMBOL_TO_CG_ID as _ALERT_CRYPTO
        yf_sym = f"{ticker}-USD" if ticker in {s.upper() for s in _ALERT_CRYPTO} else ticker
        hist = _yf.Ticker(yf_sym).history(period=period, interval="1d", auto_adjust=True)
        closes = [round(float(v), 4) for v in hist["Close"].dropna().tolist()[-max_bars:]]
        return jsonify({"ticker": ticker, "closes": closes, "period": period})
    except Exception as exc:
        return jsonify({"error": str(exc), "closes": []}), 200


@app.route("/api/miniapp/watchlist/add", methods=["POST"])
def miniapp_watchlist_add():
    """Add a ticker to the user's watchlist.
    Resolves company names (e.g. 'AMAZON' → 'AMZN') before storing.
    """
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log, save_user_trade_log
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker: return jsonify({"error": "ticker required"}), 400

    # Resolve company names → ticker symbol.
    # Heuristic: tickers are 1–5 uppercase chars; anything longer is likely a
    # company name (e.g. "AMAZON" → "AMZN", "COSTCO" → "COST") even if yfinance
    # accidentally returns a price for it from some other symbol.
    from price_alert_manager import _current_price, _CRYPTO_SYMBOLS as _ALERT_CRYPTO_SET
    likely_name = len(ticker) > 5
    if likely_name or _current_price(ticker) is None:
        # 1. Try stock name resolver
        try:
            from cmd_helpers import _resolve_ticker_candidates
            candidates = _resolve_ticker_candidates(ticker)
            if candidates:
                resolved = candidates[0]["ticker"].upper()
                if resolved != ticker and _current_price(resolved) is not None:
                    ticker = resolved
        except Exception:
            pass
    if _current_price(ticker) is None:
        # 2. Try dynamic crypto name resolution via CoinGecko search
        crypto_sym = _resolve_crypto_name_dynamic(ticker)
        if crypto_sym and _current_price(crypto_sym) is not None:
            ticker = crypto_sym

    log = load_user_trade_log(chat_id)
    watchlist = log.get("watchlist", [])
    if ticker in watchlist:
        return jsonify({"ok": False, "error": f"{ticker} already on watchlist"}), 200
    watchlist.append(ticker)
    log["watchlist"] = watchlist
    save_user_trade_log(chat_id, log)
    return jsonify({"ok": True, "ticker": ticker, "count": len(watchlist)})


@app.route("/api/miniapp/watchlist/remove", methods=["POST"])
def miniapp_watchlist_remove():
    """Remove a ticker from the user's watchlist."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import load_user_trade_log, save_user_trade_log
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker: return jsonify({"error": "ticker required"}), 400
    log = load_user_trade_log(chat_id)
    watchlist = [t for t in log.get("watchlist", []) if t != ticker]
    log["watchlist"] = watchlist
    save_user_trade_log(chat_id, log)
    return jsonify({"ok": True, "ticker": ticker, "count": len(watchlist)})


@app.route("/api/miniapp/status")
def miniapp_status():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import get_config, get_user_config
    import pytz
    from datetime import datetime, timedelta

    global_cfg = get_config()
    user_cfg   = get_user_config(chat_id)

    bot_active  = bool(global_cfg.get("enabled", True))
    pick_active = not bool(user_cfg.get("paused", False))

    # Compute next scheduled event (ET)
    ET  = pytz.timezone("America/New_York")
    now = datetime.now(ET)
    schedule = [
        ("Morning picks",         8,  30),
        ("10:30 AM confirmation", 10, 30),
        ("3:30 PM close check",   15, 30),
    ]
    next_event = None
    min_mins   = None
    for name, h, m in schedule:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        for days in range(8):
            t = candidate + timedelta(days=days)
            if t > now and t.weekday() < 5:
                mins = int((t - now).total_seconds() / 60)
                if min_mins is None or mins < min_mins:
                    min_mins   = mins
                    next_event = {"name": name, "time": t.strftime("%-I:%M %p ET"), "mins": mins}
                break

    return jsonify({
        "ok":          True,
        "bot_active":  bot_active,
        "pick_active": pick_active,
        "next_event":  next_event,
    })


# Fast company-name → ticker map (no Haiku needed)
# In-memory cache for dynamically resolved crypto names (lives until restart)
_resolved_crypto_cache: dict = {}


def _resolve_crypto_name_dynamic(query: str) -> str | None:
    """Search CoinGecko for a crypto name/symbol and return the ticker symbol.
    Results are cached in _resolved_crypto_cache for the process lifetime."""
    key = query.upper().strip()
    if key in _resolved_crypto_cache:
        return _resolved_crypto_cache[key]
    try:
        import requests as _req
        resp = _req.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": query},
            timeout=5,
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
        if coins:
            symbol = coins[0]["symbol"].upper()
            _resolved_crypto_cache[key] = symbol
            return symbol
    except Exception:
        pass
    return None


_NAME_TO_TICKER = {
    "NVIDIA": "NVDA", "NVIDEA": "NVDA", "NVDIA": "NVDA",
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "AMAZON": "AMZN",
    "GOOGLE": "GOOGL", "ALPHABET": "GOOGL",
    "META": "META", "FACEBOOK": "META",
    "TESLA": "TSLA",
    "COSTCO": "COST",
    "WALMART": "WMT",
    "NETFLIX": "NFLX",
    "BERKSHIRE": "BRK-B",
    "JPMORGAN": "JPM", "JP MORGAN": "JPM",
    "VISA": "V",
    "MASTERCARD": "MA",
    "JOHNSON": "JNJ",
    "UNITEDHEALTH": "UNH",
    "EXXON": "XOM", "EXXONMOBIL": "XOM",
    "CHEVRON": "CVX",
    "PROCTER": "PG", "PROCTERGAMBLE": "PG",
    "HOME DEPOT": "HD", "HOMEDEPOT": "HD",
    "ABBVIE": "ABBV",
    "BROADCOM": "AVGO",
    "SALESFORCE": "CRM",
    "ORACLE": "ORCL",
    "AMD": "AMD", "ADVANCED MICRO": "AMD",
    "INTEL": "INTC",
    "QUALCOMM": "QCOM",
    "ADOBE": "ADBE",
    "PAYPAL": "PYPL",
    "COINBASE": "COIN",
    "PALANTIR": "PLTR",
    "SNOWFLAKE": "SNOW",
    "UBER": "UBER",
    "AIRBNB": "ABNB",
    "SHOPIFY": "SHOP",
    "SPOTIFY": "SPOT",
    "PINTEREST": "PINS",
    "SNAP": "SNAP",
    "TWITTER": "X",
    "ROBINHOOD": "HOOD",
    "RIVIAN": "RIVN",
    "LUCID": "LCID",
    "NIO": "NIO",
    "BAIDU": "BIDU",
    "ALIBABA": "BABA",
    # Crypto name aliases
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH", "ETHER": "ETH",
    "SOLANA": "SOL",
    "DOGECOIN": "DOGE", "DOGE COIN": "DOGE",
    "SHIBA": "SHIB", "SHIBAINUOIN": "SHIB", "SHIBAINU": "SHIB",
    "CARDANO": "ADA",
    "RIPPLE": "XRP",
    "POLKADOT": "DOT",
    "AVALANCHE": "AVAX",
    "CHAINLINK": "LINK",
    "POLYGON": "MATIC", "MATIC": "MATIC",
    "PEPE": "PEPE",
    "BONK": "BONK",
    "WORLDCOIN": "WLD",
}

def _resolve_watchlist_ticker(raw: str) -> str:
    """Resolve company names to tickers without Haiku. Returns uppercased ticker."""
    upper = raw.strip().upper().replace(" ", "").replace(".", "").replace("-", "")
    # Direct name lookup
    if upper in _NAME_TO_TICKER:
        return _NAME_TO_TICKER[upper]
    # Already looks like a ticker (≤6 chars, all alphanum) — trust it
    return raw.strip().upper()


@app.route("/api/miniapp/update_watchlist", methods=["POST"])
def miniapp_update_watchlist():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import get_user_config, save_user_config
    body    = request.get_json(silent=True) or {}
    ucfg    = get_user_config(chat_id)
    # action: "add" | "remove" | "set"
    action  = body.get("action", "set")
    ticker  = (body.get("ticker") or "").strip().upper()
    tickers = body.get("tickers")   # for "set" action

    added_as = ticker
    current = ucfg.get("watchlist", [])
    if action == "add" and ticker:
        # Resolve company name → ticker (e.g. COSTCO → COST, NVIDEA → NVDA)
        resolved = _resolve_watchlist_ticker(ticker)
        added_as = resolved
        if resolved not in current:
            current = current + [resolved]
    elif action == "remove" and ticker:
        current = [t for t in current if t != ticker]
    elif action == "set" and isinstance(tickers, list):
        current = [_resolve_watchlist_ticker(t) for t in tickers if t.strip()]

    ucfg["watchlist"] = current
    save_user_config(chat_id, ucfg)
    return jsonify({"ok": True, "watchlist": current, "added_as": added_as})


@app.route("/api/miniapp/update_exclusions", methods=["POST"])
def miniapp_update_exclusions():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from config_manager import get_user_config, save_user_config
    body    = request.get_json(silent=True) or {}
    ucfg    = get_user_config(chat_id)
    action  = body.get("action", "set")
    sector  = (body.get("sector") or "").strip()
    sectors = body.get("sectors")

    current = ucfg.get("excluded_sectors", [])
    if action == "add" and sector:
        if sector not in current:
            current = current + [sector]
    elif action == "remove" and sector:
        current = [s for s in current if s != sector]
    elif action == "set" and isinstance(sectors, list):
        current = sectors

    ucfg["excluded_sectors"] = current
    save_user_config(chat_id, ucfg)
    return jsonify({"ok": True, "excluded_sectors": current})


@app.route("/api/miniapp/paper_buy", methods=["POST"])
def miniapp_paper_buy():
    """Buy a paper position from the mini app."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    shares = body.get("shares")
    price  = body.get("price")        # optional override; None = use live price
    stop_loss    = body.get("stop_loss")
    target_price = body.get("target_price")
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    if shares is not None:
        try:
            shares = float(shares)
            if shares <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "shares must be a positive number"}), 400
    else:
        shares = 1.0  # default to 1 unit when not provided
    if price is not None:
        try:   price = float(price)
        except (TypeError, ValueError): price = None
    if stop_loss is not None:
        try:   stop_loss = float(stop_loss)
        except (TypeError, ValueError): stop_loss = None
    if target_price is not None:
        try:   target_price = float(target_price)
        except (TypeError, ValueError): target_price = None
    from paper_trader import paper_buy
    try:
        msg = paper_buy(ticker, shares, chat_id, price=price,
                        stop_loss=stop_loss, target_price=target_price)
        ok  = not msg.startswith("❌")
        return jsonify({"ok": ok, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/paper_sell", methods=["POST"])
def miniapp_paper_sell():
    """Sell (close) a paper position from the mini app."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    shares = body.get("shares")   # None = sell all
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    if shares is not None:
        try:
            shares = float(shares)
            if shares <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "shares must be a positive number"}), 400
    from paper_trader import paper_sell
    try:
        msg = paper_sell(ticker, chat_id, shares=shares)
        ok  = not msg.startswith("❌")
        return jsonify({"ok": ok, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/paper_cancel", methods=["POST"])
def miniapp_paper_cancel():
    """Remove a paper position without recording it as a sale (cash refunded)."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    from paper_trader import paper_cancel
    try:
        msg = paper_cancel(ticker, chat_id)
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/miniapp/paper_add_cash", methods=["POST"])
def miniapp_paper_add_cash():
    """Add cash to the user's paper portfolio."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    body   = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if amount is None:
        return jsonify({"error": "amount required"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "amount must be > 0"}), 400
    from paper_trader import paper_add_cash
    try:
        msg = paper_add_cash(amount, chat_id)
        return jsonify({"ok": True, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route("/api/miniapp/paper_reset", methods=["POST"])
def miniapp_paper_reset():
    """Reset the user's paper portfolio to $10,000 with no positions."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403
    from paper_trader import paper_reset
    try:
        paper_reset(chat_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _backtest_fetch_prices(ticker: str) -> tuple[list, list]:
    """
    Fetch 1-year daily price history for a ticker.
    Tries yfinance first; falls back to CoinGecko for crypto tickers
    that yfinance doesn't cover (e.g. HYPE, ASTER, LAB).
    Returns (dates, closes) — both empty lists if nothing found.
    """
    import yfinance as yf

    # ── Try yfinance (stocks + major crypto like BTC-USD) ─────────────────────
    for suffix in ("", "-USD"):
        try:
            hist = yf.Ticker(ticker + suffix).history(period="1y")
            if not hist.empty:
                prices = hist["Close"].dropna()
                dates  = [d.strftime("%Y-%m-%d") for d in prices.index]
                closes = [round(float(p), 4) for p in prices.values]
                if dates:
                    return dates, closes
        except Exception:
            pass

    # ── Fall back to CoinGecko for exotic crypto ──────────────────────────────
    try:
        # Step 1: search for the coin ID by symbol
        search_url = "https://api.coingecko.com/api/v3/search"
        sr = requests.get(search_url, params={"query": ticker}, timeout=10)
        if sr.ok:
            coins = sr.json().get("coins", [])
            # Find best match: symbol matches exactly (case-insensitive)
            coin_id = None
            for c in coins[:10]:
                if c.get("symbol", "").upper() == ticker:
                    coin_id = c["id"]
                    break
            if coin_id:
                # Step 2: fetch 365 days of daily prices
                chart_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                cr = requests.get(chart_url,
                                  params={"vs_currency": "usd", "days": "365", "interval": "daily"},
                                  timeout=15)
                if cr.ok:
                    pts = cr.json().get("prices", [])  # [[timestamp_ms, price], ...]
                    if pts:
                        from datetime import datetime as _dt
                        dates  = [_dt.utcfromtimestamp(p[0] / 1000).strftime("%Y-%m-%d") for p in pts]
                        closes = [round(float(p[1]), 6) for p in pts]
                        return dates, closes
    except Exception:
        pass

    return [], []


@app.route("/api/miniapp/backtest_pick", methods=["GET"])
def miniapp_backtest_pick():
    """
    Backtest a specific pick: given ticker + entry/stop/target,
    simulate how it would have performed over the past year.
    Returns win/loss stats + daily price history for charting.
    """
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    ticker = (request.args.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400

    try:
        entry  = float(request.args.get("entry",  0) or 0)
        stop   = float(request.args.get("stop",   0) or 0)
        target = float(request.args.get("target", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid levels"}), 400

    try:
        dates, closes = _backtest_fetch_prices(ticker)
        if not dates:
            return jsonify({"error": f"No historical data found for {ticker}"}), 404

        # Simulate entries: for each day in the past year where price was
        # within 2% of the entry, measure whether target or stop was hit first.
        wins = losses = skipped = 0
        avg_days_to_exit = []

        if entry > 0 and (stop > 0 or target > 0):
            prices_list = closes
            for i, p in enumerate(prices_list):
                # Entry signal: price within 2% of entry level
                if abs(p - entry) / entry > 0.02:
                    continue
                # Simulate forward from entry
                hit_target = hit_stop = False
                days_held  = 0
                for j in range(i + 1, min(i + 60, len(prices_list))):
                    fwd = prices_list[j]
                    days_held += 1
                    if target > 0 and fwd >= target:
                        hit_target = True
                        break
                    if stop > 0 and fwd <= stop:
                        hit_stop = True
                        break
                if hit_target:
                    wins += 1
                    avg_days_to_exit.append(days_held)
                elif hit_stop:
                    losses += 1
                    avg_days_to_exit.append(days_held)
                else:
                    skipped += 1

        total_sims = wins + losses
        win_rate   = round(wins / total_sims * 100, 1) if total_sims > 0 else None
        avg_hold   = round(sum(avg_days_to_exit) / len(avg_days_to_exit), 1) if avg_days_to_exit else None

        # Risk/reward ratio
        rr = round((target - entry) / (entry - stop), 2) if (entry > 0 and stop > 0 and target > 0 and entry > stop) else None

        return jsonify({
            "ticker":      ticker,
            "entry":       entry,
            "stop":        stop,
            "target":      target,
            "wins":        wins,
            "losses":      losses,
            "skipped":     skipped,
            "win_rate":    win_rate,
            "avg_hold_days": avg_hold,
            "risk_reward": rr,
            "prices":      closes,
            "dates":       dates,
            "current_price": closes[-1] if closes else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── CLI webhook registration ──────────────────────────────────────────────────

if __name__ == "__main__":
    if "--set-webhook" in sys.argv:
        idx = sys.argv.index("--set-webhook")
        url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not url:
            print("Usage: python webhook.py --set-webhook https://your-app.onrender.com/webhook")
            sys.exit(1)
        success = set_webhook(url)
        sys.exit(0 if success else 1)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
