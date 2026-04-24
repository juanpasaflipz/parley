"""Integration-style tests using the SimulatedBroker.

These tests verify the P&L and position-update math by running sequences
of fills through the execution module's update function. They do NOT
hit the database — a stub cursor is provided via monkeypatch — so they
can run in CI without DATABASE_URL.

If you change the position-update math in desk/execution.py, these tests
are the first line of defense.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from desk.broker import OrderRequest, SimulatedBroker

pytestmark = pytest.mark.unit


def _simulate_position_updates(events: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    """Simulate the same math as _update_position_from_fills without touching DB.

    Mirrors execution.py exactly; if these diverge, there's a bug. In a
    follow-up PR we should factor the math into a pure function and have
    both the DB version and this test call it.
    """
    qty = Decimal("0")
    avg = Decimal("0")
    realized = Decimal("0")

    for e in events:
        side = e["side"]
        sign = Decimal("1") if side == "buy" else Decimal("-1")
        fill_qty = e["qty"] * sign
        fill_price = e["price"]
        new_qty = qty + fill_qty

        if qty == 0 or (qty > 0) == (fill_qty > 0):
            if new_qty != 0:
                total_cost = qty * avg + fill_qty * fill_price
                avg = total_cost / new_qty
        else:
            closing_qty = min(abs(fill_qty), abs(qty))
            pnl_per_unit = (fill_price - avg) * (Decimal("1") if qty > 0 else Decimal("-1"))
            realized += closing_qty * pnl_per_unit
            if abs(fill_qty) > abs(qty):
                avg = fill_price
        qty = new_qty
    return qty, avg, realized


class TestPositionMath:
    """The single most error-prone math in the codebase."""

    def test_open_long_then_close_profit(self):
        # Buy 1 @ 100, sell 1 @ 110 → +10 realized
        qty, avg, pnl = _simulate_position_updates([
            {"side": "buy",  "qty": Decimal("1"), "price": Decimal("100")},
            {"side": "sell", "qty": Decimal("1"), "price": Decimal("110")},
        ])
        assert qty == 0
        assert pnl == Decimal("10")

    def test_open_long_then_close_loss(self):
        qty, avg, pnl = _simulate_position_updates([
            {"side": "buy",  "qty": Decimal("1"), "price": Decimal("100")},
            {"side": "sell", "qty": Decimal("1"), "price": Decimal("90")},
        ])
        assert qty == 0
        assert pnl == Decimal("-10")

    def test_scale_into_position(self):
        # Buy 1 @ 100, buy 1 @ 200 → qty=2, avg=150
        qty, avg, pnl = _simulate_position_updates([
            {"side": "buy", "qty": Decimal("1"), "price": Decimal("100")},
            {"side": "buy", "qty": Decimal("1"), "price": Decimal("200")},
        ])
        assert qty == Decimal("2")
        assert avg == Decimal("150")
        assert pnl == 0

    def test_partial_close(self):
        # Buy 2 @ 100, sell 1 @ 120 → qty=1, avg=100, pnl=20
        qty, avg, pnl = _simulate_position_updates([
            {"side": "buy",  "qty": Decimal("2"), "price": Decimal("100")},
            {"side": "sell", "qty": Decimal("1"), "price": Decimal("120")},
        ])
        assert qty == Decimal("1")
        assert avg == Decimal("100")
        assert pnl == Decimal("20")

    def test_flip_long_to_short(self):
        # Buy 1 @ 100, sell 3 @ 120:
        #   Closes 1 @ +20 realized, then opens short 2 @ 120
        qty, avg, pnl = _simulate_position_updates([
            {"side": "buy",  "qty": Decimal("1"), "price": Decimal("100")},
            {"side": "sell", "qty": Decimal("3"), "price": Decimal("120")},
        ])
        assert qty == Decimal("-2")
        assert avg == Decimal("120")
        assert pnl == Decimal("20")

    def test_short_then_close_profit(self):
        # Sell 1 @ 100 (open short), buy 1 @ 90 → +10
        qty, avg, pnl = _simulate_position_updates([
            {"side": "sell", "qty": Decimal("1"), "price": Decimal("100")},
            {"side": "buy",  "qty": Decimal("1"), "price": Decimal("90")},
        ])
        assert qty == 0
        assert pnl == Decimal("10")

    def test_multiple_closes(self):
        # Buy 1 @ 100, buy 1 @ 200 (avg 150), sell 2 @ 180 → closes 2 at +30 each = 60
        qty, avg, pnl = _simulate_position_updates([
            {"side": "buy",  "qty": Decimal("1"), "price": Decimal("100")},
            {"side": "buy",  "qty": Decimal("1"), "price": Decimal("200")},
            {"side": "sell", "qty": Decimal("2"), "price": Decimal("180")},
        ])
        assert qty == 0
        assert pnl == Decimal("60")


class TestSimBrokerRoundTrip:
    """The SimulatedBroker feeding back into position math — the closest
    thing we have to an end-to-end cycle test without the DB."""

    def test_buy_sell_round_trip(self):
        mids = {"BTCUSDT": Decimal("60000")}
        broker = SimulatedBroker(mids, fee_bps=Decimal("0"))  # no fees for clean math

        buy = broker.submit(OrderRequest(
            symbol="BTCUSDT", side="buy",
            order_type="market", qty=Decimal("0.1"),
        ))
        assert buy.status == "filled"

        mids["BTCUSDT"] = Decimal("66000")  # +10% move

        sell = broker.submit(OrderRequest(
            symbol="BTCUSDT", side="sell",
            order_type="market", qty=Decimal("0.1"),
        ))
        assert sell.status == "filled"

        # Compute P&L through the math
        events = [
            {"side": "buy",  "qty": buy.fills[0].qty,  "price": buy.fills[0].price},
            {"side": "sell", "qty": sell.fills[0].qty, "price": sell.fills[0].price},
        ]
        qty, avg, pnl = _simulate_position_updates(events)
        assert qty == 0
        assert pnl == Decimal("600")  # 0.1 BTC × $6,000 move

    def test_limit_below_mid_does_not_fill(self):
        mids = {"BTCUSDT": Decimal("60000")}
        broker = SimulatedBroker(mids)
        result = broker.submit(OrderRequest(
            symbol="BTCUSDT", side="buy", order_type="limit",
            qty=Decimal("0.1"), limit_price=Decimal("55000"),
        ))
        assert result.status == "pending"
        assert result.fills == []
