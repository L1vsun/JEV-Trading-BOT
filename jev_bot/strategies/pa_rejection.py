"""M5 pin-bar rejection at a marked zone.

Flag ``--sig-pa-rejection`` (default off). Code finds the pin and the zone;
Jev answers R1–R4 for that side only.
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pa_common as pa
from .common import MarketContext, Policy, Signal, band, noul, reading

NAME = "pa_rejection"

R1_LONG = "pa_rej_r1_long"
R2_LONG = "pa_rej_r2_long"
R3_LONG = "pa_rej_r3_long"
R4_LONG = "pa_rej_r4_long"
R1_SHORT = "pa_rej_r1_short"
R2_SHORT = "pa_rej_r2_short"
R3_SHORT = "pa_rej_r3_short"
R4_SHORT = "pa_rej_r4_short"


def questions_for_side(side: int) -> dict[str, Question]:
    if side > 0:
        return {
            R1_LONG: noul(
                "Does the last closed M5 candle in `pa_rejection.pin` show a long lower wick that "
                "reaches under the marked support in `pa_rejection.zone`, and does it close clearly back above that zone?",
                "Long lower wick under support, close back above the zone.",
                "No clear lower-wick rejection back above the support.",
            ),
            R2_LONG: noul(
                "Did the decline into the support in `pa_rejection.approach` slow before the rejection "
                "(smaller bodies), rather than punching through impulsively?",
                "The move into the zone slowed with smaller bodies.",
                "The move into the zone was impulsive or still accelerating.",
            ),
            R3_LONG: noul(
                "Has `order_flow.last_1_minute` been predominantly buying since the rejection?",
                "One-minute flow is light, moderate or heavy buying.",
                "One-minute flow is balanced or selling.",
            ),
            R4_LONG: noul(
                "Was the support in `pa_rejection.zone` already broken and reclaimed in the last hour "
                "(a spent zone)?",
                "The zone was broken and reclaimed within the last hour.",
                "The zone has not been broken and reclaimed in the last hour.",
            ),
        }
    return {
        R1_SHORT: noul(
            "Does the last closed M5 candle in `pa_rejection.pin` show a long upper wick that "
            "reaches over the marked resistance in `pa_rejection.zone`, and does it close clearly back below that zone?",
            "Long upper wick over resistance, close back below the zone.",
            "No clear upper-wick rejection back below the resistance.",
        ),
        R2_SHORT: noul(
            "Did the rally into the resistance in `pa_rejection.approach` slow before the rejection "
            "(smaller bodies), rather than punching through impulsively?",
            "The move into the zone slowed with smaller bodies.",
            "The move into the zone was impulsive or still accelerating.",
        ),
        R3_SHORT: noul(
            "Has `order_flow.last_1_minute` been predominantly selling since the rejection?",
            "One-minute flow is light, moderate or heavy selling.",
            "One-minute flow is balanced or buying.",
        ),
        R4_SHORT: noul(
            "Was the resistance in `pa_rejection.zone` already broken and reclaimed in the last hour "
            "(a spent zone)?",
            "The zone was broken and reclaimed within the last hour.",
            "The zone has not been broken and reclaimed in the last hour.",
        ),
    }


def questions() -> dict[str, Question]:
    out = {}
    out.update(questions_for_side(1))
    out.update(questions_for_side(-1))
    out.update(pa.liquidity_questions())
    return out


def _pin_at(bar, atr: float, point: float, zones: list[pa.Zone], side: int) -> pa.PinSetup | None:
    body, span, upper, lower = pa.body_range(bar)
    if span < max(atr, 150 * point):
        return None
    if body / span > 0.30:
        return None
    if side > 0:
        if lower / span < 0.60:
            return None
        zone = pa.near_zone(bar.low, zones, atr, want="support")
        if zone is None or bar.close <= zone.price:
            return None
        return pa.PinSetup(
            1, bar.t, bar.high, bar.low, bar.close, zone, atr,
            f"pa_rej_long_{bar.t}", bar.high + 5 * point, bar.t + 2 * 300,
        )
    if upper / span < 0.60:
        return None
    zone = pa.near_zone(bar.high, zones, atr, want="resistance")
    if zone is None or bar.close >= zone.price:
        return None
    return pa.PinSetup(
        -1, bar.t, bar.high, bar.low, bar.close, zone, atr,
        f"pa_rej_short_{bar.t}", bar.low - 5 * point, bar.t + 2 * 300,
    )


def find_setup(ctx: MarketContext, *, spent: set[str] | None = None) -> pa.PinSetup | None:
    m5 = pa.closed_m5(ctx.minutes)
    atr = pa.atr14(m5)
    if atr is None or len(m5) < 5:
        return None
    if pa.spread_points(ctx.bid, ctx.ask, ctx.point) > 30:
        return None
    zones = pa.build_zones(m5, ctx.now, atr, ctx.mid)
    for bar in reversed(m5):
        if ctx.bar_t - bar.t > 3 * 300:
            break
        for side in (1, -1):
            pin = _pin_at(bar, atr, ctx.point, zones, side)
            if pin is None:
                continue
            if spent and pin.setup_id in spent:
                continue
            later = [b for b in m5 if pin.pin_t < b.t <= pin.expires_t]
            if pin.side > 0:
                triggered = any(b.high >= pin.trigger for b in later) or ctx.mid >= pin.trigger
            else:
                triggered = any(b.low <= pin.trigger for b in later) or ctx.mid <= pin.trigger
            if not triggered:
                continue
            if ctx.bar_t > pin.expires_t + 300:
                continue
            return pin
    return None


def precondition(ctx: MarketContext, *, spent: set[str] | None = None) -> bool:
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return False
    return find_setup(ctx, spent=spent) is not None


def state_fields(ctx: MarketContext, *, spent: set[str] | None = None) -> dict:
    pin = find_setup(ctx, spent=spent)
    if pin is None:
        return {}
    m5 = pa.closed_m5(ctx.minutes)
    approach = [
        f"body={abs(b.close - b.open):.2f}"
        for b in m5 if pin.pin_t - 3 * 300 <= b.t < pin.pin_t
    ]
    return {
        "pa_rejection": {
            "pin": {
                "t": pin.pin_t, "high": f"{pin.pin_high:.2f}", "low": f"{pin.pin_low:.2f}",
                "close": f"{pin.pin_close:.2f}", "side": "long" if pin.side > 0 else "short",
            },
            "zone": {"price": f"{pin.zone.price:.2f}", "kind": pin.zone.kind, "side": pin.zone.side},
            "approach": approach or ["n/a"],
            "setup_id": pin.setup_id,
        }
    }


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None,
           *, spent: set[str] | None = None) -> Signal:
    policy = policy or Policy()
    pin = find_setup(ctx, spent=spent)
    features: dict = {"strategy": NAME}
    if pin is None:
        return Signal("hold", f"{NAME}: no setup", NAME, features=features)
    features.update({
        "setup_id": pin.setup_id,
        "side": "long" if pin.side > 0 else "short",
        "zone": pin.zone.kind, "zone_price": pin.zone.price,
        "atr": round(pin.atr, 4), "trigger": pin.trigger,
    })
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return Signal("hold", f"{NAME}: avoid hours", NAME, features=features)
    ok, why = pa.liquidity_ok(response, policy)
    features["liquidity"] = why
    if not ok:
        return Signal("hold", f"{NAME}: {why}", NAME, features=features)

    keys = (R1_LONG, R2_LONG, R3_LONG, R4_LONG) if pin.side > 0 else (R1_SHORT, R2_SHORT, R3_SHORT, R4_SHORT)
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
        return Signal("hold", f"{NAME}: spent zone ({veto} yes)", NAME, features=features, answers=answers)

    entry = ctx.ask if pin.side > 0 else ctx.bid
    extreme = pin.pin_low if pin.side > 0 else pin.pin_high
    m5 = pa.closed_m5(ctx.minutes)
    zones = pa.build_zones(m5, ctx.now, pin.atr, ctx.mid)
    rr = pa.risk_reward(
        pin.side, entry, extreme, pin.atr, zones, ctx.point, ctx.bid, ctx.ask,
        ctx.limits.sl_points, ctx.limits.tp_points, ctx.max_hold_seconds,
    )
    features["risk"] = rr
    if not rr or not rr.get("ok"):
        return Signal("hold", f"{NAME}: {(rr or {}).get('reason', 'risk')}", NAME, features=features, answers=answers)
    if ctx.position and (ctx.position > 0) == (pin.side > 0):
        return Signal("hold", f"{NAME}: already positioned", NAME, features=features, answers=answers)
    action = "buy" if pin.side > 0 else "sell"
    features["table"] = action
    return Signal(
        action=action, size=1.0, reason=f"{NAME}: {pin.setup_id} R1-R3 yes R4 no",
        strategy=NAME, features=features, answers=answers,
        sl_points=rr["sl_points"], tp_points=rr["tp_points"],
        max_hold_seconds=rr["max_hold_seconds"],
    )
