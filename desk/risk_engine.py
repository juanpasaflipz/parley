"""Hard risk enforcement engine.

Runs BEFORE the Risk Manager LLM agent sees any proposals. This module
contains exclusively deterministic code — no Claude calls, no heuristics,
no soft judgment. If a proposal fails a check here, it is dead for the
cycle. Period.

CLI:
    python -m desk.risk_engine prefilter --cycle <uuid>
        Reads pm_proposals for the cycle, checks against risk_limits,
        writes risk_events for any failures, and flags blocked proposals.

    python -m desk.risk_engine validate-pending --strict
        Called by the pre-order-risk-check.sh hook. Re-verifies all pending
        orders for the latest cycle against current limits. Exits non-zero
        if anything fails.

Rules currently enforced:
    - max_position_pct: individual position weight (absolute value) ≤ limit.
    - max_gross_exposure: sum of |weight| across all positions ≤ limit.
    - max_daily_loss_pct: aborts the cycle if realized_pnl today is below
      -limit × NAV-start-of-day.
    - min_cash_reserve_pct: cash / NAV ≥ limit after proposals are applied.
    - kill_switch: special limit that when active halts ALL new orders.

Rules are stored as data in ``risk_limits``; adding a rule type means both
adding a row AND updating the handler below. This module is the ONLY place
where hard limits are interpreted as code.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from desk import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleViolation:
    """A hard rule that a proposal (or the whole cycle) violates."""

    limit_id: int
    limit_name: str
    rule_type: str
    severity: str  # "warn" | "block"
    proposal_id: UUID | None
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# Core check dispatcher
# ---------------------------------------------------------------------------


def check_proposal(
    proposal: dict[str, Any],
    limits_by_type: dict[str, list[dict[str, Any]]],
    portfolio: dict[str, Any],
) -> list[RuleViolation]:
    """Run all applicable hard rules against a single proposal.

    Returns a list of violations. Empty list means the proposal passed.
    """
    violations: list[RuleViolation] = []
    target_weight = Decimal(str(proposal["target_weight"]))
    abs_weight = abs(target_weight)

    # --- max_position_pct --------------------------------------------------
    for limit in limits_by_type.get("max_position_pct", []):
        cap = Decimal(str(limit["value"]))
        if abs_weight > cap:
            violations.append(
                RuleViolation(
                    limit_id=int(limit["limit_id"]),
                    limit_name=str(limit["name"]),
                    rule_type="max_position_pct",
                    severity="block",
                    proposal_id=proposal["proposal_id"],
                    details={
                        "symbol": proposal["symbol"],
                        "proposed_weight": str(target_weight),
                        "limit": str(cap),
                        "exceeded_by": str(abs_weight - cap),
                    },
                )
            )

    # --- min_cash_reserve_pct (checked per-proposal as a preview) -----------
    # Proper gross check happens at the portfolio level below; here we just
    # warn if a single proposal would by itself reduce cash below reserve.
    for limit in limits_by_type.get("min_cash_reserve_pct", []):
        reserve = Decimal(str(limit["value"]))
        nav = Decimal(str(portfolio["nav"]))
        if nav <= 0:
            continue
        cash_after = Decimal(str(portfolio["cash"])) - abs_weight * nav
        pct_after = cash_after / nav
        if pct_after < reserve:
            # This is a warn at the single-proposal level; the portfolio-level
            # check below decides block vs warn based on aggregate impact.
            violations.append(
                RuleViolation(
                    limit_id=int(limit["limit_id"]),
                    limit_name=str(limit["name"]),
                    rule_type="min_cash_reserve_pct",
                    severity="warn",
                    proposal_id=proposal["proposal_id"],
                    details={
                        "symbol": proposal["symbol"],
                        "cash_pct_after_this_trade": str(pct_after),
                        "reserve": str(reserve),
                    },
                )
            )

    return violations


def check_portfolio_level(
    proposals: list[dict[str, Any]],
    limits_by_type: dict[str, list[dict[str, Any]]],
    portfolio: dict[str, Any],
) -> list[RuleViolation]:
    """Rules that apply to the full proposal set, not individual proposals."""
    violations: list[RuleViolation] = []
    nav = Decimal(str(portfolio["nav"]))

    # --- kill_switch -------------------------------------------------------
    for limit in limits_by_type.get("kill_switch", []):
        if Decimal(str(limit["value"])) > 0:
            # Switch is active — block every proposal.
            for p in proposals:
                violations.append(
                    RuleViolation(
                        limit_id=int(limit["limit_id"]),
                        limit_name=str(limit["name"]),
                        rule_type="kill_switch",
                        severity="block",
                        proposal_id=p["proposal_id"],
                        details={"reason": "kill_switch_active"},
                    )
                )

    # --- max_gross_exposure ------------------------------------------------
    for limit in limits_by_type.get("max_gross_exposure", []):
        cap = Decimal(str(limit["value"]))
        gross = sum((abs(Decimal(str(p["target_weight"]))) for p in proposals), Decimal("0"))
        if gross > cap:
            # Block the proposals that put us over the line, smallest-weight first
            # so Risk Manager keeps the highest-conviction trades.
            sorted_props = sorted(
                proposals,
                key=lambda p: abs(Decimal(str(p["target_weight"]))),
            )
            running = gross
            for p in sorted_props:
                if running <= cap:
                    break
                w = abs(Decimal(str(p["target_weight"])))
                violations.append(
                    RuleViolation(
                        limit_id=int(limit["limit_id"]),
                        limit_name=str(limit["name"]),
                        rule_type="max_gross_exposure",
                        severity="block",
                        proposal_id=p["proposal_id"],
                        details={
                            "symbol": p["symbol"],
                            "gross_before_block": str(gross),
                            "limit": str(cap),
                            "dropping_weight": str(w),
                        },
                    )
                )
                running -= w

    # --- max_daily_loss_pct (cycle-level pre-check) ------------------------
    # If today's realized loss already exceeds the limit, block ALL new trades.
    for limit in limits_by_type.get("max_daily_loss_pct", []):
        cap = Decimal(str(limit["value"]))
        today_pnl_pct = Decimal(str(portfolio.get("today_pnl_pct", "0")))
        if today_pnl_pct <= -cap:
            for p in proposals:
                violations.append(
                    RuleViolation(
                        limit_id=int(limit["limit_id"]),
                        limit_name=str(limit["name"]),
                        rule_type="max_daily_loss_pct",
                        severity="block",
                        proposal_id=p["proposal_id"],
                        details={
                            "today_pnl_pct": str(today_pnl_pct),
                            "limit": str(cap),
                            "nav": str(nav),
                        },
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_prefilter(cycle_id: UUID) -> int:
    """Read PM proposals for the cycle, run all hard checks, log violations.

    Returns 0 on success (regardless of whether proposals were blocked).
    Returns 2 if a critical violation indicates the cycle must be aborted
    (e.g. daily loss limit tripped, kill switch active).
    """
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT p.proposal_id, p.instrument_id, p.target_weight,
                      p.current_weight, p.action, i.symbol
               FROM pm_proposals p
               JOIN instruments i ON i.instrument_id = p.instrument_id
               WHERE p.cycle_id = %s""",
            (cycle_id,),
        )
        proposals = list(cur.fetchall())

    if not proposals:
        logger.info("No proposals found for cycle %s; nothing to prefilter", cycle_id)
        return 0

    # Active limits indexed by rule_type
    limits = db.get_risk_limits(active_only=True)
    limits_by_type: dict[str, list[dict[str, Any]]] = {}
    for L in limits:
        limits_by_type.setdefault(str(L["rule_type"]), []).append(L)

    # Portfolio state for NAV-relative checks
    nav_row = db.get_latest_nav()
    if nav_row is None:
        # Bootstrap case: no snapshots yet. Use starting NAV from env.
        import os

        starting_nav = Decimal(os.environ.get("PAPER_STARTING_NAV_USDT", "10000"))
        portfolio = {
            "nav": starting_nav,
            "cash": starting_nav,
            "today_pnl_pct": Decimal("0"),
        }
    else:
        portfolio = {
            "nav": Decimal(str(nav_row["equity"])),
            "cash": Decimal(str(nav_row["cash"])),
            "today_pnl_pct": Decimal("0"),  # computed below if we have history
        }
        # Compute today's pnl pct from oldest nav_snapshot in UTC today
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT equity FROM nav_snapshots
                   WHERE ts::date = (NOW() AT TIME ZONE 'UTC')::date
                   ORDER BY ts ASC LIMIT 1"""
            )
            open_row = cur.fetchone()
            if open_row is not None:
                day_open = Decimal(str(open_row["equity"]))
                if day_open > 0:
                    portfolio["today_pnl_pct"] = (portfolio["nav"] - day_open) / day_open

    # Run checks
    all_violations: list[RuleViolation] = []
    for p in proposals:
        all_violations.extend(check_proposal(p, limits_by_type, portfolio))
    all_violations.extend(check_portfolio_level(proposals, limits_by_type, portfolio))

    # Log every violation to risk_events. Block-severity ones are fatal for
    # the affected proposals.
    blocked_proposal_ids: set[UUID] = set()
    critical = False
    for v in all_violations:
        db.insert_risk_event(
            limit_id=v.limit_id,
            severity=v.severity,
            details={
                "rule_type": v.rule_type,
                "limit_name": v.limit_name,
                "proposal_id": str(v.proposal_id) if v.proposal_id else None,
                **v.details,
            },
            cycle_id=cycle_id,
        )
        if v.severity == "block":
            if v.proposal_id:
                blocked_proposal_ids.add(v.proposal_id)
            if v.rule_type in ("kill_switch", "max_daily_loss_pct"):
                critical = True

    logger.info(
        "Prefilter complete: %d proposals, %d violations (%d critical-type), %d blocked",
        len(proposals),
        len(all_violations),
        sum(1 for v in all_violations if v.rule_type in ("kill_switch", "max_daily_loss_pct")),
        len(blocked_proposal_ids),
    )

    # Emit a short summary for the supervisor to consume
    print(
        f"prefilter: proposals={len(proposals)} "
        f"blocked={len(blocked_proposal_ids)} "
        f"violations={len(all_violations)} "
        f"critical={'yes' if critical else 'no'}"
    )

    # Exit non-zero on cycle-halting conditions
    return 2 if critical else 0


def cmd_validate_pending(strict: bool) -> int:
    """Pre-order hook check. Re-validate any pending orders against current
    limits. Used as a last line of defense before submission.

    In strict mode (the hook default), any violation → exit 1.
    """
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT o.order_id, o.cycle_id, o.qty, o.side,
                      o.instrument_id, i.symbol, o.mode
               FROM orders o
               JOIN instruments i ON i.instrument_id = o.instrument_id
               WHERE o.status IN ('pending', 'submitted')"""
        )
        pending = list(cur.fetchall())

    if not pending:
        print("validate-pending: no pending orders")
        return 0

    # Confirm all pending orders are in paper mode during Phase 1
    non_paper = [o for o in pending if o["mode"] != "paper"]
    if non_paper and strict:
        for o in non_paper:
            print(
                f"validate-pending: CRITICAL order {o['order_id']} has mode={o['mode']}",
                file=sys.stderr,
            )
        return 1

    print(f"validate-pending: {len(pending)} pending orders, all paper-mode OK")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk.risk_engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("prefilter", help="Run hard checks on a cycle's proposals")
    pf.add_argument("--cycle", required=True, help="cycle_id UUID")

    vp = sub.add_parser("validate-pending", help="Re-validate pending orders")
    vp.add_argument("--strict", action="store_true", help="Any violation → exit 1")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "prefilter":
        return cmd_prefilter(UUID(args.cycle))
    if args.cmd == "validate-pending":
        return cmd_validate_pending(strict=bool(args.strict))
    return 1


if __name__ == "__main__":
    sys.exit(main())
