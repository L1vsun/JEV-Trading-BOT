"""VWAP mean-reversion fade with a TypeSafe confirmation Noul.

Flag ``--sig-vwap-fade`` (default off). Code computes session VWAP, sigma and
the cost gate; Jev only answers when that gate and the z-threshold pass.
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
YES_LONG = "vwap_fade_long"
YES_SHORT = "vwap_fade_short"
QUESTION_IDS = (YES_LONG, YES_SHORT)


def questions() -> dict[str, Question]:
    return {
        YES_LONG: noul(
            "After a stretch under the lower VWAP band in `vwap_fade`, has selling pressure eased "
            "and has the forming minute turned up, reading `order_flow.last_1_minute` and "
            "`forming_candle.so_far`?",
            "Selling has eased and the forming minute is turning up after trading below the lower band.",
            "Selling is still in control, or the forming minute is not turning up.",
        ),
        YES_SHORT: noul(
            "After a stretch above the upper VWAP band in `vwap_fade`, has buying pressure eased "
            "and has the forming minute turned down, reading `order_flow.last_1_minute` and "
            "`forming_candle.so_far`?",
            "Buying has eased and the forming minute is turning down after trading above the upper band.",
            "Buying is still in control, or the forming minute is not turning down.",
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


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    policy = policy or Policy()
    feat = compute(ctx) or {}
    base = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in feat.items()}
    if not feat or not feat.get("cost_ok"):
        return Signal("hold", f"{NAME}: cost gate blocked", NAME, features=base)
    z = feat["z"]
    if z <= -1.0:
        key = YES_LONG
        side = 1
        action = "buy"
    elif z >= 1.0:
        key = YES_SHORT
        side = -1
        action = "sell"
    else:
        return Signal("hold", f"{NAME}: |z| < 1", NAME, features=base)

    value = reading(response, key)
    label = band(value, policy)
    answers = {key: {"type": "noul", "noul": value} for key in QUESTION_IDS if reading(response, key) is not None}
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
        reason=f"{NAME}: {key} >= {policy.yes:g}, z={z:+.2f}, cost_ok",
        strategy=NAME, features=base, answers=answers,
        sl_points=sl, tp_points=tp, max_hold_seconds=hold,
    )
