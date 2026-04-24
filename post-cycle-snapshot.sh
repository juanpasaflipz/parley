#!/usr/bin/env bash
# ============================================================================
# Parley — post-cycle-snapshot.sh
# ----------------------------------------------------------------------------
# Fires AFTER `python desk/execution.py submit` completes (successfully
# or not). Responsible for:
#
#   1. Reconciling any partial/pending orders into the `fills` table.
#   2. Updating cached `positions` from the fills.
#   3. Writing a row to `nav_snapshots` with current equity.
#   4. Marking the cycle as 'completed' (or 'failed' if submit errored).
#
# This is a hook rather than an LLM step because it MUST run every time.
# If the session crashes between submit and snapshot, the audit log is
# inconsistent; running via hook makes this near-atomic.
# ============================================================================

set -euo pipefail

PROJECT_ROOT="${PARLEY_PROJECT_ROOT:-$(pwd)}"
PYTHON="${PARLEY_PYTHON:-python3}"
LOG_FILE="${PROJECT_ROOT}/incidents/hook-$(date -u +%Y%m%d-%H%M%S).log"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] post-cycle-snapshot: $*" | tee -a "$LOG_FILE" >&2
}

# Hook input may include the exit code of the previous command.
# Claude Code convention: CLAUDE_TOOL_RESULT contains the last tool's output;
# CLAUDE_TOOL_EXIT_CODE contains its exit code.
PREV_EXIT="${CLAUDE_TOOL_EXIT_CODE:-0}"

log "Starting post-cycle reconciliation (prev_exit=$PREV_EXIT)"

# ----------------------------------------------------------------------------
# Step 1-4: all handled by a single Python entrypoint so the work is
# transactional.
# ----------------------------------------------------------------------------
if ! "$PYTHON" -m desk.cycle reconcile --prev-exit "$PREV_EXIT" 2>> "$LOG_FILE"; then
    log "WARNING: desk.cycle reconcile failed. Cycle may be in inconsistent state. Manual intervention required."
    # Do NOT exit non-zero here — the submission may have partially succeeded;
    # failing the hook would prevent the audit log from being written.
    # The reconcile script itself logs the problem to incidents/ for the operator.
fi

log "Post-cycle reconciliation complete"
exit 0
