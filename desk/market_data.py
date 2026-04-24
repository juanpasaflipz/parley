"""Market data client.

Thin wrapper around CCXT's Binance client, specialized for Parley's needs:

- Paper-mode enforcement at construction time (refuses live endpoints when
  ``mode=paper``).
- Retry with exponential backoff on transient errors.
- Decimal-native outputs — we never return floats for prices or quantities.
- Pulls that match our schema directly: bars for ``market_bars``, snapshots
  for ``market_snapshots``.

Phase 1 uses REST. WebSocket streaming is a Phase 2 addition once we start
caring about lower-latency signals.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import ccxt  # type: ignore[import-untyped]
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar, Decimal-native."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def as_row(self) -> tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal, None]:
        """Tuple shaped for ``desk.db.insert_bars``."""
        return (self.ts, self.open, self.high, self.low, self.close, self.volume, None)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Point-in-time market snapshot."""

    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        if self.mid == 0:
            return Decimal("0")
        return (self.ask - self.bid) / self.mid * Decimal("10000")


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

_TRANSIENT = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
    ccxt.ExchangeNotAvailable,
)


def _retry() -> Any:
    """Shared retry decorator for transient exchange errors."""
    return retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type(_TRANSIENT),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BinanceClient:
    """Binance client with paper/live mode enforcement.

    Construct via the classmethods ``paper()`` or ``live()`` rather than
    directly — they apply the correct safety checks.
    """

    TESTNET_REST = "https://testnet.binance.vision"
    LIVE_REST = "https://api.binance.com"

    def __init__(self, exchange: ccxt.Exchange, mode: str) -> None:
        self._ex = exchange
        self.mode = mode

    # --- Constructors ------------------------------------------------------

    @classmethod
    def paper(cls) -> BinanceClient:
        """Testnet client. Reads BINANCE_TESTNET_* env vars."""
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
        api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
        base_url = os.environ.get("BINANCE_TESTNET_REST_URL", cls.TESTNET_REST)
        if "testnet" not in base_url:
            raise RuntimeError(
                f"Refusing to construct paper client: BINANCE_TESTNET_REST_URL "
                f"does not contain 'testnet' ({base_url!r}). "
                "This is a hard safety check."
            )
        exchange = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot", "adjustForTimeDifference": True},
                "urls": {"api": {"public": base_url, "private": base_url}},
            }
        )
        exchange.set_sandbox_mode(True)
        logger.info("BinanceClient initialized in paper mode at %s", base_url)
        return cls(exchange, mode="paper")

    @classmethod
    def live(cls) -> BinanceClient:
        """Live client. This constructor is intentionally harsh — it refuses to
        run unless the operator has set PARLEY_MODE=live AND provided live
        credentials. Phase 1 should never call this."""
        if os.environ.get("PARLEY_MODE") != "live":
            raise RuntimeError(
                "BinanceClient.live() refused: PARLEY_MODE is not 'live'. "
                "Phase 1 is paper only."
            )
        api_key = os.environ.get("BINANCE_LIVE_API_KEY")
        api_secret = os.environ.get("BINANCE_LIVE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError(
                "BinanceClient.live() refused: live API credentials not set."
            )
        exchange = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot", "adjustForTimeDifference": True},
            }
        )
        logger.warning("BinanceClient initialized in LIVE mode — real money at risk")
        return cls(exchange, mode="live")

    # --- Public market data (no auth required) -----------------------------

    @_retry()
    def fetch_bars(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[Bar]:
        """Fetch the most recent ``limit`` bars for ``symbol``.

        ``symbol`` is the CCXT-style pair like ``BTC/USDT``. If the caller
        passes venue-style ``BTCUSDT``, we normalize.
        """
        ccxt_symbol = self._normalize_symbol(symbol)
        raw = self._ex.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
        return [_bar_from_ccxt(row) for row in raw]

    @_retry()
    def fetch_snapshot(self, symbol: str) -> Snapshot:
        """Top-of-book bid/ask snapshot."""
        ccxt_symbol = self._normalize_symbol(symbol)
        ob = self._ex.fetch_order_book(ccxt_symbol, limit=5)
        if not ob["bids"] or not ob["asks"]:
            raise RuntimeError(f"Empty order book for {symbol}")
        bid_px, bid_sz = ob["bids"][0]
        ask_px, ask_sz = ob["asks"][0]
        ts = datetime.fromtimestamp(ob["timestamp"] / 1000, tz=timezone.utc) \
            if ob.get("timestamp") else datetime.now(timezone.utc)
        return Snapshot(
            ts=ts,
            bid=Decimal(str(bid_px)),
            ask=Decimal(str(ask_px)),
            bid_size=Decimal(str(bid_sz)) if bid_sz else None,
            ask_size=Decimal(str(ask_sz)) if ask_sz else None,
        )

    @_retry()
    def fetch_recent_volume_usd(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Return (1m_volume_usd, 1h_volume_usd) based on recent trades.

        Uses the last 60 1m bars for the 1h figure and the most recent 1m bar
        for the 1m figure. Volume is in base currency × close price.
        """
        bars = self.fetch_bars(symbol, timeframe="1m", limit=60)
        if not bars:
            return Decimal("0"), Decimal("0")
        last = bars[-1]
        one_min = last.volume * last.close
        one_hour = sum((b.volume * b.close for b in bars), Decimal("0"))
        return one_min, one_hour

    # --- Auth'd market info ------------------------------------------------

    @_retry()
    def fetch_balance(self) -> dict[str, Decimal]:
        """Return free balances keyed by currency. Testnet or live depending on mode."""
        raw = self._ex.fetch_balance()
        free: dict[str, Any] = raw.get("free", {})
        return {
            cur: Decimal(str(amt))
            for cur, amt in free.items()
            if amt and Decimal(str(amt)) > 0
        }

    # --- Internal helpers --------------------------------------------------

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Accept either 'BTCUSDT' or 'BTC/USDT'; return CCXT form 'BTC/USDT'."""
        if "/" in symbol:
            return symbol
        # Naive split on USDT suffix. Expand later when we add more quotes.
        for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}/{quote}"
        raise ValueError(f"Cannot parse symbol: {symbol}")


def _bar_from_ccxt(row: list[Any]) -> Bar:
    """Convert a CCXT OHLCV row to our Decimal-native Bar."""
    ts_ms, o, h, l, c, v = row
    return Bar(
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


__all__ = ["Bar", "BinanceClient", "Snapshot"]
