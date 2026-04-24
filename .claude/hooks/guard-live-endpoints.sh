#!/usr/bin/env bash
# ============================================================================
# Parley — guard-live-endpoints.sh
# ----------------------------------------------------------------------------
# Fires before any `python desk/broker.py *` command.
#
# Defense in depth: even if something else goes wrong (corrupted settings,
# accidental edit), this hook checks the command ABOUT TO BE RUN for any
# reference to the live Binance URL or live credentials, and blocks it.
#
# The hook receives the command via stdin or $CLAUDE_TOOL_INPUT depending on
# the Claude Code version. We check both.
# ============================================================================

set -euo pipefail

PROJECT_ROOT="${PARLEY_PROJECT_ROOT:-$(pwd)}"
SETTINGS_FILE="${PROJECT_ROOT}/.claude/settings.json"

# Read the command that's about to execute
INPUT="${CLAUDE_TOOL_INPUT:-}"
if [[ -z "$INPUT" ]] && [[ ! -t 0 ]]; then
    INPUT=$(cat)
fi

# Self-filter: Claude Code matchers only filter by tool name, so this fires
# on every Bash call. Only inspect commands that touch the broker module.
case "$INPUT" in
    *"desk/broker.py"*|*"desk.broker"*|*"BINANCE_LIVE"*) ;;
    *) exit 0 ;;
esac

# Normalize for matching
INPUT_LOWER=$(echo "$INPUT" | tr '[:upper:]' '[:lower:]')

fail() {
    echo "[guard-live-endpoints] BLOCKED: $*" >&2
    echo "[guard-live-endpoints] Command was: $INPUT" >&2
    exit 2
}

# Check current mode
MODE=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE'))['mode'])" 2>/dev/null || echo "unknown")

if [[ "$MODE" != "paper" ]]; then
    # In any non-paper mode, this hook refuses to approve by default.
    # Live mode requires a different, explicitly-named live hook suite.
    fail "Mode is '$MODE'. This hook only approves paper-mode broker calls."
fi

# Patterns that indicate a live-endpoint call — block outright
FORBIDDEN_PATTERNS=(
    "api.binance.com"
    "stream.binance.com"
    "binance_live_api_key"
    "binance_live_api_secret"
    "--live"
    "--mode=live"
    "--mode live"
)

for pat in "${FORBIDDEN_PATTERNS[@]}"; do
    if echo "$INPUT_LOWER" | grep -qF "$pat"; then
        fail "Command references forbidden live endpoint or credential pattern: '$pat'"
    fi
done

exit 0
