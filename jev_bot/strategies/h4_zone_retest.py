"""H4 fractal-zone first retest (EXPERIMENT, default-off).

Fractal swings use n=3 on closed H4. After a level is broken (close more than
0.1% beyond the swing), the first return into a ±0.1% band is the only entry.
A later close more than 0.1% beyond the wrong side invalidates the zone.
SL is beyond the zone by 0.5×ATR; TP is 2R.

Entry when retest count == 1 and Z1 Z2 Z3 yes with V no.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..indicators import atr14, fractal_swings
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
    DEFAULT_MAX_BARS,
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

NAME = "h4_zone_retest"
TF = "H4"
Z1 = "h4_zr_z1"
Z2 = "h4_zr_z2"
Z3 = "h4_zr_z3"
V = "h4_zr_v"
KEYS = (Z1, Z2, Z3, V)
BAND_PCT = 0.001       # ±0.1%
INVALID_PCT = 0.001    # >0.1% beyond
SL_ATR = 0.5
FRACTAL_N = 3


def questions() -> dict[str, Question]:
    return {
        Z1: noul(
            "Is the zone in `h4_zone_retest.zone` a clear fractal swing that has already "
            "broken and is now being retested?",
            "Clear broken fractal zone now on retest.",
            "Not a clear broken-fractal retest.",
        ),
        Z2: noul(
            "Is this the first retest of that zone in `h4_zone_retest.zone` (retest_count == 1)?",
            "This is the first retest.",
            "This is not the first retest.",
        ),
        Z3: noul(
            "Does the H4 close in `h4_zone_retest.zone` respect the zone (holding as support "
            "for long / resistance for short) rather than slicing through?",
            "The close respects the zone.",
            "The close is slicing through the zone.",
        ),
        V: noul(
            "Has the zone in `h4_zone_retest.zone` already been invalidated by a close "
            "more than 0.1% beyond it?",
            "The zone is already invalidated.",
            "The zone is still valid.",
        ),
    }


@dataclass(frozen=True)
class ZoneSetup:
    side: int          # +1 long (support retest), -1 short (resistance retest)
    price: float
    swing_t: int
    break_t: int
    retest_count: int
    invalidated: bool
    setup_id: str


def _bars(ctx: MarketContext):
    return mtf_bars(ctx, TF)


def _band(price: float) -> tuple[float, float]:
    return price * (1.0 - BAND_PCT), price * (1.0 + BAND_PCT)


def _find_setups(bars) -> list[ZoneSetup]:
    """Walk confirmed fractals; track break → retests → invalidate.

    Swing high broken upward becomes support (long retests).
    Swing low broken downward becomes resistance (short retests).
    """
    swings = fractal_swings(bars, left=FRACTAL_N, right=FRACTAL_N)
    if not swings:
        return []
    out: list[ZoneSetup] = []
    for idx, price, kind in swings:
        lo_b, hi_b = _band(price)
        broken = False
        side = 1 if kind == "high" else -1
        break_t = 0
        retests = 0
        invalidated = False
        last_in_band = False
        for j in range(idx + FRACTAL_N + 1, len(bars)):
            b = bars[j]
            if not broken:
                if kind == "high" and b.close > price * (1.0 + INVALID_PCT):
                    broken = True
                    break_t = b.t
                elif kind == "low" and b.close < price * (1.0 - INVALID_PCT):
                    broken = True
                    break_t = b.t
                continue
            if side > 0 and b.close < price * (1.0 - INVALID_PCT):
                invalidated = True
                break
            if side < 0 and b.close > price * (1.0 + INVALID_PCT):
                invalidated = True
                break
            in_band = (
                lo_b <= b.low <= hi_b
                or lo_b <= b.high <= hi_b
                or (b.low <= lo_b and b.high >= hi_b)
                or lo_b <= b.close <= hi_b
            )
            if in_band and not last_in_band:
                retests += 1
            last_in_band = in_band
        if not broken:
            continue
        out.append(ZoneSetup(
            side=side, price=price, swing_t=bars[idx].t, break_t=break_t,
            retest_count=retests, invalidated=invalidated,
            setup_id=f"h4_zr_{kind}_{bars[idx].t}",
        ))
    return out


def compute(ctx: MarketContext) -> dict | None:
    bars = _bars(ctx)
    if len(bars) < FRACTAL_N * 2 + 10:
        return None
    atr = atr14(bars)
    if atr is None:
        return None
    setups = _find_setups(bars)
    last = bars[-1]
    lo_touch = None
    # Active: not invalidated, retest_count == 1, and last bar is in the band.
    active: ZoneSetup | None = None
    for z in reversed(setups):
        if z.invalidated or z.retest_count != 1:
            continue
        lo_b, hi_b = _band(z.price)
        in_band = lo_b <= last.low <= hi_b or lo_b <= last.high <= hi_b
        in_band = in_band or (lo_b <= last.close <= hi_b) or (last.low <= lo_b and last.high >= hi_b)
        if not in_band:
            continue
        # Close should respect zone: long close >= low band, short close <= high band.
        if z.side > 0 and last.close < lo_b:
            continue
        if z.side < 0 and last.close > hi_b:
            continue
        active = z
        lo_touch = lo_b
        break
    return {
        "atr": atr,
        "setups": len(setups),
        "active": active,
        "close": last.close,
        "last_t": last.t,
        "band_lo": lo_touch,
        "band_hi": (_band(active.price)[1] if active else None),
        "side": active.side if active else 0,
    }


def precondition(ctx: MarketContext) -> bool:
    feat = compute(ctx)
    return bool(feat and feat.get("active") is not None)


def state_fields(ctx: MarketContext) -> dict:
    feat = compute(ctx) or {}
    z: ZoneSetup | None = feat.get("active")
    return {
        NAME: {
            "zone": {
                "price": f"{z.price:.2f}" if z else "n/a",
                "side": "long" if z and z.side > 0 else "short" if z and z.side < 0 else "none",
                "swing_t": z.swing_t if z else None,
                "break_t": z.break_t if z else None,
                "retest_count": z.retest_count if z else 0,
                "invalidated": bool(z.invalidated) if z else True,
                "band_pct": "0.1%",
                "close": f"{feat.get('close', 0):.2f}",
                "setup_id": z.setup_id if z else None,
            }
        }
    }


def _answers(response: Response) -> dict:
    out = {}
    for key in KEYS:
        value = reading(response, key)
        if value is not None:
            out[key] = {"type": "noul", "noul": value}
    return out


def _size(ctx: MarketContext, atr: float, side: int, zone_price: float
          ) -> tuple[float, float, int] | tuple[None, str]:
    limits = ctx_limits(ctx)
    spread = ctx_spread_points(ctx)
    entry = ctx.ask if side > 0 else ctx.bid
    lo_b, hi_b = _band(zone_price)
    if side > 0:
        sl_price = lo_b - SL_ATR * atr
        raw = (entry - sl_price) / ctx.point if ctx.point > 0 else 0.0
    else:
        sl_price = hi_b + SL_ATR * atr
        raw = (sl_price - entry) / ctx.point if ctx.point > 0 else 0.0
    floor = stop_points(
        tf=TF, atr=atr, point=ctx.point, k_sl=max(SL_ATR, 1.0),
        min_sl=float(limits.min_sl_points.get(TF, 2000.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=spread,
    )
    sl = max(raw, floor)
    ok, why = cost_gate(sl, spread, float(limits.max_cost_share.get(TF, 0.05)))
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
    max_bars = int(limits.max_bars.get(TF, DEFAULT_MAX_BARS.get(TF, 30)))
    hold = hold_seconds_for(
        TF, max_bars, int(getattr(limits, "global_max_hold", 900) or 900),
        override=hold_override_for(NAME, limits=limits, ctx=ctx),
    )
    return sl, tp, hold


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    policy = policy or Policy()
    features: dict = {"strategy": NAME, "experiment": True, "default_off": True}
    answers = _answers(response)
    feat = compute(ctx)
    if feat is None:
        return hold_signal(NAME, f"{NAME}: insufficient H4 history", features, answers)
    z: ZoneSetup | None = feat.get("active")
    if z is None:
        return hold_signal(NAME, f"{NAME}: no first-retest zone", features, answers)
    features.update({
        "setup_id": z.setup_id, "zone_price": z.price,
        "retest_count": z.retest_count, "side": "long" if z.side > 0 else "short",
        "invalidated": z.invalidated,
    })
    if z.retest_count != 1:
        return hold_signal(NAME, f"{NAME}: retest_count != 1", features, answers)
    if z.invalidated:
        return hold_signal(NAME, f"{NAME}: zone invalidated", features, answers)
    if ctx.position and (ctx.position > 0) == (z.side > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features, answers)
    # Shorts still gated by allow_short for experiment module.
    allow_short = bool(getattr(ctx, "allow_short", False))
    if z.side < 0 and not allow_short:
        return hold_signal(NAME, f"{NAME}: short disabled", features, answers)

    labels = labels_for(response, KEYS, policy)
    features["labels"] = labels
    features["nouls"] = {k: reading(response, k) for k in KEYS}
    why = require_yes_no(labels, (Z1, Z2, Z3), (V,))
    if why:
        return hold_signal(NAME, f"{NAME}: {why}", features, answers)

    atr, hold = atr_or_hold(_bars(ctx), NAME)
    if hold is not None:
        hold.features.update(features)
        hold.answers = answers
        return hold
    sized = _size(ctx, atr, z.side, z.price)
    if sized[0] is None:
        return hold_signal(NAME, f"{NAME}: {sized[1]}", features, answers)
    sl, tp, max_hold = sized
    features.update({"atr": round(atr, 4), "sl_points": sl, "tp_points": tp})
    action = "buy" if z.side > 0 else "sell"
    features["table"] = action
    return entry_signal(
        action, NAME, f"{NAME}: {action} first retest Z1-Z3 yes V no",
        sl_points=sl, tp_points=tp, max_hold_seconds=max_hold,
        features=features, answers=answers,
    )
