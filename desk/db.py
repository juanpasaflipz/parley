"""Database layer.

All structured writes to the audit log go through this module. Reads may go
through the Postgres MCP for ad-hoc queries, but anything tied to a cycle
uses these helpers to ensure consistency with the schema.

Design notes:
- A single connection pool is created lazily and shared process-wide.
- All monetary and quantity values are ``Decimal`` end-to-end. We never
  allow floats into INSERT statements for price/qty columns.
- All timestamps are UTC-aware ``datetime`` objects.
- INSERT helpers return the generated ``UUID`` so callers can chain.
- The ``append_only`` decorator wraps tables that must never be updated
  or deleted from; those helpers only expose ``insert``.

See ``schema.sql`` for the full data model this module maps onto.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import orjson
import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_POOL: ConnectionPool | None = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and configure it."
        )
    return url


def get_pool() -> ConnectionPool:
    """Return the shared connection pool, creating it on first use."""
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            conninfo=_database_url(),
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row, "autocommit": False},
            name="parley",
            open=True,
        )
        logger.debug("Database pool opened")
    return _POOL


def close_pool() -> None:
    """Close the pool. Call at process shutdown; tests also use this."""
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


@contextmanager
def get_conn() -> Iterator[Connection]:
    """Check out a connection from the pool, commit on success, rollback on error."""
    with get_pool().connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# JSON helpers — we use orjson for speed and correctness on Decimal
# ---------------------------------------------------------------------------


def _default_json(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # Preserve full precision as string; callers can cast back to Decimal.
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def to_jsonb(obj: Any) -> Jsonb:
    """Wrap a Python object for JSONB insertion with correct Decimal handling."""
    return Jsonb(obj, dumps=lambda o: orjson.dumps(o, default=_default_json).decode())


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Timezone-aware current UTC timestamp. Use this everywhere, never ``datetime.utcnow()``."""
    return datetime.now(timezone.utc)


def _dec(v: Decimal | float | int | str | None) -> Decimal | None:
    """Coerce a numeric value to Decimal without going through float."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        # Convert through str to avoid float-precision leakage
        return Decimal(str(v))
    return Decimal(v)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


def get_instruments(venue: str = "binance", active_only: bool = True) -> list[dict[str, Any]]:
    """Return instruments joined with their asset info."""
    sql = """
        SELECT i.instrument_id, i.symbol, i.venue, i.min_qty,
               i.qty_precision, i.price_precision, i.is_active,
               a.asset_id, a.symbol AS asset_symbol, a.name AS asset_name,
               a.asset_class, a.quote_currency
        FROM instruments i
        JOIN assets a ON a.asset_id = i.asset_id
        WHERE i.venue = %s
        {active_clause}
        ORDER BY i.symbol
    """.format(active_clause="AND i.is_active = TRUE" if active_only else "")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (venue,))
        return list(cur.fetchall())


def get_instrument_by_symbol(symbol: str, venue: str = "binance") -> dict[str, Any] | None:
    """Lookup a single instrument row by venue symbol (e.g. BTCUSDT)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT i.*, a.symbol AS asset_symbol, a.name AS asset_name
               FROM instruments i JOIN assets a ON a.asset_id = i.asset_id
               WHERE i.venue = %s AND i.symbol = %s""",
            (venue, symbol),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def insert_bars(
    instrument_id: int,
    timeframe: str,
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal, int | None]],
) -> int:
    """Bulk-insert OHLCV bars. Upserts on conflict (same bar at same ts).

    ``bars`` is an iterable of (ts, open, high, low, close, volume, trades_count).
    Returns the number of rows inserted or updated.
    """
    if not bars:
        return 0
    sql = """
        INSERT INTO market_bars
            (instrument_id, ts, timeframe, open, high, low, close, volume, trades_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (instrument_id, timeframe, ts) DO UPDATE
            SET open = EXCLUDED.open, high = EXCLUDED.high,
                low = EXCLUDED.low, close = EXCLUDED.close,
                volume = EXCLUDED.volume, trades_count = EXCLUDED.trades_count
    """
    rows = [
        (instrument_id, ts, timeframe, _dec(o), _dec(h), _dec(l), _dec(c), _dec(v), tc)
        for (ts, o, h, l, c, v, tc) in bars
    ]
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)
        return len(rows)


def get_bars(
    instrument_id: int,
    timeframe: str,
    limit: int = 200,
    before: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` bars, oldest first."""
    sql = """
        SELECT * FROM (
            SELECT ts, open, high, low, close, volume, trades_count
            FROM market_bars
            WHERE instrument_id = %s AND timeframe = %s
              AND (%s IS NULL OR ts < %s)
            ORDER BY ts DESC
            LIMIT %s
        ) sub
        ORDER BY ts ASC
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (instrument_id, timeframe, before, before, limit))
        return list(cur.fetchall())


def insert_snapshot(
    instrument_id: int,
    ts: datetime,
    bid: Decimal,
    ask: Decimal,
    bid_size: Decimal | None = None,
    ask_size: Decimal | None = None,
) -> int:
    """Insert a point-in-time bid/ask snapshot. Returns new ``snapshot_id``."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO market_snapshots
               (instrument_id, ts, bid, ask, bid_size, ask_size)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING snapshot_id""",
            (instrument_id, ts, _dec(bid), _dec(ask), _dec(bid_size), _dec(ask_size)),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row["snapshot_id"])


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


def begin_cycle(trigger: str = "manual", config_id: UUID | None = None) -> UUID:
    """Open a new cycle and return its ``cycle_id``. Caller must finalize."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO cycles (trigger, config_id, status)
               VALUES (%s, %s, 'running') RETURNING cycle_id""",
            (trigger, config_id),
        )
        row = cur.fetchone()
        assert row is not None
        return row["cycle_id"]


def finalize_cycle(cycle_id: UUID, status: str, error: str | None = None) -> None:
    """Close a cycle with ``completed``, ``failed``, or ``aborted``."""
    if status not in ("completed", "failed", "aborted"):
        raise ValueError(f"Invalid cycle status: {status}")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE cycles SET status = %s, ended_at = NOW(), error = %s
               WHERE cycle_id = %s""",
            (status, error, cycle_id),
        )


def get_active_config_id() -> UUID | None:
    """Return the currently active desk_config, or None if none is active."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_id FROM desk_configs WHERE is_active = TRUE ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row["config_id"] if row else None


def any_running_cycle() -> UUID | None:
    """Return the cycle_id of a currently-running cycle, if any."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT cycle_id FROM cycles WHERE status = 'running' LIMIT 1")
        row = cur.fetchone()
        return row["cycle_id"] if row else None


# ---------------------------------------------------------------------------
# Agent runs (audit log — append-only)
# ---------------------------------------------------------------------------


def insert_agent_run(
    cycle_id: UUID,
    agent: str,
    model: str,
    input_: dict[str, Any],
    output: dict[str, Any] | None = None,
    reasoning: str | None = None,
    status: str = "completed",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    cost_usd: Decimal | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> UUID:
    """Insert an agent invocation row. Always log, even on failure."""
    sql = """
        INSERT INTO agent_runs
            (cycle_id, agent, model, started_at, ended_at, status,
             input_tokens, output_tokens, cache_read, cache_write, cost_usd,
             input, output, reasoning, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING run_id
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql,
            (
                cycle_id,
                agent,
                model,
                started_at or utcnow(),
                ended_at,
                status,
                input_tokens,
                output_tokens,
                cache_read,
                cache_write,
                _dec(cost_usd),
                to_jsonb(input_),
                to_jsonb(output) if output is not None else None,
                reasoning,
                error,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return row["run_id"]


# ---------------------------------------------------------------------------
# Research theses
# ---------------------------------------------------------------------------


def insert_thesis(
    cycle_id: UUID,
    run_id: UUID,
    asset_id: int,
    stance: str,
    conviction: Decimal,
    horizon: str,
    summary: str,
    raw: dict[str, Any],
    sources: list[str] | None = None,
) -> UUID:
    """Insert one research thesis row."""
    if stance not in ("bullish", "bearish", "neutral"):
        raise ValueError(f"Invalid stance: {stance}")
    if horizon not in ("intraday", "swing", "position"):
        raise ValueError(f"Invalid horizon: {horizon}")
    conv = _dec(conviction)
    assert conv is not None
    if not (Decimal("0") <= conv <= Decimal("1")):
        raise ValueError(f"Conviction must be in [0, 1], got {conv}")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_theses
               (cycle_id, run_id, asset_id, stance, conviction, horizon,
                summary, sources, raw)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING thesis_id""",
            (
                cycle_id, run_id, asset_id, stance, conv, horizon,
                summary, to_jsonb(sources or []), to_jsonb(raw),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return row["thesis_id"]


# ---------------------------------------------------------------------------
# Quant signals
# ---------------------------------------------------------------------------


def insert_signal(
    cycle_id: UUID,
    run_id: UUID,
    instrument_id: int,
    strategy: str,
    direction: str,
    strength: Decimal,
    timeframe: str,
    features: dict[str, Any],
) -> UUID:
    """Insert one quant signal row."""
    if direction not in ("long", "short", "flat"):
        raise ValueError(f"Invalid direction: {direction}")
    s = _dec(strength)
    assert s is not None
    if not (Decimal("0") <= s <= Decimal("1")):
        raise ValueError(f"Strength must be in [0, 1], got {s}")

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO quant_signals
               (cycle_id, run_id, instrument_id, strategy, direction,
                strength, timeframe, features)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING signal_id""",
            (cycle_id, run_id, instrument_id, strategy, direction, s, timeframe, to_jsonb(features)),
        )
        row = cur.fetchone()
        assert row is not None
        return row["signal_id"]


# ---------------------------------------------------------------------------
# Positions & NAV
# ---------------------------------------------------------------------------


def get_positions(include_zero: bool = False) -> list[dict[str, Any]]:
    """Return current cached positions joined with instrument info."""
    sql = """
        SELECT p.instrument_id, i.symbol, i.venue, p.qty,
               p.avg_entry_price, p.realized_pnl, p.updated_at
        FROM positions p
        JOIN instruments i ON i.instrument_id = p.instrument_id
        {where}
        ORDER BY i.symbol
    """.format(where="WHERE p.qty != 0" if not include_zero else "")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def get_latest_nav() -> dict[str, Any] | None:
    """Most recent NAV snapshot, or None if none exist yet."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT ts, cash, positions_value, equity,
                      unrealized_pnl, realized_pnl, mode
               FROM nav_snapshots ORDER BY ts DESC LIMIT 1"""
        )
        return cur.fetchone()


def insert_nav_snapshot(
    cash: Decimal,
    positions_value: Decimal,
    unrealized_pnl: Decimal = Decimal("0"),
    realized_pnl: Decimal = Decimal("0"),
    mode: str = "paper",
    ts: datetime | None = None,
) -> int:
    """Append a NAV snapshot. Equity is computed by the DB."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO nav_snapshots
               (ts, cash, positions_value, unrealized_pnl, realized_pnl, mode)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING snapshot_id""",
            (
                ts or utcnow(),
                _dec(cash),
                _dec(positions_value),
                _dec(unrealized_pnl),
                _dec(realized_pnl),
                mode,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row["snapshot_id"])


# ---------------------------------------------------------------------------
# Risk limits (read-only helper; limits are configured via seed / migrations)
# ---------------------------------------------------------------------------


def get_risk_limits(active_only: bool = True) -> list[dict[str, Any]]:
    """Return all risk limits currently configured."""
    sql = "SELECT * FROM risk_limits"
    if active_only:
        sql += " WHERE is_active = TRUE"
    sql += " ORDER BY name"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def insert_risk_event(
    limit_id: int,
    severity: str,
    details: dict[str, Any],
    cycle_id: UUID | None = None,
    decision_id: UUID | None = None,
) -> int:
    """Append a risk_events row. Always log; these are forensic data."""
    if severity not in ("info", "warn", "block"):
        raise ValueError(f"Invalid severity: {severity}")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO risk_events
               (cycle_id, limit_id, decision_id, severity, details)
               VALUES (%s, %s, %s, %s, %s) RETURNING event_id""",
            (cycle_id, limit_id, decision_id, severity, to_jsonb(details)),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row["event_id"])


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def ping() -> bool:
    """Simple connectivity check. Returns True if SELECT 1 succeeds."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except psycopg.Error as exc:
        logger.error("Database ping failed: %s", exc)
        return False


__all__ = [
    "begin_cycle",
    "close_pool",
    "finalize_cycle",
    "get_active_config_id",
    "get_bars",
    "get_conn",
    "get_instrument_by_symbol",
    "get_instruments",
    "get_latest_nav",
    "get_pool",
    "get_positions",
    "get_risk_limits",
    "insert_agent_run",
    "insert_bars",
    "insert_nav_snapshot",
    "insert_risk_event",
    "insert_signal",
    "insert_snapshot",
    "insert_thesis",
    "any_running_cycle",
    "ping",
    "to_jsonb",
    "utcnow",
]
