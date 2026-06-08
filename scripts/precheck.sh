#!/bin/bash
# precheck.sh — Run all checks before deploying.
# Usage: ./scripts/precheck.sh
#
# Checks:
#   1. Python syntax on all key files
#   2. Full pytest suite
#   3. Frontend JS syntax
#   4. Optional: dry-run morning picks (pass --dry to enable)

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
cd "$ROOT"

PASS=0
FAIL=0
DRY_RUN_MODE=false

for arg in "$@"; do
  [ "$arg" = "--dry" ] && DRY_RUN_MODE=true
done

ok()   { echo "✅  $1"; PASS=$((PASS+1)); }
fail() { echo "❌  $1"; FAIL=$((FAIL+1)); }

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🔍  StockPulz pre-deploy checks"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. Python syntax ───────────────────────────────────────────────────────────
echo "▶  Syntax check..."
PYFILES="agent.py webhook.py screener.py formatters.py bot_commands.py cmd_trades.py storage.py config_manager.py price_checker.py price_alert_manager.py"
ALL_OK=true
for f in $PYFILES; do
  if python3 -m py_compile "$f" 2>/dev/null; then
    :
  else
    fail "Syntax error in $f"
    ALL_OK=false
  fi
done
$ALL_OK && ok "Python syntax clean ($PYFILES)"

# ── 2. Pytest ─────────────────────────────────────────────────────────────────
echo ""
echo "▶  Running test suite..."
if python3 -m pytest tests/ -q --tb=short 2>&1; then
  ok "All tests passed"
else
  fail "Tests failed — fix before deploying"
fi

# ── 3. JS syntax ──────────────────────────────────────────────────────────────
echo ""
echo "▶  Frontend JS syntax..."
if python3 scripts/check_js.py 2>/dev/null; then
  ok "JS syntax clean"
else
  fail "JS syntax error in miniapp/index.html"
fi

# ── 4. Dry-run morning picks (optional) ───────────────────────────────────────
if $DRY_RUN_MODE; then
  echo ""
  echo "▶  Dry-run morning picks (MOCK_DATA + DRY_RUN)..."
  if MOCK_DATA=true DRY_RUN=true RUN_MODE=morning timeout 60 python3 agent.py 2>&1 | grep -E "DRY RUN|ERROR|Traceback" | head -20; then
    ok "Dry run completed"
  else
    fail "Dry run failed or timed out"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ $FAIL -eq 0 ]; then
  echo "  ✅  All $PASS checks passed — safe to deploy"
else
  echo "  ❌  $FAIL check(s) failed, $PASS passed — fix before deploying"
  exit 1
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
