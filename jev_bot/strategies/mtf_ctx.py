"""Helpers for multi-timeframe strategy context (closed bars via ``ctx.mtf``)."""
from __future__ import annotations

from datetime import datetime, timezone

from ..bars import Bar
from .mtf_base import MtfLimits, default_mtf_limits


def mtf_bars(ctx, tf: str) -> list[Bar]:
    """Closed bars for ``tf`` from ``ctx.mtf``, or ``[]`` when missing."""
    mtf = getattr(ctx, "mtf", None)
    if not isinstance(mtf, dict):
        return []
    bars = mtf.get(tf)
    if not bars:
        return []
    return list(bars)


def as_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def ctx_limits(ctx) -> MtfLimits:
    lim = getattr(ctx, "mtf_limits", None)
    return lim if isinstance(lim, MtfLimits) else default_mtf_limits()


def ctx_spread_points(ctx) -> float:
    sp = getattr(ctx, "spread_points", None)
    if sp is not None:
        return float(sp)
    point = float(getattr(ctx, "point", 0.0) or 0.0)
    if point <= 0:
        return 0.0
    bid = float(getattr(ctx, "bid", 0.0) or 0.0)
    ask = float(getattr(ctx, "ask", 0.0) or 0.0)
    return max(0.0, (ask - bid) / point)


def ctx_allow_short(ctx) -> bool:
    if hasattr(ctx, "allow_short") and getattr(ctx, "allow_short") is not None:
        return bool(ctx.allow_short)
    return bool(ctx_limits(ctx).allow_short)


def ctx_stops_level(ctx) -> int:
    return int(getattr(ctx, "stops_level", 0) or 0)


def seconds_until(now: datetime, hour: int, minute: int = 0) -> int:
    """Seconds from ``now`` (UTC) until today's ``hour:minute`` UTC; 0 if already past."""
    utc = as_utc(now)
    end = utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if end <= utc:
        return 0
    return int((end - utc).total_seconds())
