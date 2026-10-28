"""London opening-range breakout with a TypeSafe confirmation Noul.

Flag ``--sig-orb`` (default off). The box is the first 30 minutes from the
London open on ``Europe/London``. Optional New York open uses the same shape
on ``America/New_York`` when ``session=\"ny\"``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..typesafe import Question, Response
from .common import (
    MarketContext,
    Policy,
    Signal,
    band,
    clamp_hold,
    clamp_points,
    noul,
    reading,
    require_zone,
)
from .minutes import MinuteBar

NAME = "orb_london"
YES_LONG = "orb_break_up"
YES_SHORT = "orb_break_down"
QUESTION_IDS = (YES_LONG, YES_SHORT)
BOX_MINUTES = 30
TRADE_WINDOW_HOURS = 3


def questions() -> dict[str, Question]:
    return {
        YES_LONG: noul(
            "Did the last closed minute in `orb` close clearly above the opening-range high, "
            "and does `order_flow.aggressor_tape_30s_windows_oldest_first` support buying?",
            "The close is above the box high and the aggressor tape favours buying.",
            "The close is not a clear break above the box, or the tape does not support buying.",
        ),
        YES_SHORT: noul(
            "Did the last closed minute in `orb` close clearly below the opening-range low, "
            "and does `order_flow.aggressor_tape_30s_windows_oldest_first` support selling?",
            "The close is below the box low and the aggressor tape favours selling.",
            "The close is not a clear break below the box, or the tape does not support selling.",
        ),
    }


def _open_local(now: datetime, zone_name: str) -> datetime:
    zone = require_zone(zone_name)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(zone)
    # Cash equity open: 08:00 local. Gold often tracks that London cash open.
    open_local = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < open_local:
        open_local = open_local - timedelta(days=1)
    while open_local.weekday() >= 5:
        open_local = open_local - timedelta(days=1)
    return open_local


def opening_range(minutes: list[MinuteBar], now: datetime, *,
                  zone_name: str = "Europe/London") -> dict | None:
    """High/low of the first 30 closed minutes from the session open."""
    if not minutes:
        return None
    open_local = _open_local(now, zone_name)
    box_end_local = open_local + timedelta(minutes=BOX_MINUTES)
    window_end_local = box_end_local + timedelta(hours=TRADE_WINDOW_HOURS)
    open_utc = int(open_local.astimezone(timezone.utc).timestamp())
    box_end_utc = int(box_end_local.astimezone(timezone.utc).timestamp())
    window_end_utc = int(window_end_local.astimezone(timezone.utc).timestamp())
    now_utc = int(now.timestamp()) if now.tzinfo else int(now.replace(tzinfo=timezone.utc).timestamp())
    if now_utc < box_end_utc or now_utc >= window_end_utc:
        return None
    box_bars = [m for m in minutes if open_utc <= m.t < box_end_utc]
    if len(box_bars) < BOX_MINUTES // 2:
        # Need a usable box; half the minutes is the floor when history is short.
        return None
    high = max(m.high for m in box_bars)
    low = min(m.low for m in box_bars)
    if high <= low:
        return None
    last = minutes[-1]
    return {
        "high": high,
        "low": low,
        "height": high - low,
        "box_end": box_end_utc,
        "window_end": window_end_utc,
        "zone": zone_name,
        "last_close": last.close,
        "last_t": last.t,
        "above": last.close > high,
        "below": last.close < low,
        "day": open_local.date().isoformat(),
    }


def compute(ctx: MarketContext, *, zone_name: str = "Europe/London") -> dict | None:
    return opening_range(ctx.minutes, ctx.now, zone_name=zone_name)


def precondition(ctx: MarketContext, *, zone_name: str = "Europe/London",
                 broken_days: set[str] | None = None) -> bool:
    feat = compute(ctx, zone_name=zone_name)
    if feat is None:
        return False
    if broken_days and feat["day"] in broken_days:
        return False
    return bool(feat["above"] or feat["below"])


def state_fields(ctx: MarketContext, *, zone_name: str = "Europe/London") -> dict:
    feat = compute(ctx, zone_name=zone_name) or {}
    return {
        "orb": {
            "zone": zone_name,
            "high": f"{feat.get('high', 0):.2f}",
            "low": f"{feat.get('low', 0):.2f}",
            "last_close": f"{feat.get('last_close', 0):.2f}",
            "position": (
                "above the opening-range high" if feat.get("above")
                else "below the opening-range low" if feat.get("below")
                else "inside the opening range"
            ),
        }
    }


def _stops(ctx: MarketContext, side: int, feat: dict) -> tuple[float, float, int]:
    height = feat["height"]
    half = 0.5 * height
    if side > 0:
        # Stop: other side of the box, or half the box — whichever is closer (tighter).
        stop_dist = min(ctx.mid - feat["low"], half)
    else:
        stop_dist = min(feat["high"] - ctx.mid, half)
    stop_pts = stop_dist / ctx.point if ctx.point > 0 else 0.0
    target_pts = half / ctx.point if ctx.point > 0 else 0.0
    sl = clamp_points(stop_pts, ctx.limits.sl_points)
    tp = clamp_points(target_pts, ctx.limits.tp_points)
    hold = clamp_hold(ctx.max_hold_seconds or 0, ctx.max_hold_seconds)
    # ORB does not impose its own hold; keep the global one.
    return sl, tp, ctx.max_hold_seconds if ctx.max_hold_seconds > 0 else 0


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None,
           *, zone_name: str = "Europe/London",
           broken_days: set[str] | None = None) -> Signal:
    policy = policy or Policy()
    feat = compute(ctx, zone_name=zone_name)
    if feat is None:
        return Signal("hold", f"{NAME}: no box or outside the window", NAME)
    base = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in feat.items()}
    if broken_days and feat["day"] in broken_days:
        return Signal("hold", f"{NAME}: first breakout already taken today", NAME, features=base)
    if feat["above"]:
        key, action, side = YES_LONG, "buy", 1
    elif feat["below"]:
        key, action, side = YES_SHORT, "sell", -1
    else:
        return Signal("hold", f"{NAME}: still inside the box", NAME, features=base)

    value = reading(response, key)
    label = band(value, policy)
    answers = {}
    for qid in QUESTION_IDS:
        v = reading(response, qid)
        if v is not None:
            answers[qid] = {"type": "noul", "noul": v}
    if label == "missing":
        return Signal("hold", f"{NAME}: answer missing ({key}); not read as no", NAME,
                      features=base, answers=answers)
    if label == "mid":
        return Signal("hold", f"{NAME}: {key} unsettled ({policy.no:g} to {policy.yes:g})", NAME,
                      features=base, answers=answers)
    if label == "no":
        return Signal("hold", f"{NAME}: {key} no", NAME, features=base, answers=answers)
    if ctx.position and (ctx.position > 0) == (side > 0):
        return Signal("hold", f"{NAME}: already positioned", NAME, features=base, answers=answers)
    sl, tp, hold = _stops(ctx, side, feat)
    return Signal(
        action=action, size=1.0,
        reason=f"{NAME}: {key} >= {policy.yes:g}, close={feat['last_close']:.2f}",
        strategy=NAME, features=base, answers=answers,
        sl_points=sl, tp_points=tp, max_hold_seconds=hold if hold else None,
    )
