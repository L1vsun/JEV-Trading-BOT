"""Optional M1 / PA / M5–H4 strategies for live XAUUSD.

Each strategy owns its TypeSafe questions and truth table. Live uses
``decide_all`` so each buy/sell/exit keeps its own magic; long vs short
across strategies is allowed (optional ``--no-hedge``). Price-action
modules share ATR, zones and M5 aggregation in ``_pa_common``. Unified
pullbacks share ``_pullback_common``. Higher-TF modules read closed bars
from ``ctx.mtf``. All flags default off for live.
"""
from .common import MarketContext, Signal
from .magics import (
    ENTRY_STRATEGIES,
    STRATEGY_MAGIC_OFFSET,
    all_bot_magics,
    magic_for,
    magic_table,
    order_comment,
)
from .router import StrategyFlags, StrategyRouter, build_context, parse_strategy_flags

__all__ = [
    "MarketContext", "Signal", "StrategyFlags", "StrategyRouter",
    "build_context", "parse_strategy_flags",
    "STRATEGY_MAGIC_OFFSET", "ENTRY_STRATEGIES",
    "magic_for", "magic_table", "all_bot_magics", "order_comment",
]
