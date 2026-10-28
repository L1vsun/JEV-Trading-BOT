"""M5 session-VWAP pullback continuation (12:30–16:30 UTC).

Requires price on one side of session VWAP for ≥45 minutes and ≥1.5 ATR away,
then a pullback that touches VWAP (max 1 USD pierce), closes back on the trend
side, and entry on a break of that rejection candle's extreme. Only the 1st or
2nd pullback is traded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..bars import Bar
from ..indicators import session_vwap
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
    atr_or_hold,
    cost_gate,
    entry_signal,
    hold_signal,
    labels_for,
    require_yes_no,
    risk_equity_ok,
    stop_points,
    tp_from_rr,
)
from .mtf_ctx import (
    as_utc,
    ctx_allow_short,
    ctx_limits,
    ctx_spread_points,
    ctx_stops_level,
    mtf_bars,
    seconds_until,
)

NAME = "m5_vwap_pb"
TF = "M5"
WINDOW_START = (12, 30)
WINDOW_END = (16, 30)
MIN_SIDE_SECONDS = 45 * 60
MAX_PIERCE = 1.0  # USD
RR = 2.0
MAX_PULLBACKS = 2

J1_LONG = "m5_vwap_j1"
J2_LONG = "m5_vwap_j2"
J3_LONG = "m5_vwap_j3"
J1_SHORT = "m5_vwap_j1_short"
J2_SHORT = "m5_vwap_j2_short"
J3_SHORT = "m5_vwap_j3_short"

LONG_KEYS = (J1_LONG, J2_LONG, J3_LONG)
SHORT_KEYS = (J1_SHORT, J2_SHORT, J3_SHORT)


def questions_for_side(side: int) -> dict[str, Question]:
    if side > 0:
        return {
            J1_LONG: noul(
                "Is today a clear uptrend day relative to session VWAP in `m5_vwap_pb.trend` "
                "(price holding the bid side of VWAP with directional follow-through)?",
                "Clear uptrend day vs session VWAP.",
                "Not a clear uptrend day vs session VWAP.",
            ),
            J2_LONG: noul(
                "Did the pullback in `m5_vwap_pb.pullback` reject at session VWAP "
                "(touch / shallow pierce, then close back above)?",
                "Pullback rejected at VWAP and closed back on the trend side.",
                "Pullback did not clearly reject at VWAP.",
            ),
            J3_LONG: noul(
                "Did this pullback break bullish structure (a lower low vs the prior swing) "
                "as noted in `m5_vwap_pb.structure`?",
                "Pullback broke structure with a lower low.",
                "Pullback did not break structure.",
            ),
        }
    return {
        J1_SHORT: noul(
            "Is today a clear downtrend day relative to session VWAP in `m5_vwap_pb.trend` "
            "(price holding the offer side of VWAP with directional follow-through)?",
            "Clear downtrend day vs session VWAP.",
            "Not a clear downtrend day vs session VWAP.",
        ),
        J2_SHORT: noul(
            "Did the pullback in `m5_vwap_pb.pullback` reject at session VWAP "
            "(touch / shallow pierce, then close back below)?",
            "Pullback rejected at VWAP and closed back on the trend side.",
            "Pullback did not clearly reject at VWAP.",
        ),
        J3_SHORT: noul(
            "Did this pullback break bearish structure (a higher high vs the prior swing) "
            "as noted in `m5_vwap_pb.structure`?",
            "Pullback broke structure with a higher high.",
            "Pullback did not break structure.",
        ),
    }


def questions() -> dict[str, Question]:
    out = {}
    out.update(questions_for_side(1))
    out.update(questions_for_side(-1))
    return out


@dataclass(frozen=True)
class VwapPbSetup:
    side: int
    setup_id: str
    vwap: float
    atr: float
    pullback_n: int
    reject_t: int
    reject_high: float
    reject_low: float
    reject_close: float
    extreme: float  # stop reference (pullback extreme)
    trigger: float
    side_minutes: float
    dist_atr: float


def _session_start_unix(now: datetime) -> int:
    """UTC midnight of the current UTC day (session VWAP anchor for this module)."""
    utc = as_utc(now)
    start = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _window_bounds(now: datetime) -> tuple[int, int] | None:
    utc = as_utc(now)
    start = utc.replace(
        hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0,
    )
    end = utc.replace(
        hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0,
    )
    if utc < start:
        return None
    if utc >= end:
        return None
    return int(start.timestamp()), int(end.timestamp())


def _find_pullbacks(bars: list[Bar], *, side: int, vwap: float, atr: float,
                    window_start: int) -> list[dict]:
    """Detect completed VWAP pullback rejections in ``bars`` after ``window_start``.

    A pullback: after being extended on the trend side, a bar touches VWAP
    (pierce ≤ 1 USD), then a later bar closes back on the trend side (rejection).
    """
    if atr <= 0 or vwap <= 0:
        return []
    events: list[dict] = []
    i = 0
    n = len(bars)
    while i < n:
        b = bars[i]
        if b.t < window_start:
            i += 1
            continue
        # Require extension before counting a touch as a pullback.
        if side > 0:
            extended = b.close >= vwap + 1.5 * atr or b.high >= vwap + 1.5 * atr
        else:
            extended = b.close <= vwap - 1.5 * atr or b.low <= vwap - 1.5 * atr
        if not extended:
            i += 1
            continue
        # Scan forward for a VWAP touch / shallow pierce, then a rejection close.
        j = i
        touch = None
        while j < n:
            c = bars[j]
            if side > 0:
                # From above: low reaches VWAP, may pierce by at most MAX_PIERCE.
                touched = c.low <= vwap + 1e-9 and c.low >= vwap - MAX_PIERCE
            else:
                touched = c.high >= vwap - 1e-9 and c.high <= vwap + MAX_PIERCE
            if touched:
                touch = c
                break
            # If price ran much further away without touching, keep scanning a bit;
            # if it closed through VWAP deeply, abandon this leg.
            if side > 0 and c.close < vwap - MAX_PIERCE:
                touch = None
                break
            if side < 0 and c.close > vwap + MAX_PIERCE:
                touch = None
                break
            j += 1
        if touch is None:
            i += 1
            continue
        # Rejection: first bar at/after touch that closes back on trend side.
        reject = None
        extreme = touch.low if side > 0 else touch.high
        for k in range(j, n):
            c = bars[k]
            if side > 0:
                extreme = min(extreme, c.low)
                if c.close > vwap:
                    reject = c
                    break
                if c.low < vwap - MAX_PIERCE:
                    break
            else:
                extreme = max(extreme, c.high)
                if c.close < vwap:
                    reject = c
                    break
                if c.high > vwap + MAX_PIERCE:
                    break
        if reject is None:
            i = j + 1
            continue
        events.append({
            "touch_t": touch.t,
            "reject_t": reject.t,
            "reject_high": reject.high,
            "reject_low": reject.low,
            "reject_close": reject.close,
            "extreme": extreme,
            "trigger": reject.high if side > 0 else reject.low,
        })
        # Continue after this rejection to find the next pullback.
        i = next((idx for idx, b in enumerate(bars) if b.t > reject.t), n)
    return events


def _side_duration_seconds(bars: list[Bar], *, side: int, vwap: float,
                           through_t: int) -> float:
    """How long closes have been continuously on ``side`` of VWAP ending at ``through_t``."""
    on_side: list[Bar] = []
    for b in reversed(bars):
        if b.t > through_t:
            continue
        ok = (b.close >= vwap) if side > 0 else (b.close <= vwap)
        if not ok:
            break
        on_side.append(b)
    if len(on_side) < 2:
        return 0.0
    on_side.reverse()
    return float(on_side[-1].t - on_side[0].t)


def find_setup(ctx: MarketContext, *, spent: set[str] | None = None) -> VwapPbSetup | None:
    if getattr(ctx, "mtf", None) is None:
        return None
    bounds = _window_bounds(ctx.now)
    if bounds is None:
        return None
    window_start, _window_end = bounds
    m5 = mtf_bars(ctx, TF)
    if len(m5) < 20:
        return None
    atr, _ = atr_or_hold(m5, NAME)
    if atr is None:
        return None
    sess_start = _session_start_unix(ctx.now)
    vwap = session_vwap(m5, start_t=sess_start)
    if vwap is None:
        return None

    # Need ≥45 minutes of history on the session VWAP before trading pullbacks.
    sess_bars = [b for b in m5 if b.t >= sess_start]
    if not sess_bars or (sess_bars[-1].t - sess_bars[0].t) < MIN_SIDE_SECONDS:
        return None

    dist = abs(ctx.mid - vwap)
    if dist + 1e-12 < 1.5 * atr and abs(m5[-1].close - vwap) + 1e-12 < 1.5 * atr:
        # Still allow if we are in an active pullback near VWAP after having been extended;
        # extension is checked inside pullback detection via historical bars.
        pass

    candidates: list[VwapPbSetup] = []
    sides = (1, -1) if ctx_allow_short(ctx) else (1,)
    for side in sides:
        dur = _side_duration_seconds(m5, side=side, vwap=vwap, through_t=m5[-1].t)
        # Trend side must have been established for ≥45 min at some point in the window;
        # use max run from bars since window_start / session.
        if dur < MIN_SIDE_SECONDS:
            # Also accept if earlier in the window the side was held for 45m and we are
            # now in a pullback (duration may dip during the touch). Check pre-window run.
            pre = [b for b in m5 if b.t < window_start]
            if pre:
                dur_pre = _side_duration_seconds(pre + m5, side=side, vwap=vwap,
                                                 through_t=window_start)
            else:
                dur_pre = 0.0
            # Peak extension since session:
            if side > 0:
                peak = max((b.high for b in sess_bars), default=ctx.mid)
                max_dist = peak - vwap
            else:
                trough = min((b.low for b in sess_bars), default=ctx.mid)
                max_dist = vwap - trough
            if dur_pre < MIN_SIDE_SECONDS and dur < MIN_SIDE_SECONDS:
                continue
            if max_dist + 1e-12 < 1.5 * atr:
                continue
        else:
            if side > 0:
                peak = max((b.high for b in sess_bars), default=ctx.mid)
                max_dist = peak - vwap
            else:
                trough = min((b.low for b in sess_bars), default=ctx.mid)
                max_dist = vwap - trough
            if max_dist + 1e-12 < 1.5 * atr:
                continue

        events = _find_pullbacks(m5, side=side, vwap=vwap, atr=atr, window_start=window_start)
        if not events:
            continue
        # Only 1st or 2nd pullback.
        for n, ev in enumerate(events[:MAX_PULLBACKS], start=1):
            setup_id = f"m5_vwap_pb:{side}:{ev['reject_t']}"
            if spent and setup_id in spent:
                continue
            # Entry on break of rejection extreme: last closed bar broke it, or mid through it.
            trigger = ev["trigger"]
            later = [b for b in m5 if b.t > ev["reject_t"]]
            if side > 0:
                broken = any(b.high >= trigger for b in later) or ctx.mid >= trigger
            else:
                broken = any(b.low <= trigger for b in later) or ctx.mid <= trigger
            if not broken:
                # Also allow entry on the rejection bar itself if it closed beyond its open
                # mid already through — still require a break confirmation via mid/ask.
                if not later and (
                    (side > 0 and ctx.mid >= trigger) or (side < 0 and ctx.mid <= trigger)
                ):
                    broken = True
            if not broken:
                continue
            # Stale: more than a few M5 bars after rejection without us trading.
            if later and later[-1].t - ev["reject_t"] > 3 * 300:
                continue
            candidates.append(VwapPbSetup(
                side=side,
                setup_id=setup_id,
                vwap=vwap,
                atr=atr,
                pullback_n=n,
                reject_t=ev["reject_t"],
                reject_high=ev["reject_high"],
                reject_low=ev["reject_low"],
                reject_close=ev["reject_close"],
                extreme=ev["extreme"],
                trigger=trigger,
                side_minutes=max(dur, MIN_SIDE_SECONDS) / 60.0,
                dist_atr=max_dist / atr if atr > 0 else 0.0,
            ))
        # Prefer the latest active pullback for this side.
    if not candidates:
        return None
    # Most recent rejection wins.
    candidates.sort(key=lambda s: s.reject_t, reverse=True)
    return candidates[0]


def precondition(ctx: MarketContext, *, spent: set[str] | None = None) -> bool:
    if getattr(ctx, "mtf", None) is None:
        return False
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return False
    return find_setup(ctx, spent=spent) is not None


def state_fields(ctx: MarketContext, *, spent: set[str] | None = None) -> dict:
    setup = find_setup(ctx, spent=spent)
    if setup is None:
        return {}
    return {
        "m5_vwap_pb": {
            "trend": {
                "side": "long" if setup.side > 0 else "short",
                "vwap": f"{setup.vwap:.2f}",
                "side_minutes": f"{setup.side_minutes:.0f}",
                "extension_atr": f"{setup.dist_atr:.2f}",
            },
            "pullback": {
                "n": setup.pullback_n,
                "reject_t": setup.reject_t,
                "high": f"{setup.reject_high:.2f}",
                "low": f"{setup.reject_low:.2f}",
                "close": f"{setup.reject_close:.2f}",
                "trigger": f"{setup.trigger:.2f}",
            },
            "structure": {
                "extreme": f"{setup.extreme:.2f}",
                "note": (
                    "watch for lower low vs prior swing" if setup.side > 0
                    else "watch for higher high vs prior swing"
                ),
            },
            "setup_id": setup.setup_id,
            "atr": f"{setup.atr:.2f}",
        }
    }


def _risk(ctx: MarketContext, setup: VwapPbSetup) -> tuple[float, float, int] | Signal:
    limits = ctx_limits(ctx)
    spread = ctx_spread_points(ctx)
    if spread > limits.max_spread_points + 1e-9:
        return hold_signal(NAME, f"{NAME}: spread {spread:.0f}pts", features={"spread": spread})
    entry = ctx.ask if setup.side > 0 else ctx.bid
    cushion = max(1.0, 0.5 * setup.atr)
    if setup.side > 0:
        sl_price = setup.extreme - cushion
        dist = entry - sl_price
    else:
        sl_price = setup.extreme + cushion
        dist = sl_price - entry
    if dist <= 0:
        return hold_signal(NAME, f"{NAME}: non-positive stop distance")
    structural = dist / ctx.point if ctx.point > 0 else 0.0
    floor = stop_points(
        tf=TF,
        atr=setup.atr,
        point=ctx.point,
        k_sl=float(limits.k_sl.get(TF, 1.5)),
        min_sl=float(limits.min_sl_points.get(TF, 300.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=spread,
    )
    # SL = pullback extreme ± max(1 USD, 0.5 ATR), then max with min SL via stop_points.
    sl_pts = max(structural, floor)
    ok, why = cost_gate(sl_pts, spread, float(limits.max_cost_share.get(TF, 0.10)))
    if not ok:
        return hold_signal(NAME, f"{NAME}: {why}")
    eq = getattr(ctx, "equity", None)
    lot = getattr(ctx, "lot", None)
    tick_value = getattr(ctx, "tick_value", None)
    tick_size = getattr(ctx, "tick_size", None)
    if eq is not None and lot is not None and tick_value is not None and tick_size is not None:
        ok, why = risk_equity_ok(sl_pts, ctx.point, float(tick_value), float(tick_size),
                                 float(lot), float(eq))
        if not ok:
            return hold_signal(NAME, f"{NAME}: {why}")
    tp_pts = tp_from_rr(sl_pts, RR)
    # Global --sl-points/--tp-points (200/400) apply only to M1 / 1-s strategies.
    hold = seconds_until(ctx.now, WINDOW_END[0], WINDOW_END[1])
    if hold <= 0:
        return hold_signal(NAME, f"{NAME}: past {WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} UTC")
    # Window end is the session time stop (explicit hold override).
    return sl_pts, tp_pts, hold


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None,
           *, spent: set[str] | None = None, **kw) -> Signal:
    del kw
    policy = policy or Policy()
    features: dict = {"strategy": NAME}
    if getattr(ctx, "mtf", None) is None:
        return hold_signal(NAME, f"{NAME}: no mtf", features=features)
    setup = find_setup(ctx, spent=spent)
    if setup is None:
        return hold_signal(NAME, f"{NAME}: no setup", features=features)
    features.update({
        "setup_id": setup.setup_id,
        "side": "long" if setup.side > 0 else "short",
        "vwap": round(setup.vwap, 4),
        "atr": round(setup.atr, 4),
        "pullback_n": setup.pullback_n,
        "trigger": round(setup.trigger, 4),
        "extreme": round(setup.extreme, 4),
    })
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return hold_signal(NAME, f"{NAME}: avoid hours", features=features)

    keys = LONG_KEYS if setup.side > 0 else SHORT_KEYS
    labels = labels_for(response, keys, policy)
    answers = {}
    for k in keys:
        v = reading(response, k)
        if v is not None:
            answers[k] = {"type": "noul", "noul": v}
    features["labels"] = labels
    # Buy/sell: J1 yes, J2 yes, J3 no.
    why = require_yes_no(labels, (keys[0], keys[1]), (keys[2],))
    if why:
        return hold_signal(NAME, f"{NAME}: {why}", features=features, answers=answers)

    risk = _risk(ctx, setup)
    if isinstance(risk, Signal):
        risk.features.update(features)
        risk.answers = answers
        return risk
    sl_pts, tp_pts, hold = risk
    features["sl_points"] = sl_pts
    features["tp_points"] = tp_pts
    if ctx.position and (ctx.position > 0) == (setup.side > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features=features, answers=answers)
    action = "buy" if setup.side > 0 else "sell"
    return entry_signal(
        action, NAME, f"{NAME}: {setup.setup_id} J1-J2 yes J3 no",
        sl_points=sl_pts, tp_points=tp_pts, max_hold_seconds=hold,
        features=features, answers=answers,
    )
