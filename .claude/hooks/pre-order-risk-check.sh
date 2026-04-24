#!/usr/bin/env bash
# ============================================================================
# Parley — pre-order-risk-check.sh
# ----------------------------------------------------------------------------
# This hook fires BEFORE any `python desk/execution.py submit` command.
# It is the last gate between an LLM-approved order and the broker.
#
# It enforces four things, all in shell (no LLM, no Python business logic):
#
#   1. Mode check: if .claude/settings.json mode != "paper", we must have
#      an explicit BINANCE_TESTNET_REST_URL — we NEVER allow this hook
#      to pass while pointing at production endpoints in Phase 1.
#
#   2. Environment check: required env vars are present.
#
#   3. Database reachability: DATABASE_URL must respond to a trivial
#      `SELECT 1`. If the DB is down, we do not submit orders we
#      can't log.
#
#   4. Risk engine pre-flight: calls
#      `python desk/risk_engine.py validate-pending --strict` which
#      re-checks every pending order against current risk_limits.
#      Any failure aborts the submission.
#
# Exit codes:
#   0  → proceed with submission
#   1  → block submission (non-fatal; operator sees the reason)
#   2  → block submission due to critical safety violation (alert)
# ============================================================================

set -euo pipefail

PROJECT_ROOT="${PARLEY_PROJECT_ROOT:-$(pwd)}"
SETTINGS_FILE="${PROJECT_ROOT}/.claude/settings.json"
LOG_FILE="${PROJECT_ROOT}/incidents/hook-$(date -u +%Y%m%d-%H%M%S).log"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE" >&2
}

fail_hard() {
    log "CRITICAL: $*"
    log "Aborting order submission. This is a hard-rule enforcement."
    exit 2
}

fail_soft() {
    log "BLOCKED: $*"
    exit 1
}

# ----------------------------------------------------------------------------
# 1. Mode check
# ----------------------------------------------------------------------------
if [[ ! -f "$SETTINGS_FILE" ]]; then
    fail_hard "Settings file not found at $SETTINGS_FILE"
fi

MODE=$(python3 -c "import json,sys; print(json.load(open('$SETTINGS_FILE'))['mode'])" 2>/dev/null || echo "unknown")

if [[ "$MODE" != "paper" ]]; then
    fail_hard "Mode is '$MODE', not 'paper'. Phase 1 hard rule: paper only. If you intend to trade live, this hook must be explicitly replaced in a named live configuration with operator confirmation. It is never flipped by default."
fi

log "Mode check passed: paper"

# ----------------------------------------------------------------------------
# 2. Environment check
# ----------------------------------------------------------------------------
ENV_FILE="${PROJECT_ROOT}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    fail_hard ".env file not found at $ENV_FILE"
fi

# Source without exporting full contents — just check required keys exist
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

REQUIRED_VARS=(
    "DATABASE_URL"
    "BINANCE_TESTNET_API_KEY"
    "BINANCE_TESTNET_API_SECRET"
    "BINANCE_TESTNET_REST_URL"
)

for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        fail_hard "Required env var $var is missing or empty"
    fi
done

# Explicitly verify we are NOT pointing at live
if [[ "$BINANCE_TESTNET_REST_URL" != *"testnet"* ]]; then
    fail_hard "BINANCE_TESTNET_REST_URL does not contain 'testnet': $BINANCE_TESTNET_REST_URL"
fi

# Explicitly ensure no live credentials leaked into the environment
if [[ -n "${BINANCE_LIVE_API_KEY:-}" ]]; then
    fail_hard "BINANCE_LIVE_API_KEY is set in environment. Live credentials must not be present during paper mode."
fi

log "Environment check passed"

# ----------------------------------------------------------------------------
# 3. Database reachability
# ----------------------------------------------------------------------------
if ! psql "$DATABASE_URL" -c "SELECT 1;" > /dev/null 2>&1; then
    fail_hard "Cannot reach DATABASE_URL. Orders must be loggable to the audit trail before submission."
fi

log "Database reachability check passed"

# ----------------------------------------------------------------------------
# 4. Risk engine pre-flight
# ----------------------------------------------------------------------------
PYTHON="${PARLEY_PYTHON:-python3}"

if ! "$PYTHON" desk/risk_engine.py validate-pending --strict 2>> "$LOG_FILE"; then
    fail_soft "risk_engine.py validate-pending failed. See $LOG_FILE"
fi

log "Risk engine pre-flight passed"

# ----------------------------------------------------------------------------
# Allow submission
# ----------------------------------------------------------------------------
log "All pre-order checks passed. Proceeding with submission."
exit 0
