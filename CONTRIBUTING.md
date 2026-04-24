# Contributing to Parley

Thanks for being here. Parley is a research project, and good research
projects live or die by their contributor community. This document is
designed to make contribution easy, unambiguous, and — where possible —
fun.

## The house rules

1. **Paper trading is the default and only merged mode.** No PR that
   changes default behavior from paper to live will be accepted, ever.
2. **Intellectual honesty is mandatory.** "This didn't work, here's why"
   is a first-class contribution. Over-claimed performance is not.
3. **The audit log is sacred.** No PR removes, rewrites, or bypasses
   rows in `agent_runs`, `risk_events`, `fills`, or `nav_snapshots`.
   Soft-delete flags are fine; hard deletes are not.
4. **LLMs never hold the final decision on risk or quantity.** Hard
   limits are code. Order quantities are code. LLMs propose; code
   enforces.
5. **Everything is in English, UTC, and Decimal.** No float money, no
   local timestamps, no Spanglish in comments (even though the project
   is built in Mexico City — English keeps the contributor pool broad).

## What kind of contributions are welcome

### Tier 1 — ideal first contributions

These are well-scoped, isolated, and don't require you to understand the
whole system. Look for issues labeled `good first issue`.

- **New quant strategies.** Add an indicator/strategy to the quant
  agent's library. One file in `desk/strategies/`, one registration in
  the strategy map, one test in `tests/strategies/`. See the [add a
  strategy](#extension-adding-a-strategy) walkthrough below.
- **New soft risk rules.** Add a rule the Risk Manager can cite
  (e.g. "avoid trades during FOMC blackout windows"). One entry in the
  risk agent's soft_rules prompt, one test case.
- **Documentation.** Real documentation with working examples, not
  prose. If a quickstart step is wrong, fixing it is a strong PR.
- **Postmortems.** Pick a past cycle from the public reports and write
  a deeper analysis. Genuinely valuable for the community.
- **Visualizations.** Charts and plots for the dashboard or for the
  weekly reports.

### Tier 2 — medium-lift contributions

- **New exchange adapters.** Add Coinbase, Kraken, Bitso as alternative
  venues. Implements the `Broker` interface in `desk/broker.py`.
  Paper mode must work first; live mode gated behind the same
  `mode: paper` config check.
- **Alternative data sources.** On-chain providers, news feeds, social
  sentiment. New source plugs into `desk/cycle.py::gather_research`.
- **New slash commands.** Repeatable operator workflows like
  `/regime-check`, `/walk-forward`, `/compare-configs`.

### Tier 3 — architecture-level contributions

Please open a GitHub Discussion before starting these, so we don't waste
your time.

- **New agents.** E.g. a Macro Analyst, a Correlation Monitor. New
  agents require schema additions, a subagent file, and integration
  into the cycle flow.
- **Alternative orchestration modes.** E.g. event-driven instead of
  scheduled cycles.
- **Live trading.** Changes to `broker.py` that enable live submission.
  These get extra scrutiny and require reproducible paper-trading
  evidence across multiple weeks of the proposed config.

### What we don't want

- Profit-maximizing strategy tuning. Parley is a research platform, not
  a race-to-alpha. "My strategy made 300% on BTC in Q4 2021" PRs will
  be closed.
- Marketing language. No "revolutionary AI-powered autonomous trading"
  anywhere in the codebase.
- Scope creep into custody, fund management, or copy-trading.
- Dependencies that add a multi-hundred-MB install for one feature.

---

## Extension: adding a strategy

This is the most common contribution, so it gets a full walkthrough.

Every strategy is evaluated by the Quant agent. A strategy is a function
that takes OHLCV bars and returns `(direction, strength, features)`.

**1.** Create `desk/strategies/macd_cross.py`:

```python
from decimal import Decimal
from .base import Strategy, Signal, Direction
import pandas as pd

class MacdCross(Strategy):
    name = "macd_cross_12_26_9"
    min_bars = 50
    default_timeframe = "1h"

    def evaluate(self, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.min_bars:
            return Signal(Direction.FLAT, Decimal("0"),
                          {"error": "insufficient_bars"})

        ema12 = bars["close"].ewm(span=12).mean()
        ema26 = bars["close"].ewm(span=26).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9).mean()

        curr_macd, prev_macd = macd.iloc[-1], macd.iloc[-2]
        curr_sig,  prev_sig  = signal.iloc[-1], signal.iloc[-2]

        features = {
            "macd": float(curr_macd),
            "signal": float(curr_sig),
            "histogram": float(curr_macd - curr_sig),
        }

        # Bullish cross
        if prev_macd < prev_sig and curr_macd > curr_sig:
            strength = min(abs(curr_macd - curr_sig) / bars["close"].iloc[-1] * 100,
                           Decimal("1.0"))
            return Signal(Direction.LONG, Decimal(str(strength)), features)

        # Bearish cross
        if prev_macd > prev_sig and curr_macd < curr_sig:
            strength = min(abs(curr_macd - curr_sig) / bars["close"].iloc[-1] * 100,
                           Decimal("1.0"))
            return Signal(Direction.SHORT, Decimal(str(strength)), features)

        return Signal(Direction.FLAT, Decimal("0"), features)
```

**2.** Register it in `desk/strategies/__init__.py`:

```python
from .macd_cross import MacdCross
STRATEGIES = { ..., MacdCross.name: MacdCross }
```

**3.** Add a test in `tests/strategies/test_macd_cross.py` that:
   - Generates synthetic bars with a known MACD cross
   - Asserts the signal direction and strength
   - Asserts graceful failure with insufficient bars

**4.** Add a short entry in `docs/strategies.md`.

**5.** Open a PR. CI will run it against the paper-trading test harness.

---

## Extension: adding an exchange

Implement the `Broker` protocol in `desk/broker.py`. Minimum methods:

```python
class Broker(Protocol):
    mode: Literal["paper", "live"]
    async def get_bars(self, symbol: str, tf: str, limit: int) -> pd.DataFrame: ...
    async def get_snapshot(self, symbol: str) -> MarketSnapshot: ...
    async def get_balance(self) -> dict[str, Decimal]: ...
    async def submit_order(self, order: Order) -> SubmissionResult: ...
    async def cancel_order(self, order_id: str) -> None: ...
```

Then register it in `desk/broker/__init__.py::BROKERS`. Paper mode must
work first — your testnet or in-memory simulator. Live mode is added as
a follow-up PR after paper mode has been green for at least one week of
reports.

---

## Extension: adding an agent

**Requires an open Discussion first.** Adding agents changes the cycle
flow, which changes the interpretation of every historical cycle in the
database.

If approved:
1. Add a subagent file in `.claude/agents/`.
2. Add output table(s) to `schema.sql` via a numbered migration.
3. Wire it into the cycle flow in `CLAUDE.md` (the supervisor needs to
   know about it).
4. Add to the slash command orchestration.
5. Bump the schema version in `desk_configs.config`.

---

## Development setup

```bash
git clone https://github.com/<your-fork>/parley.git
cd parley

# Python
uv sync --dev                 # installs dev dependencies including pytest

# Pre-commit hooks
pre-commit install

# Database (local Postgres via Docker is fine for tests)
docker compose up -d postgres
export DATABASE_URL=postgres://parley:parley@localhost:5432/parley_test
psql $DATABASE_URL -f schema.sql

# Run the test suite
pytest

# Run the paper-trading smoke test (requires a Binance testnet key in .env)
pytest tests/integration -m smoke
```

### CI expectations

Every PR runs:
- `ruff check` + `ruff format --check`
- `mypy` on `desk/`
- `pytest` unit tests
- A paper-trading smoke test that runs one cycle end-to-end on fixed
  seed data
- Schema migration validity (does `schema.sql` still apply cleanly?)

PRs that touch risk (`desk/risk_engine.py` or the risk-manager agent) or
execution (`desk/execution.py` or the execution-trader agent) require
two reviewers. Everything else requires one.

---

## Commit style

Conventional commits, short subjects, sentence-case. Examples:

```
feat(quant): add MACD cross strategy
fix(risk): correct max_gross_exposure off-by-one
docs: clarify paper vs live mode in quickstart
refactor(broker): extract rate limiter into middleware
```

`BREAKING CHANGE:` footer for anything that changes schema, settings,
or agent output format.

---

## Reporting bugs

Include:
1. What you ran (exact slash command or script).
2. The `cycle_id` if applicable.
3. Logs from `agent_runs.error` and the Python stderr.
4. Whether it's reproducible, and if so, minimal steps.

If the bug caused a hard-rule violation (e.g. a position exceeded
limits), label it `critical` — these jump the queue.

---

## Security

If you find a vulnerability — especially one that could bypass the hard
risk gate, leak API keys, or submit orders outside the operator's
intent — please do not file a public issue. Email the maintainer listed
in `SECURITY.md` (if not yet present, open a minimal issue requesting
a private channel and do not include details).

---

## Recognition

Contributors are credited in `CONTRIBUTORS.md` and in any research
output from the project. Substantial research contributions (new
agents, significant strategy improvements, novel backtests) may be
cited as co-authors on papers or reports derived from the project.

---

## A word on tone

Parley's slack/discussions channel policy: assume good faith, prefer
specific over general, don't post price predictions, and be kind to
people who are learning. The project attracts people from very
different backgrounds — academics, quant hobbyists, crypto people,
AI engineers. That diversity is a feature. Keep it welcoming.

Welcome aboard.
