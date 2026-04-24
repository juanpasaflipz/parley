"""Cycle orchestration CLI.

This module is the Python surface area that the supervisor (Claude Code
main session, driven by CLAUDE.md) invokes during a cycle. Each subcommand
does ONE deterministic thing and prints a JSON blob (or a short status
line) that the supervisor can read.

Subcommands:
    begin              Start a cycle row; prints {"cycle_id": "..."}
    gather-research    Pull news/sentiment/onchain for the universe
    fetch-bars         Pull recent bars, write to Postgres and CSV
    portfolio          Return NAV/cash/positions JSON
    reconcile          Post-submit: update positions, write NAV snapshot
    dump               Full cycle artifact for postmortem
    status             Desk state report for /status
    new-config         Create a desk_config row
    new-experiment     Create an experiments row
    end                Finalize a cycle (supervisor uses this on error paths)

All subcommands are idempotent or append-only. No subcommand ever deletes
a row. See CLAUDE.md hard rule #5.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from desk import db
from desk.market_data import BinanceClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    """Emit a JSON payload on stdout. Supervisor reads this."""
    print(json.dumps(payload, default=str))


def _starting_nav() -> Decimal:
    return Decimal(os.environ.get("PAPER_STARTING_NAV_USDT", "10000"))


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------


def cmd_begin(trigger: str) -> int:
    if db.any_running_cycle() is not None:
        _emit({"error": "cycle_already_running",
               "running_cycle_id": str(db.any_running_cycle())})
        return 1
    config_id = db.get_active_config_id()
    if config_id is None:
        _emit({"error": "no_active_config",
               "hint": "Run /new-config first"})
        return 1
    cycle_id = db.begin_cycle(trigger=trigger, config_id=config_id)
    _emit({"cycle_id": str(cycle_id), "config_id": str(config_id)})
    return 0


# ---------------------------------------------------------------------------
# gather-research
# ---------------------------------------------------------------------------


def cmd_gather_research(cycle_id: UUID) -> int:
    """Phase 1: return a minimal stub. Phase 2 wires up CryptoPanic + Fear & Greed.

    The structure is the contract between this command and the
    research-analyst subagent. Keeping it stable across phases means the
    agent prompt doesn't change when we add real data sources.
    """
    instruments = db.get_instruments()
    universe = sorted({i["asset_symbol"] for i in instruments})

    # Prior theses per asset (most recent per asset)
    prior: dict[str, dict[str, Any]] = {}
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (rt.asset_id)
                      a.symbol AS asset_symbol, rt.stance, rt.conviction,
                      rt.horizon, rt.summary, rt.raw, rt.created_at
               FROM research_theses rt
               JOIN assets a ON a.asset_id = rt.asset_id
               ORDER BY rt.asset_id, rt.created_at DESC"""
        )
        for row in cur.fetchall():
            prior[row["asset_symbol"]] = {
                "stance": row["stance"],
                "conviction": str(row["conviction"]),
                "horizon": row["horizon"],
                "summary": row["summary"],
                "what_would_invalidate": (row.get("raw") or {}).get("what_would_invalidate"),
                "asof": row["created_at"].isoformat() if row["created_at"] else None,
            }

    payload = {
        "cycle_id": str(cycle_id),
        "universe": universe,
        "news": [],           # Phase 2
        "onchain": {},        # Phase 2
        "sentiment": {},      # Phase 2
        "macro": {},          # Phase 2
        "prior_thesis": prior,
        "notes": (
            "Phase 1 stub: research inputs are empty. The research-analyst "
            "subagent should produce neutral theses with conviction capped "
            "at 0.2 until real data sources are wired up."
        ),
    }
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# fetch-bars
# ---------------------------------------------------------------------------


def cmd_fetch_bars(
    cycle_id: UUID,
    symbol: str,
    timeframe: str,
    limit: int,
) -> int:
    """Pull recent bars, upsert into market_bars, dump the CSV to /tmp/."""
    inst = db.get_instrument_by_symbol(symbol)
    if inst is None:
        _emit({"error": "unknown_symbol", "symbol": symbol})
        return 1

    mode = os.environ.get("PARLEY_MODE", "paper")
    client = BinanceClient.paper() if mode == "paper" else BinanceClient.live()
    bars = client.fetch_bars(symbol, timeframe=timeframe, limit=limit)

    # Persist into market_bars
    rows = [b.as_row() for b in bars]
    if rows:
        db.insert_bars(int(inst["instrument_id"]), timeframe, rows)

    # Dump CSV for the quant subagent to consume
    csv_path = Path(f"/tmp/parley_bars_{cycle_id}_{symbol}_{timeframe}.csv")
    csv_path.parent.mkdir(exist_ok=True)
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume])

    _emit({
        "cycle_id": str(cycle_id),
        "symbol": symbol,
        "timeframe": timeframe,
        "bars_count": len(bars),
        "csv_path": str(csv_path),
        "instrument_id": int(inst["instrument_id"]),
    })
    return 0


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------


def cmd_portfolio(cycle_id: UUID) -> int:
    """Return current portfolio state. PM subagent consumes this."""
    positions = db.get_positions(include_zero=False)
    nav_row = db.get_latest_nav()

    if nav_row is None:
        nav = _starting_nav()
        cash = nav
        positions_value = Decimal("0")
    else:
        nav = Decimal(str(nav_row["equity"]))
        cash = Decimal(str(nav_row["cash"]))
        positions_value = Decimal(str(nav_row["positions_value"]))

    pos_payload = []
    for p in positions:
        qty = Decimal(str(p["qty"]))
        avg = Decimal(str(p["avg_entry_price"]))
        # Crude current-value estimate using avg_entry; real mark-to-market
        # would fetch live price. Reconcile step updates this with real mids.
        weight = (qty * avg) / nav if nav > 0 else Decimal("0")
        pos_payload.append({
            "symbol": p["symbol"],
            "qty": str(qty),
            "avg_entry_price": str(avg),
            "weight": str(weight),
            "realized_pnl": str(p["realized_pnl"]),
        })

    _emit({
        "cycle_id": str(cycle_id),
        "nav": str(nav),
        "cash": str(cash),
        "positions_value": str(positions_value),
        "positions": pos_payload,
        "mode": os.environ.get("PARLEY_MODE", "paper"),
    })
    return 0


# ---------------------------------------------------------------------------
# reconcile — called by post-cycle-snapshot.sh hook
# ---------------------------------------------------------------------------


def cmd_reconcile(prev_exit: int, cycle_id: UUID | None = None) -> int:
    """Mark-to-market the portfolio and write a NAV snapshot.

    Invoked by the post-cycle-snapshot.sh hook after desk.execution submit
    completes. Must not fail in a way that prevents the audit log from
    being written — errors are logged but exit 0.
    """
    try:
        mode = os.environ.get("PARLEY_MODE", "paper")

        # Mark-to-market using live mids
        positions = db.get_positions(include_zero=False)
        positions_value = Decimal("0")
        unrealized_pnl = Decimal("0")

        if positions:
            client = BinanceClient.paper() if mode == "paper" else BinanceClient.live()
            for p in positions:
                try:
                    snap = client.fetch_snapshot(p["symbol"])
                    mid = snap.mid
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Mark-to-market failed for %s: %s", p["symbol"], exc)
                    mid = Decimal(str(p["avg_entry_price"]))
                qty = Decimal(str(p["qty"]))
                avg = Decimal(str(p["avg_entry_price"]))
                positions_value += qty * mid
                unrealized_pnl += qty * (mid - avg)

        # Cash: start_nav + sum(realized_pnl) - cost_of_open_positions
        starting = _starting_nav()
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS rp FROM positions")
            realized = Decimal(str(cur.fetchone()["rp"]))
            cur.execute(
                """SELECT COALESCE(SUM(qty * avg_entry_price), 0) AS cost
                   FROM positions WHERE qty != 0"""
            )
            cost_of_open = Decimal(str(cur.fetchone()["cost"]))

        cash = starting + realized - cost_of_open

        db.insert_nav_snapshot(
            cash=cash,
            positions_value=positions_value,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized,
            mode=mode,
        )

        # Finalize cycle if one was supplied
        if cycle_id is not None:
            status = "completed" if prev_exit == 0 else "failed"
            error = None if prev_exit == 0 else f"submit exited {prev_exit}"
            db.finalize_cycle(cycle_id, status, error)

        _emit({
            "reconciled": True,
            "cash": str(cash),
            "positions_value": str(positions_value),
            "equity": str(cash + positions_value),
            "unrealized_pnl": str(unrealized_pnl),
            "realized_pnl": str(realized),
        })
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("reconcile failed: %s", exc)
        # Still return 0 so the hook doesn't abort the session
        _emit({"reconciled": False, "error": str(exc)})
        return 0


# ---------------------------------------------------------------------------
# dump — full cycle artifact for /postmortem
# ---------------------------------------------------------------------------


def cmd_dump(cycle_id: UUID) -> int:
    """Dump every audit row tied to a cycle as one JSON blob."""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.*, dc.name AS config_name
               FROM cycles c
               LEFT JOIN desk_configs dc ON dc.config_id = c.config_id
               WHERE c.cycle_id = %s""",
            (cycle_id,),
        )
        cycle = cur.fetchone()
        if cycle is None:
            _emit({"error": "cycle_not_found", "cycle_id": str(cycle_id)})
            return 1

        def fetch(sql: str) -> list[dict[str, Any]]:
            cur.execute(sql, (cycle_id,))
            return list(cur.fetchall())

        theses = fetch(
            """SELECT rt.*, a.symbol AS asset_symbol
               FROM research_theses rt
               JOIN assets a ON a.asset_id = rt.asset_id
               WHERE rt.cycle_id = %s ORDER BY a.symbol"""
        )
        signals = fetch(
            """SELECT qs.*, i.symbol
               FROM quant_signals qs
               JOIN instruments i ON i.instrument_id = qs.instrument_id
               WHERE qs.cycle_id = %s ORDER BY i.symbol, qs.strategy"""
        )
        proposals = fetch(
            """SELECT pp.*, i.symbol
               FROM pm_proposals pp
               JOIN instruments i ON i.instrument_id = pp.instrument_id
               WHERE pp.cycle_id = %s ORDER BY i.symbol"""
        )
        risk_evs = fetch(
            """SELECT * FROM risk_events WHERE cycle_id = %s ORDER BY ts"""
        )
        decisions = fetch(
            """SELECT rd.*, i.symbol
               FROM risk_decisions rd
               JOIN pm_proposals pp ON pp.proposal_id = rd.proposal_id
               JOIN instruments i ON i.instrument_id = pp.instrument_id
               WHERE rd.cycle_id = %s ORDER BY i.symbol"""
        )
        orders = fetch(
            """SELECT o.*, i.symbol FROM orders o
               JOIN instruments i ON i.instrument_id = o.instrument_id
               WHERE o.cycle_id = %s ORDER BY o.submitted_at"""
        )
        fills: list[dict[str, Any]] = []
        if orders:
            order_ids = [o["order_id"] for o in orders]
            cur.execute(
                """SELECT * FROM fills WHERE order_id = ANY(%s) ORDER BY ts""",
                (order_ids,),
            )
            fills = list(cur.fetchall())
        agent_runs = fetch(
            """SELECT run_id, agent, model, started_at, ended_at, status,
                      input_tokens, output_tokens, cost_usd, reasoning
               FROM agent_runs WHERE cycle_id = %s ORDER BY started_at"""
        )

    _emit({
        "cycle": dict(cycle),
        "theses": theses,
        "signals": signals,
        "proposals": proposals,
        "risk_events": risk_evs,
        "decisions": decisions,
        "orders": orders,
        "fills": fills,
        "agent_runs": agent_runs,
    })
    return 0


# ---------------------------------------------------------------------------
# status — /status backing
# ---------------------------------------------------------------------------


def cmd_status(days: int) -> int:
    mode = os.environ.get("PARLEY_MODE", "paper")
    nav_row = db.get_latest_nav()
    if nav_row:
        nav = Decimal(str(nav_row["equity"]))
        cash = Decimal(str(nav_row["cash"]))
    else:
        nav = _starting_nav()
        cash = nav

    positions = db.get_positions()
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT status, COUNT(*) AS n FROM cycles
               WHERE started_at > NOW() - INTERVAL '%s days'
               GROUP BY status""",
            (days,),
        )
        cycle_counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            """SELECT cycle_id, started_at, status FROM cycles
               ORDER BY started_at DESC LIMIT 1"""
        )
        last_cycle = cur.fetchone()

        # Day-open NAV for today's change
        cur.execute(
            """SELECT equity FROM nav_snapshots
               WHERE ts::date = (NOW() AT TIME ZONE 'UTC')::date
               ORDER BY ts ASC LIMIT 1"""
        )
        day_open_row = cur.fetchone()
        day_open = Decimal(str(day_open_row["equity"])) if day_open_row else nav
        today_pct = ((nav - day_open) / day_open * 100) if day_open > 0 else Decimal("0")

        cur.execute("SELECT name, is_active, version FROM desk_configs WHERE is_active = TRUE")
        active_cfg = cur.fetchone()

    _emit({
        "mode": mode,
        "active_config": dict(active_cfg) if active_cfg else None,
        "nav": str(nav),
        "cash": str(cash),
        "today_pct": str(today_pct),
        "positions": [
            {
                "symbol": p["symbol"],
                "qty": str(p["qty"]),
                "avg_entry_price": str(p["avg_entry_price"]),
                "realized_pnl": str(p["realized_pnl"]),
            }
            for p in positions
        ],
        "cycle_counts": cycle_counts,
        "last_cycle": dict(last_cycle) if last_cycle else None,
    })
    return 0


# ---------------------------------------------------------------------------
# new-config
# ---------------------------------------------------------------------------


def _hash_file(p: Path) -> str | None:
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cmd_new_config(name: str, notes: str | None, activate: bool) -> int:
    """Create a desk_config row with hashes of current agent prompts.

    We deliberately compute hashes at creation time, not read-time. This
    means editing an agent prompt does NOT silently update a historical
    config — you must create a new one, which is the desired behavior.
    """
    agents_dir = Path(".claude/agents")
    prompt_hashes = {
        "research": _hash_file(agents_dir / "research-analyst.md"),
        "quant": _hash_file(agents_dir / "quant.md"),
        "pm": _hash_file(agents_dir / "portfolio-manager.md"),
        "risk": _hash_file(agents_dir / "risk-manager.md"),
        "execution": _hash_file(agents_dir / "execution-trader.md"),
    }

    # Snapshot current risk_limits for reproducibility
    limits = db.get_risk_limits(active_only=False)
    limits_snapshot = [
        {
            "name": L["name"],
            "rule_type": L["rule_type"],
            "value": str(L["value"]),
            "scope": L["scope"],
            "scope_ref": L["scope_ref"],
            "is_active": L["is_active"],
        }
        for L in limits
    ]

    config_json = {
        "research":  {"model": "claude-opus-4-7",   "prompt_hash": prompt_hashes["research"]},
        "quant":     {"model": "claude-sonnet-4-6", "prompt_hash": prompt_hashes["quant"]},
        "pm":        {"model": "claude-opus-4-7",   "prompt_hash": prompt_hashes["pm"]},
        "risk":      {"model": "claude-opus-4-7",   "prompt_hash": prompt_hashes["risk"]},
        "execution": {"model": "claude-sonnet-4-6", "prompt_hash": prompt_hashes["execution"]},
        "risk_limits_snapshot": limits_snapshot,
        "universe": [i["symbol"] for i in db.get_instruments()],
        "timeframes": {"signals": "1h", "storage": "1m"},
        "notes": notes or f"Config {name} created at {datetime.now(timezone.utc).isoformat()}",
    }

    with db.get_conn() as conn, conn.cursor() as cur:
        # Check uniqueness
        cur.execute("SELECT config_id FROM desk_configs WHERE name = %s", (name,))
        if cur.fetchone() is not None:
            _emit({"error": "config_name_exists", "name": name})
            return 1

        if activate:
            cur.execute("UPDATE desk_configs SET is_active = FALSE WHERE is_active = TRUE")

        cur.execute(
            """INSERT INTO desk_configs (name, description, version, is_active, config)
               VALUES (%s, %s, 1, %s, %s) RETURNING config_id""",
            (name, notes, activate, db.to_jsonb(config_json)),
        )
        row = cur.fetchone()
        assert row is not None
        cfg_id = row["config_id"]

    _emit({
        "config_id": str(cfg_id),
        "name": name,
        "activated": activate,
        "prompt_hashes": prompt_hashes,
    })
    return 0


# ---------------------------------------------------------------------------
# new-experiment
# ---------------------------------------------------------------------------


def cmd_new_experiment(name: str, hypothesis: str, config_name: str | None) -> int:
    with db.get_conn() as conn, conn.cursor() as cur:
        if config_name:
            cur.execute("SELECT config_id FROM desk_configs WHERE name = %s", (config_name,))
            row = cur.fetchone()
            if row is None:
                _emit({"error": "config_not_found", "name": config_name})
                return 1
            config_id = row["config_id"]
        else:
            config_id = db.get_active_config_id()
            if config_id is None:
                _emit({"error": "no_active_config"})
                return 1

        cur.execute(
            """INSERT INTO experiments (name, hypothesis, config_id)
               VALUES (%s, %s, %s) RETURNING experiment_id""",
            (name, hypothesis, config_id),
        )
        exp_row = cur.fetchone()
        assert exp_row is not None
        exp_id = exp_row["experiment_id"]

    _emit({
        "experiment_id": str(exp_id),
        "name": name,
        "config_id": str(config_id),
        "hypothesis": hypothesis,
    })
    return 0


# ---------------------------------------------------------------------------
# end
# ---------------------------------------------------------------------------


def cmd_end(cycle_id: UUID, status: str, error: str | None) -> int:
    db.finalize_cycle(cycle_id, status, error)
    _emit({"cycle_id": str(cycle_id), "status": status})
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk.cycle")
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("begin")
    b.add_argument("--trigger", default="manual")

    gr = sub.add_parser("gather-research")
    gr.add_argument("--cycle", required=True)

    fb = sub.add_parser("fetch-bars")
    fb.add_argument("--cycle", required=True)
    fb.add_argument("--symbol", required=True)
    fb.add_argument("--tf", default="1h")
    fb.add_argument("--limit", type=int, default=200)

    pf = sub.add_parser("portfolio")
    pf.add_argument("--cycle", required=True)

    rc = sub.add_parser("reconcile")
    rc.add_argument("--prev-exit", type=int, default=0)
    rc.add_argument("--cycle", default=None)

    dp = sub.add_parser("dump")
    dp.add_argument("--cycle", required=True)

    st = sub.add_parser("status")
    st.add_argument("--days", type=int, default=7)
    st.add_argument("--format", default="json")

    nc = sub.add_parser("new-config")
    nc.add_argument("--name", required=True)
    nc.add_argument("--notes", default=None)
    nc.add_argument("--activate", action="store_true")

    ne = sub.add_parser("new-experiment")
    ne.add_argument("--name", required=True)
    ne.add_argument("--hypothesis", required=True)
    ne.add_argument("--config", default=None)

    en = sub.add_parser("end")
    en.add_argument("--cycle", required=True)
    en.add_argument("--status", required=True,
                    choices=("completed", "failed", "aborted"))
    en.add_argument("--error", default=None)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    match args.cmd:
        case "begin":
            return cmd_begin(args.trigger)
        case "gather-research":
            return cmd_gather_research(UUID(args.cycle))
        case "fetch-bars":
            return cmd_fetch_bars(UUID(args.cycle), args.symbol, args.tf, args.limit)
        case "portfolio":
            return cmd_portfolio(UUID(args.cycle))
        case "reconcile":
            return cmd_reconcile(args.prev_exit, UUID(args.cycle) if args.cycle else None)
        case "dump":
            return cmd_dump(UUID(args.cycle))
        case "status":
            return cmd_status(args.days)
        case "new-config":
            return cmd_new_config(args.name, args.notes, args.activate)
        case "new-experiment":
            return cmd_new_experiment(args.name, args.hypothesis, args.config)
        case "end":
            return cmd_end(UUID(args.cycle), args.status, args.error)
    return 1


if __name__ == "__main__":
    sys.exit(main())
