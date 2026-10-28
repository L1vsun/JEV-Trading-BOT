"""M5 New York opening-range breakout (09:30–10:00 America/New_York).

Code builds the ORB range and the first close outside it; Jev answers J1–J4
for that side only. One trade per session; no new entries after 17:00 UTC;
time stop at 21:00 UTC via ``max_hold_seconds``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, clamp_points, noul, reading, require_zone
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

NAME = "m5_ny_orb"
TF = "M5"
ZONE = "America/New_York"
RANGE_START = (9, 30)
RANGE_END = (10, 0)
NO_ENTRY_UTC_HOUR = 17
TIME_STOP_UTC_HOUR = 21
RR = 2.0

J1_LONG = "m5_ny_j1"
J2_LONG = "m5_ny_j2"
J3_LONG = "m5_ny_j3"
J4_LONG = "m5_ny_j4"
J1_SHORT = "m5_ny_j1_short"
J2_SHORT = "m5_ny_j2_short"
J3_SHORT = "m5_ny_j3_short"
J4_SHORT = "m5_ny_j4_short"

LONG_KEYS = (J1_LONG, J2_LONG, J3_LONG, J4_LONG)
SHORT_KEYS = (J1_SHORT, J2_SHORT, J3_SHORT, J4_SHORT)


def questions_for_side(side: int) -> dict[str, Question]:
    if side > 0:
        return {
            J1_LONG: noul(
                "Does the breakout candle in `m5_ny_orb.breakout` show a strong bullish body that "
                "closes clearly above the opening-range high in `m5_ny_orb.range`?",
                "Strong bullish body closing clearly above the range high.",
                "The close is weak, mixed, or not clearly above the range high.",
            ),
            J2_LONG: noul(
                "Has price already traded back inside the opening range in `m5_ny_orb.range` "
                "after this breakout?",
                "Price is already back inside the range.",
                "Price has not traded back inside the range.",
            ),
            J3_LONG: noul(
                "Has today's opening range in `m5_ny_orb.range` already been broken on both sides?",
                "The range has already been broken both above and below today.",
                "The range has not been broken on both sides today.",
            ),
            J4_LONG: noul(
                "Is there important US economic data in the current New York window that makes "
                "this ORB breakout unreliable?",
                "Important US data falls in this window.",
                "No important US data is scheduled in this window.",
            ),
        }
    return {
        J1_SHORT: noul(
            "Does the breakout candle in `m5_ny_orb.breakout` show a strong bearish body that "
            "closes clearly below the opening-range low in `m5_ny_orb.range`?",
            "Strong bearish body closing clearly below the range low.",
            "The close is weak, mixed, or not clearly below the range low.",
        ),
        J2_SHORT: noul(
            "Has price already traded back inside the opening range in `m5_ny_orb.range` "
            "after this breakout?",
            "Price is already back inside the range.",
            "Price has not traded back inside the range.",
        ),
        J3_SHORT: noul(
            "Has today's opening range in `m5_ny_orb.range` already been broken on both sides?",
            "The range has already been broken both above and below today.",
            "The range has not been broken on both sides today.",
        ),
        J4_SHORT: noul(
            "Is there important US economic data in the current New York window that makes "
            "this ORB breakout unreliable?",
            "Important US data falls in this window.",
            "No important US data is scheduled in this window.",
        ),
    }


def questions() -> dict[str, Question]:
    out = {}
    out.update(questions_for_side(1))
    out.update(questions_for_side(-1))
    return out


@dataclass(frozen=True)
class NyOrbSetup:
    side: int
    session_id: str
    high: float
    low: float
    width: float
    breakout_t: int
    breakout_close: float
    atr: float


def _session_window(now: datetime) -> tuple[datetime, datetime, str] | None:
    """Return (range_start_utc, range_end_utc, session_id) for the current NY cash day."""
    zone = require_zone(ZONE)
    utc = as_utc(now)
    local = utc.astimezone(zone)
    start_local = local.replace(
        hour=RANGE_START[0], minute=RANGE_START[1], second=0, microsecond=0,
    )
    end_local = local.replace(
        hour=RANGE_END[0], minute=RANGE_END[1], second=0, microsecond=0,
    )
    # Before today's range start, use the previous weekday's ORB.
    if local < start_local:
        start_local -= timedelta(days=1)
        end_local -= timedelta(days=1)
    while start_local.weekday() >= 5:
        start_local -= timedelta(days=1)
        end_local -= timedelta(days=1)
    session_id = f"m5_ny_orb:{start_local.date().isoformat()}"
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        session_id,
    )


def find_setup(ctx: MarketContext, *, spent: set[str] | None = None) -> NyOrbSetup | None:
    if getattr(ctx, "mtf", None) is None:
        return None
    m5 = mtf_bars(ctx, TF)
    if len(m5) < 20:
        return None
    atr, _ = atr_or_hold(m5, NAME)
    if atr is None:
        return None
    window = _session_window(ctx.now)
    if window is None:
        return None
    range_start, range_end, session_id = window
    if spent and session_id in spent:
        return None
    utc = as_utc(ctx.now)
    # No entry at or after 17:00 UTC.
    if utc >= utc.replace(hour=NO_ENTRY_UTC_HOUR, minute=0, second=0, microsecond=0):
        return None
    if utc < range_end:
        return None

    start_unix = int(range_start.timestamp())
    end_unix = int(range_end.timestamp())
    box = [b for b in m5 if start_unix <= b.t < end_unix]
    # Six M5 opens in 09:30–10:00; require at least half if history is thin.
    if len(box) < 3:
        return None
    high = max(b.high for b in box)
    low = min(b.low for b in box)
    if high <= low:
        return None

    post = [b for b in m5 if b.t >= end_unix]
    if not post:
        return None
    first = None
    for b in post:
        if b.close > high or b.close < low:
            first = b
            break
    if first is None or first.t != post[-1].t:
        # Only the first M5 close outside the range.
        return None
    if first.close > high:
        side = 1
    elif first.close < low:
        side = -1
    else:
        return None
    if side < 0 and not ctx_allow_short(ctx):
        return None
    return NyOrbSetup(
        side=side,
        session_id=session_id,
        high=high,
        low=low,
        width=high - low,
        breakout_t=first.t,
        breakout_close=first.close,
        atr=atr,
    )


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
        "m5_ny_orb": {
            "range": {
                "high": f"{setup.high:.2f}",
                "low": f"{setup.low:.2f}",
                "width": f"{setup.width:.2f}",
            },
            "breakout": {
                "t": setup.breakout_t,
                "close": f"{setup.breakout_close:.2f}",
                "side": "long" if setup.side > 0 else "short",
            },
            "session_id": setup.session_id,
            "atr": f"{setup.atr:.2f}",
        }
    }


def _risk(ctx: MarketContext, setup: NyOrbSetup) -> tuple[float, float, int, str] | Signal:
    limits = ctx_limits(ctx)
    spread = ctx_spread_points(ctx)
    if spread > limits.max_spread_points + 1e-9:
        return hold_signal(NAME, f"{NAME}: spread {spread:.0f}pts", features={"spread": spread})
    floor = stop_points(
        tf=TF,
        atr=setup.atr,
        point=ctx.point,
        k_sl=float(limits.k_sl.get(TF, 1.5)),
        min_sl=float(limits.min_sl_points.get(TF, 300.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=spread,
    )
    # SL = max(range width, 1.5*ATR) then broker/min floors via stop_points.
    width_pts = setup.width / ctx.point if ctx.point > 0 else 0.0
    sl_pts = max(width_pts, floor)
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
    sl_pts = clamp_points(sl_pts, ctx.limits.sl_points)
    tp_pts = clamp_points(tp_pts, ctx.limits.tp_points)
    hold = seconds_until(ctx.now, TIME_STOP_UTC_HOUR)
    if hold <= 0:
        return hold_signal(NAME, f"{NAME}: past {TIME_STOP_UTC_HOUR:02d}:00 UTC time stop")
    # Session time stop is an explicit override of the global bar-hold cap.
    return sl_pts, tp_pts, hold, "ok"


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None,
           *, spent: set[str] | None = None, **kw) -> Signal:
    del kw  # accept extras from routers without failing
    policy = policy or Policy()
    features: dict = {"strategy": NAME}
    if getattr(ctx, "mtf", None) is None:
        return hold_signal(NAME, f"{NAME}: no mtf", features=features)
    setup = find_setup(ctx, spent=spent)
    if setup is None:
        return hold_signal(NAME, f"{NAME}: no setup", features=features)
    features.update({
        "session_id": setup.session_id,
        "side": "long" if setup.side > 0 else "short",
        "high": round(setup.high, 4),
        "low": round(setup.low, 4),
        "width": round(setup.width, 4),
        "atr": round(setup.atr, 4),
        "breakout_t": setup.breakout_t,
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
    why = require_yes_no(labels, (keys[0],), keys[1:])
    if why:
        return hold_signal(NAME, f"{NAME}: {why}", features=features, answers=answers)

    risk = _risk(ctx, setup)
    if isinstance(risk, Signal):
        risk.features.update(features)
        risk.answers = answers
        return risk
    sl_pts, tp_pts, hold, _ = risk
    features["sl_points"] = sl_pts
    features["tp_points"] = tp_pts
    if ctx.position and (ctx.position > 0) == (setup.side > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features=features, answers=answers)
    action = "buy" if setup.side > 0 else "sell"
    return entry_signal(
        action, NAME, f"{NAME}: {setup.session_id} J1 yes J2-J4 no",
        sl_points=sl_pts, tp_points=tp_pts, max_hold_seconds=hold,
        features=features, answers=answers,
    )
