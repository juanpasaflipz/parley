"""Shared pytest fixtures and configuration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def synthetic_bars_df() -> pd.DataFrame:
    """200 hourly bars with a deterministic random walk. For indicator tests."""
    rng = np.random.default_rng(seed=42)
    n = 200
    prices = 60000 + np.cumsum(rng.standard_normal(n) * 100)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    df = pd.DataFrame(
        {
            "open": prices + rng.standard_normal(n) * 10,
            "high": prices + np.abs(rng.standard_normal(n) * 50),
            "low": prices - np.abs(rng.standard_normal(n) * 50),
            "close": prices,
            "volume": np.abs(rng.standard_normal(n) * 100) + 500,
        },
        index=pd.DatetimeIndex([start + timedelta(hours=h) for h in range(n)], name="ts"),
    )
    return df


@pytest.fixture
def tiny_bars_df(synthetic_bars_df: pd.DataFrame) -> pd.DataFrame:
    """Only 5 bars — for testing insufficient-bars handling."""
    return synthetic_bars_df.tail(5)


@pytest.fixture
def sample_decimal_bars() -> list:
    """A handful of Bar objects for testing bars_to_df conversion."""
    from desk.market_data import Bar

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(
            ts=start + timedelta(hours=h),
            open=Decimal("60000.12"),
            high=Decimal("60100.50"),
            low=Decimal("59950.00"),
            close=Decimal("60050.25") + Decimal(str(h)),
            volume=Decimal("12.345"),
        )
        for h in range(60)
    ]
