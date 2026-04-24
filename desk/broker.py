"""Broker abstraction.

Defines a minimal ``Broker`` protocol that ``desk.execution`` uses to submit
orders and retrieve fills, and provides two implementations:

- ``BinanceBroker``: thin wrapper over ``BinanceClient`` that submits real
  (testnet or live) orders. In Phase 1 only the testnet path is reachable.
- ``SimulatedBroker``: in-memory fills using the most recent mid price.
  Used for backtests and for unit tests that should never hit the network.

Adding a new venue (Coinbase, Kraken, Bitso) means implementing this
protocol. See CONTRIBUTING.md → "Extension: adding an exchange."
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

import ccxt  # type: ignore[import-untyped]

from desk.db import utcnow
from desk.market_data import BinanceClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """What we send to the broker. Quantities already rounded to precision."""

    symbol: str                       # venue symbol e.g. "BTCUSDT"
    side: str                         # "buy" | "sell"
    order_type: str                   # "market" | "limit"
    qty: Decimal
    limit_price: Decimal | None = None
    client_order_id: str = field(default_factory=lambda: f"parley-{uuid.uuid4().hex[:16]}")


@dataclass(frozen=True, slots=True)
class OrderFill:
    """Single fill event. One OrderRequest may produce multiple fills."""

    venue_fill_id: str
    ts: datetime
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Broker's response to a submit. Status summarizes the outcome."""

    venue_order_id: str
    status: str                       # "filled" | "partial" | "pending" | "rejected" | "cancelled"
    fills: list[OrderFill]
    raw: dict[str, object]            # venue-native response for debugging


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Broker(Protocol):
    """Contract every venue adapter must satisfy."""

    mode: str                         # "paper" | "live"
    venue: str                        # "binance", "coinbase", ...

    def submit(self, req: OrderRequest) -> SubmissionResult: ...
    def cancel(self, venue_order_id: str, symbol: str) -> None: ...
    def get_balance(self) -> dict[str, Decimal]: ...


# ---------------------------------------------------------------------------
# BinanceBroker — wraps BinanceClient for order submission
# ---------------------------------------------------------------------------


class BinanceBroker:
    """Binance spot broker. Uses testnet in paper mode, live in live mode.

    Phase 1: only paper mode is reachable. The ``.live()`` constructor
    on ``BinanceClient`` itself refuses unless ``PARLEY_MODE=live``.
    """

    venue = "binance"

    def __init__(self, client: BinanceClient) -> None:
        self._client = client
        self.mode = client.mode

    @classmethod
    def paper(cls) -> BinanceBroker:
        return cls(BinanceClient.paper())

    @classmethod
    def live(cls) -> BinanceBroker:
        return cls(BinanceClient.live())

    # ------------------------------------------------------------------

    def submit(self, req: OrderRequest) -> SubmissionResult:
        """Submit one child order. Returns status and any immediate fills.

        For market orders on testnet, fills are typically immediate and
        returned in the same response. For limits, status may be
        ``pending`` until a separate poll picks up the fill.
        """
        ccxt_symbol = BinanceClient._normalize_symbol(req.symbol)
        params = {"newClientOrderId": req.client_order_id}
        try:
            if req.order_type == "market":
                raw = self._client._ex.create_order(
                    symbol=ccxt_symbol,
                    type="market",
                    side=req.side,
                    amount=float(req.qty),
                    params=params,
                )
            elif req.order_type == "limit":
                if req.limit_price is None:
                    raise ValueError("limit order requires limit_price")
                raw = self._client._ex.create_order(
                    symbol=ccxt_symbol,
                    type="limit",
                    side=req.side,
                    amount=float(req.qty),
                    price=float(req.limit_price),
                    params=params,
                )
            else:
                raise ValueError(f"Unsupported order_type: {req.order_type}")
        except ccxt.InsufficientFunds as exc:
            logger.warning("Insufficient funds for %s: %s", req, exc)
            return SubmissionResult(
                venue_order_id="",
                status="rejected",
                fills=[],
                raw={"error": "insufficient_funds", "message": str(exc)},
            )
        except ccxt.InvalidOrder as exc:
            logger.warning("Invalid order %s: %s", req, exc)
            return SubmissionResult(
                venue_order_id="",
                status="rejected",
                fills=[],
                raw={"error": "invalid_order", "message": str(exc)},
            )

        return _parse_ccxt_submission(raw)

    def cancel(self, venue_order_id: str, symbol: str) -> None:
        ccxt_symbol = BinanceClient._normalize_symbol(symbol)
        self._client._ex.cancel_order(venue_order_id, ccxt_symbol)

    def get_balance(self) -> dict[str, Decimal]:
        return self._client.fetch_balance()


def _parse_ccxt_submission(raw: dict[str, object]) -> SubmissionResult:
    """Normalize a CCXT order response into our internal SubmissionResult."""
    venue_id = str(raw.get("id", ""))
    status_raw = raw.get("status", "pending")

    status_map = {
        "closed": "filled",
        "open": "pending",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "rejected": "rejected",
    }
    status = status_map.get(str(status_raw), "pending")

    fills = []
    raw_trades = raw.get("trades") or []
    if isinstance(raw_trades, list):
        for t in raw_trades:
            if not isinstance(t, dict):
                continue
            ts_ms = t.get("timestamp")
            ts = (
                datetime.fromtimestamp(int(ts_ms) / 1000, tz=utcnow().tzinfo)
                if ts_ms
                else utcnow()
            )
            fills.append(
                OrderFill(
                    venue_fill_id=str(t.get("id", "")),
                    ts=ts,
                    qty=Decimal(str(t.get("amount", "0"))),
                    price=Decimal(str(t.get("price", "0"))),
                    fee=Decimal(str((t.get("fee") or {}).get("cost", "0"))),
                    fee_currency=str((t.get("fee") or {}).get("currency", "USDT")),
                )
            )

    # Market orders on testnet sometimes have `filled`/`average` but no `trades`
    if not fills and status == "filled":
        filled_qty = Decimal(str(raw.get("filled") or raw.get("amount") or "0"))
        avg_price = Decimal(str(raw.get("average") or raw.get("price") or "0"))
        if filled_qty > 0 and avg_price > 0:
            fills.append(
                OrderFill(
                    venue_fill_id=venue_id,
                    ts=utcnow(),
                    qty=filled_qty,
                    price=avg_price,
                    fee=Decimal(str((raw.get("fee") or {}).get("cost", "0")))
                    if isinstance(raw.get("fee"), dict)
                    else Decimal("0"),
                    fee_currency=(
                        str((raw.get("fee") or {}).get("currency", "USDT"))
                        if isinstance(raw.get("fee"), dict)
                        else "USDT"
                    ),
                )
            )

    return SubmissionResult(
        venue_order_id=venue_id,
        status=status,
        fills=fills,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# SimulatedBroker — for tests and backtests
# ---------------------------------------------------------------------------


class SimulatedBroker:
    """In-memory broker that fills immediately at a provided mid price.

    Not a realistic simulator — assumes infinite liquidity and zero latency.
    Use for unit tests and offline backtests, not for latency-sensitive
    research.
    """

    mode = "paper"
    venue = "sim"

    def __init__(self, mid_provider: "dict[str, Decimal]", fee_bps: Decimal = Decimal("10")) -> None:
        """``mid_provider`` maps symbol -> current mid price. Callers mutate it
        between submissions to simulate price moves."""
        self._mid = mid_provider
        self._fee_bps = fee_bps
        self._cancelled: set[str] = set()

    def submit(self, req: OrderRequest) -> SubmissionResult:
        mid = self._mid.get(req.symbol)
        if mid is None:
            return SubmissionResult(
                venue_order_id="",
                status="rejected",
                fills=[],
                raw={"error": "no_mid_price"},
            )

        # Naive fill model: market = mid, limit = min(limit, mid) for buys, max for sells
        if req.order_type == "market":
            fill_price = mid
        else:
            if req.limit_price is None:
                return SubmissionResult(
                    venue_order_id="",
                    status="rejected",
                    fills=[],
                    raw={"error": "no_limit_price"},
                )
            if req.side == "buy" and req.limit_price < mid:
                # Not crossed
                return SubmissionResult(
                    venue_order_id=req.client_order_id,
                    status="pending",
                    fills=[],
                    raw={"note": "limit below mid"},
                )
            if req.side == "sell" and req.limit_price > mid:
                return SubmissionResult(
                    venue_order_id=req.client_order_id,
                    status="pending",
                    fills=[],
                    raw={"note": "limit above mid"},
                )
            fill_price = req.limit_price

        fee = req.qty * fill_price * self._fee_bps / Decimal("10000")
        fill = OrderFill(
            venue_fill_id=f"sim-{uuid.uuid4().hex[:12]}",
            ts=utcnow(),
            qty=req.qty,
            price=fill_price,
            fee=fee,
            fee_currency="USDT",
        )
        return SubmissionResult(
            venue_order_id=req.client_order_id,
            status="filled",
            fills=[fill],
            raw={"simulated": True, "mid": str(mid)},
        )

    def cancel(self, venue_order_id: str, symbol: str) -> None:
        self._cancelled.add(venue_order_id)

    def get_balance(self) -> dict[str, Decimal]:
        return {}


# ---------------------------------------------------------------------------
# Factory — choose broker by mode + venue
# ---------------------------------------------------------------------------


def get_broker(venue: str = "binance", mode: str | None = None) -> Broker:
    """Return the correct broker for the configured venue and mode.

    ``mode`` defaults to the ``PARLEY_MODE`` env var, which the hooks set.
    """
    mode = mode or os.environ.get("PARLEY_MODE", "paper")
    if venue == "binance":
        if mode == "paper":
            return BinanceBroker.paper()
        if mode == "live":
            return BinanceBroker.live()
        raise ValueError(f"Unknown mode: {mode}")
    if venue == "sim":
        raise ValueError("SimulatedBroker must be constructed directly with a mid_provider")
    raise ValueError(f"Unknown venue: {venue}")


__all__ = [
    "BinanceBroker",
    "Broker",
    "OrderFill",
    "OrderRequest",
    "SimulatedBroker",
    "SubmissionResult",
    "get_broker",
]
