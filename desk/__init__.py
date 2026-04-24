"""Parley — multi-agent crypto trading desk.

A research-stage system where five specialized AI agents (Research, Quant,
Portfolio Manager, Risk, Execution) deliberate through structured outputs
logged to Postgres.

Phase 1: Paper trading only on Binance testnet.

See CLAUDE.md for the supervisor's constitution and README.md for an
architectural overview. Never run Parley with live capital without
reading DISCLAIMER.md.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
