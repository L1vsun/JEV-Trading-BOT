"""Optional M1 and M5 strategies for live XAUUSD.

Each strategy owns its TypeSafe questions and truth table. The router ORs
their votes and holds when long and short disagree. Price-action modules
share ATR, zones and M5 aggregation in ``_pa_common``.
"""
from .common import MarketContext, Signal
from .router import StrategyFlags, StrategyRouter, build_context

__all__ = ["MarketContext", "Signal", "StrategyFlags", "StrategyRouter", "build_context"]
