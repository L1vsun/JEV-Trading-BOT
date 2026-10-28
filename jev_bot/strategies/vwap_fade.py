"""VWAP mean-reversion fade with two TypeSafe confirmation Nouls per side.

Flag ``--sig-vwap-fade`` (default off). Code computes session VWAP, sigma and
the cost gate; Jev only answers when that gate and the z-threshold pass.
Both Nouls on the active side must be yes (≥0.7); mid / missing / no → hold.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from ..risk import require_new_york
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
    roundtrip_cost,
)

NAME = "vwap_fade"
YES_LONG_FLOW = "vwap_fade_long_flow"
YES_LONG_LOCATION = "vwap_fade_long_location"
YES_SHORT_FLOW = "vwap_fade_short_flow"
YES_SHORT_LOCATION = "vwap_fade_short_location"
LONG_KEYS = (YES_LONG_FLOW, YES_LONG_LOCATION)
SHORT_KEYS = (YES_SHORT_FLOW, YES_SHORT_LOCATION)
QUESTION_IDS = LONG_KEYS + SHORT_KEYS


def questions() -> dict[str, Question]:
    return {
        YES_LONG_FLOW: noul(
            "After a stretch under the lower VWAP band in `vwap_fade`, has selling pressure eased "
            "in `order_flow.last_1_minute`?",
            "Selling has eased; one-minute flow is balanced or buying after trading below the lower band.",
            "Selling is still in control on the one-minute flow.",
        ),
        YES_LONG_LOCATION: noul(
            "After a stretch under the lower VWAP band in `vwap_fade`, has the forming minute "
            "turned up in `forming_candle.so_far`?",
            "The forming minute is turning up after trading below the lower band.",
            "The forming minute is not turning up.",
        ),
        YES_SHORT_FLOW: noul(
            "After a stretch above the upper VWAP band in `vwap_fade`, has buying pressure eased "
            "in `order_flow.last_1_minute`?",
            "Buying has eased; one-minute flow is balanced or selling after trading above the upper band.",
            "Buying is still in control on the one-minute flow.",
        ),
        YES_SHORT_LOCATION: noul(
            "After a stretch above the upper VWAP band in `vwap_fade`, has the forming minute "
            "turned down in `forming_candle.so_far`?",
            "The forming minute is turning down after trading above the upper band.",
            "The forming minute is not turning down.",
        ),
    }


def _session_start_unix(now: datetime) -> int:
    """Unix second of the current gold-session open (17:05 America/New_York)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny = require_new_york()
    local = now.astimezone(ny)
    open_at = local.replace(hour=17, minute=5, second=0, microsecond=0)
    # Before today's 17:05 NY, the open is yesterday's 17:05 (or Sunday's when
    # the week just started). Gold is closed Saturday; Sunday before 17:05 has
    # no open yet.
    if local < open_at:
        from datetime import timedelta
        open_at = open_at - timedelta(days=1)
    # Skip Saturday open (does not exist) and Friday-after-close leftovers.
    while open_at.weekday() == 5:
        from datetime import timedelta
        open_at = open_at - timedelta(days=1)
    return int(open_at.astimezone(timezone.utc).timestamp())


def session_vwap(times: list[int], mids: list[float], volumes: list[float],
                 start_unix: int) -> tuple[float, float, float] | None:
    """Return ``(vwap, sigma, z)`` for bars at or after ``start_unix``, or None."""
    w_sum = 0.0
    px_sum = 0.0
    pts: list[tuple[float, float]] = []
    for t, mid, vol in zip(times, mids, volumes):
        if t < start_unix:
            continue
        w = max(float(vol), 1e-9)
        w_sum += w
        px_sum += w * mid
        pts.append((mid, w))
    if w_sum <= 0 or len(pts) < 30:
        return None
    vwap = px_sum / w_sum
    var = sum(w * (p - vwap) ** 2 for p, w in pts) / w_sum
    sigma = math.sqrt(max(var, 0.0))
    if sigma <= 1e-12:
        return None
    z = (pts[-1][0] - vwap) / sigma
    return vwap, sigma, z


def compute(ctx: MarketContext) -> dict | None:
    """Feature snapshot, or None when the setup cannot be judged yet."""
    try:
        start = _session_start_unix(ctx.now)
    except Exception:
        return None
    vols = [max(b + s, 0.0) for b, s in zip(ctx.buy_vol, ctx.sell_vol)]
    # Prefer tick volume; fall back to unit weight so a quiet second still counts.
    vols = [v if v > 0 else 1.0 for v in vols]
    stats = session_vwap(ctx.times, ctx.mids, vols, start)
    if stats is None:
        return None
    vwap, sigma, z = stats
    rt = roundtrip_cost(ctx.bid, ctx.ask, ctx.limits.deviation_points, ctx.point)
    dist = abs(ctx.mid - vwap)
    cost_ok = dist >= 4.0 * rt and sigma >= 3.0 * rt
    return {
        "vwap": vwap,
        "sigma": sigma,
        "z": z,
        "roundtrip": rt,
        "distance": dist,
        "cost_ok": cost_ok,
        "session_start": start,
    }


def precondition(ctx: MarketContext) -> bool:
    feat = compute(ctx)
    if feat is None or not feat["cost_ok"]:
        return False
    return feat["z"] <= -1.0 or feat["z"] >= 1.0


def state_fields(ctx: MarketContext) -> dict:
    feat = compute(ctx) or {}
    return {
        "vwap_fade": {
            "z": f"{feat.get('z', 0):+.2f}",
            "distance_vs_roundtrip": (
                "at least four round-trips" if feat.get("cost_ok") else "inside the cost band"
            ),
            "band": (
                "below the lower band" if feat.get("z", 0) <= -1
                else "above the upper band" if feat.get("z", 0) >= 1
                else "inside the bands"
            ),
        }
    }


def _stops(ctx: MarketContext, side: int, feat: dict) -> tuple[float, float, int]:
    sigma_pts = feat["sigma"] / ctx.point if ctx.point > 0 else 0.0
    dist_pts = abs(ctx.mid - feat["vwap"]) / ctx.point if ctx.point > 0 else 0.0
    sl = clamp_points(1.5 * sigma_pts, ctx.limits.sl_points)
    tp = clamp_points(dist_pts, ctx.limits.tp_points)
    hold = clamp_hold(600, ctx.max_hold_seconds)
    return sl, tp, hold


def _both_yes(
    response: Response, keys: tuple[str, ...], policy: Policy,
) -> tuple[str | None, dict]:
    """Return ``(hold_reason, answers)``. ``hold_reason`` is None when both are yes."""
    answers: dict = {}
    for key in QUESTION_IDS:
        value = reading(response, key)
        if value is not None:
            answers[key] = {"type": "noul", "noul": value}
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


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    policy = policy or Policy()
    feat = compute(ctx) or {}
    base = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in feat.items()}
    if not feat or not feat.get("cost_ok"):
        return Signal("hold", f"{NAME}: cost gate blocked", NAME, features=base)
    z = feat["z"]
    if z <= -1.0:
        keys = LONG_KEYS
        side = 1
        action = "buy"
    elif z >= 1.0:
        keys = SHORT_KEYS
        side = -1
        action = "sell"
    else:
        return Signal("hold", f"{NAME}: |z| < 1", NAME, features=base)

    hold_reason, answers = _both_yes(response, keys, policy)
    if hold_reason is not None:
        return Signal("hold", hold_reason, NAME, features=base, answers=answers)
    if ctx.position and (ctx.position > 0) == (side > 0):
        return Signal("hold", f"{NAME}: already positioned", NAME, features=base, answers=answers)
    sl, tp, hold = _stops(ctx, side, feat)
    joined = " & ".join(keys)
    return Signal(
        action=action, size=1.0,
        reason=f"{NAME}: {joined} >= {policy.yes:g}, z={z:+.2f}, cost_ok",
        strategy=NAME, features=base, answers=answers,
        sl_points=sl, tp_points=tp, max_hold_seconds=hold,
    )
