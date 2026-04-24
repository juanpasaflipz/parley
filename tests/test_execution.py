"""Tests for execution module — focused on the deterministic math that
must never round in the wrong direction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from desk.execution import _quantize_down, _quantize_price

pytestmark = pytest.mark.unit


class TestQuantizeDown:
    """This is the most safety-critical helper in the codebase.

    If it ever rounds UP, we can submit an order quantity larger than what
    Risk Manager approved. Every test here is a regression guard.
    """

    def test_rounds_down_never_up(self):
        # 0.12345678 with precision 6 → 0.123456 (not .123457)
        assert _quantize_down(Decimal("0.12345678"), 6) == Decimal("0.123456")

    def test_rounds_down_at_zero_boundary(self):
        # 0.0000009 with precision 6 → 0.000000 (not .000001)
        assert _quantize_down(Decimal("0.0000009"), 6) == Decimal("0")

    def test_exact_value_unchanged(self):
        assert _quantize_down(Decimal("0.123456"), 6) == Decimal("0.123456")

    def test_zero_precision(self):
        assert _quantize_down(Decimal("5.9"), 0) == Decimal("5")

    def test_handles_large_values(self):
        assert _quantize_down(Decimal("1234.5678"), 2) == Decimal("1234.56")

    def test_negative_precision_raises(self):
        with pytest.raises(ValueError):
            _quantize_down(Decimal("1"), -1)

    def test_nine_repeat_pathology(self):
        # 0.9999999999 with precision 6 → 0.999999 (classic float-vs-decimal bug spot)
        assert _quantize_down(Decimal("0.9999999999"), 6) == Decimal("0.999999")


class TestQuantizePrice:
    def test_rounds_to_precision(self):
        assert _quantize_price(Decimal("60123.456"), 2) == Decimal("60123.46")

    def test_half_rounding(self):
        # Default rounding is banker's (ROUND_HALF_EVEN) in Python Decimal
        # But price rounding doesn't affect safety — we just want consistency
        r = _quantize_price(Decimal("60123.455"), 2)
        assert r in (Decimal("60123.45"), Decimal("60123.46"))

    def test_no_decimals(self):
        assert _quantize_price(Decimal("60123.99"), 0) == Decimal("60124")
