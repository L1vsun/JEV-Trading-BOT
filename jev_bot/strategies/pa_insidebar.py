"""M5 inside-bar breakout in the mother-candle direction.

Flag ``--sig-pa-insidebar`` (default off).
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pa_common as pa
from .common import MarketContext, Policy, Signal, band, noul, reading

NAME = "pa_insidebar"

I1_LONG = "pa_ib_i1_long"
I2_LONG = "pa_ib_i2_long"
I3_LONG = "pa_ib_i3_long"
I4_LONG = "pa_ib_i4_long"
I1_SHORT = "pa_ib_i1_short"
I2_SHORT = "pa_ib_i2_short"
I3_SHORT = "pa_ib_i3_short"
I4_SHORT = "pa_ib_i4_short"


def questions_for_side(side: int) -> dict[str, Question]:
    if side > 0:
        return {
            I1_LONG: noul(
                "Is the mother candle in `pa_insidebar.mother` a strong bullish candle in the direction "
                "of the current M5 structure in `pa_insidebar.structure`?",
                "Strong bullish mother candle aligned with the M5 structure.",
                "The mother candle is weak, mixed, or not aligned with the structure.",
            ),
            I2_LONG: noul(
                "Is the inside candle in `pa_insidebar.inside` a quiet pause without a counter-impulse?",
                "Quiet inside bar with no meaningful sell impulse.",
                "The inside bar shows a counter-impulse or is not quiet.",
            ),
            I3_LONG: noul(
                "Does `order_flow.last_1_minute` run in the breakout direction (buying)?",
                "One-minute flow is light, moderate or heavy buying.",
                "One-minute flow is balanced or selling.",
            ),
            I4_LONG: noul(
                "Is there clear resistance in `pa_insidebar.overhead` sitting directly above the breakout?",
                "A clear resistance sits directly above the breakout.",
                "No clear resistance sits directly above the breakout.",
            ),
        }
    return {
        I1_SHORT: noul(
            "Is the mother candle in `pa_insidebar.mother` a strong bearish candle in the direction "
            "of the current M5 structure in `pa_insidebar.structure`?",
            "Strong bearish mother candle aligned with the M5 structure.",
            "The mother candle is weak, mixed, or not aligned with the structure.",
        ),
        I2_SHORT: noul(
            "Is the inside candle in `pa_insidebar.inside` a quiet pause without a counter-impulse?",
            "Quiet inside bar with no meaningful buy impulse.",
            "The inside bar shows a counter-impulse or is not quiet.",
        ),
        I3_SHORT: noul(
            "Does `order_flow.last_1_minute` run in the breakout direction (selling)?",
            "One-minute flow is light, moderate or heavy selling.",
            "One-minute flow is balanced or buying.",
        ),
        I4_SHORT: noul(
            "Is there clear support in `pa_insidebar.overhead` sitting directly below the breakout?",
            "A clear support sits directly below the breakout.",
            "No clear support sits directly below the breakout.",
        ),
    }


def questions() -> dict[str, Question]:
    out = {}
    out.update(questions_for_side(1))
    out.update(questions_for_side(-1))
    out.update(pa.liquidity_questions())
    return out


def _inside_pair(mother, inside, atr: float, point: float) -> pa.InsideSetup | None:
    body, span, _, _ = pa.body_range(mother)
    if span < max(0.6 * atr, 200 * point):
        return None
    if body / span < 0.70 - 1e-9:
        return None
    if inside.high > mother.high or inside.low < mother.low:
        return None
    _, inside_span, _, _ = pa.body_range(inside)
    if inside_span > 0.50 * span + 1e-9:
        return None
    bullish = mother.close > mother.open
    side = 1 if bullish else -1
    trigger = (mother.high + 5 * point) if side > 0 else (mother.low - 5 * point)
    setup_id = f"pa_ib_{'long' if side > 0 else 'short'}_{mother.t}"
    return pa.InsideSetup(
        side, mother.t, inside.t, mother.high, mother.low, mother.close, atr,
        setup_id, trigger, inside.t + 2 * 300,
    )


def find_setup(ctx: MarketContext, *, spent: set[str] | None = None) -> pa.InsideSetup | None:
    m5 = pa.closed_m5(ctx.minutes)
    atr = pa.atr14(m5)
    if atr is None or len(m5) < 6:
        return None
    if pa.spread_points(ctx.bid, ctx.ask, ctx.point) > 30:
        return None
    # Mother then inside: scan recent pairs.
    for i in range(len(m5) - 2, max(len(m5) - 8, 0), -1):
        mother, inside = m5[i], m5[i + 1]
        setup = _inside_pair(mother, inside, atr, ctx.point)
        if setup is None:
            continue
        if spent and setup.setup_id in spent:
            continue
        later = [b for b in m5 if setup.inside_t < b.t <= setup.expires_t]
        if setup.side > 0:
            triggered = any(b.high >= setup.trigger for b in later) or ctx.mid >= setup.trigger
        else:
            triggered = any(b.low <= setup.trigger for b in later) or ctx.mid <= setup.trigger
        if not triggered:
            continue
        if ctx.bar_t > setup.expires_t + 300:
            continue
        return setup
    return None


def precondition(ctx: MarketContext, *, spent: set[str] | None = None) -> bool:
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return False
    return find_setup(ctx, spent=spent) is not None


def state_fields(ctx: MarketContext, *, spent: set[str] | None = None) -> dict:
    setup = find_setup(ctx, spent=spent)
    if setup is None:
        return {}
    m5 = pa.closed_m5(ctx.minutes)
    struct = pa.structure_swings(m5) or {}
    zones = pa.build_zones(m5, ctx.now, setup.atr, ctx.mid)
    overhead = pa.next_opposing_zone(ctx.mid, setup.side, zones)
    return {
        "pa_insidebar": {
            "mother": {
                "t": setup.mother_t, "high": f"{setup.mother_high:.2f}", "low": f"{setup.mother_low:.2f}",
                "close": f"{setup.mother_close:.2f}",
                "direction": "up" if setup.side > 0 else "down",
            },
            "inside": {"t": setup.inside_t},
            "structure": {
                "hh": bool(struct.get("hh")), "hl": bool(struct.get("hl")),
                "lh": bool(struct.get("lh")), "ll": bool(struct.get("ll")),
            },
            "overhead": (
                {"price": f"{overhead.price:.2f}", "kind": overhead.kind} if overhead else {"price": "none"}
            ),
            "setup_id": setup.setup_id,
        }
    }


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None,
           *, spent: set[str] | None = None) -> Signal:
    policy = policy or Policy()
    setup = find_setup(ctx, spent=spent)
    features: dict = {"strategy": NAME}
    if setup is None:
        return Signal("hold", f"{NAME}: no setup", NAME, features=features)
    features.update({
        "setup_id": setup.setup_id, "side": "long" if setup.side > 0 else "short",
        "atr": round(setup.atr, 4), "trigger": setup.trigger,
    })
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return Signal("hold", f"{NAME}: avoid hours", NAME, features=features)
    ok, why = pa.liquidity_ok(response, policy)
    features["liquidity"] = why
    if not ok:
        return Signal("hold", f"{NAME}: {why}", NAME, features=features)

    keys = (I1_LONG, I2_LONG, I3_LONG, I4_LONG) if setup.side > 0 else (I1_SHORT, I2_SHORT, I3_SHORT, I4_SHORT)
    labels, probs, answers = {}, {}, {}
    for key in keys:
        value = reading(response, key)
        labels[key] = band(value, policy)
        probs[key] = value
        if value is not None:
            answers[key] = {"type": "noul", "noul": value}
    features["nouls"] = probs
    features["labels"] = labels

    for key in keys[:3]:
        if labels[key] == "missing":
            return Signal("hold", f"{NAME}: {key} missing; not read as no", NAME, features=features, answers=answers)
        if labels[key] == "mid":
            return Signal("hold", f"{NAME}: {key} unsettled", NAME, features=features, answers=answers)
        if labels[key] != "yes":
            return Signal("hold", f"{NAME}: {key} no", NAME, features=features, answers=answers)
    veto = keys[3]
    if labels[veto] in ("missing", "mid"):
        return Signal("hold", f"{NAME}: {veto} {labels[veto]}; not a clear no", NAME, features=features, answers=answers)
    if labels[veto] != "no":
        return Signal("hold", f"{NAME}: overhead veto ({veto} yes)", NAME, features=features, answers=answers)

    entry = ctx.ask if setup.side > 0 else ctx.bid
    # Body extreme (not the wick): a ≥200 pt mother would otherwise always
    # miss the 120–200 SL window if the stop sat beyond the full span.
    mother_bars = [b for b in pa.closed_m5(ctx.minutes) if b.t == setup.mother_t]
    mother = mother_bars[0] if mother_bars else None
    if mother is None:
        return Signal("hold", f"{NAME}: mother missing", NAME, features=features, answers=answers)
    if setup.side > 0:
        extreme = min(mother.open, mother.close)
    else:
        extreme = max(mother.open, mother.close)
    m5 = pa.closed_m5(ctx.minutes)
    zones = pa.build_zones(m5, ctx.now, setup.atr, ctx.mid)
    rr = pa.risk_reward(
        setup.side, entry, extreme, setup.atr, zones, ctx.point, ctx.bid, ctx.ask,
        ctx.limits.sl_points, ctx.limits.tp_points, ctx.max_hold_seconds,
    )
    features["risk"] = rr
    if not rr or not rr.get("ok"):
        return Signal("hold", f"{NAME}: {(rr or {}).get('reason', 'risk')}", NAME, features=features, answers=answers)
    if ctx.position and (ctx.position > 0) == (setup.side > 0):
        return Signal("hold", f"{NAME}: already positioned", NAME, features=features, answers=answers)
    action = "buy" if setup.side > 0 else "sell"
    features["table"] = action
    return Signal(
        action=action, size=1.0, reason=f"{NAME}: {setup.setup_id} I1-I3 yes I4 no",
        strategy=NAME, features=features, answers=answers,
        sl_points=rr["sl_points"], tp_points=rr["tp_points"],
        max_hold_seconds=rr["max_hold_seconds"],
    )
