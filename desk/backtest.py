"""Historical backtesting.

Phase 1 implements ``signals`` mode only — walks stored 1m bars forward,
resamples to the signal timeframe, runs strategies, and simulates trades
with simple rule-based PM + Risk stand-ins. No LLM calls, no exchange
hits. Fast.

``agents`` mode (full five-agent replay) is a Phase 3 concern — it needs
point-in-time Postgres views to avoid look-ahead bias, and is expensive
to run at scale.

CLI:
    python -m desk.backtest init --symbol BTCUSDT --from 2024-01-01 \\
                                  --to 2024-06-30 --mode signals
    python -m desk.backtest run-signals --run-id <uuid>
    python -m desk.backtest report --run-id <uuid>

Results are written to ``backtests/runs/<run_id>/`` and NOT to the main
audit-log tables — backtests are separate from live cycles.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from desk import db
from desk.broker import OrderRequest, SimulatedBroker
from desk.indicators import STRATEGIES, Direction, IndicatorResult, bars_to_df

logger = logging.getLogger(__name__)


BACKTESTS_DIR = Path("backtests/runs")


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------


@dataclass
class RunMetadata:
    run_id: str
    symbol: str
    from_date: str
    to_date: str
    mode: str                       # "signals" | "agents"
    starting_nav: str
    strategies: list[str]
    signal_timeframe: str = "1h"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def path(self) -> Path:
        return BACKTESTS_DIR / self.run_id

    def save(self) -> None:
        self.path().mkdir(parents=True, exist_ok=True)
        (self.path() / "metadata.json").write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, run_id: str) -> "RunMetadata":
        p = BACKTESTS_DIR / run_id / "metadata.json"
        if not p.exists():
            raise FileNotFoundError(f"Run metadata not found: {p}")
        return cls(**json.loads(p.read_text()))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(
    symbol: str,
    from_date: str,
    to_date: str,
    mode: str,
    config_name: str | None,
) -> int:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    if from_dt >= to_dt:
        print("init: from must be before to", file=sys.stderr)
        return 1

    inst = db.get_instrument_by_symbol(symbol)
    if inst is None:
        print(f"init: unknown symbol {symbol}", file=sys.stderr)
        return 1

    # Bar coverage check — need at least some 1m bars in the window
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS n,
                      MIN(ts) AS first, MAX(ts) AS last
               FROM market_bars
               WHERE instrument_id = %s AND timeframe = '1m'
                 AND ts >= %s AND ts <= %s""",
            (inst["instrument_id"], from_dt, to_dt),
        )
        row = cur.fetchone()
    if not row or int(row["n"]) < 200:
        print(
            f"init: only {row['n'] if row else 0} 1m bars in window; need ≥200. "
            f"Ingest more history first.",
            file=sys.stderr,
        )
        return 1

    meta = RunMetadata(
        run_id=str(uuid.uuid4()),
        symbol=symbol,
        from_date=from_date,
        to_date=to_date,
        mode=mode,
        starting_nav="10000",
        strategies=sorted(STRATEGIES),
    )
    meta.save()
    print(json.dumps({
        "run_id": meta.run_id,
        "bars_in_window": int(row["n"]),
        "first_bar": row["first"].isoformat() if row["first"] else None,
        "last_bar": row["last"].isoformat() if row["last"] else None,
        "path": str(meta.path()),
    }))
    return 0


# ---------------------------------------------------------------------------
# run-signals — the simulator
# ---------------------------------------------------------------------------


def _combine_signals_to_target(results: dict[str, IndicatorResult]) -> Decimal:
    """Deterministic PM stand-in for signals mode.

    Rules:
        - Count direction votes weighted by strength.
        - If net vote > 0.3, go long with weight = min(0.20, net_vote).
        - If net vote < -0.3, go short with weight = max(-0.20, net_vote).
        - Otherwise flat.

    This is a simple, reproducible combiner. It's not trying to be smart;
    it's trying to be a stable baseline that agent mode can be compared
    against.
    """
    net = 0.0
    for r in results.values():
        if r.direction == Direction.LONG:
            net += r.strength
        elif r.direction == Direction.SHORT:
            net -= r.strength
    # Normalize by number of strategies to stay in [-1, 1]
    if results:
        net /= len(results)
    if net > 0.3:
        return min(Decimal("0.20"), Decimal(str(net)))
    if net < -0.3:
        return max(Decimal("-0.20"), Decimal(str(net)))
    return Decimal("0")


def _load_bars_between(instrument_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT ts, open, high, low, close, volume FROM market_bars
               WHERE instrument_id = %s AND timeframe = '1m'
                 AND ts >= %s AND ts <= %s
               ORDER BY ts ASC""",
            (instrument_id, start, end),
        )
        return list(cur.fetchall())


def cmd_run_signals(run_id: str) -> int:
    meta = RunMetadata.load(run_id)
    inst = db.get_instrument_by_symbol(meta.symbol)
    if inst is None:
        print("run-signals: symbol vanished from DB?", file=sys.stderr)
        return 1

    from_dt = datetime.fromisoformat(meta.from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(meta.to_date).replace(tzinfo=timezone.utc)

    raw_bars = _load_bars_between(inst["instrument_id"], from_dt, to_dt)
    if len(raw_bars) < 200:
        print(f"run-signals: insufficient bars ({len(raw_bars)})", file=sys.stderr)
        return 1

    # Build a minute-indexed DataFrame, resample to 1h for signals
    import pandas as pd
    df_1m = pd.DataFrame(raw_bars)
    df_1m["ts"] = pd.to_datetime(df_1m["ts"], utc=True)
    df_1m = df_1m.set_index("ts").astype({"open": float, "high": float,
                                           "low": float, "close": float,
                                           "volume": float})
    df_1h = df_1m.resample("1h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()

    # Pre-compute a mid-price series (use close as mid for sim purposes)
    mids: dict[str, Decimal] = {meta.symbol: Decimal("0")}
    broker = SimulatedBroker(mids, fee_bps=Decimal("10"))

    nav = Decimal(meta.starting_nav)
    cash = nav
    position_qty = Decimal("0")
    position_avg = Decimal("0")
    realized_pnl = Decimal("0")

    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []

    # Walk each 1h bar; require at least 200 bars of history
    WARMUP = 200
    strategy_names = list(STRATEGIES)

    for i in range(WARMUP, len(df_1h)):
        window = df_1h.iloc[: i + 1]
        now_bar = window.iloc[-1]
        now_ts: datetime = window.index[-1].to_pydatetime()
        close = Decimal(str(now_bar["close"]))
        mids[meta.symbol] = close

        # Run all strategies
        results = {
            name: STRATEGIES[name](window)
            for name in strategy_names
        }

        target_weight = _combine_signals_to_target(results)
        current_weight = (position_qty * close) / (cash + position_qty * close) \
            if (cash + position_qty * close) > 0 else Decimal("0")

        delta_weight = target_weight - current_weight
        delta_notional = delta_weight * (cash + position_qty * close)

        # Only trade if delta is meaningful
        if abs(delta_notional) < Decimal("50"):  # ignore sub-$50 moves
            pass
        else:
            side = "buy" if delta_notional > 0 else "sell"
            raw_qty = abs(delta_notional) / close
            qty_prec = int(inst["qty_precision"])
            q = Decimal(10) ** -qty_prec
            from decimal import ROUND_DOWN
            qty = raw_qty.quantize(q, rounding=ROUND_DOWN)

            if qty > Decimal(str(inst["min_qty"])):
                req = OrderRequest(symbol=meta.symbol, side=side,
                                   order_type="market", qty=qty)
                result = broker.submit(req)
                for f in result.fills:
                    signed_qty = f.qty if side == "buy" else -f.qty

                    if position_qty == 0 or (position_qty > 0) == (signed_qty > 0):
                        # Opening or adding
                        new_qty = position_qty + signed_qty
                        if new_qty != 0:
                            position_avg = (
                                position_qty * position_avg + signed_qty * f.price
                            ) / new_qty
                        position_qty = new_qty
                    else:
                        # Reducing or flipping
                        closing = min(abs(signed_qty), abs(position_qty))
                        sign = Decimal("1") if position_qty > 0 else Decimal("-1")
                        realized_pnl += closing * (f.price - position_avg) * sign
                        new_qty = position_qty + signed_qty
                        if abs(signed_qty) > abs(position_qty):
                            position_avg = f.price
                        position_qty = new_qty

                    cash -= signed_qty * f.price
                    cash -= f.fee
                    trades.append({
                        "ts": f.ts.isoformat(),
                        "side": side,
                        "qty": str(f.qty),
                        "price": str(f.price),
                        "fee": str(f.fee),
                        "post_cash": str(cash),
                        "post_qty": str(position_qty),
                        "target_weight": str(target_weight),
                    })

        # Record equity at end of bar
        mark_value = position_qty * close
        equity = cash + mark_value
        nav = equity
        equity_curve.append({
            "ts": now_ts.isoformat(),
            "close": str(close),
            "cash": str(cash),
            "mark_value": str(mark_value),
            "equity": str(equity),
            "position_qty": str(position_qty),
            "realized_pnl": str(realized_pnl),
        })

    # Persist
    run_dir = meta.path()
    run_dir.mkdir(parents=True, exist_ok=True)

    with (run_dir / "equity_curve.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=equity_curve[0].keys())
        w.writeheader()
        w.writerows(equity_curve)

    with (run_dir / "trades.csv").open("w", newline="") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=trades[0].keys())
            w.writeheader()
            w.writerows(trades)
        else:
            f.write("ts,side,qty,price,fee,post_cash,post_qty,target_weight\n")

    # Compute metrics
    metrics = _compute_metrics(equity_curve, trades, starting_nav=Decimal(meta.starting_nav))
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    print(json.dumps({
        "run_id": run_id,
        "bars_simulated": len(equity_curve),
        "trades": len(trades),
        "final_equity": str(nav),
        "metrics": metrics,
    }))
    return 0


def _compute_metrics(
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    starting_nav: Decimal,
) -> dict[str, Any]:
    if not equity_curve:
        return {}
    equities = np.array([float(p["equity"]) for p in equity_curve])
    returns = np.diff(equities) / equities[:-1]
    final = equities[-1]
    peak = np.maximum.accumulate(equities)
    drawdown = (equities - peak) / peak
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    sharpe = 0.0
    if len(returns) > 1 and returns.std() > 0:
        # Hourly returns; annualize sqrt(24 * 365)
        sharpe = float(returns.mean() / returns.std() * np.sqrt(24 * 365))

    win_rate = 0.0
    if trades:
        # Crude: count trades that moved equity up
        wins = sum(1 for i, t in enumerate(trades) if i < len(trades) - 1
                   and float(trades[i + 1].get("post_cash", 0)) > float(t.get("post_cash", 0)))
        win_rate = wins / len(trades) if trades else 0.0

    return {
        "final_equity": f"{final:.2f}",
        "total_return_pct": f"{(final / float(starting_nav) - 1) * 100:.2f}",
        "max_drawdown_pct": f"{max_dd * 100:.2f}",
        "sharpe_annualized": f"{sharpe:.2f}",
        "n_trades": len(trades),
        "n_bars": len(equity_curve),
        "win_rate_pct": f"{win_rate * 100:.1f}",
    }


# ---------------------------------------------------------------------------
# report — human-readable markdown summary
# ---------------------------------------------------------------------------


def cmd_report(run_id: str) -> int:
    meta = RunMetadata.load(run_id)
    run_dir = meta.path()
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        print("report: run-signals has not been run yet", file=sys.stderr)
        return 1
    metrics = json.loads(metrics_path.read_text())

    report = f"""# Backtest report — {run_id[:8]}

**Symbol:** {meta.symbol}
**Period:** {meta.from_date} → {meta.to_date}
**Mode:** {meta.mode}
**Strategies:** {', '.join(meta.strategies)}
**Starting NAV:** {meta.starting_nav} USDT

## Results

| Metric                 | Value                          |
|------------------------|--------------------------------|
| Final equity           | {metrics['final_equity']} USDT |
| Total return           | {metrics['total_return_pct']}% |
| Max drawdown           | {metrics['max_drawdown_pct']}% |
| Sharpe (annualized)    | {metrics['sharpe_annualized']} |
| Trades                 | {metrics['n_trades']}          |
| Bars simulated         | {metrics['n_bars']}            |
| Win rate               | {metrics['win_rate_pct']}%     |

## Caveats

- Backtest uses close-as-mid (no bid/ask spread simulation beyond bps fee).
- Infinite liquidity assumed — no slippage or partial fills.
- Strategies evaluated on closed bars only; no intrabar look-ahead.
- Signals-mode uses a deterministic PM stand-in, NOT the LLM agents.
  Agent-mode results may differ substantially.

See ``equity_curve.csv`` and ``trades.csv`` in the run directory for
raw data. These metrics are a starting point, not a trading recommendation.
"""
    (run_dir / "report.md").write_text(report)
    print(f"report written to {run_dir / 'report.md'}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desk.backtest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init")
    i.add_argument("--symbol", required=True)
    i.add_argument("--from", dest="from_date", required=True)
    i.add_argument("--to", dest="to_date", required=True)
    i.add_argument("--mode", default="signals", choices=("signals", "agents"))
    i.add_argument("--config", default=None)

    r = sub.add_parser("run-signals")
    r.add_argument("--run-id", required=True)

    rp = sub.add_parser("report")
    rp.add_argument("--run-id", required=True)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "init":
        return cmd_init(
            args.symbol, args.from_date, args.to_date, args.mode, args.config
        )
    if args.cmd == "run-signals":
        return cmd_run_signals(args.run_id)
    if args.cmd == "report":
        return cmd_report(args.run_id)
    return 1


if __name__ == "__main__":
    sys.exit(main())
