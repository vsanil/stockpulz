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

from webhook import app  # noqa: E402

app.run(host="127.0.0.1", port=int(os.environ["PORT"]), debug=False)
