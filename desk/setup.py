"""Bootstrap script for a fresh Parley installation.

Run after ``psql $DATABASE_URL -f schema.sql`` to:
    1. Verify schema + seed data are present
    2. Ensure risk_limits are populated with sane defaults
    3. Create a default ``baseline-v1`` desk_config if no config exists
    4. Write an initial nav_snapshots row reflecting PAPER_STARTING_NAV_USDT
    5. Print a readable summary the operator can verify

Idempotent — safe to run repeatedly. Nothing is destructive; rows are only
inserted where they're missing.

Usage:
    python -m desk.setup
    python -m desk.setup --force-nav    # overwrite NAV snapshot (dangerous;
                                        # only use on an empty-positions desk)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from decimal import Decimal

from desk import db
from desk.cycle import cmd_new_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default seed data (also present in schema.sql but re-asserted here)
# ---------------------------------------------------------------------------


DEFAULT_ASSETS = [
    ("BTC", "Bitcoin"),
    ("ETH", "Ethereum"),
    ("SOL", "Solana"),
]

DEFAULT_RISK_LIMITS = [
    ("max_single_position", "max_position_pct",     Decimal("0.20")),
    ("max_daily_drawdown",  "max_daily_loss_pct",   Decimal("0.05")),
    ("max_gross_exposure",  "max_gross_exposure",   Decimal("1.00")),
    ("min_cash_reserve",    "min_cash_reserve_pct", Decimal("0.10")),
    ("kill_switch",         "kill_switch",          Decimal("0")),
]


# ---------------------------------------------------------------------------
# Checks + seeds
# ---------------------------------------------------------------------------


def ensure_schema() -> None:
    """Confirm the schema has been loaded."""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS n FROM information_schema.tables
               WHERE table_name = 'cycles'"""
        )
        row = cur.fetchone()
        if not row or int(row["n"]) < 1:
            raise SystemExit(
                "Schema is not initialized. Run:\n"
                "    psql $DATABASE_URL -f schema.sql"
            )


def ensure_assets() -> list[str]:
    """Insert missing default assets. Returns list of symbols inserted."""
    inserted: list[str] = []
    with db.get_conn() as conn, conn.cursor() as cur:
        for symbol, name in DEFAULT_ASSETS:
            cur.execute(
                """INSERT INTO assets (symbol, name)
                   VALUES (%s, %s) ON CONFLICT (symbol) DO NOTHING
                   RETURNING symbol""",
                (symbol, name),
            )
            row = cur.fetchone()
            if row:
                inserted.append(row["symbol"])
    return inserted


def ensure_instruments() -> list[str]:
    """Insert USDT pairs for each default asset on Binance."""
    inserted: list[str] = []
    with db.get_conn() as conn, conn.cursor() as cur:
        for symbol, _ in DEFAULT_ASSETS:
            venue_symbol = f"{symbol}USDT"
            cur.execute(
                """INSERT INTO instruments
                   (asset_id, symbol, venue, qty_precision, price_precision)
                   SELECT asset_id, %s, 'binance', 6, 2
                   FROM assets WHERE symbol = %s
                   ON CONFLICT (venue, symbol) DO NOTHING
                   RETURNING symbol""",
                (venue_symbol, symbol),
            )
            row = cur.fetchone()
            if row:
                inserted.append(row["symbol"])
    return inserted


def ensure_risk_limits() -> list[str]:
    inserted: list[str] = []
    with db.get_conn() as conn, conn.cursor() as cur:
        for name, rule_type, value in DEFAULT_RISK_LIMITS:
            cur.execute(
                """INSERT INTO risk_limits (name, rule_type, value, scope)
                   VALUES (%s, %s, %s, 'global')
                   ON CONFLICT (name) DO NOTHING RETURNING name""",
                (name, rule_type, value),
            )
            row = cur.fetchone()
            if row:
                inserted.append(row["name"])
    return inserted


def ensure_desk_config() -> bool:
    """Create baseline-v1 if no config exists. Returns True if created."""
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM desk_configs")
        row = cur.fetchone()
        if row and int(row["n"]) > 0:
            return False
    rc = cmd_new_config(
        name="baseline-v1",
        notes="Initial baseline config seeded by desk.setup.",
        activate=True,
    )
    if rc != 0:
        raise SystemExit(f"Failed to create baseline-v1 config (rc={rc})")
    return True


def ensure_initial_nav(force: bool) -> bool:
    """Write an initial nav_snapshots row if none exist (or if --force-nav).

    Returns True if a row was written.
    """
    existing = db.get_latest_nav()
    if existing and not force:
        return False
    starting = Decimal(os.environ.get("PAPER_STARTING_NAV_USDT", "10000"))
    db.insert_nav_snapshot(
        cash=starting,
        positions_value=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        mode=os.environ.get("PARLEY_MODE", "paper"),
    )
    return True


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary() -> None:
    instruments = db.get_instruments()
    limits = db.get_risk_limits(active_only=True)
    nav = db.get_latest_nav()

    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, is_active, version FROM desk_configs ORDER BY created_at DESC"
        )
        configs = list(cur.fetchall())

    print()
    print("━" * 60)
    print("  Parley — setup complete")
    print("━" * 60)
    print(f"  Mode:           {os.environ.get('PARLEY_MODE', 'paper')}")
    print(f"  Universe:       {', '.join(i['symbol'] for i in instruments)}")
    print()
    print("  Active risk limits:")
    for L in limits:
        print(f"    {L['name']:24s} {L['rule_type']:24s} {L['value']}")
    print()
    print("  Desk configs:")
    for c in configs:
        marker = "★" if c["is_active"] else " "
        print(f"    {marker} {c['name']} (v{c['version']})")
    print()
    if nav:
        print(f"  NAV:            {nav['equity']} USDT")
        print(f"    cash          {nav['cash']}")
        print(f"    positions     {nav['positions_value']}")
    print("━" * 60)
    print()
    print("Next steps:")
    print("  1. Start Claude Code in this directory:   claude")
    print("  2. In the Claude Code session:            /run-cycle --dry")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk.setup")
    parser.add_argument(
        "--force-nav",
        action="store_true",
        help="Overwrite NAV snapshot even if one exists (dangerous).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not db.ping():
        raise SystemExit("DATABASE_URL is not reachable. Check your .env.")

    ensure_schema()
    assets_new = ensure_assets()
    instruments_new = ensure_instruments()
    limits_new = ensure_risk_limits()
    config_new = ensure_desk_config()
    nav_new = ensure_initial_nav(force=args.force_nav)

    if assets_new:
        logger.info("Inserted assets: %s", assets_new)
    if instruments_new:
        logger.info("Inserted instruments: %s", instruments_new)
    if limits_new:
        logger.info("Inserted risk limits: %s", limits_new)
    if config_new:
        logger.info("Created baseline-v1 desk_config (active)")
    if nav_new:
        logger.info("Wrote initial nav_snapshots row")

    print_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
