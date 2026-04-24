"""Unit tests for the indicators module."""

from __future__ import annotations

import pytest

from desk.indicators import (
    STRATEGIES,
    Direction,
    IndicatorResult,
    atr,
    bars_to_df,
    bollinger,
    ema,
    macd,
    rsi,
    run_strategy,
    sma,
)

pytestmark = pytest.mark.unit


class TestPrimitives:
    """Tests for primitive indicator functions."""

    def test_sma_shape(self, synthetic_bars_df):
        s = sma(synthetic_bars_df["close"], length=20)
        assert len(s) == len(synthetic_bars_df)
        assert s.iloc[:19].isna().all()
        assert s.iloc[19:].notna().all()

    def test_ema_shape(self, synthetic_bars_df):
        s = ema(synthetic_bars_df["close"], length=14)
        assert len(s) == len(synthetic_bars_df)
        assert s.iloc[13:].notna().all()

    def test_rsi_range(self, synthetic_bars_df):
        r = rsi(synthetic_bars_df["close"], length=14)
        valid = r.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_macd_columns(self, synthetic_bars_df):
        m = macd(synthetic_bars_df["close"])
        assert set(m.columns) == {"macd", "signal", "hist"}

    def test_bollinger_columns(self, synthetic_bars_df):
        bb = bollinger(synthetic_bars_df["close"])
        assert set(bb.columns) == {"mid", "upper", "lower", "width", "pct_b"}
        # Upper should always be >= mid >= lower after warmup
        valid = bb.dropna()
        assert (valid["upper"] >= valid["mid"]).all()
        assert (valid["mid"] >= valid["lower"]).all()

    def test_atr_positive(self, synthetic_bars_df):
        a = atr(synthetic_bars_df, length=14)
        assert (a.dropna() > 0).all()


class TestStrategies:
    """Tests that each strategy produces valid output."""

    @pytest.mark.parametrize("strategy_name", sorted(STRATEGIES))
    def test_returns_valid_result(self, strategy_name, synthetic_bars_df):
        result = run_strategy(strategy_name, synthetic_bars_df)
        assert isinstance(result, IndicatorResult)
        assert isinstance(result.direction, Direction)
        assert 0.0 <= result.strength <= 1.0
        assert isinstance(result.features, dict)

    @pytest.mark.parametrize("strategy_name", sorted(STRATEGIES))
    def test_insufficient_bars(self, strategy_name, tiny_bars_df):
        result = run_strategy(strategy_name, tiny_bars_df)
        assert result.direction == Direction.FLAT
        assert result.strength == 0.0
        assert result.features.get("error") == "insufficient_bars"

    def test_unknown_strategy_raises(self, synthetic_bars_df):
        with pytest.raises(ValueError, match="Unknown strategy"):
            run_strategy("nonexistent", synthetic_bars_df)

    def test_as_dict_shape(self, synthetic_bars_df):
        result = run_strategy("ma_cross_20_50", synthetic_bars_df)
        d = result.as_dict()
        assert set(d.keys()) == {"direction", "strength", "features"}
        assert d["direction"] in ("long", "short", "flat")


class TestBarsToDF:
    """Tests for the Bar → DataFrame conversion."""

    def test_shape_and_columns(self, sample_decimal_bars):
        df = bars_to_df(sample_decimal_bars)
        assert df.shape == (60, 5)
        assert set(df.columns) == {"open", "high", "low", "close", "volume"}
        assert df.index.name == "ts"

    def test_sorted_ascending(self, sample_decimal_bars):
        # Feed in reverse, assert output is ascending
        df = bars_to_df(reversed(sample_decimal_bars))
        assert df.index.is_monotonic_increasing

    def test_empty_input(self):
        df = bars_to_df([])
        assert df.empty

    def test_decimals_converted_to_floats(self, sample_decimal_bars):
        df = bars_to_df(sample_decimal_bars)
        for col in ("open", "high", "low", "close", "volume"):
            assert df[col].dtype.kind == "f"
