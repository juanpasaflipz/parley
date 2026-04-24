#!/usr/bin/env bash
# ============================================================================
# Parley — session-start-check.sh
# ----------------------------------------------------------------------------
# Fires once at the start of each Claude Code session. Purpose: give the
# operator a clear status banner and catch common misconfigurations
# before the supervisor tries to run a cycle.
#
# Output is shown to the operator in the terminal. Non-fatal warnings
# do not block; fatal misconfigurations exit non-zero and the supervisor
# will refuse to start a cycle until fixed.
# ============================================================================

set -euo pipefail

PROJECT_ROOT="${PARLEY_PROJECT_ROOT:-$(pwd)}"
SETTINGS_FILE="${PROJECT_ROOT}/.claude/settings.json"
ENV_FILE="${PROJECT_ROOT}/.env"

RED=$'\033[0;31m'
YELLOW=$'\033[0;33m'
GREEN=$'\033[0;32m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

warn() { echo "${YELLOW}⚠  $*${RESET}"; }
ok()   { echo "${GREEN}✓  $*${RESET}"; }
fail() { echo "${RED}✗  $*${RESET}"; exit 1; }

echo ""
echo "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo "${BOLD}  Parley — session startup check${RESET}"
echo "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

# Settings
if [[ ! -f "$SETTINGS_FILE" ]]; then
    fail ".claude/settings.json not found"
fi

MODE=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE'))['mode'])" 2>/dev/null || echo "unknown")
case "$MODE" in
    paper)
        ok "Mode: ${BOLD}paper${RESET}${GREEN} (Binance testnet)${RESET}"
        ;;
    live)
        echo "${RED}${BOLD}⚠  ⚠  ⚠   MODE IS LIVE   ⚠  ⚠  ⚠${RESET}"
        echo "${RED}You are in live trading mode. Real money is at risk.${RESET}"
        echo "${RED}Phase 1 hard rule: paper only. If this is not intentional,${RESET}"
        echo "${RED}edit .claude/settings.json now.${RESET}"
        ;;
    *)
        fail "Mode '$MODE' is not recognized. Expected 'paper' or 'live'."
        ;;
esac

# .env
if [[ ! -f "$ENV_FILE" ]]; then
    fail ".env not found. Copy .env.example to .env and fill in."
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${DATABASE_URL:-}" ]]; then
    fail "DATABASE_URL not set in .env"
fi
ok "DATABASE_URL present"

if [[ -z "${BINANCE_TESTNET_API_KEY:-}" ]]; then
    warn "BINANCE_TESTNET_API_KEY not set — cycles will fail at submission step"
else
    ok "BINANCE_TESTNET_API_KEY present"
fi

# Database reachability
if psql "$DATABASE_URL" -c "SELECT 1;" > /dev/null 2>&1; then
    ok "Database reachable"
else
    fail "Database unreachable. Check DATABASE_URL and network."
fi

# Schema present?
SCHEMA_CHECK=$(psql "$DATABASE_URL" -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='cycles';" 2>/dev/null || echo "0")
if [[ "$SCHEMA_CHECK" -lt 1 ]]; then
    fail "Schema not initialized. Run: psql \$DATABASE_URL -f schema.sql"
fi
ok "Schema initialized"

# Active desk_config?
ACTIVE_CONFIG=$(psql "$DATABASE_URL" -tAc "SELECT COUNT(*) FROM desk_configs WHERE is_active=TRUE;" 2>/dev/null || echo "0")
if [[ "$ACTIVE_CONFIG" -lt 1 ]]; then
    warn "No active desk_config. Run /new-config before /run-cycle."
else
    CONFIG_NAME=$(psql "$DATABASE_URL" -tAc "SELECT name FROM desk_configs WHERE is_active=TRUE LIMIT 1;" 2>/dev/null)
    ok "Active config: ${BOLD}$CONFIG_NAME${RESET}"
fi

# Last cycle?
LAST_CYCLE=$(psql "$DATABASE_URL" -tAc "SELECT started_at||' '||status FROM cycles ORDER BY started_at DESC LIMIT 1;" 2>/dev/null || echo "")
if [[ -n "$LAST_CYCLE" ]]; then
    echo "   Last cycle: $LAST_CYCLE"
fi

# Any running cycle (that would block a new one)?
RUNNING=$(psql "$DATABASE_URL" -tAc "SELECT COUNT(*) FROM cycles WHERE status='running';" 2>/dev/null || echo "0")
if [[ "$RUNNING" -gt 0 ]]; then
    warn "$RUNNING cycle(s) in 'running' state. Resolve before starting new cycle."
fi

echo "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
exit 0
