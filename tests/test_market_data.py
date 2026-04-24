"""Tests for market_data — specifically the paper-mode safety checks."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure no Binance env leaks between tests."""
    for key in [
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_TESTNET_REST_URL",
        "BINANCE_LIVE_API_KEY",
        "BINANCE_LIVE_API_SECRET",
        "PARLEY_MODE",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def mock_ccxt():
    """Stub out ccxt so tests don't hit the network or depend on install."""
    with patch("desk.market_data.ccxt") as mocked:
        mocked.NetworkError = type("NetworkError", (Exception,), {})
        mocked.RequestTimeout = type("RequestTimeout", (Exception,), {})
        mocked.DDoSProtection = type("DDoSProtection", (Exception,), {})
        mocked.ExchangeNotAvailable = type("ExchangeNotAvailable", (Exception,), {})
        exchange = MagicMock()
        mocked.binance.return_value = exchange
        yield mocked, exchange


class TestPaperModeEnforcement:
    """These are the single most important tests in the codebase.

    Every one of them enforces that paper mode cannot accidentally point at
    production endpoints. If any of these breaks, Parley's safety story is
    compromised.
    """

    def test_paper_refuses_non_testnet_url(self, monkeypatch, mock_ccxt):
        from desk.market_data import BinanceClient

        monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
        monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
        monkeypatch.setenv("BINANCE_TESTNET_REST_URL", "https://api.binance.com")

        with pytest.raises(RuntimeError, match="does not contain 'testnet'"):
            BinanceClient.paper()

    def test_paper_accepts_testnet_url(self, monkeypatch, mock_ccxt):
        from desk.market_data import BinanceClient

        monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
        monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
        monkeypatch.setenv("BINANCE_TESTNET_REST_URL", "https://testnet.binance.vision")

        client = BinanceClient.paper()
        assert client.mode == "paper"

    def test_live_refused_without_mode_env(self, mock_ccxt):
        from desk.market_data import BinanceClient

        with pytest.raises(RuntimeError, match="PARLEY_MODE is not 'live'"):
            BinanceClient.live()

    def test_live_refused_even_with_mode_if_keys_missing(self, monkeypatch, mock_ccxt):
        from desk.market_data import BinanceClient

        monkeypatch.setenv("PARLEY_MODE", "live")
        with pytest.raises(RuntimeError, match="live API credentials not set"):
            BinanceClient.live()


class TestSymbolNormalization:
    def test_plain_symbol(self):
        from desk.market_data import BinanceClient

        assert BinanceClient._normalize_symbol("BTCUSDT") == "BTC/USDT"
        assert BinanceClient._normalize_symbol("ETHUSDT") == "ETH/USDT"
        assert BinanceClient._normalize_symbol("SOLUSDT") == "SOL/USDT"

    def test_already_formatted(self):
        from desk.market_data import BinanceClient

        assert BinanceClient._normalize_symbol("BTC/USDT") == "BTC/USDT"

    def test_alternate_quote(self):
        from desk.market_data import BinanceClient

        assert BinanceClient._normalize_symbol("BTCBTC") == "BTC/BTC"

    def test_bad_symbol_raises(self):
        from desk.market_data import BinanceClient

        with pytest.raises(ValueError, match="Cannot parse"):
            BinanceClient._normalize_symbol("XYZ")


class TestSnapshot:
    def test_mid_and_spread(self):
        from datetime import datetime, timezone
        from decimal import Decimal

        from desk.market_data import Snapshot

        snap = Snapshot(
            ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            bid=Decimal("100.00"),
            ask=Decimal("100.10"),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
        )
        assert snap.mid == Decimal("100.05")
        # (0.10 / 100.05) * 10000 ≈ 9.99 bps
        assert abs(snap.spread_bps - Decimal("10")) < Decimal("0.1")
