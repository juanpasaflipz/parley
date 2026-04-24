"""Technical indicator computations.

Pure functions that take a ``pandas.DataFrame`` of OHLCV data and return
indicator values. No Decimal here because indicators are dimensionless; the
*prices* stay Decimal everywhere else in the system.

The ``Bar`` rows from ``desk.market_data`` should be converted to a
DataFrame via ``bars_to_df`` before passing here. That converts Decimals to
floats at the boundary, which is appropriate for indicator math.

Adding a new indicator? See CONTRIBUTING.md → "Extension: adding a strategy."
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from desk.market_data import Bar


# ---------------------------------------------------------------------------
# Bar → DataFrame
# ---------------------------------------------------------------------------


def bars_to_df(bars: Iterable[Bar]) -> pd.DataFrame:
    """Convert an iterable of Bar to a DataFrame indexed by UTC timestamp.

    Columns: ``open, high, low, close, volume`` — all float for indicator math.
    The DataFrame is sorted ascending by ts (oldest first).
    """
    rows = [
        {
            "ts": b.ts,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume),
        }
        for b in bars
    ]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("ts").reset_index(drop=True)
    df = df.set_index("ts")
    return df


# ---------------------------------------------------------------------------
# Signal dataclass — small, avoids pandas_ta dependency in hot path
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    """What a strategy returns. ``strength`` is 0..1."""

    direction: Direction
    strength: float
    features: dict[str, float | int | str | None]

    def as_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction.value,
            "strength": float(self.strength),
            "features": self.features,
        }


# ---------------------------------------------------------------------------
# Indicator primitives
# ---------------------------------------------------------------------------


def sma(series: pd.Series, length: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI. Returns a Series of 0..100 values."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD, signal, histogram."""
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "hist": macd_line - signal_line,
        }
    )


def bollinger(series: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    """Bollinger bands. Returns mid (SMA), upper, lower, width, pct_b."""
    mid = sma(series, length)
    sd = series.rolling(window=length, min_periods=length).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    width = (upper - lower) / mid.replace(0, np.nan)
    pct_b = (series - lower) / (upper - lower).replace(0, np.nan)
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower, "width": width, "pct_b": pct_b}
    )


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average true range. Requires columns high, low, close."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_close = c.shift(1)
    tr = pd.concat(
        [(h - l), (h - prev_close).abs(), (l - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def realized_vol(series: pd.Series, length: int = 20) -> pd.Series:
    """Annualized realized volatility from log returns. Length in bars."""
    log_ret = np.log(series / series.shift(1))
    return log_ret.rolling(window=length, min_periods=length).std() * np.sqrt(365 * 24)


# ---------------------------------------------------------------------------
# Strategy rules — each returns an IndicatorResult
# ---------------------------------------------------------------------------


def strategy_rsi_divergence(
    df: pd.DataFrame,
    length: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    lookback: int = 10,
) -> IndicatorResult:
    """Simple oversold/overbought with divergence check.

    LONG: RSI exits oversold (crosses above 30) AND price made a higher
    low in the last ``lookback`` bars while RSI also made a higher low.
    SHORT: Mirror logic at overbought.
    """
    if len(df) < max(length * 2, lookback + length + 2):
        return IndicatorResult(
            Direction.FLAT, 0.0, {"error": "insufficient_bars", "have": len(df)}
        )

    r = rsi(df["close"], length=length)
    curr_rsi = float(r.iloc[-1])
    prev_rsi = float(r.iloc[-2])

    features: dict[str, float | int | str | None] = {
        "rsi": curr_rsi,
        "rsi_prev": prev_rsi,
        "close": float(df["close"].iloc[-1]),
    }

    window = df.tail(lookback + 1)
    price_hl = window["low"].iloc[-1] > window["low"].min()
    rsi_hl = r.tail(lookback + 1).iloc[-1] > r.tail(lookback + 1).min()
    price_lh = window["high"].iloc[-1] < window["high"].max()
    rsi_lh = r.tail(lookback + 1).iloc[-1] < r.tail(lookback + 1).max()

    features["price_higher_low"] = bool(price_hl)
    features["rsi_higher_low"] = bool(rsi_hl)

    # Long trigger
    if prev_rsi <= oversold < curr_rsi and price_hl and rsi_hl:
        strength = min(1.0, (oversold - min(prev_rsi, oversold - 5)) / 10 + 0.4)
        return IndicatorResult(Direction.LONG, strength, features)

    # Short trigger
    if prev_rsi >= overbought > curr_rsi and price_lh and rsi_lh:
        strength = min(1.0, (max(prev_rsi, overbought + 5) - overbought) / 10 + 0.4)
        return IndicatorResult(Direction.SHORT, strength, features)

    return IndicatorResult(Direction.FLAT, 0.0, features)


def strategy_ma_cross(
    df: pd.DataFrame,
    fast: int = 20,
    slow: int = 50,
) -> IndicatorResult:
    """Classic MA cross. Strength scales with current separation."""
    if len(df) < slow + 2:
        return IndicatorResult(
            Direction.FLAT, 0.0, {"error": "insufficient_bars", "have": len(df)}
        )
    fast_ma = sma(df["close"], fast)
    slow_ma = sma(df["close"], slow)

    curr_fast = float(fast_ma.iloc[-1])
    curr_slow = float(slow_ma.iloc[-1])
    prev_fast = float(fast_ma.iloc[-2])
    prev_slow = float(slow_ma.iloc[-2])
    close = float(df["close"].iloc[-1])

    features: dict[str, float | int | str | None] = {
        "fast_ma": curr_fast,
        "slow_ma": curr_slow,
        "close": close,
        "separation_pct": (curr_fast - curr_slow) / curr_slow * 100 if curr_slow else 0.0,
    }

    # Bullish cross — fast crosses above slow
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        strength = min(1.0, abs(curr_fast - curr_slow) / close * 200)
        return IndicatorResult(Direction.LONG, strength, features)

    # Bearish cross
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        strength = min(1.0, abs(curr_fast - curr_slow) / close * 200)
        return IndicatorResult(Direction.SHORT, strength, features)

    # No cross but persistent separation
    if curr_fast > curr_slow:
        strength = min(0.5, (curr_fast - curr_slow) / close * 100)
        return IndicatorResult(Direction.LONG, strength, features)
    if curr_fast < curr_slow:
        strength = min(0.5, (curr_slow - curr_fast) / close * 100)
        return IndicatorResult(Direction.SHORT, strength, features)

    return IndicatorResult(Direction.FLAT, 0.0, features)


def strategy_bb_breakout(
    df: pd.DataFrame,
    length: int = 20,
    std: float = 2.0,
) -> IndicatorResult:
    """Bollinger band breakout. LONG when close breaks above upper after
    touching band recently; SHORT mirror. Flat inside the bands."""
    if len(df) < length + 2:
        return IndicatorResult(
            Direction.FLAT, 0.0, {"error": "insufficient_bars", "have": len(df)}
        )
    bb = bollinger(df["close"], length=length, std=std)
    close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    upper = float(bb["upper"].iloc[-1])
    lower = float(bb["lower"].iloc[-1])
    pct_b = float(bb["pct_b"].iloc[-1])
    width = float(bb["width"].iloc[-1])

    features: dict[str, float | int | str | None] = {
        "close": close,
        "upper": upper,
        "lower": lower,
        "pct_b": pct_b,
        "width": width,
    }

    if prev_close <= upper < close:
        # Fresh upper breakout — strength scales with width (wider bands => stronger signal)
        strength = min(1.0, 0.4 + width * 5)
        return IndicatorResult(Direction.LONG, strength, features)

    if prev_close >= lower > close:
        strength = min(1.0, 0.4 + width * 5)
        return IndicatorResult(Direction.SHORT, strength, features)

    return IndicatorResult(Direction.FLAT, 0.0, features)


def strategy_macd_signal(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> IndicatorResult:
    """MACD / signal line crossover."""
    if len(df) < slow + signal + 2:
        return IndicatorResult(
            Direction.FLAT, 0.0, {"error": "insufficient_bars", "have": len(df)}
        )
    m = macd(df["close"], fast=fast, slow=slow, signal=signal)
    curr_macd = float(m["macd"].iloc[-1])
    curr_sig = float(m["signal"].iloc[-1])
    prev_macd = float(m["macd"].iloc[-2])
    prev_sig = float(m["signal"].iloc[-2])
    close = float(df["close"].iloc[-1])

    features: dict[str, float | int | str | None] = {
        "macd": curr_macd,
        "signal": curr_sig,
        "hist": curr_macd - curr_sig,
        "close": close,
    }

    if prev_macd <= prev_sig and curr_macd > curr_sig:
        strength = min(1.0, abs(curr_macd - curr_sig) / close * 1000)
        return IndicatorResult(Direction.LONG, strength, features)

    if prev_macd >= prev_sig and curr_macd < curr_sig:
        strength = min(1.0, abs(curr_macd - curr_sig) / close * 1000)
        return IndicatorResult(Direction.SHORT, strength, features)

    return IndicatorResult(Direction.FLAT, 0.0, features)


def strategy_volume_spike(
    df: pd.DataFrame,
    length: int = 20,
    threshold: float = 2.5,
) -> IndicatorResult:
    """Flag when current bar's volume is a multiple of recent average.

    Direction comes from the current bar's candle direction.
    """
    if len(df) < length + 1:
        return IndicatorResult(
            Direction.FLAT, 0.0, {"error": "insufficient_bars", "have": len(df)}
        )
    avg_vol = df["volume"].tail(length + 1).iloc[:-1].mean()
    curr_vol = float(df["volume"].iloc[-1])
    ratio = curr_vol / avg_vol if avg_vol > 0 else 0.0
    close = float(df["close"].iloc[-1])
    open_px = float(df["open"].iloc[-1])

    features: dict[str, float | int | str | None] = {
        "volume": curr_vol,
        "avg_volume": float(avg_vol),
        "ratio": float(ratio),
        "close": close,
        "open": open_px,
    }

    if ratio < threshold:
        return IndicatorResult(Direction.FLAT, 0.0, features)

    strength = min(1.0, (ratio - threshold) / threshold + 0.4)
    if close > open_px:
        return IndicatorResult(Direction.LONG, strength, features)
    if close < open_px:
        return IndicatorResult(Direction.SHORT, strength, features)
    return IndicatorResult(Direction.FLAT, 0.0, features)


# ---------------------------------------------------------------------------
# Registry — the Quant subagent reads this to know what exists
# ---------------------------------------------------------------------------


STRATEGIES = {
    "rsi_divergence": strategy_rsi_divergence,
    "ma_cross_20_50": strategy_ma_cross,
    "bb_breakout_20_2": strategy_bb_breakout,
    "macd_signal": strategy_macd_signal,
    "volume_spike": strategy_volume_spike,
}


def run_strategy(name: str, df: pd.DataFrame) -> IndicatorResult:
    """Dispatch to a named strategy with default params."""
    if name not in STRATEGIES:
        raise ValueError(
            f"Unknown strategy: {name}. Available: {sorted(STRATEGIES)}"
        )
    return STRATEGIES[name](df)


__all__ = [
    "Direction",
    "IndicatorResult",
    "STRATEGIES",
    "atr",
    "bars_to_df",
    "bollinger",
    "ema",
    "macd",
    "realized_vol",
    "rsi",
    "run_strategy",
    "sma",
    "strategy_bb_breakout",
    "strategy_ma_cross",
    "strategy_macd_signal",
    "strategy_rsi_divergence",
    "strategy_volume_spike",
]
