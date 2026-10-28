"""London opening-range breakout with two TypeSafe confirmation Nouls per side.

Flag ``--sig-orb`` (default off). The box is the first 30 minutes from the
London open on ``Europe/London``. Optional New York open uses the same shape
on ``America/New_York`` when ``session=\"ny\"``.
Both Nouls on the active side must be yes (≥0.7); mid / missing / no → hold.
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
YES_LONG_BODY = "orb_break_up_body"
YES_LONG_FOLLOW = "orb_break_up_follow"
YES_SHORT_BODY = "orb_break_down_body"
YES_SHORT_FOLLOW = "orb_break_down_follow"
LONG_KEYS = (YES_LONG_BODY, YES_LONG_FOLLOW)
SHORT_KEYS = (YES_SHORT_BODY, YES_SHORT_FOLLOW)
QUESTION_IDS = LONG_KEYS + SHORT_KEYS
BOX_MINUTES = 30
TRADE_WINDOW_HOURS = 3


def questions() -> dict[str, Question]:
    return {
        YES_LONG_BODY: noul(
            "Did the last closed minute in `orb` close clearly above the opening-range high?",
            "The close is clearly above the box high.",
            "The close is not a clear break above the box high.",
        ),
        YES_LONG_FOLLOW: noul(
            "Does `order_flow.aggressor_tape_30s_windows_oldest_first` support buying "
            "after the opening-range break higher?",
            "The aggressor tape favours buying after the upside break.",
            "The tape does not support buying after the upside break.",
        ),
        YES_SHORT_BODY: noul(
            "Did the last closed minute in `orb` close clearly below the opening-range low?",
            "The close is clearly below the box low.",
            "The close is not a clear break below the box low.",
        ),
        YES_SHORT_FOLLOW: noul(
            "Does `order_flow.aggressor_tape_30s_windows_oldest_first` support selling "
            "after the opening-range break lower?",
            "The aggressor tape favours selling after the downside break.",
            "The tape does not support selling after the downside break.",
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


def _both_yes(
    response: Response, keys: tuple[str, ...], policy: Policy,
) -> tuple[str | None, dict]:
    """Return ``(hold_reason, answers)``. ``hold_reason`` is None when both are yes."""
    answers: dict = {}
    for qid in QUESTION_IDS:
        value = reading(response, qid)
        if value is not None:
            answers[qid] = {"type": "noul", "noul": value}
    for key in keys:
        label = band(reading(response, key), policy)
        if label == "missing":
            return f"{NAME}: answer missing ({key}); not read as no", answers
        if label == "mid":
            return (
                f"{NAME}: {key} unsettled ({policy.no:g} to {policy.yes:g})",
                answers,
            )
        if label == "no":
            return f"{NAME}: {key} no", answers
    return None, answers


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
        keys, action, side = LONG_KEYS, "buy", 1
    elif feat["below"]:
        keys, action, side = SHORT_KEYS, "sell", -1
    else:
        return Signal("hold", f"{NAME}: still inside the box", NAME, features=base)

    hold_reason, answers = _both_yes(response, keys, policy)
    if hold_reason is not None:
        return Signal("hold", hold_reason, NAME, features=base, answers=answers)
    if ctx.position and (ctx.position > 0) == (side > 0):
        return Signal("hold", f"{NAME}: already positioned", NAME, features=base, answers=answers)
    sl, tp, hold = _stops(ctx, side, feat)
    joined = " & ".join(keys)
    return Signal(
        action=action, size=1.0,
        reason=f"{NAME}: {joined} >= {policy.yes:g}, close={feat['last_close']:.2f}",
        strategy=NAME, features=base, answers=answers,
        sl_points=sl, tp_points=tp, max_hold_seconds=hold if hold else None,
    )
