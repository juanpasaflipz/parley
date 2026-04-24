# Disclaimer

Plain-English version first. Legal-style version at the bottom.

## Read this before you run Parley with real money

You probably shouldn't. And if you do, these are the things you need to
understand before taking that risk.

### 1. Parley is a research project, not a product

It is built to study how multi-agent LLM systems behave when trading
crypto. It is not built to make money. It is not tested for making
money. It has no track record of making money.

The default configuration trades paper (simulated) money on Binance
testnet. This is intentional. Paper trading is where Parley is designed
to live.

### 2. Going live is one config flag — and one catastrophic mistake away

Parley has a `mode` setting. When it's `paper`, orders go to Binance
testnet. When it's `live`, orders go to real Binance with real money.
The code enforces this boundary, but the flag is ultimately controlled
by you. If you flip it, you are responsible for everything that follows.

Even if the paper-trading numbers look good, **past paper performance is
worse than useless for predicting live performance**, for several specific
reasons:

- Paper trading has perfect fills. Real markets do not.
- Paper trading has no slippage. Real markets always do.
- Paper trading has no one else reacting to your orders. Real markets do.
- Paper trading has no exchange outages, API rate limits, or flash crashes
  happening at the worst possible moment. Real markets have all of those.
- The testnet's price and liquidity do not perfectly match the live market.

### 3. LLMs can be wrong, confidently

The agents in Parley are Claude instances. Claude is a capable model,
but like every LLM it can:

- Produce outputs that are internally coherent but factually wrong.
- Be influenced by prompt injection from news articles, forum posts, or
  other data the Research agent reads.
- Miscalibrate confidence — sounding certain about things that are
  actually uncertain.
- Drift over time as the underlying model is updated by Anthropic.

Parley has code-level guardrails (the hard risk gate, the deterministic
order builder) specifically because LLM outputs cannot be trusted as the
final word on quantity or risk. These guardrails reduce the chance of a
catastrophic LLM-caused error. They do not eliminate it.

### 4. Crypto markets are unusually hostile

Even without AI involvement, crypto is a harder market to trade
profitably than equities. Volatility is higher, liquidity is thinner in
less-traded pairs, exchange risk is real, and the market is populated
by well-capitalized professional traders and market makers whose
advantage over a retail AI system is substantial and persistent.

If your thesis is "an LLM-driven desk will find edge in crypto that
professionals have missed," please be honest with yourself about how
likely that is.

### 5. Things that can go wrong, specifically

In rough order of likelihood, from most to least common:

- **The desk loses money steadily.** Most quant strategies don't work.
  Most multi-agent systems underperform simple baselines. This is the
  normal outcome.
- **The desk loses money suddenly.** A correlated drawdown across
  positions during a vol spike. Paper trading does not fully simulate
  these because paper liquidity is infinite.
- **An agent hallucinates and the soft risk layer doesn't catch it.**
  The hard risk gate will still catch it at the code layer. But resize
  errors, wrong-direction trades, or confused reasoning can all happen.
- **The Binance API goes down mid-cycle.** Orders may be in an
  indeterminate state. Parley's reconciliation logic handles the common
  cases; edge cases may require manual intervention.
- **Your API keys are compromised.** Use read-only and withdrawal-
  disabled keys. Never commit them. Rotate regularly.
- **The testnet behaves differently from live.** Fills are more optimistic,
  rate limits are more generous, and liquidity is synthetic. A strategy
  that works on testnet may fail on live for these reasons alone.
- **A dependency is compromised.** CCXT, Binance SDK, or any transitive
  dependency could in theory be supply-chain-attacked. Pin versions.
- **The code has a bug.** Parley is early-stage open source. There will
  be bugs. Some will be embarrassing. Some may lose money.

### 6. If you lose money

That is your responsibility, not the project's or the contributors'.
Nothing in Parley's codebase, documentation, or any public communication
from its maintainers or contributors constitutes financial, investment,
legal, tax, or trading advice.

### 7. Regulatory reality

Depending on where you live:

- Algorithmic trading may require registration or licensing.
- Using AI to make trading decisions may have disclosure obligations.
- Certain strategies (e.g. spoofing, layering) are illegal, and while
  Parley does not implement them, a contributor could.
- Crypto tax rules are unforgiving and vary by jurisdiction.

You are responsible for understanding and complying with the rules in
your jurisdiction. Parley does not advise on this and will not help you
evade it.

### 8. The maintainers' stance

The maintainers of Parley do not run it with meaningful personal capital.
If and when any of them do, they will disclose it in `reports/`. The
project's purpose is research, not personal trading operations. We are
not flexing wealth; we are studying a system.

---

## Formal disclaimer

Parley and all associated code, documentation, and research outputs are
provided "AS IS", without warranty of any kind, express or implied,
including but not limited to the warranties of merchantability, fitness
for a particular purpose, and non-infringement. In no event shall the
authors, maintainers, or contributors be liable for any claim, damages,
or other liability, whether in an action of contract, tort, or otherwise,
arising from, out of, or in connection with Parley or the use or other
dealings in Parley.

Parley is not a broker-dealer, investment adviser, commodity trading
advisor, or any other form of regulated financial professional. Nothing
produced by Parley — including but not limited to research theses,
trading signals, portfolio proposals, risk decisions, or order
recommendations — constitutes financial, investment, legal, tax, or
trading advice.

Trading cryptocurrencies involves substantial risk of loss and is not
suitable for every investor. Past performance, paper or live, is not
indicative of future results. The use of leverage, margin, or
derivatives can magnify gains and losses. You should consult with a
qualified financial advisor and conduct your own research before making
any trading decisions.

By using Parley, you acknowledge that you have read, understood, and
agreed to this disclaimer in full.

---

*Last updated: Phase 1 release. Material changes to this disclaimer
will be noted in the changelog and will trigger a minor version bump.*
