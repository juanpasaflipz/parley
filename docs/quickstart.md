# Parley — Quickstart

Get from `git clone` to a first paper-trading cycle in roughly 20 minutes.

**This is Phase 1: paper trading only, on Binance testnet. No real money
is at risk at any point in this walkthrough.**

---

## Prerequisites

You'll need accounts and installs before starting:

1. **A Neon Postgres database.** Free tier works fine. Sign up at
   [neon.tech](https://neon.tech), create a project, copy the connection
   string. You'll paste it into `.env`.

2. **A Binance Testnet account.** Create at
   [testnet.binance.vision](https://testnet.binance.vision). Generate an
   HMAC-SHA256 API key in the "Generate HMAC-SHA256 Key" section. Save
   the key *and* secret — the secret is only shown once. These keys
   can trade only imaginary testnet USDT; they cannot touch real money.

3. **Claude Code CLI** installed and logged in (Max Pro / API key both
   work). See [code.claude.com/docs](https://code.claude.com/docs).

4. **Python 3.11+** and [**uv**](https://github.com/astral-sh/uv) for
   dependency management.

5. **`psql`** CLI tool available on your PATH.

---

## 1. Clone and install

```bash
git clone https://github.com/<your-org>/parley.git
cd parley

# Install Python dependencies (dev group includes pytest/ruff/mypy)
uv sync --dev

# Verify pytest passes on a fresh checkout
uv run pytest
# → expect "80 passed" or better
```

---

## 2. Configure your environment

```bash
cp .env.example .env
```

Open `.env` in your editor and fill in:

- `DATABASE_URL` — the Neon connection string, including `?sslmode=require`
- `BINANCE_TESTNET_API_KEY` — from the Binance testnet page
- `BINANCE_TESTNET_API_SECRET` — same
- Leave `BINANCE_TESTNET_REST_URL` at its default (`https://testnet.binance.vision`)
- `PAPER_STARTING_NAV_USDT` — how large you want your paper desk to be. Default 10000.

Optional for Phase 1 (leave blank):

- `CRYPTOPANIC_API_KEY`, `COINGECKO_API_KEY` — wire up Research inputs in Phase 2

---

## 3. Initialize the database

```bash
# Create all tables, views, seed rows
psql $DATABASE_URL -f schema.sql

# Run the idempotent bootstrap — ensures default assets, instruments,
# risk limits, and a baseline desk_config exist, plus an initial NAV row.
uv run python -m desk.setup
```

You should see a summary like:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Parley — setup complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Mode:           paper
  Universe:       BTCUSDT, ETHUSDT, SOLUSDT

  Active risk limits:
    max_single_position      max_position_pct         0.20
    max_daily_drawdown       max_daily_loss_pct       0.05
    max_gross_exposure       max_gross_exposure       1.00
    min_cash_reserve         min_cash_reserve_pct     0.10
    kill_switch              kill_switch              0

  Desk configs:
    ★ baseline-v1 (v1)

  NAV:            10000 USDT
    cash          10000
    positions     0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 4. Ingest some market history

The Quant subagent needs recent bars to compute indicators. The
supervisor will pull these on the fly each cycle, but for fast iteration
you can pre-load:

```bash
# Pull 200 most recent 1h bars for each instrument into market_bars
for SYM in BTCUSDT ETHUSDT SOLUSDT; do
  uv run python -m desk.cycle fetch-bars \
    --cycle 00000000-0000-0000-0000-000000000000 \
    --symbol $SYM --tf 1h --limit 200
done
```

(The dummy cycle UUID is fine for bootstrap ingestion; subsequent cycles
will use real IDs.)

---

## 5. First cycle — dry run

Start Claude Code in the project directory:

```bash
claude
```

You should see Parley's session-start banner:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Parley — session startup check
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓  Mode: paper (Binance testnet)
✓  DATABASE_URL present
✓  BINANCE_TESTNET_API_KEY present
✓  Database reachable
✓  Schema initialized
✓  Active config: baseline-v1
```

Then inside Claude Code:

```
/run-cycle --dry
```

`--dry` walks all 13 steps of the cycle — Research, Quant, PM, hard
risk gate, Risk Manager, order building — but **does not submit orders**.
This is what you want on your first run, to confirm the agent
subagents return valid output and nothing has weird edge-cases.

Expect the first cycle to take 60–120 seconds. You'll see the supervisor
delegate to each subagent, insert to Postgres, and report progress.

### Phase 1 expectation

Because the research inputs are currently stubbed, the Research Analyst
will produce mostly-neutral theses with conviction capped at 0.2. The
Portfolio Manager, given low-conviction inputs, will typically propose
no trades or only very small ones. **This is correct and deliberate.** The
desk is being honest that it has no edge until real data sources are
wired up.

---

## 6. First real cycle (paper mode)

When you're ready, drop `--dry`:

```
/run-cycle
```

This runs the same flow but actually submits orders to Binance testnet
through the `pre-order-risk-check.sh` hook (which will re-verify paper
mode, re-run the risk engine, and only then let submission proceed).

Inspect the result:

```
/status
```

You'll see your NAV, any open positions, and recent cycle activity.

---

## 7. Understanding what happened

For any cycle, run:

```
/postmortem <cycle_id>
```

This writes a structured markdown postmortem to
`reports/postmortems/<YYYY-MM-DD>-<cycle_prefix>.md` walking through the
thesis, signals, PM proposals, risk decisions, orders, and fills.

Over time, this directory becomes the research record of your desk.

---

## 8. Running a backtest

Historical simulation uses `signals` mode by default — fast, deterministic
PM, no LLM calls. Useful for validating strategies before watching them
run live.

```
/backtest BTCUSDT 2024-10-01 2025-01-01
```

This creates a backtest run in `backtests/runs/<run_id>/`, simulates
every 1h bar in the window, and produces:

- `equity_curve.csv` — NAV at every bar
- `trades.csv` — every simulated trade
- `metrics.json` — Sharpe, max DD, total return, etc.
- `report.md` — human-readable summary

Backtest coverage requires the corresponding 1m bars to exist in
`market_bars`. For large windows, run an ingest script first — Phase 2
will include a dedicated `desk/ingest.py` module.

---

## Common problems

### "No active desk_config" error

Run `/new-config` inside Claude Code to create one, then `/run-cycle`.

### "mode is 'paper' but BINANCE_TESTNET_REST_URL points at production"

The pre-order hook caught a misconfiguration. Fix `.env` — the URL must
contain `testnet`.

### Tests hang or timeout

Some tests import ccxt. If `uv sync` hasn't completed or the install
was interrupted, tests will fail at import. Run `uv sync --dev` again.

### Neon connection drops mid-cycle

Neon connections can idle-disconnect. The connection pool in
`desk/db.py` recreates them automatically, but if a cycle is in the
middle of the hard risk gate when a disconnect happens, the cycle
may be marked `failed` and need to be manually retried. Rare but real.

---

## Next steps

Now that your desk is running:

1. **Read [`CLAUDE.md`](../CLAUDE.md)** — the supervisor's constitution.
   This is what Claude Code loads at every session start.

2. **Read the subagent prompts in [`.claude/agents/`](../.claude/agents/).**
   These define each agent's scope, output format, and hard rules.

3. **Read the [schema](../schema.sql).** Understanding the data model
   makes everything else easier.

4. **Run `/run-cycle` periodically.** Once a day is a good cadence for
   the `swing` time horizon the default strategies are tuned for.

5. **Write a weekly report.** Commit `reports/YYYY-WW.md` summarizing
   what the desk did, what worked, what didn't. This is the research
   artifact that makes Parley worth running.

6. **Consider contributing.** See [`CONTRIBUTING.md`](../CONTRIBUTING.md)
   for good-first-issue ideas. The most valuable contributions are
   new strategies, new soft risk rules, and honest postmortems.

And when you've had enough data to draw conclusions — **do not go live.**
Read [`DISCLAIMER.md`](../DISCLAIMER.md). Live trading is outside the
scope of Phase 1 for every operator, regardless of paper performance.
