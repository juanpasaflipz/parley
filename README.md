# Parley

> A multi-agent trading desk where research, risk, and execution parley each
> cycle — built on Claude Code, deterministic risk enforcement, and an
> audit log designed as the product.

**Status: Phase 1 — paper trading research. Not for live capital.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Phase](https://img.shields.io/badge/phase-paper%20trading-yellow)]()
[![Claude Code](https://img.shields.io/badge/built%20on-Claude%20Code-8A2BE2)]()

---

## What this is

Parley is an autonomous crypto trading desk run by five specialized AI
agents: a Research Analyst, a Quant, a Portfolio Manager, a Risk Manager,
and an Execution Trader. Each cycle, they deliberate through structured
outputs logged to Postgres, passing decisions down the chain until orders
hit the exchange — currently Binance testnet.

It is a **research project** asking a specific question:

> Can a multi-agent LLM system achieve positive risk-adjusted returns in
> crypto paper trading, and which agent-architecture choices most affect
> performance?

If you are looking for a bot to make you money, this is not that.
If you are interested in how multi-agent LLM systems actually behave when
they have to cooperate under hard constraints, welcome.

---

## Why Parley vs. the other multi-agent trading frameworks

There are several serious prior-art projects in this space
([TradingAgents](https://github.com/TauricResearch/TradingAgents),
[AgenticTrading](https://github.com/Open-Finance-Lab/AgenticTrading),
[AI-Trader](https://github.com/HKUDS/AI-Trader)). Parley differs in four
specific ways:

1. **Claude Code-native orchestration.** Agents are Claude Code
   subagents in `.claude/agents/`, not LangGraph nodes or CrewAI tasks.
   The supervisor is Claude Code itself. This makes the system
   inspectable, interruptible, and trivially modifiable from your IDE.

2. **Deterministic hard-risk gate separate from LLM risk reasoning.**
   Position limits, drawdown limits, and kill switches are enforced in
   code before the Risk Manager agent sees anything. The LLM reasons
   about market regime; code enforces the rules. An injected prompt or
   hallucination cannot violate a limit.

3. **Paper-first with a live-trading path that actually exists.** Binance
   testnet now, Binance live later with one config flag flip. No
   "for educational purposes" fiction where the system was never meant
   to trade.

4. **Audit-log-as-product.** Every thesis, signal, proposal, risk
   decision, order, and fill is written to Postgres with full lineage.
   Six months later you can ask "why did the PM short ETH on March 3rd"
   and get a complete answer. This is the actual research artifact.

---

## Architecture

```
                       ┌─────────────────────┐
                       │  Supervisor (you in │
                       │   Claude Code CLI)  │
                       └──────────┬──────────┘
                                  │ delegates via Task tool
        ┌──────────┬──────────────┼──────────────┬──────────┐
        ▼          ▼              ▼              ▼          ▼
   ┌─────────┐ ┌───────┐ ┌──────────────┐ ┌──────────┐ ┌────────────┐
   │Research │ │ Quant │ │  Portfolio   │ │   Risk   │ │ Execution  │
   │Analyst  │ │       │ │   Manager    │ │ Manager  │ │  Trader    │
   └────┬────┘ └───┬───┘ └──────┬───────┘ └────┬─────┘ └─────┬──────┘
        │          │            │              │             │
        │   theses │   signals  │  proposals   │  decisions  │  orders
        ▼          ▼            ▼              ▼             ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │          Postgres (Neon) — the audit log, the product           │
   └─────────────────────────────────────────────────────────────────┘
                                  │
                     ┌────────────┴────────────┐
                     ▼                         ▼
            ┌─────────────────┐       ┌─────────────────┐
            │  Hard risk gate │       │  Order builder  │
            │    (code, not   │       │    (code, not   │
            │       LLM)      │       │       LLM)      │
            └─────────────────┘       └─────────────────┘
                                               │
                                               ▼
                                      ┌─────────────────┐
                                      │ Binance Testnet │
                                      │  (paper mode)   │
                                      └─────────────────┘
```

**The cycle.** Supervisor invokes Research (once) → Quant (once per
instrument) → PM (once) → [hard risk filter, code] → Risk (once) →
[order builder, code] → Execution (once per decision) → [submit orders,
code] → snapshot NAV.

Full flow and hard rules are in [`CLAUDE.md`](CLAUDE.md) — the desk's
constitution, read by Claude Code at the start of every session.

---

## Stack

| Layer              | Tech                                                        |
|--------------------|-------------------------------------------------------------|
| Orchestration      | Claude Code CLI + subagents                                 |
| Agent brains       | Claude Opus (research, PM, risk) + Sonnet (quant, execution)|
| Backend            | Python 3.11+, CCXT, Pandas, NumPy                           |
| Database           | Postgres (Neon-compatible)                                  |
| Exchange           | Binance testnet (paper) → Binance live (future)             |
| Dashboard          | Next.js 15 + TypeScript (Phase 2)                           |
| Automation         | Claude Code slash commands, hooks, GitHub Actions           |

---

## Quickstart

Requires Node.js 20+, Python 3.11+, a Claude Code CLI install
([docs](https://code.claude.com/docs)), a Neon Postgres database, and
a Binance testnet account.

```bash
git clone https://github.com/<your-org>/parley.git
cd parley

# Install Python backend
uv sync                           # or: pip install -e .

# Set up environment
cp .env.example .env
# Edit .env with your Neon DATABASE_URL, BINANCE_TESTNET_API_KEY,
# BINANCE_TESTNET_API_SECRET

# Initialize database
psql $DATABASE_URL -f schema.sql

# Seed universe and initial desk config
python -m desk.setup

# Start Claude Code in the project
claude

# Inside Claude Code:
> /run-cycle --dry
```

`--dry` runs all five agents and logs everything to Postgres but does not
submit orders. Run this first to confirm your setup works before removing
the flag.

A more detailed walkthrough is in [`docs/quickstart.md`](docs/quickstart.md).

---

## What a cycle looks like

```
$ claude
> /run-cycle

▸ Cycle 7f3a... started (config: baseline-v1)
▸ Gathering research inputs... done (42 news items, 18 onchain metrics)
▸ Delegating to research-analyst (opus)... done
   BTC: bullish, conviction 0.62, horizon swing
   ETH: neutral, conviction 0.31
   SOL: bullish, conviction 0.71, horizon swing
▸ Fetching bars (3 instruments × 1h × 200)... done
▸ Delegating to quant (sonnet, 3 parallel)... done
   BTC: 4 signals (3 long, 1 flat)
   ETH: 4 signals (2 flat, 1 long, 1 short)
   SOL: 4 signals (3 long, 1 flat)
▸ Delegating to portfolio-manager (opus)... done
   Proposed: BTC +0.14, ETH 0.00, SOL +0.18 (gross 0.32, cash 0.68)
▸ Hard risk filter... 3/3 passed
▸ Delegating to risk-manager (opus)... done
   BTC: approved 0.14
   SOL: resized 0.18 → 0.12 (realized_vol > 2x 90d median)
▸ Building orders... 2 orders (both buys)
▸ Submitting to Binance testnet... 2 filled
▸ Cycle 7f3a completed in 94s. NAV: 10,127.43 USDT (+0.31%)
```

Every line above is a row in Postgres, queryable forever.

---

## Project layout

```
parley/
├── CLAUDE.md                     # Desk constitution (read by Claude Code)
├── README.md
├── CONTRIBUTING.md
├── DISCLAIMER.md
├── LICENSE                       # Apache 2.0
├── schema.sql                    # Postgres schema
├── .claude/
│   ├── agents/                   # Five subagent personas
│   ├── commands/                 # Slash commands (/run-cycle, /backtest, ...)
│   ├── hooks/                    # Pre/post hooks for risk + snapshot
│   └── settings.json             # Paper/live mode, MCP, permissions
├── desk/                         # Python backend
│   ├── market_data.py
│   ├── indicators.py
│   ├── risk_engine.py            # Hard-rule enforcement
│   ├── execution.py              # Order building + submission
│   ├── broker.py                 # Binance testnet/live adapter
│   ├── db.py
│   └── cycle.py                  # Supervisor helpers
├── backtests/                    # Historical simulation results
├── reports/                      # Weekly desk reports (committed)
├── dashboard/                    # Next.js dashboard (Phase 2)
└── docs/
```

---

## Research reports

Every week, a report is committed to `reports/YYYY-WW.md` summarizing
what the paper-traded desk did, its P&L, notable decisions, agent
failures, and any config changes. These are honest records — including
the weeks where the desk lost money, made obvious mistakes, or agents
disagreed in interesting ways.

Start with [`reports/README.md`](reports/README.md) for the index.

---

## Roadmap

**Phase 1 (current):** Paper trading with fixed universe (BTC, ETH, SOL).
Five agents, deterministic risk gate, Binance testnet. Weekly reports.

**Phase 2:** Dashboard (Next.js), expanded strategy library, experiment
framework for A/B testing agent configurations, backtest CLI.

**Phase 3:** Multi-venue support (Coinbase, Kraken, Bitso), broader
universe, alternative data sources (on-chain analytics providers),
optional tiny live capital for operators who opt in.

**Never:** Parley will not pursue "managed fund" features, custody,
copy-trading marketplaces, or anything that turns it into a product for
non-operators. It stays a research system.

See [issues labeled `roadmap`](../../issues?q=is%3Aissue+label%3Aroadmap)
for the live list.

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version:

- Good first issues are labeled [`good first issue`](../../issues?q=is%3Aissue+label%3A%22good+first+issue%22).
- Common extensions are well-scoped: adding a strategy to the quant
  agent, adding a new exchange adapter, adding an agent.
- All contributions go through paper-mode CI — no PR that touches
  execution or risk is merged without reproducible paper-trading proof.
- Intellectual honesty is the house rule. "This didn't work" is a
  valuable contribution.

Questions or ideas: [GitHub Discussions](../../discussions).

---

## Related work

Parley stands on the shoulders of prior multi-agent trading research:

- Xiao et al., **TradingAgents: Multi-Agents LLM Financial Trading
  Framework** ([arXiv:2412.20138](https://arxiv.org/abs/2412.20138)) —
  the paper that popularized the agents-as-trading-desk pattern.
- [Open-Finance-Lab/AgenticTrading](https://github.com/Open-Finance-Lab/AgenticTrading) —
  academic framework with DAG-based orchestration and memory agents.
- [HKUDS/AI-Trader](https://github.com/HKUDS/AI-Trader) — agent-native
  trading with cross-agent signal sharing.

If you cite Parley in research, please also cite the works above.

---

## Disclaimer

Parley is a research project, not financial advice, not an investment
product, and not warranted to do anything. Using it with real money can
lose you real money. Read [`DISCLAIMER.md`](DISCLAIMER.md) before going
anywhere near live trading.

---

## License

Apache 2.0 — see [`LICENSE`](LICENSE). Patent grant included.
Contributions are accepted under the same license via the standard
Apache 2.0 inbound=outbound convention.
