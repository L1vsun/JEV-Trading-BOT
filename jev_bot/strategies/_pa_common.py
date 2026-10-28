"""Shared price-action building blocks: M5 candles, ATR, fractals, zones.

All functions are deterministic. They take closed bars and return numbers the
strategies feed into their truth tables. No TypeSafe calls live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .common import clamp_hold, clamp_points, require_zone
from .minutes import MinuteBar, closed_minutes

POINT_HINT = 0.01  # XAUUSD default; callers pass ctx.point


@dataclass(frozen=True)
class Zone:
    """A horizontal level the price can reject from."""

    price: float
    kind: str          # prev_day_high | prev_day_low | asia_high | asia_low | swing | round
    side: str          # support | resistance


@dataclass(frozen=True)
class PinSetup:
    side: int          # +1 long, -1 short
    pin_t: int
    pin_high: float
    pin_low: float
    pin_close: float
    zone: Zone
    atr: float
    setup_id: str
    trigger: float     # price that must be broken by +5 points
    expires_t: int     # unix open of the last M5 bar still allowed to trigger


@dataclass(frozen=True)
class InsideSetup:
    side: int
    mother_t: int
    inside_t: int
    mother_high: float
    mother_low: float
    mother_close: float
    atr: float
    setup_id: str
    trigger: float
    expires_t: int


def closed_m5(minutes: list[MinuteBar]) -> list[MinuteBar]:
    """Aggregate closed M1 bars into closed M5 bars. The forming M5 is dropped."""
    if not minutes:
        return []
    buckets: dict[int, list[MinuteBar]] = {}
    for bar in minutes:
        key = (int(bar.t) // 300) * 300
        buckets.setdefault(key, []).append(bar)
    if len(buckets) < 2:
        return []
    out: list[MinuteBar] = []
    for key in sorted(buckets)[:-1]:
        rows = buckets[key]
        out.append(MinuteBar(
            t=key,
            open=rows[0].open,
            high=max(r.high for r in rows),
            low=min(r.low for r in rows),
            close=rows[-1].close,
            buy_vol=sum(r.buy_vol for r in rows),
            sell_vol=sum(r.sell_vol for r in rows),
        ))
    return out


def atr14(bars: list[MinuteBar], period: int = 14) -> float | None:
    """Wilder ATR on closed bars. Needs ``period + 1`` bars."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return float(atr)


def fractal_swings(bars: list[MinuteBar], left: int = 2, right: int = 2) -> list[tuple[int, float, str]]:
    """Return ``(index, price, 'high'|'low')`` for 2/2 fractals. Confirmed only."""
    out: list[tuple[int, float, str]] = []
    n = len(bars)
    for i in range(left, n - right):
        h = bars[i].high
        l = bars[i].low
        if all(h > bars[j].high for j in range(i - left, i)) and all(h > bars[j].high for j in range(i + 1, i + right + 1)):
            out.append((i, h, "high"))
        if all(l < bars[j].low for j in range(i - left, i)) and all(l < bars[j].low for j in range(i + 1, i + right + 1)):
            out.append((i, l, "low"))
    return out


def cluster_levels(levels: list[float], atr: float, tol_atr: float = 0.25) -> list[float]:
    """Merge nearby levels within ``tol_atr * atr`` into their mean."""
    if not levels or atr <= 0:
        return list(levels)
    tol = tol_atr * atr
    ordered = sorted(levels)
    groups: list[list[float]] = [[ordered[0]]]
    for px in ordered[1:]:
        if abs(px - groups[-1][-1]) <= tol:
            groups[-1].append(px)
        else:
            groups.append([px])
    return [sum(g) / len(g) for g in groups]


def round_levels(mid: float, step_small: float = 5.0, step_large: float = 10.0,
                 count: int = 3) -> list[float]:
    """Nearest $5 and $10 round numbers around ``mid``."""
    out: set[float] = set()
    for step in (step_small, step_large):
        base = round(mid / step) * step
        for k in range(-count, count + 1):
            out.add(base + k * step)
    return sorted(out)


def _day_bounds_ny(now: datetime, days_back: int = 1) -> tuple[int, int]:
    """Unix range for a New York calendar day ``days_back`` days before ``now``."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny = require_zone("America/New_York")
    local = now.astimezone(ny)
    day = (local.date() - timedelta(days=days_back))
    start = datetime(day.year, day.month, day.day, tzinfo=ny)
    end = start + timedelta(days=1)
    return int(start.astimezone(timezone.utc).timestamp()), int(end.astimezone(timezone.utc).timestamp())


def _asia_bounds(now: datetime) -> tuple[int, int]:
    """Most recent completed Asia session: 09:00–15:00 Asia/Tokyo."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tokyo = require_zone("Asia/Tokyo")
    local = now.astimezone(tokyo)
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    end = local.replace(hour=15, minute=0, second=0, microsecond=0)
    if local < end:
        start -= timedelta(days=1)
        end -= timedelta(days=1)
    while start.weekday() >= 5:
        start -= timedelta(days=1)
        end -= timedelta(days=1)
    return int(start.astimezone(timezone.utc).timestamp()), int(end.astimezone(timezone.utc).timestamp())


def build_zones(m5: list[MinuteBar], now: datetime, atr: float, mid: float) -> list[Zone]:
    """Previous-day H/L, Asia H/L, clustered fractal swings, round numbers."""
    zones: list[Zone] = []
    if not m5 or atr <= 0:
        return zones
    d0, d1 = _day_bounds_ny(now, 1)
    day_bars = [b for b in m5 if d0 <= b.t < d1]
    if day_bars:
        zones.append(Zone(max(b.high for b in day_bars), "prev_day_high", "resistance"))
        zones.append(Zone(min(b.low for b in day_bars), "prev_day_low", "support"))
    a0, a1 = _asia_bounds(now)
    asia = [b for b in m5 if a0 <= b.t < a1]
    if asia:
        zones.append(Zone(max(b.high for b in asia), "asia_high", "resistance"))
        zones.append(Zone(min(b.low for b in asia), "asia_low", "support"))
    swings = fractal_swings(m5)
    highs = cluster_levels([p for _, p, k in swings if k == "high"], atr)
    lows = cluster_levels([p for _, p, k in swings if k == "low"], atr)
    for px in highs:
        zones.append(Zone(px, "swing", "resistance"))
    for px in lows:
        zones.append(Zone(px, "swing", "support"))
    for px in round_levels(mid):
        side = "support" if px <= mid else "resistance"
        zones.append(Zone(px, "round", side))
    return zones


def near_zone(price: float, zones: list[Zone], atr: float, *,
              want: str, max_beyond_atr: float = 0.2) -> Zone | None:
    """Closest zone of the wanted side within ``max_beyond_atr`` of ``price``.

    Round-number levels may act as either support or resistance. A shallow
    pierce beyond the level still counts as in/behind the zone.
    """
    best: Zone | None = None
    best_d = float("inf")
    for z in zones:
        if z.kind != "round" and z.side != want:
            continue
        if abs(price - z.price) > max_beyond_atr * atr:
            continue
        d = abs(price - z.price)
        if d < best_d:
            best_d = d
            best = z
    return best


def body_range(bar: MinuteBar) -> tuple[float, float, float, float]:
    """Return ``(body, span, upper_wick, lower_wick)``."""
    body = abs(bar.close - bar.open)
    span = max(bar.high - bar.low, 1e-12)
    upper = bar.high - max(bar.open, bar.close)
    lower = min(bar.open, bar.close) - bar.low
    return body, span, upper, lower


def points(distance: float, point: float) -> float:
    if point <= 0:
        return 0.0
    return distance / point


def spread_points(bid: float, ask: float, point: float) -> float:
    return points(max(0.0, ask - bid), point)


def next_opposing_zone(mid: float, side: int, zones: list[Zone]) -> Zone | None:
    """Nearest zone against the trade: resistance above a long, support below a short."""
    want = "resistance" if side > 0 else "support"
    candidates = []
    for z in zones:
        if side > 0 and z.price > mid:
            candidates.append(z)
        elif side < 0 and z.price < mid:
            candidates.append(z)
    if not candidates:
        return None
    if side > 0:
        return min(candidates, key=lambda z: z.price)
    return max(candidates, key=lambda z: z.price)


def risk_reward(side: int, entry: float, extreme: float, atr: float, zones: list[Zone],
                point: float, bid: float, ask: float, global_sl: float, global_tp: float,
                global_hold: int) -> dict | None:
    """Compute SL/TP in points with the PA window and cost gate.

    Returns None when the setup must hold (SL outside 120–200, cost gate, …).
    """
    raw_sl = abs(entry - extreme) + 0.1 * atr
    sl_pts = points(raw_sl, point)
    if not (120.0 <= sl_pts <= 200.0):
        return {"ok": False, "reason": f"sl window {sl_pts:.0f}pts not in 120-200", "sl_points": sl_pts}
    opp = next_opposing_zone(entry, side, zones)
    if opp is None:
        tp_dist = global_tp * point if global_tp > 0 else 400 * point
    else:
        tp_dist = max(0.0, abs(opp.price - entry) - 10 * point)
    tp_pts = points(tp_dist, point)
    if global_tp > 0:
        tp_pts = min(tp_pts, global_tp)
    else:
        tp_pts = min(tp_pts, 400.0)
    spr = spread_points(bid, ask, point)
    need = max(104.0, 1.5 * sl_pts, 4.0 * (spr + 6.0))
    if tp_pts + 1e-9 < need:
        return {
            "ok": False,
            "reason": f"cost gate tp={tp_pts:.0f} < need={need:.0f}",
            "sl_points": sl_pts,
            "tp_points": tp_pts,
            "need_tp": need,
        }
    sl_c = clamp_points(sl_pts, global_sl if global_sl > 0 else 200.0)
    tp_c = clamp_points(tp_pts, global_tp if global_tp > 0 else 400.0)
    # If clamping would violate the cost gate, hold.
    if tp_c + 1e-9 < need:
        return {"ok": False, "reason": f"cost gate after clamp tp={tp_c:.0f}", "sl_points": sl_c, "tp_points": tp_c}
    hold = clamp_hold(900, global_hold if global_hold > 0 else 900)
    return {
        "ok": True,
        "sl_points": sl_c,
        "tp_points": tp_c,
        "max_hold_seconds": hold,
        "need_tp": need,
        "spread_points": spr,
    }


def structure_swings(m5: list[MinuteBar]) -> dict | None:
    """Last two swing highs and lows for HH/HL vs LH/LL."""
    swings = fractal_swings(m5)
    highs = [(i, p) for i, p, k in swings if k == "high"]
    lows = [(i, p) for i, p, k in swings if k == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    return {
        "hh": h2[1] > h1[1],
        "hl": l2[1] > l1[1],
        "lh": h2[1] < h1[1],
        "ll": l2[1] < l1[1],
        "highs": (h1[1], h2[1]),
        "lows": (l1[1], l2[1]),
    }


def liquidity_ok(response, policy) -> tuple[bool, str]:
    """Existing spread/volatility Nouls must read as no."""
    from .common import band, reading
    for key in ("spread_wide", "volatility_violent"):
        label = band(reading(response, key), policy)
        if label == "missing":
            return False, f"{key} missing; not read as no"
        if label == "mid":
            return False, f"{key} unsettled"
        if label == "yes":
            return False, f"{key} yes"
    return True, "liquidity clear"


def liquidity_questions():
    """Reuse the consensus wording for the two liquidity vetoes."""
    from ..questions import decision_questions
    qs = decision_questions()
    return {k: qs[k] for k in ("spread_wide", "volatility_violent")}
