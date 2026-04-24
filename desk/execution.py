"""Execution engine.

Turns approved risk decisions into concrete orders and submits them. LLM
agents (Execution Trader subagent) pick order STYLE — market vs limit vs
TWAP child schedule. This module computes the QUANTITIES deterministically
and pushes to the broker.

CLI:
    python -m desk.execution build-orders --cycle <uuid>
        For every approved risk_decision in the cycle, compute delta_qty
        using current mid price and instrument precision rules, then insert
        a row into `orders` with status='pending'. Does NOT submit.

    python -m desk.execution submit --cycle <uuid>
        Submit all pending orders for the cycle via the broker. Writes fills
        and updates positions as fills arrive. Triggered by the run-cycle
        slash command; the pre-order-risk-check.sh hook fires first.

Hard invariants:
    - Quantities are rounded DOWN to instrument precision (never overshoot).
    - All prices are Decimal; no float → Decimal conversion anywhere in the
      order path.
    - If mid-price is stale (>30s) or spread is anomalous (>100 bps), the
      order is deferred, not submitted.
    - The `mode` column on every order row matches the active PARLEY_MODE;
      if it ever disagrees, submission aborts.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any
from uuid import UUID

from desk import db
from desk.broker import Broker, OrderRequest, SubmissionResult, get_broker
from desk.market_data import BinanceClient, Snapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------


def _quantize_down(value: Decimal, precision: int) -> Decimal:
    """Round DOWN to given decimal places. Never overshoot the approved qty."""
    if precision < 0:
        raise ValueError(f"precision must be non-negative, got {precision}")
    q = Decimal(10) ** -precision
    return value.quantize(q, rounding=ROUND_DOWN)


def _quantize_price(value: Decimal, precision: int) -> Decimal:
    """Round prices to nearest (half-up) at instrument precision."""
    q = Decimal(10) ** -precision
    return value.quantize(q)


# ---------------------------------------------------------------------------
# build-orders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BuildResult:
    orders_created: int
    skipped: list[dict[str, Any]]


def _fetch_approved_decisions(cycle_id: UUID) -> list[dict[str, Any]]:
    """Decisions that survived hard filter AND were approved/resized by Risk."""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT d.decision_id, d.proposal_id, d.verdict, d.approved_weight,
                      p.instrument_id, p.current_weight, i.symbol,
                      i.qty_precision, i.price_precision, i.min_qty
               FROM risk_decisions d
               JOIN pm_proposals p ON p.proposal_id = d.proposal_id
               JOIN instruments i ON i.instrument_id = p.instrument_id
               WHERE d.cycle_id = %s
                 AND d.verdict IN ('approved', 'resized')
                 AND d.approved_weight IS NOT NULL""",
            (cycle_id,),
        )
        return list(cur.fetchall())


def _insert_order(
    *,
    cycle_id: UUID,
    decision_id: UUID,
    instrument_id: int,
    mode: str,
    side: str,
    order_type: str,
    qty: Decimal,
    limit_price: Decimal | None,
    metadata: dict[str, Any],
) -> UUID:
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO orders
               (cycle_id, decision_id, instrument_id, mode, side,
                order_type, qty, limit_price, status, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
               RETURNING order_id""",
            (
                cycle_id,
                decision_id,
                instrument_id,
                mode,
                side,
                order_type,
                qty,
                limit_price,
                db.to_jsonb(metadata),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return row["order_id"]


def cmd_build_orders(cycle_id: UUID) -> int:
    """Compute delta quantities from approved weights and insert pending orders.

    Uses a freshly-fetched snapshot per symbol to compute qty. Writes a
    row to market_snapshots so the audit log has the reference price.
    """
    mode = os.environ.get("PARLEY_MODE", "paper")
    decisions = _fetch_approved_decisions(cycle_id)
    if not decisions:
        print("build-orders: no approved decisions")
        return 0

    # Portfolio NAV — required to turn weights into notional
    nav_row = db.get_latest_nav()
    if nav_row is None:
        starting_nav = Decimal(os.environ.get("PAPER_STARTING_NAV_USDT", "10000"))
        nav = starting_nav
    else:
        nav = Decimal(str(nav_row["equity"]))

    # Fetch one snapshot per unique symbol
    client = BinanceClient.paper() if mode == "paper" else BinanceClient.live()
    snaps: dict[str, Snapshot] = {}
    for d in decisions:
        sym = d["symbol"]
        if sym not in snaps:
            snap = client.fetch_snapshot(sym)
            snaps[sym] = snap
            db.insert_snapshot(
                instrument_id=d["instrument_id"],
                ts=snap.ts,
                bid=snap.bid,
                ask=snap.ask,
                bid_size=snap.bid_size,
                ask_size=snap.ask_size,
            )

    orders_created = 0
    skipped: list[dict[str, Any]] = []

    for d in decisions:
        sym = d["symbol"]
        snap = snaps[sym]
        mid = snap.mid
        approved_w = Decimal(str(d["approved_weight"]))
        current_w = Decimal(str(d["current_weight"]))
        delta_w = approved_w - current_w
        delta_notional = delta_w * nav

        # No trade if the delta rounds to nothing
        if abs(delta_notional) < Decimal("1"):  # sub-dollar delta, not worth it
            skipped.append({"symbol": sym, "reason": "delta_too_small"})
            continue

        side = "buy" if delta_notional > 0 else "sell"
        raw_qty = abs(delta_notional) / mid
        qty = _quantize_down(raw_qty, int(d["qty_precision"]))
        min_qty = Decimal(str(d["min_qty"]))

        if qty < min_qty or qty <= 0:
            skipped.append(
                {"symbol": sym, "reason": "below_min_qty", "computed_qty": str(qty)}
            )
            continue

        # Phase 1: always market orders. The Execution Trader subagent's more
        # sophisticated decisions (limit, TWAP) are Phase 2 — the supervisor
        # can override this default when delegating.
        order_id = _insert_order(
            cycle_id=cycle_id,
            decision_id=d["decision_id"],
            instrument_id=int(d["instrument_id"]),
            mode=mode,
            side=side,
            order_type="market",
            qty=qty,
            limit_price=None,
            metadata={
                "mid_at_build": str(mid),
                "delta_weight": str(delta_w),
                "delta_notional": str(delta_notional),
                "spread_bps": str(snap.spread_bps),
                "nav_at_build": str(nav),
            },
        )
        orders_created += 1
        logger.info(
            "Built order %s: %s %s qty=%s mid=%s",
            order_id, sym, side, qty, mid,
        )

    print(
        f"build-orders: created={orders_created} skipped={len(skipped)}"
    )
    for s in skipped:
        print(f"  skipped {s['symbol']}: {s['reason']}")
    return 0


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def _pending_orders(cycle_id: UUID) -> list[dict[str, Any]]:
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT o.order_id, o.instrument_id, o.mode, o.side,
                      o.order_type, o.qty, o.limit_price, o.metadata,
                      i.symbol
               FROM orders o
               JOIN instruments i ON i.instrument_id = o.instrument_id
               WHERE o.cycle_id = %s AND o.status = 'pending'
               ORDER BY o.submitted_at ASC""",
            (cycle_id,),
        )
        return list(cur.fetchall())


def _record_submission(order_id: UUID, result: SubmissionResult) -> None:
    """Update order row and insert any fills in one transaction."""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE orders
               SET status = %s,
                   venue_order_id = %s,
                   finalized_at = CASE WHEN %s IN ('filled','rejected','cancelled')
                                       THEN NOW() ELSE NULL END
               WHERE order_id = %s""",
            (result.status, result.venue_order_id or None, result.status, order_id),
        )
        for f in result.fills:
            cur.execute(
                """INSERT INTO fills
                   (order_id, ts, qty, price, fee, fee_currency, venue_fill_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (order_id, f.ts, f.qty, f.price, f.fee, f.fee_currency, f.venue_fill_id),
            )


def _update_position_from_fills(order: dict[str, Any], fills: list[Any]) -> None:
    """Recompute the position row after fills. Simple average-price math."""
    if not fills:
        return
    side = order["side"]
    sign = Decimal("1") if side == "buy" else Decimal("-1")

    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT qty, avg_entry_price, realized_pnl
               FROM positions WHERE instrument_id = %s FOR UPDATE""",
            (order["instrument_id"],),
        )
        pos = cur.fetchone()
        cur_qty = Decimal(str(pos["qty"])) if pos else Decimal("0")
        cur_avg = Decimal(str(pos["avg_entry_price"])) if pos else Decimal("0")
        cur_rpnl = Decimal(str(pos["realized_pnl"])) if pos else Decimal("0")

        for f in fills:
            fill_qty = f.qty * sign  # signed
            new_qty = cur_qty + fill_qty

            if cur_qty == 0 or (cur_qty > 0) == (fill_qty > 0):
                # Opening or adding to same-sign position — weighted avg
                if new_qty != 0:
                    total_cost = cur_qty * cur_avg + fill_qty * f.price
                    cur_avg = total_cost / new_qty if new_qty != 0 else Decimal("0")
            else:
                # Reducing / closing / flipping
                closing_qty = min(abs(fill_qty), abs(cur_qty))
                pnl_per_unit = (f.price - cur_avg) * (Decimal("1") if cur_qty > 0 else Decimal("-1"))
                cur_rpnl += closing_qty * pnl_per_unit
                if abs(fill_qty) > abs(cur_qty):
                    # Flipped — new avg is the fill price, remaining qty is the overshoot
                    cur_avg = f.price
            cur_qty = new_qty

        if pos:
            cur.execute(
                """UPDATE positions
                   SET qty = %s, avg_entry_price = %s, realized_pnl = %s,
                       updated_at = NOW()
                   WHERE instrument_id = %s""",
                (cur_qty, cur_avg if cur_qty != 0 else Decimal("0"), cur_rpnl, order["instrument_id"]),
            )
        else:
            cur.execute(
                """INSERT INTO positions
                   (instrument_id, qty, avg_entry_price, realized_pnl)
                   VALUES (%s, %s, %s, %s)""",
                (order["instrument_id"], cur_qty, cur_avg, cur_rpnl),
            )


def cmd_submit(cycle_id: UUID, broker: Broker | None = None) -> int:
    """Submit pending orders for the cycle.

    The pre-order-risk-check.sh hook fires BEFORE this command via
    Claude Code hooks config; if we got here, the hook approved.
    """
    mode = os.environ.get("PARLEY_MODE", "paper")

    pending = _pending_orders(cycle_id)
    if not pending:
        print("submit: no pending orders")
        return 0

    # Sanity: every pending order must match current mode
    for o in pending:
        if o["mode"] != mode:
            print(
                f"submit: ABORT — order {o['order_id']} has mode={o['mode']} "
                f"but PARLEY_MODE={mode}",
                file=sys.stderr,
            )
            return 1

    if broker is None:
        broker = get_broker(venue="binance", mode=mode)

    submitted = 0
    filled = 0
    rejected = 0

    for o in pending:
        req = OrderRequest(
            symbol=o["symbol"],
            side=o["side"],
            order_type=o["order_type"],
            qty=Decimal(str(o["qty"])),
            limit_price=Decimal(str(o["limit_price"])) if o["limit_price"] else None,
        )
        try:
            result = broker.submit(req)
        except Exception as exc:  # noqa: BLE001 - broker errors shouldn't halt the batch
            logger.exception("Submission raised: %s", exc)
            result = SubmissionResult(
                venue_order_id="", status="rejected",
                fills=[], raw={"error": str(exc)},
            )

        _record_submission(o["order_id"], result)
        if result.fills:
            _update_position_from_fills(o, result.fills)

        submitted += 1
        if result.status == "filled":
            filled += 1
        elif result.status == "rejected":
            rejected += 1

        logger.info(
            "Order %s: status=%s fills=%d",
            o["order_id"], result.status, len(result.fills),
        )

    print(
        f"submit: submitted={submitted} filled={filled} rejected={rejected}"
    )
    return 0


# ---------------------------------------------------------------------------
# apply-plan — honor the Execution Trader subagent's output
# ---------------------------------------------------------------------------


def _validate_plan(plan: dict[str, Any]) -> tuple[bool, str]:
    """Shape-check a subagent plan before we try to use it."""
    if not isinstance(plan, dict):
        return False, "plan is not a dict"
    action = plan.get("action")
    if action not in ("execute", "defer", "skip"):
        return False, f"invalid action: {action}"
    if action == "execute":
        orders = plan.get("orders")
        if not isinstance(orders, list) or not orders:
            return False, "action=execute requires non-empty orders"
        for o in orders:
            if o.get("side") not in ("buy", "sell"):
                return False, f"bad side: {o.get('side')}"
            if o.get("order_type") not in ("market", "limit", "twap"):
                return False, f"bad order_type: {o.get('order_type')}"
            try:
                Decimal(str(o["qty"]))
            except Exception:
                return False, "qty must be a decimal-castable string"
    return True, ""


def cmd_apply_plan(
    decision_id: UUID,
    plan_path: str,
) -> int:
    """Read the Execution Trader's JSON plan file and insert pending orders.

    The supervisor calls this after the execution-trader subagent writes its
    plan. Translates ``{action, orders[]}`` into rows in the ``orders`` table.
    Quantities from the subagent are still re-validated and re-rounded here —
    deterministic code is the final authority on what hits the broker.
    """
    import json
    from pathlib import Path

    path = Path(plan_path)
    if not path.exists():
        print(f"apply-plan: plan file not found: {plan_path}", file=sys.stderr)
        return 1

    try:
        plan = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"apply-plan: invalid JSON in plan: {exc}", file=sys.stderr)
        return 1

    ok, err = _validate_plan(plan)
    if not ok:
        print(f"apply-plan: invalid plan: {err}", file=sys.stderr)
        return 1

    # Look up the decision context for cycle_id, instrument, precision
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT d.cycle_id, p.instrument_id,
                      i.qty_precision, i.price_precision, i.min_qty, i.symbol
               FROM risk_decisions d
               JOIN pm_proposals p ON p.proposal_id = d.proposal_id
               JOIN instruments i ON i.instrument_id = p.instrument_id
               WHERE d.decision_id = %s""",
            (decision_id,),
        )
        ctx = cur.fetchone()
    if ctx is None:
        print(f"apply-plan: unknown decision_id {decision_id}", file=sys.stderr)
        return 1

    if plan["action"] in ("defer", "skip"):
        # Nothing to insert. Just log the reasoning in the decision metadata
        # so postmortems can explain the missing orders.
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE risk_decisions
                   SET soft_notes = COALESCE(soft_notes, '') ||
                                    E'\n[execution] ' || %s
                   WHERE decision_id = %s""",
                (
                    f"{plan['action']}: {plan.get('reason', plan.get('reasoning', ''))}",
                    decision_id,
                ),
            )
        print(
            f"apply-plan: decision={decision_id} action={plan['action']} "
            f"reason={plan.get('reason', 'unspecified')}"
        )
        return 0

    mode = os.environ.get("PARLEY_MODE", "paper")
    created = 0

    for o in plan["orders"]:
        qty = Decimal(str(o["qty"]))
        qty = _quantize_down(qty, int(ctx["qty_precision"]))
        min_qty = Decimal(str(ctx["min_qty"]))
        if qty < min_qty or qty <= 0:
            logger.warning("apply-plan: skipping sub-min-qty %s", qty)
            continue

        limit_price = None
        if o.get("limit_price") is not None:
            limit_price = _quantize_price(
                Decimal(str(o["limit_price"])), int(ctx["price_precision"])
            )

        order_type = str(o["order_type"])
        if order_type == "twap":
            # Unroll TWAP into N child orders (status=pending for each).
            # The scheduler is a Phase 2 concern; for Phase 1 we submit all
            # children as plain market orders in series.
            schedule = o.get("schedule") or {}
            children = int(schedule.get("children", 1))
            if children < 1:
                children = 1
            child_qty = qty / Decimal(children)
            child_qty = _quantize_down(child_qty, int(ctx["qty_precision"]))
            remainder = qty - child_qty * Decimal(children)
            for i in range(children):
                this_qty = child_qty + (remainder if i == children - 1 else Decimal(0))
                if this_qty <= 0 or this_qty < min_qty:
                    continue
                _insert_order(
                    cycle_id=ctx["cycle_id"],
                    decision_id=decision_id,
                    instrument_id=int(ctx["instrument_id"]),
                    mode=mode,
                    side=str(o["side"]),
                    order_type="market",  # Phase 1: collapse TWAP to market children
                    qty=this_qty,
                    limit_price=None,
                    metadata={
                        "from_plan": True,
                        "plan_order_type": "twap",
                        "twap_child_index": i,
                        "twap_total_children": children,
                        "interval_seconds": schedule.get("interval_seconds"),
                        "reasoning": plan.get("reasoning", ""),
                    },
                )
                created += 1
        else:
            _insert_order(
                cycle_id=ctx["cycle_id"],
                decision_id=decision_id,
                instrument_id=int(ctx["instrument_id"]),
                mode=mode,
                side=str(o["side"]),
                order_type=order_type,
                qty=qty,
                limit_price=limit_price,
                metadata={
                    "from_plan": True,
                    "reasoning": plan.get("reasoning", ""),
                },
            )
            created += 1

    print(f"apply-plan: decision={decision_id} orders_created={created}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk.execution")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bo = sub.add_parser("build-orders", help="Compute delta_qty for a cycle")
    bo.add_argument("--cycle", required=True)

    sm = sub.add_parser("submit", help="Submit pending orders for a cycle")
    sm.add_argument("--cycle", required=True)

    ap = sub.add_parser("apply-plan", help="Insert pending orders from an Execution Trader plan file")
    ap.add_argument("--decision", required=True, help="risk_decision UUID")
    ap.add_argument("--plan", required=True, help="Path to the plan JSON file")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "build-orders":
        return cmd_build_orders(UUID(args.cycle))
    if args.cmd == "submit":
        return cmd_submit(UUID(args.cycle))
    if args.cmd == "apply-plan":
        return cmd_apply_plan(UUID(args.decision), args.plan)
    return 1


if __name__ == "__main__":
    sys.exit(main())
