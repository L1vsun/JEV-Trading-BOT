"""H1 previous-day breakout of the New York calendar day high/low.

Prior day high/low are taken from closed H1 bars whose open falls on the previous
``America/New_York`` calendar date (bar opens mapped to NY local date). The first
H1 close beyond that level on the current NY day is the only entry; later retests
are ignored.

SL 1.5×ATR, TP 2R, max_bars 100. BUY when B1 B2 B4 yes and B3 no; SELL is the
mirror when ``ctx.allow_short``.
"""
from __future__ import annotations

from ..indicators import atr14, local_now
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
    atr_or_hold,
    cost_gate,
    entry_signal,
    hold_override_for,
    hold_seconds_for,
    hold_signal,
    labels_for,
    require_yes_no,
    risk_equity_ok,
    stop_points,
    tp_from_rr,
)
from .mtf_ctx import (
    ctx_allow_short,
    ctx_limits,
    ctx_spread_points,
    ctx_stops_level,
    mtf_bars,
)

NAME = "h1_prevday_break"
TF = "H1"
B1 = "h1_pd_b1"
B2 = "h1_pd_b2"
B3 = "h1_pd_b3"
B4 = "h1_pd_b4"
KEYS = (B1, B2, B3, B4)
K_SL = 2.0  # H1 default ATR multiple; min SL floor is still 1000 pts
MAX_BARS = 100
NY = "America/New_York"


def questions() -> dict[str, Question]:
    return {
        B1: noul(
            "Is the break of the prior NY day level in `h1_prevday_break.levels` a genuine "
            "breakout (close beyond the level), not a wick poke?",
            "The close is a genuine breakout beyond the prior day level.",
            "The move is only a wick poke or not a clear breakout.",
        ),
        B2: noul(
            "Does H1 order flow / follow-through in `h1_prevday_break.follow` support the "
            "breakout direction?",
            "Follow-through supports the breakout direction.",
            "Follow-through does not support the breakout.",
        ),
        B3: noul(
            "Is this a late or exhausted break in `h1_prevday_break.timing` "
            "(already extended far beyond the level)?",
            "The break is late or exhausted.",
            "The break is timely, not exhausted.",
        ),
        B4: noul(
            "Is this the first break of the prior NY day high/low today in "
            "`h1_prevday_break.levels`?",
            "This is the first break of that level today.",
            "This is not the first break today.",
        ),
    }


def _bars(ctx: MarketContext):
    return mtf_bars(ctx, TF)


def _allow_short(ctx: MarketContext) -> bool:
    return ctx_allow_short(ctx)


def _ny_date(unix_t: int):
    return local_now(int(unix_t), NY).date()


def compute(ctx: MarketContext) -> dict | None:
    bars = _bars(ctx)
    if len(bars) < 48:
        return None
    last = bars[-1]
    today = _ny_date(last.t)
    # Prior NY calendar day with H1 bars.
    by_day: dict = {}
    for b in bars:
        d = _ny_date(b.t)
        by_day.setdefault(d, []).append(b)
    prior_dates = sorted(d for d in by_day if d < today)
    if not prior_dates:
        return None
    prior = prior_dates[-1]
    prior_bars = by_day[prior]
    day_high = max(b.high for b in prior_bars)
    day_low = min(b.low for b in prior_bars)
    today_bars = by_day.get(today, [])
    if not today_bars:
        return None

    first_up = None
    first_dn = None
    for b in today_bars:
        if first_up is None and b.close > day_high:
            first_up = b
        if first_dn is None and b.close < day_low:
            first_dn = b

    long_setup = first_up is not None and first_up.t == last.t
    short_setup = first_dn is not None and first_dn.t == last.t and _allow_short(ctx)
    # Prefer long if both somehow fire on the same bar (rare).
    side = 1 if long_setup else -1 if short_setup else 0
    level = day_high if side > 0 else day_low if side < 0 else None
    extension = abs(last.close - level) if level is not None else 0.0
    atr = atr14(bars)
    return {
        "prior_date": prior.isoformat(),
        "today": today.isoformat(),
        "day_high": day_high,
        "day_low": day_low,
        "first_up_t": first_up.t if first_up else None,
        "first_dn_t": first_dn.t if first_dn else None,
        "long_setup": long_setup,
        "short_setup": short_setup,
        "side": side,
        "level": level,
        "extension": extension,
        "close": last.close,
        "last_t": last.t,
        "atr": atr,
        "is_first": side != 0,
    }


def precondition(ctx: MarketContext) -> bool:
    feat = compute(ctx)
    return bool(feat and feat.get("side"))


def state_fields(ctx: MarketContext) -> dict:
    feat = compute(ctx) or {}
    return {
        NAME: {
            "levels": {
                "prior_date": feat.get("prior_date"),
                "today": feat.get("today"),
                "day_high": f"{feat.get('day_high', 0):.2f}",
                "day_low": f"{feat.get('day_low', 0):.2f}",
                "close": f"{feat.get('close', 0):.2f}",
                "first_break": bool(feat.get("is_first")),
                "proposed_side": (
                    "long" if feat.get("side", 0) > 0 else "short" if feat.get("side", 0) < 0 else "none"
                ),
            },
            "follow": {
                "extension": f"{feat.get('extension', 0):.2f}",
                "atr": f"{feat.get('atr', 0) or 0:.2f}",
            },
            "timing": {
                "extension_vs_atr": (
                    f"{(feat['extension'] / feat['atr']):.2f}"
                    if feat.get("atr") and feat.get("extension") is not None
                    else "n/a"
                ),
            },
        }
    }


def _answers(response: Response) -> dict:
    out = {}
    for key in KEYS:
        value = reading(response, key)
        if value is not None:
            out[key] = {"type": "noul", "noul": value}
    return out


def _size(ctx: MarketContext, atr: float) -> tuple[float, float, int] | tuple[None, str]:
    limits = ctx_limits(ctx)
    spread = ctx_spread_points(ctx)
    sl = stop_points(
        tf=TF, atr=atr, point=ctx.point, k_sl=K_SL,
        min_sl=float(limits.min_sl_points.get(TF, 1000.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=spread,
    )
    ok, why = cost_gate(sl, spread, float(limits.max_cost_share.get(TF, 0.07)))
    if not ok:
        return None, why
    ok, why = risk_equity_ok(
        sl, ctx.point,
        float(getattr(ctx, "tick_value", 0.0) or 0.0),
        float(getattr(ctx, "tick_size", 0.0) or 0.0),
        float(getattr(ctx, "lot", 0.01) or 0.01),
        getattr(ctx, "equity", None),
    )
    if not ok:
        return None, why
    tp = tp_from_rr(sl, 2.0)
    hold = hold_seconds_for(
        TF, MAX_BARS, int(getattr(limits, "global_max_hold", 900) or 900),
        override=hold_override_for(NAME, limits=limits, ctx=ctx),
    )
    return sl, tp, hold


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    policy = policy or Policy()
    features: dict = {"strategy": NAME}
    answers = _answers(response)
    feat = compute(ctx)
    if feat is None:
        return hold_signal(NAME, f"{NAME}: insufficient H1 history", features, answers)
    side = int(feat.get("side") or 0)
    features.update({
        "prior_date": feat.get("prior_date"),
        "day_high": feat.get("day_high"),
        "day_low": feat.get("day_low"),
        "side": "long" if side > 0 else "short" if side < 0 else "none",
        "is_first": feat.get("is_first"),
    })
    if side == 0:
        return hold_signal(NAME, f"{NAME}: no first break on last H1", features, answers)
    if ctx.position and (ctx.position > 0) == (side > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features, answers)

    labels = labels_for(response, KEYS, policy)
    features["labels"] = labels
    features["nouls"] = {k: reading(response, k) for k in KEYS}
    # BUY B1 B2 B4 yes B3 no
    why = require_yes_no(labels, (B1, B2, B4), (B3,))
    if why:
        return hold_signal(NAME, f"{NAME}: {why}", features, answers)

    atr, hold = atr_or_hold(_bars(ctx), NAME)
    if hold is not None:
        hold.features.update(features)
        hold.answers = answers
        return hold
    sized = _size(ctx, atr)
    if sized[0] is None:
        return hold_signal(NAME, f"{NAME}: {sized[1]}", features, answers)
    sl, tp, max_hold = sized
    features.update({"atr": round(atr, 4), "sl_points": sl, "tp_points": tp, "max_bars": MAX_BARS})
    action = "buy" if side > 0 else "sell"
    features["table"] = action
    return entry_signal(
        action, NAME, f"{NAME}: {action} B1 B2 B4 yes B3 no",
        sl_points=sl, tp_points=tp, max_hold_seconds=max_hold,
        features=features, answers=answers,
    )
