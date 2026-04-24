---
description: Reconstruct and explain a past cycle. Pulls all agent outputs, risk decisions, orders, and fills for a given cycle_id, then walks the decision chain step-by-step. Use after unexpected outcomes to understand why the desk decided what it decided.
argument-hint: "<cycle_id>"
allowed-tools: Read, Bash, Grep, Glob
---

# /postmortem

Reconstruct a past cycle from the audit log and explain what happened.
This is the single most valuable routine operation for a research
project — understanding *why* matters more than profit.

Arguments: `$ARGUMENTS` — should be a single UUID or UUID prefix (first
8 chars OK).

## Procedure

1. Resolve cycle_id. If a prefix was given, query: `bash: psql
   $DATABASE_URL -tAc "SELECT cycle_id FROM cycles WHERE
   cycle_id::text LIKE '$ARG%' LIMIT 2;"`. Fail with helpful error if
   0 or ≥2 match.

2. Pull the full cycle artifact via:
   `bash: python -m desk.cycle dump --cycle $CYCLE_ID --format json`
   This returns a single JSON blob with:
   - cycle metadata (config, timing, status, cost)
   - research_theses for the cycle
   - quant_signals grouped by instrument
   - pm_proposals
   - risk_events (hard filter results)
   - risk_decisions
   - orders + fills
   - NAV before and after

3. Read and analyze. Do NOT paraphrase each row — use judgment about
   what actually matters. The operator wants to understand the *story*,
   not re-read the database.

## Output structure

Write the postmortem to
`reports/postmortems/<YYYY-MM-DD>-<cycle_prefix>.md` with this
structure, then surface it in the chat:

```markdown
# Postmortem — Cycle <prefix> (<started_at UTC>)

**Config:** <config_name>
**Duration:** <Ns>
**Outcome:** <completed|failed>
**NAV Δ:** <before → after, pct>

## Thesis
One paragraph summarizing what Research saw that cycle. Call out any
asset where the thesis differed meaningfully from the prior cycle.

## Signals
What did Quant find? Highlight agreement vs disagreement with Research.

## Decision
What did PM propose, and why? Include the specific rationale fields,
paraphrased briefly.

## Risk filter
Hard: what got blocked?
Soft: what got resized or rejected, citing which soft rules fired.

## Execution
What orders were built, submitted, filled? Any slippage vs expected?

## What happened
A paragraph telling the story. Causation, not just description.

## What we learn
Specific, concrete lessons. If nothing interesting happened, say so.
Do not manufacture insight.

## Open questions
Things to watch for in future cycles or investigate further.
```

## Rules

- **Never fabricate rationale.** If a rationale is missing from the
  audit log, write "rationale not captured" — do not reconstruct it.
- **Preserve agent language where useful.** If the PM's own wording is
  illuminating, quote it (fewer than 15 words) with attribution.
- **No hindsight bias.** Assessing "was this a good decision" uses only
  information available at the time, plus the realized outcome. Do not
  grade decisions by subsequent unrelated market moves.
- **No financial advice.** This is forensic analysis of a research
  cycle, not trading guidance.
