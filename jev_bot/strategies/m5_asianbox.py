"""M5 Asian-box breakout (box 00:00–07:00 UTC, trade 07:00–10:00 UTC).

Box hours are overridable via ``ctx.asian_box = (start_h, end_h)``. Code finds
the first close outside the box in the breakout window; Jev answers J1–J3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

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

NAME = "m5_asianbox"
TF = "M5"
DEFAULT_BOX = (0, 7)
BREAKOUT_END_HOUR = 10
RR = 2.0

J1_LONG = "m5_ab_j1"
J2_LONG = "m5_ab_j2"
J3_LONG = "m5_ab_j3"
J1_SHORT = "m5_ab_j1_short"
J2_SHORT = "m5_ab_j2_short"
J3_SHORT = "m5_ab_j3_short"

LONG_KEYS = (J1_LONG, J2_LONG, J3_LONG)
SHORT_KEYS = (J1_SHORT, J2_SHORT, J3_SHORT)


def questions_for_side(side: int) -> dict[str, Question]:
    if side > 0:
        return {
            J1_LONG: noul(
                "Does the breakout candle in `m5_asianbox.breakout` show a strong bullish body "
                "that closes clearly above the Asian box high in `m5_asianbox.box`?",
                "Strong bullish body closing clearly above the box high.",
                "The close is weak, mixed, or not clearly above the box high.",
            ),
            J2_LONG: noul(
                "Has price already traded back inside the Asian box in `m5_asianbox.box` "
                "after this breakout?",
                "Price is already back inside the box.",
                "Price has not traded back inside the box.",
            ),
            J3_LONG: noul(
                "Does the breakout candle in `m5_asianbox.breakout` look like a liquidity sweep "
                "(long wick, small body) rather than a genuine breakout?",
                "Long wick with a small body — a liquidity sweep.",
                "Not a liquidity-sweep shape; body supports a genuine breakout.",
            ),
        }
    return {
        J1_SHORT: noul(
            "Does the breakout candle in `m5_asianbox.breakout` show a strong bearish body "
            "that closes clearly below the Asian box low in `m5_asianbox.box`?",
            "Strong bearish body closing clearly below the box low.",
            "The close is weak, mixed, or not clearly below the box low.",
        ),
        J2_SHORT: noul(
            "Has price already traded back inside the Asian box in `m5_asianbox.box` "
            "after this breakout?",
            "Price is already back inside the box.",
            "Price has not traded back inside the box.",
        ),
        J3_SHORT: noul(
            "Does the breakout candle in `m5_asianbox.breakout` look like a liquidity sweep "
            "(long wick, small body) rather than a genuine breakout?",
            "Long wick with a small body — a liquidity sweep.",
            "Not a liquidity-sweep shape; body supports a genuine breakout.",
        ),
    }


def questions() -> dict[str, Question]:
    out = {}
    out.update(questions_for_side(1))
    out.update(questions_for_side(-1))
    return out


@dataclass(frozen=True)
class AsianBoxSetup:
    side: int
    session_id: str
    high: float
    low: float
    box_start_h: int
    box_end_h: int
    breakout_t: int
    breakout_close: float
    atr: float


def _box_hours(ctx: MarketContext) -> tuple[int, int]:
    raw = getattr(ctx, "asian_box", None)
    if raw is None:
        return DEFAULT_BOX
    try:
        start_h, end_h = int(raw[0]), int(raw[1])
    except (TypeError, ValueError, IndexError):
        return DEFAULT_BOX
    if not (0 <= start_h < end_h <= 23):
        return DEFAULT_BOX
    return start_h, end_h


def _windows(now: datetime, start_h: int, end_h: int) -> tuple[int, int, int, str]:
    """Box start/end and breakout-window end as UTC unix, plus session id."""
    utc = as_utc(now)
    box_start = utc.replace(hour=start_h, minute=0, second=0, microsecond=0)
    box_end = utc.replace(hour=end_h, minute=0, second=0, microsecond=0)
    brk_end = utc.replace(hour=BREAKOUT_END_HOUR, minute=0, second=0, microsecond=0)
    # Before today's box start, roll back one day.
    if utc < box_start:
        box_start -= timedelta(days=1)
        box_end -= timedelta(days=1)
        brk_end -= timedelta(days=1)
    # If we are past today's breakout window, still describe today's session (setup will be None).
    if end_h > BREAKOUT_END_HOUR:
        # atypical override; keep breakout end after box end by at least 1h if possible
        brk_end = max(brk_end, box_end + timedelta(hours=1))
    session_id = f"m5_asianbox:{box_start.date().isoformat()}"
    return (
        int(box_start.timestamp()),
        int(box_end.timestamp()),
        int(brk_end.timestamp()),
        session_id,
    )


def find_setup(ctx: MarketContext, *, spent: set[str] | None = None) -> AsianBoxSetup | None:
    if getattr(ctx, "mtf", None) is None:
        return None
    m5 = mtf_bars(ctx, TF)
    if len(m5) < 20:
        return None
    atr, _ = atr_or_hold(m5, NAME)
    if atr is None:
        return None
    start_h, end_h = _box_hours(ctx)
    box_start, box_end, brk_end, session_id = _windows(ctx.now, start_h, end_h)
    if spent and session_id in spent:
        return None
    now_unix = int(as_utc(ctx.now).timestamp())
    if now_unix < box_end or now_unix >= brk_end:
        return None

    box = [b for b in m5 if box_start <= b.t < box_end]
    # 00:00–07:00 → 84 M5 bars; require a usable fraction.
    if len(box) < 12:
        return None
    high = max(b.high for b in box)
    low = min(b.low for b in box)
    if high <= low:
        return None

    post = [b for b in m5 if box_end <= b.t < brk_end]
    if not post:
        return None
    first = None
    for b in post:
        if b.close > high or b.close < low:
            first = b
            break
    if first is None or first.t != post[-1].t:
        return None
    if first.close > high:
        side = 1
    elif first.close < low:
        side = -1
    else:
        return None
    if side < 0 and not ctx_allow_short(ctx):
        return None
    return AsianBoxSetup(
        side=side,
        session_id=session_id,
        high=high,
        low=low,
        box_start_h=start_h,
        box_end_h=end_h,
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
        "m5_asianbox": {
            "box": {
                "high": f"{setup.high:.2f}",
                "low": f"{setup.low:.2f}",
                "hours_utc": f"{setup.box_start_h:02d}:00-{setup.box_end_h:02d}:00",
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


def _risk(ctx: MarketContext, setup: AsianBoxSetup) -> tuple[float, float, int] | Signal:
    limits = ctx_limits(ctx)
    spread = ctx_spread_points(ctx)
    if spread > limits.max_spread_points + 1e-9:
        return hold_signal(NAME, f"{NAME}: spread {spread:.0f}pts", features={"spread": spread})
    entry = ctx.ask if setup.side > 0 else ctx.bid
    if setup.side > 0:
        opp = max(0.0, entry - setup.low)
    else:
        opp = max(0.0, setup.high - entry)
    floor = stop_points(
        tf=TF,
        atr=setup.atr,
        point=ctx.point,
        k_sl=float(limits.k_sl.get(TF, 1.5)),
        min_sl=float(limits.min_sl_points.get(TF, 300.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=spread,
    )
    # SL = max(opposite side of box, 1.5*ATR) with min/broker floors via stop_points.
    opp_pts = opp / ctx.point if ctx.point > 0 else 0.0
    sl_pts = max(opp_pts, floor)
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
    hold = seconds_until(ctx.now, BREAKOUT_END_HOUR)
    if hold <= 0:
        return hold_signal(NAME, f"{NAME}: past session end {BREAKOUT_END_HOUR:02d}:00 UTC")
    # Breakout-window end is the session time stop (explicit hold override).
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
        "session_id": setup.session_id,
        "side": "long" if setup.side > 0 else "short",
        "high": round(setup.high, 4),
        "low": round(setup.low, 4),
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
    sl_pts, tp_pts, hold = risk
    features["sl_points"] = sl_pts
    features["tp_points"] = tp_pts
    if ctx.position and (ctx.position > 0) == (setup.side > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features=features, answers=answers)
    action = "buy" if setup.side > 0 else "sell"
    return entry_signal(
        action, NAME, f"{NAME}: {setup.session_id} J1 yes J2-J3 no",
        sl_points=sl_pts, tp_points=tp_pts, max_hold_seconds=hold,
        features=features, answers=answers,
    )
