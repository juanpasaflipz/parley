-- ============================================================================
-- AI TRADING DESK — Postgres Schema (Neon-compatible)
-- ============================================================================
-- Design principles:
--   1. Every agent decision is logged with full context (inputs + outputs)
--   2. Trades reconcile exactly: intent → order → fill → position delta
--   3. JSONB for agent outputs (flexible) + typed columns for things we query
--   4. Append-only audit log; state tables derived from events where possible
--   5. UTC everywhere, microsecond precision
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ----------------------------------------------------------------------------
-- REFERENCE DATA
-- ----------------------------------------------------------------------------

-- Tradeable instruments. Start with crypto; structure allows expansion.
CREATE TABLE assets (
    asset_id        SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL UNIQUE,        -- 'BTC', 'ETH', 'SOL'
    name            TEXT NOT NULL,
    asset_class     TEXT NOT NULL DEFAULT 'crypto',
    quote_currency  TEXT NOT NULL DEFAULT 'USDT',
    venue           TEXT NOT NULL DEFAULT 'binance',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The trading pair actually traded: BTC/USDT on binance, etc.
CREATE TABLE instruments (
    instrument_id   SERIAL PRIMARY KEY,
    asset_id        INT NOT NULL REFERENCES assets(asset_id),
    symbol          TEXT NOT NULL,               -- 'BTCUSDT'
    venue           TEXT NOT NULL,               -- 'binance', 'coinbase'
    min_qty         NUMERIC(28, 12) NOT NULL DEFAULT 0,
    qty_precision   INT NOT NULL DEFAULT 8,
    price_precision INT NOT NULL DEFAULT 2,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (venue, symbol)
);

-- ----------------------------------------------------------------------------
-- MARKET DATA
-- ----------------------------------------------------------------------------
-- OHLCV bars. Partition-friendly if this gets huge later.
-- For research: keep it simple, one table, index on (instrument, ts).
CREATE TABLE market_bars (
    instrument_id   INT NOT NULL REFERENCES instruments(instrument_id),
    ts              TIMESTAMPTZ NOT NULL,
    timeframe       TEXT NOT NULL,               -- '1m', '5m', '1h', '1d'
    open            NUMERIC(28, 12) NOT NULL,
    high            NUMERIC(28, 12) NOT NULL,
    low             NUMERIC(28, 12) NOT NULL,
    close           NUMERIC(28, 12) NOT NULL,
    volume          NUMERIC(28, 12) NOT NULL,
    trades_count    INT,
    PRIMARY KEY (instrument_id, timeframe, ts)
);
CREATE INDEX idx_market_bars_ts ON market_bars(ts DESC);

-- Snapshot of best bid/ask when we decide to trade, for slippage analysis.
CREATE TABLE market_snapshots (
    snapshot_id     BIGSERIAL PRIMARY KEY,
    instrument_id   INT NOT NULL REFERENCES instruments(instrument_id),
    ts              TIMESTAMPTZ NOT NULL,
    bid             NUMERIC(28, 12) NOT NULL,
    ask             NUMERIC(28, 12) NOT NULL,
    bid_size        NUMERIC(28, 12),
    ask_size        NUMERIC(28, 12),
    mid             NUMERIC(28, 12) GENERATED ALWAYS AS ((bid + ask) / 2) STORED
);
CREATE INDEX idx_snapshots_inst_ts ON market_snapshots(instrument_id, ts DESC);

-- ----------------------------------------------------------------------------
-- AGENT CYCLES & DECISIONS
-- ----------------------------------------------------------------------------
-- A "cycle" = one full pass through the desk: Research → Quant → PM → Risk → Execution.
-- Everything a cycle produces links back to its cycle_id.
CREATE TABLE cycles (
    cycle_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
                    -- 'running','completed','failed','aborted'
    trigger         TEXT NOT NULL,               -- 'cron','manual','event'
    config_id       UUID,                        -- FK to desk_configs, nullable for manual
    notes           TEXT,
    error           TEXT
);
CREATE INDEX idx_cycles_started ON cycles(started_at DESC);

-- A desk config = snapshot of which models/prompts/params were running.
-- This is gold for research: "which config produced these results?"
CREATE TABLE desk_configs (
    config_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    description     TEXT,
    version         INT NOT NULL DEFAULT 1,
    is_active       BOOLEAN NOT NULL DEFAULT FALSE,
    config          JSONB NOT NULL,
                    -- { "research": {"model":"claude-opus-4-7","prompt_hash":"..."},
                    --   "quant":    {"model":"claude-haiku-4-5",...},
                    --   "risk":     {"max_position_pct":0.20,...}, ... }
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cycles
    ADD CONSTRAINT fk_cycles_config
    FOREIGN KEY (config_id) REFERENCES desk_configs(config_id);

-- Every agent invocation within a cycle. This is the audit log.
CREATE TABLE agent_runs (
    run_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id        UUID NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    agent           TEXT NOT NULL,
                    -- 'research','quant','pm','risk','execution'
    model           TEXT NOT NULL,               -- 'claude-opus-4-7' etc
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
    input_tokens    INT,
    output_tokens   INT,
    cache_read      INT,
    cache_write     INT,
    cost_usd        NUMERIC(12, 6),
    input           JSONB NOT NULL,              -- what we fed the agent
    output          JSONB,                       -- structured output
    reasoning       TEXT,                        -- free-text thesis / justification
    error           TEXT
);
CREATE INDEX idx_agent_runs_cycle ON agent_runs(cycle_id);
CREATE INDEX idx_agent_runs_agent_time ON agent_runs(agent, started_at DESC);

-- Research outputs: thesis per asset per cycle.
CREATE TABLE research_theses (
    thesis_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id        UUID NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    run_id          UUID NOT NULL REFERENCES agent_runs(run_id),
    asset_id        INT NOT NULL REFERENCES assets(asset_id),
    stance          TEXT NOT NULL,               -- 'bullish','bearish','neutral'
    conviction      NUMERIC(4, 3) NOT NULL,      -- 0.000 – 1.000
    horizon         TEXT NOT NULL,               -- 'intraday','swing','position'
    summary         TEXT NOT NULL,
    sources         JSONB NOT NULL DEFAULT '[]', -- list of URLs / refs
    raw             JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_theses_cycle_asset ON research_theses(cycle_id, asset_id);

-- Quant signals: each indicator/strategy outputs a signal per instrument per cycle.
CREATE TABLE quant_signals (
    signal_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id        UUID NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    run_id          UUID NOT NULL REFERENCES agent_runs(run_id),
    instrument_id   INT NOT NULL REFERENCES instruments(instrument_id),
    strategy        TEXT NOT NULL,               -- 'rsi_divergence','ma_cross',...
    direction       TEXT NOT NULL,               -- 'long','short','flat'
    strength        NUMERIC(4, 3) NOT NULL,      -- 0.000 – 1.000
    timeframe       TEXT NOT NULL,
    features        JSONB NOT NULL,              -- computed indicator values
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_signals_cycle_inst ON quant_signals(cycle_id, instrument_id);

-- Portfolio Manager proposes target positions.
CREATE TABLE pm_proposals (
    proposal_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id        UUID NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    run_id          UUID NOT NULL REFERENCES agent_runs(run_id),
    instrument_id   INT NOT NULL REFERENCES instruments(instrument_id),
    target_weight   NUMERIC(6, 5) NOT NULL,      -- -1.00000 to 1.00000 (fraction of NAV)
    current_weight  NUMERIC(6, 5) NOT NULL,
    action          TEXT NOT NULL,               -- 'open','add','trim','close','hold'
    rationale       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Risk Manager decisions on each proposal.
CREATE TABLE risk_decisions (
    decision_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id        UUID NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    run_id          UUID NOT NULL REFERENCES agent_runs(run_id),
    proposal_id     UUID NOT NULL REFERENCES pm_proposals(proposal_id),
    verdict         TEXT NOT NULL,               -- 'approved','resized','rejected'
    approved_weight NUMERIC(6, 5),               -- null if rejected
    rules_triggered JSONB NOT NULL DEFAULT '[]', -- hard rules that fired
    soft_notes      TEXT,                        -- LLM commentary on market regime etc
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_risk_proposal ON risk_decisions(proposal_id);

-- ----------------------------------------------------------------------------
-- ORDERS, FILLS, POSITIONS
-- ----------------------------------------------------------------------------
-- Orders produced by Execution agent. Paper or live.
CREATE TABLE orders (
    order_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_id        UUID REFERENCES cycles(cycle_id),
    decision_id     UUID REFERENCES risk_decisions(decision_id),
    instrument_id   INT NOT NULL REFERENCES instruments(instrument_id),
    mode            TEXT NOT NULL DEFAULT 'paper',  -- 'paper','live'
    side            TEXT NOT NULL,               -- 'buy','sell'
    order_type      TEXT NOT NULL,               -- 'market','limit','twap'
    qty             NUMERIC(28, 12) NOT NULL,
    limit_price     NUMERIC(28, 12),
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- 'pending','submitted','partial','filled','cancelled','rejected'
    venue_order_id  TEXT,                        -- exchange ID if live
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finalized_at    TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_orders_cycle ON orders(cycle_id);
CREATE INDEX idx_orders_status ON orders(status) WHERE status IN ('pending','submitted','partial');

-- Individual fills (one order may produce many fills).
CREATE TABLE fills (
    fill_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    order_id        UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    qty             NUMERIC(28, 12) NOT NULL,
    price           NUMERIC(28, 12) NOT NULL,
    fee             NUMERIC(28, 12) NOT NULL DEFAULT 0,
    fee_currency    TEXT NOT NULL DEFAULT 'USDT',
    venue_fill_id   TEXT
);
CREATE INDEX idx_fills_order ON fills(order_id);

-- Current position per instrument (derived, but cached for speed).
-- Could be a view over fills; storing makes dashboards fast.
CREATE TABLE positions (
    instrument_id   INT PRIMARY KEY REFERENCES instruments(instrument_id),
    qty             NUMERIC(28, 12) NOT NULL DEFAULT 0,
    avg_entry_price NUMERIC(28, 12) NOT NULL DEFAULT 0,
    realized_pnl    NUMERIC(28, 12) NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Daily NAV snapshot: equity, cash, positions_value, pnl.
-- This is what you chart for research.
CREATE TABLE nav_snapshots (
    snapshot_id     BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cash            NUMERIC(28, 12) NOT NULL,
    positions_value NUMERIC(28, 12) NOT NULL,
    equity          NUMERIC(28, 12) GENERATED ALWAYS AS (cash + positions_value) STORED,
    unrealized_pnl  NUMERIC(28, 12) NOT NULL DEFAULT 0,
    realized_pnl    NUMERIC(28, 12) NOT NULL DEFAULT 0,
    mode            TEXT NOT NULL DEFAULT 'paper'
);
CREATE INDEX idx_nav_ts ON nav_snapshots(ts DESC);

-- ----------------------------------------------------------------------------
-- RISK LIMITS (hard rules, code-enforced)
-- ----------------------------------------------------------------------------
CREATE TABLE risk_limits (
    limit_id        SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    rule_type       TEXT NOT NULL,
                    -- 'max_position_pct','max_daily_loss_pct','max_gross_exposure',
                    -- 'max_correlation','kill_switch','min_cash_reserve_pct'
    value           NUMERIC NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'global', -- 'global','per_asset','per_class'
    scope_ref       TEXT,                           -- e.g. 'BTC' if per_asset
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Log when limits fire, even if they only resize (not just reject).
CREATE TABLE risk_events (
    event_id        BIGSERIAL PRIMARY KEY,
    cycle_id        UUID REFERENCES cycles(cycle_id),
    limit_id        INT NOT NULL REFERENCES risk_limits(limit_id),
    decision_id     UUID REFERENCES risk_decisions(decision_id),
    severity        TEXT NOT NULL,               -- 'info','warn','block'
    details         JSONB NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- EXPERIMENTS (research layer)
-- ----------------------------------------------------------------------------
-- Group cycles into experiments so you can compare configurations.
CREATE TABLE experiments (
    experiment_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    hypothesis      TEXT NOT NULL,
    start_ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    end_ts          TIMESTAMPTZ,
    config_id       UUID NOT NULL REFERENCES desk_configs(config_id),
    status          TEXT NOT NULL DEFAULT 'active', -- 'active','concluded','aborted'
    conclusion      TEXT
);

CREATE TABLE experiment_cycles (
    experiment_id   UUID NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    cycle_id        UUID NOT NULL REFERENCES cycles(cycle_id) ON DELETE CASCADE,
    PRIMARY KEY (experiment_id, cycle_id)
);

-- ----------------------------------------------------------------------------
-- VIEWS for dashboards
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_latest_cycle AS
SELECT c.*, dc.name AS config_name
FROM cycles c
LEFT JOIN desk_configs dc ON dc.config_id = c.config_id
ORDER BY c.started_at DESC
LIMIT 1;

CREATE OR REPLACE VIEW v_cycle_cost AS
SELECT
    cycle_id,
    SUM(cost_usd)       AS total_cost_usd,
    SUM(input_tokens)   AS total_input_tokens,
    SUM(output_tokens)  AS total_output_tokens,
    COUNT(*)            AS agent_invocations
FROM agent_runs
GROUP BY cycle_id;

CREATE OR REPLACE VIEW v_open_positions AS
SELECT
    p.instrument_id,
    i.symbol,
    p.qty,
    p.avg_entry_price,
    p.realized_pnl,
    p.updated_at
FROM positions p
JOIN instruments i ON i.instrument_id = p.instrument_id
WHERE p.qty != 0;

-- ============================================================================
-- SEED DATA (minimal, for Phase 1)
-- ============================================================================
INSERT INTO assets (symbol, name) VALUES
    ('BTC',  'Bitcoin'),
    ('ETH',  'Ethereum'),
    ('SOL',  'Solana')
ON CONFLICT (symbol) DO NOTHING;

INSERT INTO instruments (asset_id, symbol, venue, qty_precision, price_precision)
SELECT asset_id, symbol || 'USDT', 'binance', 6, 2
FROM assets WHERE symbol IN ('BTC','ETH','SOL')
ON CONFLICT (venue, symbol) DO NOTHING;

INSERT INTO risk_limits (name, rule_type, value, scope) VALUES
    ('max_single_position', 'max_position_pct',     0.20, 'global'),
    ('max_daily_drawdown',  'max_daily_loss_pct',   0.05, 'global'),
    ('max_gross_exposure',  'max_gross_exposure',   1.00, 'global'),
    ('min_cash_reserve',    'min_cash_reserve_pct', 0.10, 'global')
ON CONFLICT (name) DO NOTHING;
