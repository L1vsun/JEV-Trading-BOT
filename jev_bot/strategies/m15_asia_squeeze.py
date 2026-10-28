"""M15 Asia-range squeeze breakout (18:00–03:00 NY).

Code measures Asia width vs daily ATR and the first M15 breakout close;
Jev answers B1–B4. Entry when B1–B3 are yes and B4 (fell back into Asia) is no.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from ..bars import Bar
from ..indicators import atr14, local_now, session_bounds
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
)
from .mtf_ctx import ctx_allow_short, ctx_limits, ctx_spread_points, ctx_stops_level, mtf_bars

NAME = "m15_asia_squeeze"
B1 = "m15_as_b1"
B2 = "m15_as_b2"
B3 = "m15_as_b3"
B4 = "m15_as_b4"
YES_KEYS = (B1, B2, B3)
NO_KEYS = (B4,)
QUESTION_IDS = YES_KEYS + NO_KEYS

NY = "America/New_York"
ASIA_START = (18, 0)
ASIA_END = (3, 0)
MAX_ASIA_ATR_D1 = 0.35
BREAK_BUF = 0.1
ENTRY_HOURS = 4
FLAT_HM = (16, 30)
TP_WIDTH_MULT = 1.5
K_SL = 1.5
TF = "M15"


def questions() -> dict[str, Question]:
    return {
        B1: noul(
            "Has the last closed M15 candle in `m15_asia_squeeze.breakout` left the Asia range "
            "in `m15_asia_squeeze.asia` clearly in the breakout direction?",
            "Price has left the Asia range clearly in the breakout direction.",
            "Price has not clearly left the Asia range in that direction.",
        ),
        B2: noul(
            "Was the opposite side of the Asia range in `m15_asia_squeeze.asia` not tested "
            "before this breakout?",
            "The opposite Asia side was not tested before the breakout.",
            "The opposite Asia side was already tested before the breakout.",
        ),
        B3: noul(
            "Have consecutive M15 candles run in the breakout direction since the London open "
            "in `m15_asia_squeeze`?",
            "Consecutive candles have printed in the breakout direction since London.",
            "Candles since London are mixed or against the breakout.",
        ),
        B4: noul(
            "Has price already fallen back into the Asia range after the breakout "
            "(an exit veto for new entry)?",
            "Price fell back into the Asia range after the breakout.",
            "Price has not fallen back into the Asia range.",
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


def _asia_bounds(unix_t: int) -> tuple[int, int]:
    """Asia 18:00–03:00 NY. After 03:00, return the session that just ended."""
    local = local_now(unix_t, NY)
    # Between 03:00 and 18:00, session_bounds would open the *next* Asia window;
    # probe just before 03:00 so we keep the completed overnight box.
    if ASIA_END <= (local.hour, local.minute) < ASIA_START:
        probe = local.replace(hour=2, minute=59, second=0, microsecond=0)
        return session_bounds(int(probe.timestamp()), NY, ASIA_START, ASIA_END)
    return session_bounds(unix_t, NY, ASIA_START, ASIA_END)


def _atr_d1(ctx: MarketContext) -> float | None:
    d1 = mtf_bars(ctx, "D1")
    if d1:
        atr = atr14(d1)
        if atr is not None and atr > 0:
            return float(atr)
    h1 = mtf_bars(ctx, "H1")
    atr_h1 = atr14(h1)
    if atr_h1 is None or atr_h1 <= 0:
        return None
    return float(atr_h1) * math.sqrt(24.0)


def find_setup(ctx: MarketContext, *, traded_days: set[str] | None = None) -> dict | None:
    if getattr(ctx, "mtf", None) is None:
        return None
    m15 = mtf_bars(ctx, TF)
    if len(m15) < 20:
        return None
    if _past_flat(ctx):
        return None
    atr = atr14(m15)
    if atr is None or atr <= 0:
        return None
    atr_d1 = _atr_d1(ctx)
    if atr_d1 is None or atr_d1 <= 0:
        return None

    lim = ctx_limits(ctx)
    if ctx_spread_points(ctx) > float(lim.max_spread_points) + 1e-12:
        return None

    unix_t = _unix(ctx)
    asia_start, asia_end = _asia_bounds(unix_t)
    if unix_t < asia_end:
        return None
    window_end = asia_end + ENTRY_HOURS * 3600
    if unix_t >= window_end:
        return None

    day = local_now(asia_end, NY).date().isoformat()
    if traded_days and day in traded_days:
        return None

    asia_bars = [b for b in m15 if asia_start <= b.t < asia_end]
    if len(asia_bars) < 8:
        return None
    asia_high = max(b.high for b in asia_bars)
    asia_low = min(b.low for b in asia_bars)
    asia_width = asia_high - asia_low
    if asia_width <= 0:
        return None
    if asia_width / atr_d1 > MAX_ASIA_ATR_D1 + 1e-12:
        return None

    buf = BREAK_BUF * atr
    first: Bar | None = None
    side = 0
    for b in m15:
        if b.t < asia_end:
            continue
        if b.t >= window_end:
            break
        if b.close > asia_high + buf:
            first, side = b, 1
            break
        if b.close < asia_low - buf:
            first, side = b, -1
            break
    if first is None or side == 0:
        return None
    if not ctx_allow_short(ctx) and side < 0:
        return None
    if m15[-1].t != first.t:
        return None

    return {
        "day": day,
        "side": side,
        "asia_high": asia_high,
        "asia_low": asia_low,
        "asia_width": asia_width,
        "asia_width_atr_d1": asia_width / atr_d1,
        "asia_start": asia_start,
        "asia_end": asia_end,
        "window_end": window_end,
        "atr": atr,
        "atr_d1": atr_d1,
        "breakout_t": first.t,
        "breakout_close": first.close,
        "breakout_high": first.high,
        "breakout_low": first.low,
        "breakout_open": first.open,
        "setup_id": f"m15_as_{'long' if side > 0 else 'short'}_{day}",
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
        "m15_asia_squeeze": {
            "asia": {
                "high": f"{setup['asia_high']:.2f}",
                "low": f"{setup['asia_low']:.2f}",
                "width": f"{setup['asia_width']:.2f}",
                "width_atr_d1": f"{setup['asia_width_atr_d1']:.3f}",
            },
            "breakout": {
                "side": side,
                "t": setup["breakout_t"],
                "open": f"{setup['breakout_open']:.2f}",
                "high": f"{setup['breakout_high']:.2f}",
                "low": f"{setup['breakout_low']:.2f}",
                "close": f"{setup['breakout_close']:.2f}",
            },
            "setup_id": setup["setup_id"],
        }
    }


def _risk(ctx: MarketContext, setup: dict) -> tuple[float, float, int] | Signal:
    lim = ctx_limits(ctx)
    point = float(ctx.point)
    if point <= 0:
        return hold_signal(NAME, f"{NAME}: bad point")
    atr = float(setup["atr"])
    spread = ctx_spread_points(ctx)
    atr_sl = stop_points(
        tf=TF, atr=atr, point=point, k_sl=K_SL,
        min_sl=float(lim.min_sl_points.get(TF, 500.0)),
        stops_level=ctx_stops_level(ctx), spread_points=spread,
    )
    entry = float(ctx.ask if setup["side"] > 0 else ctx.bid)
    if setup["side"] > 0:
        opp_dist = (entry - float(setup["asia_low"])) / point
    else:
        opp_dist = (float(setup["asia_high"]) - entry) / point
    sl_pts = max(atr_sl, opp_dist)
    tp_pts = TP_WIDTH_MULT * float(setup["asia_width"]) / point
    if tp_pts <= 0 or sl_pts <= 0:
        return hold_signal(NAME, f"{NAME}: bad sl/tp")
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
        "asia_high": round(setup["asia_high"], 4),
        "asia_low": round(setup["asia_low"], 4),
        "atr": round(setup["atr"], 4),
        "asia_width_atr_d1": round(setup["asia_width_atr_d1"], 4),
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
        action, NAME, f"{NAME}: {setup['setup_id']} B1-B3 yes B4 no",
        sl_points=sl_pts, tp_points=tp_pts, max_hold_seconds=hold,
        features=features, answers=answers,
    )
