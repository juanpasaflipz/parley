"""Tests for _validate_plan — ensures subagent output can't cause corruption."""

from __future__ import annotations

import pytest

from desk.execution import _validate_plan

pytestmark = pytest.mark.unit


class TestValidatePlan:
    def test_valid_execute(self):
        plan = {
            "action": "execute",
            "orders": [{
                "side": "buy", "order_type": "market",
                "qty": "0.01", "limit_price": None, "schedule": None,
            }],
        }
        ok, err = _validate_plan(plan)
        assert ok, err

    def test_valid_defer(self):
        ok, err = _validate_plan({"action": "defer", "reason": "stale mid"})
        assert ok, err

    def test_valid_skip(self):
        ok, err = _validate_plan({"action": "skip", "reason": "below_min_qty"})
        assert ok, err

    def test_bad_action(self):
        ok, err = _validate_plan({"action": "yolo"})
        assert not ok

    def test_execute_requires_orders(self):
        ok, err = _validate_plan({"action": "execute", "orders": []})
        assert not ok
        assert "orders" in err

    def test_bad_side(self):
        plan = {
            "action": "execute",
            "orders": [{"side": "long", "order_type": "market", "qty": "0.01"}],
        }
        ok, err = _validate_plan(plan)
        assert not ok
        assert "side" in err

    def test_bad_order_type(self):
        plan = {
            "action": "execute",
            "orders": [{"side": "buy", "order_type": "midnight", "qty": "0.01"}],
        }
        ok, err = _validate_plan(plan)
        assert not ok
        assert "order_type" in err

    def test_non_decimal_qty(self):
        plan = {
            "action": "execute",
            "orders": [{"side": "buy", "order_type": "market", "qty": "lots"}],
        }
        ok, err = _validate_plan(plan)
        assert not ok
        assert "decimal" in err.lower()

    def test_not_a_dict(self):
        ok, err = _validate_plan("execute")  # type: ignore[arg-type]
        assert not ok
