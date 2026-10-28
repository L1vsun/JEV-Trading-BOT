"""Per-strategy magic numbers: base magic + named offset.

Each entry strategy that can open a live position gets its own MT5 magic
so the bot can hold up to one position per strategy without OR-combining
votes into a single net decision. Filters (``session_dir``,
``pa_structure``) are listed for documentation; they never open orders but
share the same offset table so overrides stay consistent.

``consensus`` keeps offset ``0`` (the ``--magic`` base itself).
"""
from __future__ import annotations

# Offset from ``--magic`` base. Keep values unique within the bot family.
STRATEGY_MAGIC_OFFSET: dict[str, int] = {
    # M1 / consensus
    "consensus": 0,
    "vwap_fade": 1,
    "orb_london": 2,
    # Filters (documented; do not open positions)
    "session_dir": 3,
    # Price action
    "pa_rejection": 4,
    "pa_insidebar": 5,
    "pa_structure": 6,
    # M5
    "m5_ny_orb": 10,
    "m5_asianbox": 11,
    "m5_vwap_pb": 12,
    # M15
    "m15_ny_orb": 20,
    "m15_asia_squeeze": 21,
    "m15_trend_pullback": 22,
    # H1
    "h1_trend_long": 30,
    "h1_pullback": 31,
    "h1_prevday_break": 32,
    # H4
    "h4_donchian": 40,
    "h4_ema_pullback": 41,
    "h4_zone_retest": 42,
    # Unified pullbacks (legacy aliases keep their offsets above)
    "pullback_m1": 50,
    "pullback_m5": 51,
    "pullback_m15": 52,
    "pullback_h1": 53,
    "pullback_h4": 54,
}

# Strategies that can open / close their own position (not pure filters).
ENTRY_STRATEGIES: frozenset[str] = frozenset({
    "consensus",
    "vwap_fade",
    "orb_london",
    "pa_rejection",
    "pa_insidebar",
    "m5_ny_orb",
    "m5_asianbox",
    "m5_vwap_pb",
    "m15_ny_orb",
    "m15_asia_squeeze",
    "m15_trend_pullback",
    "h1_trend_long",
    "h1_pullback",
    "h1_prevday_break",
    "h4_donchian",
    "h4_ema_pullback",
    "h4_zone_retest",
    "pullback_m1",
    "pullback_m5",
    "pullback_m15",
    "pullback_h1",
    "pullback_h4",
})


def magic_table(overrides: dict[str, int] | None = None) -> dict[str, int]:
    """Return the offset table with optional JSON / CLI overrides merged in."""
    table = dict(STRATEGY_MAGIC_OFFSET)
    if overrides:
        for name, offset in overrides.items():
            table[str(name)] = int(offset)
    return table


def magic_for(strategy: str, base: int, overrides: dict[str, int] | None = None) -> int:
    """Resolve ``base + offset`` for ``strategy``.

    Raises ``KeyError`` when the strategy is not in the table (after overrides).
    """
    table = magic_table(overrides)
    try:
        offset = table[strategy]
    except KeyError as exc:
        raise KeyError(f"unknown strategy for magic offset: {strategy}") from exc
    return int(base) + int(offset)


def all_bot_magics(base: int, overrides: dict[str, int] | None = None) -> set[int]:
    """Every magic this bot family may own (base + all table offsets)."""
    return {int(base) + int(off) for off in magic_table(overrides).values()}


def order_comment(strategy: str) -> str:
    """MT5 order comment (max 31 chars) tagging the strategy name."""
    text = f"jev:{strategy}" if strategy else "jev"
    return text[:31]
