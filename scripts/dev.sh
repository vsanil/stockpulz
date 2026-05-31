#!/bin/bash
# dev.sh — Start local dev environment in one command.
# Usage: ./scripts/dev.sh
#
# What it does:
#   1. Loads .env
#   2. Starts Flask on port 5000
#   3. Opens a cloudflared tunnel
#   4. Registers the tunnel URL as the Telegram webhook

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
ENV_FILE="$ROOT/.env"

# ── Load .env ──────────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "❌  .env not found at $ENV_FILE"
  echo "    Copy your secrets there first — see README."
  exit 1
fi
set -a && source "$ENV_FILE" && set +a
echo "✅  Loaded .env"

# ── Kill any leftover processes ────────────────────────────────────────────────
pkill -f "python3 webhook.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

# ── Start Flask ────────────────────────────────────────────────────────────────
cd "$ROOT"
python3 webhook.py > /tmp/flask.log 2>&1 &
FLASK_PID=$!
echo "🚀  Flask started (pid $FLASK_PID) — logs: tail -f /tmp/flask.log"

# Wait for Flask to be ready
for i in $(seq 1 20); do
  if curl -s http://localhost:5000/ > /dev/null 2>&1; then
    break
  fi
  sleep 0.3
done
echo "✅  Flask is up at http://localhost:5000"

# ── Start cloudflared tunnel ───────────────────────────────────────────────────
cloudflared tunnel --url http://localhost:5000 > /tmp/cf.log 2>&1 &
CF_PID=$!
echo "🌐  cloudflared started (pid $CF_PID) — waiting for URL..."

# Wait for tunnel URL
TUNNEL_URL=""
for i in $(seq 1 30); do
  TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cf.log 2>/dev/null | head -1)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  sleep 0.5
done

if [ -z "$TUNNEL_URL" ]; then
  echo "❌  Could not get cloudflared tunnel URL. Check /tmp/cf.log"
  exit 1
fi
echo "✅  Tunnel: $TUNNEL_URL"

# ── Register Telegram webhook ──────────────────────────────────────────────────
python3 "$ROOT/webhook.py" --set-webhook "$TUNNEL_URL/webhook"
echo "✅  Telegram webhook registered"

# ── Print summary ──────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅  Dev environment ready!"
echo ""
echo "  🤖  Telegram bot:  @TheStockPulzBot"
echo "  📱  Miniapp (browser): http://localhost:5000/miniapp?chat_id=$TELEGRAM_CHAT_ID"
echo "  📱  Miniapp (Telegram): $TUNNEL_URL/miniapp?chat_id=$TELEGRAM_CHAT_ID"
echo "  📋  Flask logs:    tail -f /tmp/flask.log"
echo "  🌐  Tunnel logs:   tail -f /tmp/cf.log"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Press Ctrl+C to stop everything."
echo ""

# ── Keep running and clean up on exit ─────────────────────────────────────────
trap "echo ''; echo 'Stopping...'; kill $FLASK_PID $CF_PID 2>/dev/null; exit 0" INT TERM
wait
