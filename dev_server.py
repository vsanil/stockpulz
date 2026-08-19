"""
dev_server.py — local-only launcher for the preview panel.

Loads .env into os.environ (the preview sandbox can't `source` dotfiles
via bash) and runs the Flask app. Never used in production — Render uses
gunicorn via the dashboard Start Command.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

env_path = os.path.join(ROOT, ".env")
try:
    with open(env_path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())
    print(f"[dev_server] Loaded env from {env_path}")
except OSError as exc:
    print(f"[dev_server] WARNING: could not read .env ({exc}) — relying on inherited env.")

os.environ.setdefault("PORT", "5050")

# ── Local-only safety, all three deliberate ──────────────────────────────────
# 1. Drop the bot token. _miniapp_auth() honours a ?chat_id= param ONLY when no
#    token is configured, so this is what lets the app be opened as a given user
#    outside Telegram — WITHOUT setting MINIAPP_AUTH_DISABLED on production,
#    which would disable cross-user-takeover protection on the live service.
#    It also makes any accidental Telegram send a no-op rather than a real DM.
os.environ.pop("TELEGRAM_BOT_TOKEN", None)

# 2. Never write telemetry from a dev session. The app talks to the REAL gist
#    (that is the point — real positions and picks), but traffic_hours.json and
#    usage_counts.json are evidence used to make product decisions, and hits
#    from a developer poking around are not user behaviour. The usage counter in
#    particular was just repaired after a week of recording nothing; poisoning
#    its first real data would be worse than having none.
from webhook import app  # noqa: E402

import traffic_tracker  # noqa: E402
traffic_tracker.record = lambda *a, **k: False
traffic_tracker.flush = lambda *a, **k: False

@app.before_request
def _dev_block_usage_writes():
    from flask import request, jsonify
    if request.path == "/api/miniapp/usage":
        return jsonify({"ok": True, "dev": "not recorded"}), 200

print("[dev_server] LOCAL MODE — param auth, no Telegram sends, telemetry muted.")
print(f"[dev_server] open http://127.0.0.1:{os.environ['PORT']}/miniapp?chat_id=<your_chat_id>")

# 3. Bind to loopback only — never expose a param-auth server beyond this machine.
app.run(host="127.0.0.1", port=int(os.environ["PORT"]), debug=False)
