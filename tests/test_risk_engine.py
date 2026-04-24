"""Tests for risk_engine — the deterministic hard-rule enforcement layer."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from desk.risk_engine import check_portfolio_level, check_proposal

pytestmark = pytest.mark.unit


def _limit(name: str, rule_type: str, value: str, limit_id: int = 1) -> dict:
    return {
        "limit_id": limit_id,
        "name": name,
        "rule_type": rule_type,
        "value": Decimal(value),
        "scope": "global",
        "scope_ref": None,
        "is_active": True,
    }


def _proposal(symbol: str, weight: str) -> dict:
    return {
        "proposal_id": uuid4(),
        "instrument_id": 1,
        "target_weight": Decimal(weight),
        "current_weight": Decimal("0"),
        "action": "open",
        "symbol": symbol,
    }


class TestMaxPositionPct:
    def test_passes_under_limit(self):
        limits = {"max_position_pct": [_limit("max_pos", "max_position_pct", "0.20")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        vs = check_proposal(_proposal("BTCUSDT", "0.15"), limits, portfolio)
        assert [v for v in vs if v.rule_type == "max_position_pct"] == []

    def test_blocks_over_limit(self):
        limits = {"max_position_pct": [_limit("max_pos", "max_position_pct", "0.20")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        vs = check_proposal(_proposal("BTCUSDT", "0.25"), limits, portfolio)
        v = next(v for v in vs if v.rule_type == "max_position_pct")
        assert v.severity == "block"
        assert v.details["exceeded_by"] == "0.05"

    def test_checks_absolute_value_for_shorts(self):
        limits = {"max_position_pct": [_limit("max_pos", "max_position_pct", "0.20")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        vs = check_proposal(_proposal("BTCUSDT", "-0.25"), limits, portfolio)
        assert any(v.rule_type == "max_position_pct" and v.severity == "block" for v in vs)


class TestKillSwitch:
    def test_inactive_kill_switch_passes(self):
        limits = {"kill_switch": [_limit("kill", "kill_switch", "0")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        proposals = [_proposal("BTCUSDT", "0.10")]
        vs = check_portfolio_level(proposals, limits, portfolio)
        assert vs == []

    def test_active_kill_switch_blocks_everything(self):
        limits = {"kill_switch": [_limit("kill", "kill_switch", "1")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        proposals = [_proposal("BTCUSDT", "0.10"), _proposal("ETHUSDT", "0.05")]
        vs = check_portfolio_level(proposals, limits, portfolio)
        assert len(vs) == 2
        assert all(v.severity == "block" and v.rule_type == "kill_switch" for v in vs)


class TestMaxDailyLoss:
    def test_under_loss_passes(self):
        limits = {"max_daily_loss_pct": [_limit("max_dd", "max_daily_loss_pct", "0.05")]}
        portfolio = {
            "nav": Decimal("9800"),
            "cash": Decimal("9800"),
            "today_pnl_pct": Decimal("-0.02"),
        }
        proposals = [_proposal("BTCUSDT", "0.10")]
        vs = check_portfolio_level(proposals, limits, portfolio)
        assert [v for v in vs if v.rule_type == "max_daily_loss_pct"] == []

    def test_over_loss_blocks_everything(self):
        limits = {"max_daily_loss_pct": [_limit("max_dd", "max_daily_loss_pct", "0.05")]}
        portfolio = {
            "nav": Decimal("9400"),
            "cash": Decimal("9400"),
            "today_pnl_pct": Decimal("-0.06"),
        }
        proposals = [_proposal("BTCUSDT", "0.10"), _proposal("ETHUSDT", "0.05")]
        vs = check_portfolio_level(proposals, limits, portfolio)
        critical = [v for v in vs if v.rule_type == "max_daily_loss_pct"]
        assert len(critical) == 2
        assert all(v.severity == "block" for v in critical)


class TestMaxGrossExposure:
    def test_under_limit_passes(self):
        limits = {"max_gross_exposure": [_limit("gross", "max_gross_exposure", "1.00")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        proposals = [
            _proposal("BTCUSDT", "0.30"),
            _proposal("ETHUSDT", "0.30"),
            _proposal("SOLUSDT", "0.30"),
        ]
        vs = check_portfolio_level(proposals, limits, portfolio)
        assert [v for v in vs if v.rule_type == "max_gross_exposure"] == []

    def test_over_limit_drops_smallest_weights_first(self):
        """When gross exceeds the cap, risk_engine blocks the smallest-weight
        proposals first — preserving the highest-conviction trades."""
        limits = {"max_gross_exposure": [_limit("gross", "max_gross_exposure", "0.50")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        proposals = [
            _proposal("BTCUSDT", "0.30"),  # biggest
            _proposal("ETHUSDT", "0.15"),  # middle
            _proposal("SOLUSDT", "0.10"),  # smallest
        ]
        # Gross = 0.55, limit = 0.50 → must drop 0.05. Only the smallest
        # (SOL at 0.10) is blocked.
        vs = check_portfolio_level(proposals, limits, portfolio)
        blocked = [v for v in vs if v.rule_type == "max_gross_exposure"]
        assert len(blocked) == 1
        assert blocked[0].details["symbol"] == "SOLUSDT"


class TestMinCashReserve:
    def test_warn_when_single_trade_breaches(self):
        limits = {"min_cash_reserve_pct": [_limit("cash", "min_cash_reserve_pct", "0.10")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        # Weight 0.95 would leave cash at 500 = 0.05, below 0.10 reserve
        vs = check_proposal(_proposal("BTCUSDT", "0.95"), limits, portfolio)
        cash_vs = [v for v in vs if v.rule_type == "min_cash_reserve_pct"]
        assert len(cash_vs) == 1
        assert cash_vs[0].severity == "warn"

    def test_passes_when_reserve_intact(self):
        limits = {"min_cash_reserve_pct": [_limit("cash", "min_cash_reserve_pct", "0.10")]}
        portfolio = {"nav": Decimal("10000"), "cash": Decimal("10000")}
        vs = check_proposal(_proposal("BTCUSDT", "0.15"), limits, portfolio)
        cash_vs = [v for v in vs if v.rule_type == "min_cash_reserve_pct"]
        assert cash_vs == []
