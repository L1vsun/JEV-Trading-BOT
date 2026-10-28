"""M15 New York opening-range breakout (09:30 NY, first four M15 bars).

Code builds the OR box and the first breakout close; Jev answers A1–A4.
Entry when A1–A3 are yes and A4 (news chaos) is no.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..bars import Bar
from ..indicators import atr14, local_now
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
    cost_gate,
    entry_signal,
    hold_signal,
    labels_for,
    require_yes_no,
    risk_equity_ok,
    stop_points,
    tp_from_rr,
)
from .mtf_ctx import ctx_allow_short, ctx_limits, ctx_spread_points, ctx_stops_level, mtf_bars

NAME = "m15_ny_orb"
A1 = "m15_ny_a1"
A2 = "m15_ny_a2"
A3 = "m15_ny_a3"
A4 = "m15_ny_a4"
YES_KEYS = (A1, A2, A3)
NO_KEYS = (A4,)
QUESTION_IDS = YES_KEYS + NO_KEYS

NY = "America/New_York"
OR_BARS = 4
OR_START = (9, 30)
OR_WIDTH_ATR = (0.8, 3.0)
BREAK_BUF = 0.1
MIN_BODY = 0.50
TRADE_HOURS_AFTER_OR = 2
FLAT_HM = (16, 30)
TP_RR = 1.5
TF = "M15"


def questions() -> dict[str, Question]:
    return {
        A1: noul(
            "Did the last closed M15 candle in `m15_ny_orb.breakout` close clearly outside the "
            "opening range in `m15_ny_orb.range` in the breakout direction?",
            "The close is clearly outside the OR in the breakout direction.",
            "The close is not a clear break outside the OR in that direction.",
        ),
        A2: noul(
            "Does `m15_ny_orb.breakout` show rising tick activity without long counter-wicks "
            "against the breakout?",
            "Tick activity is rising and counter-wicks are not long.",
            "Tick activity is flat/falling, or there are long counter-wicks against the breakout.",
        ),
        A3: noul(
            "Has price in `m15_ny_orb` not returned back into the opening range after the breakout?",
            "Price has stayed outside the OR since the breakout.",
            "Price has returned into the opening range.",
        ),
        A4: noul(
            "Is the tape in a news-chaos / unreadable state around this NY OR breakout?",
            "News chaos: spreads or tape look unreadable around the breakout.",
            "No news chaos; the breakout tape is readable.",
        ),
    }


def _unix(ctx: MarketContext) -> int:
    now = getattr(ctx, "now", None)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return int(now.replace(tzinfo=timezone.utc).timestamp())
        return int(now.timestamp())
    return int(getattr(ctx, "bar_t", 0) or 0)


def _past_flat(ctx: MarketContext) -> bool:
    local = local_now(_unix(ctx), NY)
    return (local.hour, local.minute) >= FLAT_HM


def _seconds_to_flat(ctx: MarketContext) -> int:
    local = local_now(_unix(ctx), NY)
    target = local.replace(hour=FLAT_HM[0], minute=FLAT_HM[1], second=0, microsecond=0)
    if local >= target:
        return 0
    return max(0, int((target - local).total_seconds()))


def _or_session_start(unix_t: int) -> datetime:
    local = local_now(unix_t, NY)
    start = local.replace(hour=OR_START[0], minute=OR_START[1], second=0, microsecond=0)
    if local < start:
        start = start - timedelta(days=1)
    while start.weekday() >= 5:
        start = start - timedelta(days=1)
    return start


def _body_ok(bar: Bar) -> bool:
    span = bar.span
    if span <= 1e-12:
        return False
    return bar.body / span >= MIN_BODY - 1e-12


def find_setup(ctx: MarketContext, *, traded_days: set[str] | None = None) -> dict | None:
    """First M15 close outside the NY OR within the trade window, or None."""
    if getattr(ctx, "mtf", None) is None:
        return None
    m15 = mtf_bars(ctx, TF)
    if len(m15) < OR_BARS + 14:
        return None
    if _past_flat(ctx):
        return None
    atr = atr14(m15)
    if atr is None or atr <= 0:
        return None
    lim = ctx_limits(ctx)
    if ctx_spread_points(ctx) > float(lim.max_spread_points) + 1e-12:
        return None

    unix_t = _unix(ctx)
    start_local = _or_session_start(unix_t)
    start_utc = int(start_local.astimezone(timezone.utc).timestamp())
    range_end_utc = start_utc + OR_BARS * 900
    window_end_utc = range_end_utc + TRADE_HOURS_AFTER_OR * 3600
    if unix_t < range_end_utc or unix_t >= window_end_utc:
        return None

    day = start_local.date().isoformat()
    if traded_days and day in traded_days:
        return None

    or_bars: list[Bar] = []
    for i in range(OR_BARS):
        want = start_utc + i * 900
        match = [b for b in m15 if b.t == want]
        if not match:
            return None
        or_bars.append(match[-1])
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)
    or_width = or_high - or_low
    if or_width <= 0:
        return None
    ratio = or_width / atr
    if ratio < OR_WIDTH_ATR[0] - 1e-12 or ratio > OR_WIDTH_ATR[1] + 1e-12:
        return None

    buf = BREAK_BUF * atr
    first: Bar | None = None
    side = 0
    for b in m15:
        if b.t < range_end_utc:
            continue
        if b.t >= window_end_utc:
            break
        if not _body_ok(b):
            continue
        if b.close > or_high + buf:
            first, side = b, 1
            break
        if b.close < or_low - buf:
            first, side = b, -1
            break
    if first is None or side == 0:
        return None
    if not ctx_allow_short(ctx) and side < 0:
        return None
    if m15[-1].t != first.t:
        return None

    or_mid = 0.5 * (or_high + or_low)
    return {
        "day": day,
        "side": side,
        "or_high": or_high,
        "or_low": or_low,
        "or_mid": or_mid,
        "or_width": or_width,
        "or_width_atr": ratio,
        "atr": atr,
        "breakout_t": first.t,
        "breakout_close": first.close,
        "breakout_high": first.high,
        "breakout_low": first.low,
        "breakout_open": first.open,
        "breakout_tick_vol": first.tick_vol,
        "range_end": range_end_utc,
        "window_end": window_end_utc,
        "setup_id": f"m15_ny_{'long' if side > 0 else 'short'}_{day}",
    }


def precondition(ctx: MarketContext, *, traded_days: set[str] | None = None, **kw) -> bool:
    if getattr(ctx, "mtf", None) is None:
        return False
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return False
    return find_setup(ctx, traded_days=traded_days) is not None


def state_fields(ctx: MarketContext, *, traded_days: set[str] | None = None, **kw) -> dict:
    setup = find_setup(ctx, traded_days=traded_days)
    if setup is None:
        return {}
    side = "long" if setup["side"] > 0 else "short"
    return {
        "m15_ny_orb": {
            "range": {
                "high": f"{setup['or_high']:.2f}",
                "low": f"{setup['or_low']:.2f}",
                "mid": f"{setup['or_mid']:.2f}",
                "width_atr": f"{setup['or_width_atr']:.2f}",
            },
            "breakout": {
                "side": side,
                "t": setup["breakout_t"],
                "open": f"{setup['breakout_open']:.2f}",
                "high": f"{setup['breakout_high']:.2f}",
                "low": f"{setup['breakout_low']:.2f}",
                "close": f"{setup['breakout_close']:.2f}",
                "tick_vol": f"{setup['breakout_tick_vol']:.0f}",
            },
            "setup_id": setup["setup_id"],
        }
    }


def _risk(ctx: MarketContext, setup: dict) -> tuple[float, float, int] | Signal:
    lim = ctx_limits(ctx)
    point = float(ctx.point)
    atr = float(setup["atr"])
    spread = ctx_spread_points(ctx)
    atr_sl = stop_points(
        tf=TF, atr=atr, point=point, k_sl=float(lim.k_sl.get(TF, 1.5)),
        min_sl=float(lim.min_sl_points.get(TF, 500.0)),
        stops_level=ctx_stops_level(ctx), spread_points=spread,
    )
    entry = float(ctx.ask if setup["side"] > 0 else ctx.bid)
    mid_dist = abs(entry - float(setup["or_mid"])) / point if point > 0 else 0.0
    sl_pts = max(atr_sl, mid_dist)
    ok, why = cost_gate(sl_pts, spread, float(lim.max_cost_share.get(TF, 0.10)))
    if not ok:
        return hold_signal(NAME, f"{NAME}: {why}")
    eq = getattr(ctx, "equity", None)
    lot = getattr(ctx, "lot", None)
    tick_value = getattr(ctx, "tick_value", None)
    tick_size = getattr(ctx, "tick_size", None)
    if eq is not None and lot is not None and tick_value is not None and tick_size is not None:
        ok, why = risk_equity_ok(sl_pts, point, float(tick_value), float(tick_size),
                                 float(lot), float(eq))
        if not ok:
            return hold_signal(NAME, f"{NAME}: {why}")
    tp_pts = tp_from_rr(sl_pts, TP_RR)
    hold = _seconds_to_flat(ctx)
    if hold <= 0:
        return hold_signal(NAME, f"{NAME}: flat after 16:30 NY")
    return sl_pts, tp_pts, hold


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None,
           **kw) -> Signal:
    policy = policy or Policy()
    traded_days = kw.get("traded_days")
    features: dict = {"strategy": NAME}
    if getattr(ctx, "mtf", None) is None:
        return hold_signal(NAME, f"{NAME}: no mtf", features)
    if _past_flat(ctx):
        return hold_signal(NAME, f"{NAME}: flat after 16:30 NY", features)
    setup = find_setup(ctx, traded_days=traded_days)
    if setup is None:
        return hold_signal(NAME, f"{NAME}: no setup", features)
    features.update({
        "setup_id": setup["setup_id"],
        "side": "long" if setup["side"] > 0 else "short",
        "or_high": round(setup["or_high"], 4),
        "or_low": round(setup["or_low"], 4),
        "atr": round(setup["atr"], 4),
        "or_width_atr": round(setup["or_width_atr"], 4),
    })
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return hold_signal(NAME, f"{NAME}: avoid hours", features)

    labels = labels_for(response, QUESTION_IDS, policy)
    answers = {}
    probs = {}
    for key in QUESTION_IDS:
        value = reading(response, key)
        probs[key] = value
        if value is not None:
            answers[key] = {"type": "noul", "noul": value}
    features["nouls"] = probs
    features["labels"] = labels
    blocked = require_yes_no(labels, YES_KEYS, NO_KEYS)
    if blocked:
        return hold_signal(NAME, f"{NAME}: {blocked}", features, answers)

    risk = _risk(ctx, setup)
    if isinstance(risk, Signal):
        risk.features = {**features, **(risk.features or {})}
        risk.answers = answers
        return risk
    sl_pts, tp_pts, hold = risk
    features["sl_points"] = sl_pts
    features["tp_points"] = tp_pts
    if ctx.position and (ctx.position > 0) == (setup["side"] > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features, answers)
    action = "buy" if setup["side"] > 0 else "sell"
    features["table"] = action
    return entry_signal(
        action, NAME, f"{NAME}: {setup['setup_id']} A1-A3 yes A4 no",
        sl_points=sl_pts, tp_points=tp_pts, max_hold_seconds=hold,
        features=features, answers=answers,
    )
