"""Shared types and helpers for live strategy modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..questions import NO, YES, Decision, Policy, _band, _reading
from ..risk import RiskError, RiskLimits
from ..typesafe import Noul, Question, Response

__all__ = [
    "YES", "NO", "Policy", "Decision", "Signal", "MarketContext",
    "noul", "band", "reading", "clamp_points", "clamp_hold",
    "roundtrip_cost", "require_zone",
]


@dataclass
class Signal:
    """One strategy's vote. The router ORs these and holds on a conflict."""

    action: str
    reason: str
    strategy: str
    size: float = 0.0
    features: dict = field(default_factory=dict)
    sl_points: float | None = None
    tp_points: float | None = None
    max_hold_seconds: int | None = None
    composite: float = 0.0
    answers: dict = field(default_factory=dict)


@dataclass
class MarketContext:
    """Numbers the strategies read. Built once per decision tick in live."""

    now: datetime
    bar_t: int
    mid: float
    bid: float
    ask: float
    point: float
    digits: int
    position: int
    semantic: dict
    mids: list[float]
    volumes: list[float]
    times: list[int]
    buy_vol: list[float]
    sell_vol: list[float]
    limits: RiskLimits
    max_hold_seconds: int
    minutes: list  # MinuteBar objects
    avoid: object | None = None  # AvoidHours | None; optional entry-hour block


def noul(question: str, yes: str, no: str) -> Noul:
    return Noul(question, {"true": yes, "false": no})


def reading(response: Response, key: str) -> float | None:
    return _reading(response, key)


def band(value: float | None, policy: Policy | None = None) -> str:
    return _band(value, policy or Policy())


def clamp_points(wanted: float, global_points: float) -> float:
    """Strategy distance may only be tighter than the global limit.

    Global ``0`` means that side is off, so the strategy distance stands.
    A non-positive ``wanted`` means the strategy leaves that side unset.
    """
    if wanted <= 0:
        return 0.0
    if global_points <= 0:
        return float(wanted)
    return float(min(wanted, global_points))


def clamp_hold(wanted: int, global_hold: int) -> int:
    """Strategy hold may only be shorter than the global limit.

    Global ``0`` means the global hold is off, so the strategy hold stands.
    """
    if wanted <= 0:
        return int(global_hold) if global_hold > 0 else 0
    if global_hold <= 0:
        return int(wanted)
    return int(min(wanted, global_hold))


def roundtrip_cost(bid: float, ask: float, deviation_points: int, point: float) -> float:
    """Live spread plus slippage both ways, in price units."""
    spread = max(0.0, ask - bid)
    slip = 2.0 * max(0, int(deviation_points)) * max(point, 0.0)
    return spread + slip


def require_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise RiskError(
            f"cannot load {name}. Install the IANA time zone database "
            "(on Windows: pip install tzdata)."
        ) from None


def to_decision(sig: Signal, t: int) -> Decision:
    return Decision(
        t=t,
        action=sig.action,
        size=sig.size,
        composite=sig.composite,
        gates={"strategy": True},
        reason=sig.reason,
        answers=sig.answers,
        strategy=sig.strategy,
        features=sig.features,
        sl_points=sig.sl_points,
        tp_points=sig.tp_points,
        max_hold_seconds=sig.max_hold_seconds,
    )
