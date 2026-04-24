"""Tests for the broker abstraction and SimulatedBroker."""

from __future__ import annotations

from decimal import Decimal

import pytest

from desk.broker import (
    OrderRequest,
    SimulatedBroker,
    SubmissionResult,
    _parse_ccxt_submission,
)

pytestmark = pytest.mark.unit


class TestSimulatedBroker:
    def test_market_fills_immediately_at_mid(self):
        mids = {"BTCUSDT": Decimal("60000")}
        broker = SimulatedBroker(mid_provider=mids, fee_bps=Decimal("10"))
        req = OrderRequest(
            symbol="BTCUSDT", side="buy", order_type="market",
            qty=Decimal("0.01"),
        )
        result = broker.submit(req)
        assert result.status == "filled"
        assert len(result.fills) == 1
        fill = result.fills[0]
        assert fill.qty == Decimal("0.01")
        assert fill.price == Decimal("60000")
        # Fee: 0.01 * 60000 * 10 / 10000 = 0.6
        assert fill.fee == Decimal("0.6")
        assert fill.fee_currency == "USDT"

    def test_limit_below_mid_for_buy_pends(self):
        mids = {"BTCUSDT": Decimal("60000")}
        broker = SimulatedBroker(mids)
        req = OrderRequest(
            symbol="BTCUSDT", side="buy", order_type="limit",
            qty=Decimal("0.01"), limit_price=Decimal("59500"),
        )
        result = broker.submit(req)
        assert result.status == "pending"
        assert result.fills == []

    def test_limit_at_or_above_mid_for_buy_fills(self):
        mids = {"BTCUSDT": Decimal("60000")}
        broker = SimulatedBroker(mids)
        req = OrderRequest(
            symbol="BTCUSDT", side="buy", order_type="limit",
            qty=Decimal("0.01"), limit_price=Decimal("60000"),
        )
        result = broker.submit(req)
        assert result.status == "filled"

    def test_limit_above_mid_for_sell_pends(self):
        mids = {"BTCUSDT": Decimal("60000")}
        broker = SimulatedBroker(mids)
        req = OrderRequest(
            symbol="BTCUSDT", side="sell", order_type="limit",
            qty=Decimal("0.01"), limit_price=Decimal("60500"),
        )
        result = broker.submit(req)
        assert result.status == "pending"

    def test_missing_mid_rejects(self):
        broker = SimulatedBroker({})
        req = OrderRequest(
            symbol="XYZUSDT", side="buy", order_type="market",
            qty=Decimal("0.01"),
        )
        result = broker.submit(req)
        assert result.status == "rejected"

    def test_limit_missing_price_rejects(self):
        broker = SimulatedBroker({"BTCUSDT": Decimal("60000")})
        req = OrderRequest(
            symbol="BTCUSDT", side="buy", order_type="limit",
            qty=Decimal("0.01"), limit_price=None,
        )
        result = broker.submit(req)
        assert result.status == "rejected"


class TestCCXTParsing:
    """Normalization of CCXT response shapes."""

    def test_closed_with_trades(self):
        raw = {
            "id": "abc123",
            "status": "closed",
            "trades": [
                {
                    "id": "t1",
                    "timestamp": 1714000000000,
                    "amount": "0.01",
                    "price": "60000",
                    "fee": {"cost": "0.6", "currency": "USDT"},
                }
            ],
        }
        r = _parse_ccxt_submission(raw)
        assert r.venue_order_id == "abc123"
        assert r.status == "filled"
        assert len(r.fills) == 1
        assert r.fills[0].qty == Decimal("0.01")

    def test_closed_without_trades_uses_average(self):
        raw = {
            "id": "abc123",
            "status": "closed",
            "filled": "0.005",
            "average": "61000.5",
            "fee": {"cost": "0.3", "currency": "USDT"},
        }
        r = _parse_ccxt_submission(raw)
        assert r.status == "filled"
        assert len(r.fills) == 1
        assert r.fills[0].qty == Decimal("0.005")
        assert r.fills[0].price == Decimal("61000.5")

    def test_open_maps_to_pending(self):
        raw = {"id": "xyz", "status": "open"}
        r = _parse_ccxt_submission(raw)
        assert r.status == "pending"
        assert r.fills == []

    def test_rejected_maps_through(self):
        raw = {"id": "xyz", "status": "rejected"}
        r = _parse_ccxt_submission(raw)
        assert r.status == "rejected"
