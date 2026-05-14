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

    text    = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text or not chat_id:
        return jsonify({"status": "ignored", "reason": "empty text or chat_id"}), 200

    print(f"[webhook] Received from {chat_id}: {text!r}")

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
    stocks = picks_raw.get("stocks", {})
    crypto = picks_raw.get("crypto", {})
    etfs   = picks_raw.get("etfs",   {})
    st  = stocks.get("short_term", [])
    lt  = stocks.get("long_term",  [])
    cst = crypto.get("short_term", [])
    clt = crypto.get("long_term",  [])
    est = etfs.get("short_term",   [])
    elt = etfs.get("long_term",    [])
    total_picks = len(st) + len(lt) + len(cst) + len(clt) + len(est) + len(elt)

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
            "stocks_st": st,  "stocks_lt": lt,
            "crypto_st": cst, "crypto_lt": clt,
            "etfs_st":   est, "etfs_lt":   elt,
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


def _miniapp_auth() -> str | None:
    """
    Extract and validate chat_id from the Mini App request.
    Returns chat_id string if authorised, None otherwise.
    The Mini App passes chat_id + init_data as query params (GET) or JSON body (POST).
    We do a lightweight check: chat_id must be in allowed_users.
    (Full Telegram initData HMAC validation can be added later.)
    """
    if request.method == "POST":
        body    = request.get_json(silent=True) or {}
        chat_id = str(body.get("chat_id", "")).strip()
    else:
        chat_id = str(request.args.get("chat_id", "")).strip()

    if not chat_id:
        return None
    allowed = get_allowed_users()
    # Also allow the owner even if not in allowed_users list
    owner = os.environ.get("TELEGRAM_CHAT_ID", "")
    if chat_id not in allowed and chat_id != owner:
        return None
    return chat_id


@app.route("/api/miniapp/picks")
def miniapp_picks():
    """Return today's picks for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_picks, get_user_config
    picks = load_picks() or {}
    ucfg  = get_user_config(chat_id)

    # Filter assets based on user preference
    assets = ucfg.get("assets", "both")
    if assets == "stocks":
        picks.pop("crypto", None)
    elif assets == "crypto":
        picks.pop("stocks", None)
        picks.pop("etfs", None)

    return jsonify(picks)


@app.route("/api/miniapp/positions")
def miniapp_positions():
    """Return open positions with live P&L for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import load_user_trade_log
    from datetime import date as _date
    import yfinance as _yf

    log      = load_user_trade_log(chat_id)
    open_pos = log.get("open", [])

    _CRYPTO_SYMBOLS = {
        "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","MATIC",
        "LINK","UNI","ATOM","LTC","BCH","ALGO","XLM","VET","ICP","FIL",
        "TRX","NEAR","OP","ARB","SUI","APT","INJ","SEI","TIA","HYPE",
    }

    def _price(ticker):
        ticker = ticker.upper()
        yf_sym = f"{ticker}-USD" if ticker in _CRYPTO_SYMBOLS else ticker
        try:
            fi = _yf.Ticker(yf_sym).fast_info
            p  = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            if p: return float(p)
        except Exception:
            pass
        return None

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
        result.append({
            "ticker":        sym,
            "symbol":        sym,
            "asset_type":    t.get("asset_type", "stock"),
            "entry_price":   entry,
            "current_price": round(cur, 4) if cur else None,
            "target_price":  t.get("target_price"),
            "stop_loss":     t.get("stop_loss"),
            "pnl_pct":       pnl,
            "days_held":     days,
        })

    return jsonify({"positions": result})


@app.route("/api/miniapp/stats")
def miniapp_stats():
    """Return trade stats + closed trade history for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from trade_logger import get_performance_stats
    from config_manager import load_user_trade_log

    stats  = get_performance_stats(chat_id) or {}
    log    = load_user_trade_log(chat_id)
    closed = sorted(
        log.get("closed", []),
        key=lambda t: t.get("closed_date", ""),
        reverse=True,
    )

    # Map field names for the Mini App
    mapped = {
        "count":                 stats.get("count", 0),
        "wins":                  stats.get("wins", 0),
        "losses":                stats.get("losses", 0),
        "win_rate":              stats.get("win_rate", 0),
        "avg_gain":              stats.get("avg_gain", 0),
        "avg_loss":              stats.get("avg_loss", 0),
        "best":                  list(stats["best"])  if stats.get("best")  else ["—", 0],
        "worst":                 list(stats["worst"]) if stats.get("worst") else ["—", 0],
        "cumulative_return_pct": stats.get("cumulative_return_pct", 0),
        "streak":                stats.get("streak", 0),
    } if stats else {}

    return jsonify({"stats": mapped, "closed": closed})


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

    from trade_logger import add_holding
    from config_manager import load_picks
    picks = load_picks() or {}
    trade, existed = add_holding(ticker, chat_id, picks=picks)
    return jsonify({"ok": True, "existed": existed, "trade": trade})


@app.route("/api/miniapp/settings")
def miniapp_settings():
    """Return the user's settings for the Mini App."""
    chat_id = _miniapp_auth()
    if not chat_id:
        return jsonify({"error": "unauthorised"}), 403

    from config_manager import get_user_config
    ucfg = get_user_config(chat_id)
    return jsonify({"settings": {
        "risk_profile":   ucfg.get("risk_profile", "moderate"),
        "assets":         ucfg.get("assets", "both"),
        "stock_budget":   ucfg.get("stock_budget"),
        "crypto_budget":  ucfg.get("crypto_budget"),
        "paused":         ucfg.get("paused", False),
    }})


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
