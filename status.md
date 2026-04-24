---
description: Show current desk state — active config, open positions, NAV, recent cycles, and any open incidents. Read-only. Safe to run anytime.
argument-hint: "[--days=7]"
allowed-tools: Read, Bash, Grep, Glob
---

# /status

Read-only snapshot of the desk. Does not modify anything. Useful at
session start or between cycles to orient.

Arguments: `$ARGUMENTS`

Parse `--days=N` to control how far back the cycle history window goes.
Default: 7.

## What to show

Run `bash: python -m desk.cycle status --days $DAYS --format text`
which returns a formatted report. If that script doesn't exist yet,
fall back to direct psql queries against:

- `v_latest_cycle` — most recent cycle
- `v_open_positions` — current positions
- `nav_snapshots` — latest equity
- `cycles` — recent N days, with status counts
- `incidents/` directory — any markdown files newer than 7 days

## Output format

```
Parley — Desk Status
──────────────────────────────────────────────────
  Mode:          paper (Binance testnet)
  Active config: <name> (v<version>)
  Universe:      BTCUSDT, ETHUSDT, SOLUSDT

  NAV:           $X,XXX.XX    (Δ 7d: +X.XX%)
  Cash:          $X,XXX.XX    (XX.X%)
  Gross expo:    XX.X%

  Open positions:
    BTCUSDT  qty X.XXXX  @ $XX,XXX avg  PnL +$XX.XX (+X.X%)
    ETHUSDT  qty X.XXXX  @ $X,XXX  avg  PnL -$XX.XX (-X.X%)

  Recent cycles (last <days>d):
    completed: N    failed: N    running: N
    last:      <timestamp> (<status>)

  Incidents (last 7d):
    (none) | <N items — see incidents/>
──────────────────────────────────────────────────
```

## Rules

- **Read-only.** Absolutely no writes, no order submissions, no config
  changes.
- **No recommendations.** Do not say "you should close this position"
  or similar. Report state; do not judge.
- **Surface problems clearly.** If there's a running cycle older than
  an hour, a failed cycle, or an unread incident, put it in bold at the
  top. Quiet failure is worse than loud failure.
