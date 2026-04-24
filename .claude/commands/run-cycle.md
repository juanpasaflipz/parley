---
description: Run one full paper-trading cycle. Orchestrates all five subagents and the deterministic code gates in sequence. Use --dry to run agents but not submit orders.
argument-hint: "[--dry]"
allowed-tools: Task, Read, Bash, Write
---

# /run-cycle

Execute one full trading desk cycle per the procedure in CLAUDE.md §
"The cycle (your core loop)."

Arguments: `$ARGUMENTS`

If `$ARGUMENTS` contains `--dry`, set `DRY_RUN=1` and skip step 12
(submit orders). All other steps still run, and all audit rows are still
written — dry mode is for agent-output validation, not to hide decisions
from the log.

## Procedure

Follow the 13 steps in CLAUDE.md exactly. The compressed version, for
orientation:

1. Insert a `cycles` row with status `running`. Capture `cycle_id`.
2. `bash: python -m desk.cycle gather-research --cycle $CYCLE_ID` —
   returns JSON with news, onchain, sentiment, macro.
3. Delegate to **research-analyst** subagent. Pass the JSON + prior
   thesis per asset. Insert the five theses into `research_theses`.
4. For each instrument in the universe, `bash: python -m desk.cycle
   fetch-bars --cycle $CYCLE_ID --symbol $SYM --tf 1h --limit 200`.
   Each writes a CSV path.
5. For each instrument (parallel, up to 3 at a time), delegate to
   **quant** subagent with its CSV path. Insert signals into
   `quant_signals`.
6. `bash: python -m desk.cycle portfolio --cycle $CYCLE_ID` — returns
   NAV, cash, positions JSON.
7. Delegate to **portfolio-manager** subagent. Insert proposals into
   `pm_proposals`.
8. `bash: python desk/risk_engine.py prefilter --cycle $CYCLE_ID` —
   hard risk gate, **NOT an LLM step**. Blocked proposals logged to
   `risk_events`. Exit non-zero if a critical limit fires; supervisor
   stops and surfaces.
9. Delegate to **risk-manager** subagent with surviving proposals +
   regime data. Insert decisions into `risk_decisions`.
10. `bash: python desk/execution.py build-orders --cycle $CYCLE_ID` —
    compute `delta_qty` deterministically.
11. For each approved decision, delegate to **execution-trader**
    subagent with the decision + market snapshot. Insert orders into
    `orders`.
12. If NOT `--dry`: `bash: python desk/execution.py submit --cycle
    $CYCLE_ID`. This triggers `pre-order-risk-check.sh` automatically.
    The `post-cycle-snapshot.sh` hook fires after and updates positions
    and `nav_snapshots`.
13. Update `cycles.status = 'completed'` (or `'failed'` with error).

## Reporting

At the end, output a compact status table:

```
Cycle <id> completed in <Ns>.
  Theses:     BTC <stance/conviction>, ETH …, SOL …
  Signals:    N total across N strategies
  Proposals:  N (N approved, N resized, N rejected)
  Orders:     N submitted, N filled, N pending
  NAV:        $X,XXX.XX  (Δ +X.XX%)
  Cost:       ~$N.NN in Claude usage (approx)
```

If ANY step failed, say which step, surface the error, and do NOT
attempt recovery.

## Hard rules reminder

- You CANNOT skip the hard risk gate (step 8) under any circumstance.
- You CANNOT submit orders in any mode other than `paper`.
- You CANNOT mutate `risk_limits`, `risk_events`, `fills`, or
  `agent_runs` rows except through the prescribed code paths.
- If anything seems wrong, stop and write to `incidents/`.
