---
description: Run a historical backtest. Replays a configured strategy mix against stored 1m bars over a date range, with optional multi-agent or signals-only modes. Results stored in backtests/runs/.
argument-hint: "<symbol> <from-YYYY-MM-DD> <to-YYYY-MM-DD> [--mode=signals|agents] [--config=<desk_config_name>]"
allowed-tools: Task, Read, Bash, Write, Grep, Glob
---

# /backtest

Run a historical paper simulation over stored OHLCV data. Unlike
`/run-cycle`, this does NOT submit anything to the exchange; it walks
historical bars forward and replays the decision process cycle-by-cycle.

Arguments: `$ARGUMENTS`

Parse as: `<symbol> <from> <to> [flags]` where:

- `symbol` — e.g. `BTCUSDT`
- `from`, `to` — ISO dates (UTC)
- `--mode=signals` (default) — runs quant signals only, uses a simple
  rule-based PM stand-in. Fast.
- `--mode=agents` — runs the full five-agent chain for each cycle.
  Slow and usage-heavy; use only when studying agent behavior.
- `--config=<name>` — `desk_configs.name` to run under. Default: the
  currently active config.

## Procedure

1. Validate inputs. Reject if `from >= to` or if bars are missing for
   the requested range (query `market_bars` to check coverage).

2. Create a run row: `bash: python -m desk.backtest init --symbol
   $SYMBOL --from $FROM --to $TO --mode $MODE --config $CONFIG`.
   Returns a `run_id` and a working directory under `backtests/runs/`.

3. For `signals` mode: `bash: python -m desk.backtest run-signals
   --run-id $RUN_ID`. Walk bars, compute signals, simulate PM/Risk as
   code. Fast. All outputs written under the run directory.

4. For `agents` mode: iterate cycles. At each cycle timestamp, call the
   supervisor's cycle flow but with a frozen "as-of" view of the
   database (no future bars visible). The Task tool delegates to the
   real subagents. WARNING: this is expensive — confirm with operator
   before starting a long backtest in agents mode.

5. When complete: `bash: python -m desk.backtest report --run-id
   $RUN_ID` produces:
   - `equity_curve.csv`
   - `trades.csv`
   - `metrics.json` (Sharpe, max drawdown, win rate, exposure, turnover)
   - `report.md` — human-readable summary
   - `plots/` — equity curve, drawdown chart, per-trade P&L

6. Output a compact summary:

```
Backtest <run_id>:
  Period:        <from> → <to>  (<N> cycles)
  Mode:          <mode>
  Final NAV:     $X,XXX.XX  (Δ +X.XX%)
  Max drawdown:  X.XX%
  Sharpe (1h):   X.XX
  Win rate:      XX.X%
  Turnover:      X.Xx
  Report:        backtests/runs/<run_id>/report.md
```

## Rules

- **Never use future data.** The backtest engine enforces point-in-time
  views; do not work around this.
- **Never claim profitability.** Paper backtests on stored data are
  subject to survivorship, look-ahead, and fit bias. Report metrics;
  do not recommend trading.
- **Large agents-mode runs** (>500 cycles) must be confirmed by the
  operator — they consume meaningful Claude usage.
- **All runs are logged** under `backtests/runs/<run_id>/`. Do not
  delete old runs; they are research artifacts.
