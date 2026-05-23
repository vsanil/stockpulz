"""
webhook.py — Flask app to receive Telegram bot updates via webhook.
Deploy to Render.com free tier. After deploying, register the webhook URL once:

    python webhook.py --set-webhook https://your-render-url.onrender.com/webhook

Or call the /register endpoint manually.
"""

import os
import sys
import time
import threading
import secrets as _sec
from datetime import datetime as _dt, timedelta as _td
from functools import wraps as _wraps

import requests
from flask import Flask, request, jsonify, session, redirect, send_from_directory

from config_manager import get_config, get_allowed_users
from telegram_notifier import handle_incoming_command, handle_callback_query, set_webhook, send_typing_action, typing_until_done, send_message

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or _sec.token_hex(32)

# ── In-memory magic-link token store: {token: expiry_datetime_utc} ───────────
_admin_tokens: dict = {}


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
button{background:#4f8ef7;color:#fff;border:none;border-radius:10px;padding:13px 24px;font-size:15px;font-weight:600;cursor:pointer;width:100%;transition:opacity .15s}
button:hover{opacity:.88}
button:disabled{opacity:.45;cursor:default}
.msg{margin-top:16px;font-size:13px;color:#7c8899;min-height:20px}
.msg.ok{color:#34d399}
.msg.err{color:#f87171}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#34d399;margin-right:8px;vertical-align:middle}
</style>
</head>
<body>
<div class="card">
  <h1>📈 StockPulz Admin</h1>
  <p class="sub">Send a one-time login link to your Telegram.</p>
  <button id="btn" onclick="go()">Send me a login link</button>
  <p class="msg" id="msg"></p>
</div>
<script>
const errParam = new URLSearchParams(location.search).get('error');
if(errParam){
  const el=document.getElementById('msg');
  el.textContent='Link expired or already used — try again.';
  el.className='msg err';
}
async function go(){
  const btn=document.getElementById('btn'),msg=document.getElementById('msg');
  btn.disabled=true;
  msg.textContent='Sending…';msg.className='msg';
  try{
    const r=await fetch('/admin/request',{method:'POST'});
    const d=await r.json();
    if(d.sent){msg.textContent='Check your Telegram — link expires in 5 min.';msg.className='msg ok';}
    else{msg.textContent='Failed: '+d.error;msg.className='msg err';btn.disabled=false;}
  }catch(e){msg.textContent='Network error — try again.';msg.className='msg err';btn.disabled=false;}
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
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
.metric{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px}
.m-label{font-size:11px;color:var(--muted);margin-bottom:5px;text-transform:uppercase;letter-spacing:.04em}
.m-val{font-size:30px;font-weight:700}
.m-sub{font-size:11px;color:var(--muted);margin-top:3px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px}
.card-title{font-size:11px;font-weight:600;color:var(--muted);margin-bottom:12px;text-transform:uppercase;letter-spacing:.06em}
.row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
.row:last-child{border-bottom:none}
.avatar{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}
.av-blue{background:#1c2e4a;color:var(--accent)}
.av-green{background:#1a2e1a;color:var(--green)}
.u-info{flex:1;min-width:0}
.u-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.u-meta{font-size:11px;color:var(--muted);margin-top:1px}
.pill{font-size:10px;padding:2px 8px;border-radius:20px;font-weight:600;flex-shrink:0}
.p-active{background:rgba(52,211,153,.12);color:var(--green)}
.p-paused{background:rgba(251,191,36,.12);color:var(--amber)}
.p-idle{background:rgba(124,136,153,.12);color:var(--muted)}
.cron-row{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px}
.cron-row:last-child{border-bottom:none}
.cron-left{display:flex;align-items:center;gap:8px}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.d-green{background:var(--green)}
.d-amber{background:var(--amber)}
.d-muted{background:var(--muted);opacity:.4}
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
</style>
</head>
<body>
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
var picks={},activeTab='';

function age(iso){
  if(!iso)return'—';
  try{
    var d=new Date(iso+(iso.endsWith('Z')?'':'Z')),now=new Date();
    var m=Math.round((now-d)/60000);
    if(m<2)return'just now';
    if(m<60)return m+'m ago';
    if(m<1440)return Math.round(m/60)+'h ago';
    return Math.round(m/1440)+'d ago';
  }catch(e){return iso.slice(0,16).replace('T',' ');}
}

function ini(first,uname){
  var s=(first||uname||'??');
  return s.slice(0,2).toUpperCase();
}

function metrics(s){
  return '<div class="metrics">'
    +'<div class="metric"><div class="m-label">Total users</div><div class="m-val">'+s.total_users+'</div><div class="m-sub">'+s.active_today+' active today</div></div>'
    +'<div class="metric"><div class="m-label">Picks today</div><div class="m-val">'+s.total_picks+'</div><div class="m-sub">stocks · crypto · ETFs</div></div>'
    +'<div class="metric"><div class="m-label">Open positions</div><div class="m-val">'+s.open_positions+'</div><div class="m-sub">across all users</div></div>'
    +'<div class="metric"><div class="m-label">Unread feedback</div><div class="m-val">'+s.unread_feedback+'</div><div class="m-sub">&nbsp;</div></div>'
    +'</div>';
}

function users(list){
  if(!list.length)return'<p class="empty">No users yet</p>';
  return list.map(function(u){
    var name=u.first_name||u.username||('user_'+u.id.slice(-4));
    var tag=u.is_admin?' <span style="color:var(--muted);font-weight:400;font-size:11px">(admin)</span>':'';
    var pill=u.paused?'<span class="pill p-paused">paused</span>':u.is_active?'<span class="pill p-active">active</span>':'<span class="pill p-idle">idle</span>';
    var avcls=u.is_admin?'av-green':'av-blue';
    return'<div class="row"><div class="avatar '+avcls+'">'+ini(u.first_name,u.username)+'</div>'
      +'<div class="u-info"><div class="u-name">'+name+tag+'</div>'
      +'<div class="u-meta">'+u.open_positions+' position'+(u.open_positions!==1?'s':'')
      +' &middot; last seen '+age(u.last_seen)+'</div></div>'+pill+'</div>';
  }).join('');
}

var SCHED={
  morning:'7:00 AM ET',premarket:'8:45 AM ET',confirmation:'10:30 AM ET',
  close_check:'3:30 PM ET',eod_summary:'4:15 PM ET',prescreener:'11:00 PM ET',
  price_alerts:'every 30 min',weekly:'Sat 8:00 AM',week_ahead:'Sun 8:00 AM'
};

function cron(c,lastMorning){
  return Object.keys(SCHED).map(function(k){
    var last=c[k]||(k==='morning'?lastMorning:'');
    var dc=last?'d-green':'d-muted';
    return'<div class="cron-row"><span class="cron-left"><span class="dot '+dc+'"></span>'+k.replace(/_/g,' ')+'</span>'
      +'<span class="cron-time">'+(last?age(last):'not yet run')+' &middot; '+SCHED[k]+'</span></div>';
  }).join('');
}

function pickRows(arr,isCrypto){
  if(!arr||!arr.length)return'<p class="empty">No picks</p>';
  return arr.map(function(p){
    var sym=isCrypto?(p.symbol||p.ticker||''):(p.ticker||p.symbol||'');
    var entry=p.entry_price||'—',tgt=p.target_price||'—',stop=p.stop_loss;
    var det=stop?'entry $'+entry+' &middot; tgt $'+tgt+' &middot; stop $'+stop:'entry $'+entry+' &middot; tgt $'+tgt;
    return'<div class="pick-row"><span class="ticker">'+sym+'</span><span class="detail">'+det+'</span></div>';
  }).join('');
}

var TABS=[
  ['stocks_st','ST Stocks',false],['stocks_lt','LT Stocks',false],
  ['crypto_st','Crypto ST',true],['crypto_lt','Crypto LT',true],
  ['etfs_st','ETF ST',false],['etfs_lt','ETF LT',false]
];

function picksPanel(p){
  picks=p;
  var avail=TABS.filter(function(t){return p[t[0]]&&p[t[0]].length;});
  if(!avail.length)return'<p class="empty">No picks today</p>';
  if(!avail.find(function(t){return t[0]===activeTab;}))activeTab=avail[0][0];
  var tabsHtml=avail.map(function(t){
    return'<button class="tab'+(t[0]===activeTab?' on':'')+'" onclick="switchTab(\''+t[0]+'\')">'
      +t[1]+' ('+p[t[0]].length+')</button>';
  }).join('');
  var isCrypto=activeTab.startsWith('crypto');
  return'<div class="tabs" id="tabs">'+tabsHtml+'</div><div id="pcontent">'+pickRows(p[activeTab],isCrypto)+'</div>';
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
    var name=f.first_name||f.username||('user_'+f.chat_id.slice(-4));
    var u=f.read?'':'<span class="unread"></span>';
    return'<div class="fb-row"><div class="fb-meta">'+u+name+' &middot; '+age(f.submitted_at)+'</div>'
      +'<div class="fb-text">'+f.text+'</div></div>';
  }).join('');
}

async function load(){
  var r;
  try{r=await fetch('/admin/data');}
  catch(e){document.getElementById('root').innerHTML='<p class="empty">Network error — retrying in 60s</p>';return;}
  if(r.status===302||r.status===401){window.location='/admin/login';return;}
  if(!r.ok){document.getElementById('root').innerHTML='<p class="empty">Error '+r.status+'</p>';return;}
  var d=await r.json();
  document.getElementById('root').innerHTML=
    metrics(d.stats)
    +'<div class="grid2">'
      +'<div class="card"><div class="card-title">Users</div>'+users(d.users)+'</div>'
      +'<div class="card"><div class="card-title">Cron health</div>'+cron(d.cron,d.last_morning_run)+'</div>'
    +'</div>'
    +'<div class="grid2">'
      +'<div class="card"><div class="card-title">Today\'s picks</div>'+picksPanel(d.picks)+'</div>'
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


@app.route("/admin/request", methods=["POST"])
def admin_request_link():
    """Generate a one-time magic link and send it to the owner's Telegram."""
    # Purge expired tokens
    now = _dt.utcnow()
    for t in list(_admin_tokens):
        if _admin_tokens[t] < now:
            del _admin_tokens[t]

    token = _sec.token_urlsafe(32)
    _admin_tokens[token] = now + _td(minutes=5)

    base = (os.environ.get("RENDER_EXTERNAL_URL") or request.host_url).rstrip("/")
    link = f"{base}/admin/verify?t={token}"

    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not owner:
        return jsonify({"sent": False, "error": "TELEGRAM_CHAT_ID not set"}), 500

    send_message(
        f"🔐 <b>StockPulz Admin login link</b>\n\nExpires in 5 minutes — single use only.\n\n{link}",
        chat_id=owner,
    )
    return jsonify({"sent": True})


@app.route("/admin/verify")
def admin_verify():
    """Validate the magic-link token, set session cookie, redirect to dashboard."""
    token = request.args.get("t", "")
    now = _dt.utcnow()
    expiry = _admin_tokens.get(token)
    if not expiry or now > expiry:
        return redirect("/admin/login?error=expired")
    del _admin_tokens[token]          # single-use: consume immediately
    session["admin"] = True
    session.permanent = False         # session expires when browser closes
    return redirect("/admin")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")


@app.route("/admin")
@_require_admin
def admin_dashboard():
    return _ADMIN_DASH_HTML, 200


@app.route("/admin/data")
@_require_admin
def admin_data():
    """Return JSON payload for the dashboard."""
    from config_manager import (
        get_allowed_users, get_user_config, load_picks,
        load_user_trade_log, load_feedback, get_config,
        count_unread_feedback,
    )

    cfg     = get_config()
    owner   = os.environ.get("TELEGRAM_CHAT_ID", "")
    now_utc = _dt.utcnow()
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

    return jsonify({
        "stats": {
            "total_users":      len(users),
            "active_today":     active_today,
            "total_picks":      total_picks,
            "open_positions":   total_open,
            "unread_feedback":  count_unread_feedback(),
        },
        "users": users,
        "picks": {
            "stocks_st":      st,      "stocks_lt": lt,
            "crypto_st":      cst,     "crypto_lt": clt,
            "etfs_st":        est,     "etfs_lt":   elt,
            "commodities_st": comm_st, "commodities_lt": comm_lt,
            "options_plays":  opts,
        },
        "feedback":        load_feedback()[:20],
        "cron":            cron,
        "last_morning_run": cfg.get("last_morning_run", ""),
    })


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

        picks["_meta"] = {
            "weekend":    is_weekend,
            "has_picks":  has_picks,
            "day":        now_et.strftime("%A"),
            "picks_date": picks_date,       # ISO date string or None
            "today":      now_et.strftime("%Y-%m-%d"),
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
    """Return open positions with live P&L for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_user_trade_log
    from datetime import date as _date
    from market_data import get_live_price as _price

    log      = load_user_trade_log(chat_id)
    open_pos = log.get("open", [])

    result = []
    today  = _date.today()
    for t in open_pos:
        sym   = t["ticker"]
        entry = t.get("entry_price")
        cur   = _price(sym)
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

    entry_override  = body.get("entry_price")
    stop_override   = body.get("stop_loss")
    shares_override = body.get("shares")

    from trade_logger import add_holding
    from config_manager import load_picks
    picks = load_picks() or {}
    trade, existed = add_holding(ticker, chat_id, picks=picks,
                                 entry_override=entry_override,
                                 stop_override=stop_override,
                                 shares_override=shares_override)

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
    """Return 3-month daily OHLCV data for the Mini App chart overlay."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    ticker     = ticker.upper()
    asset_type = request.args.get("asset_type", "")
    is_crypto  = asset_type == "crypto" or ticker in _CHART_CRYPTO

    try:
        from market_data import get_ohlcv
        data = get_ohlcv(ticker)

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
        return jsonify({"ticker": ticker, "data": data})
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

    add_feedback(chat_id, text, username=username, first_name=first_name)

    # Notify admin
    try:
        from telegram_api import send_message as _send
        admin_id  = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
        name_str  = first_name or f"user {chat_id}"
        uname_str = f"  @{username}" if username else ""
        _send(
            f"💬 <b>New feedback</b> (miniapp) from <b>{name_str}</b>{uname_str}\n\n{text}",
            admin_id,
        )
    except Exception:
        pass

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
    try:
        import yfinance as _yf
        yf_sym = f"{ticker}-USD" if ticker in crypto_set else ticker
        fi     = _yf.Ticker(yf_sym).fast_info
        price  = getattr(fi, "last_price", None)
        prev   = getattr(fi, "previous_close", None)
        if price:
            price  = float(price)
            change = round((price - float(prev)) / float(prev) * 100, 2) if prev and float(prev) > 0 else None
            _quote_cache[ticker] = {"price": price, "change_pct": change, "ts": now}
            return price, change
    except Exception:
        pass
    return None, None


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
        price, change_pct = _fast_quote(ticker, _ALERT_CRYPTO)
        if price is not None:
            return jsonify({"ticker": ticker, "price": round(price, 6),
                            "change_pct": change_pct})
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
                                        "change_pct": change_pct})
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


@app.route("/api/miniapp/regime")
def miniapp_regime():
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
    from market_regime import get_market_regime
    try:
        r = get_market_regime()
        return jsonify({"ok": True, "regime": r})
    except Exception as e:
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


@app.route("/api/miniapp/performance")
def miniapp_performance():
    """Return bot track record for the Mini App performance tab."""
    chat_id = _miniapp_auth()
    if not chat_id: return jsonify({"error": "unauthorised"}), 403
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
        return jsonify({
            "ok":           True,
            "stats":        stats,
            "context_30d":  ctx30,
            "period_stats": period_stats,
        })
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
