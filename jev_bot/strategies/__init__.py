"""Optional M1 strategies for live XAUUSD.

Each strategy owns its TypeSafe questions and truth table. The router ORs
their votes and holds when long and short disagree.
"""
from .common import MarketContext, Signal
from .router import StrategyFlags, StrategyRouter, build_context

__all__ = ["MarketContext", "Signal", "StrategyFlags", "StrategyRouter", "build_context"]
