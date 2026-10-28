"""H4 Donchian 20 breakout on closed H4.

Close above the prior 20-bar high (``donchian`` excludes the current bar) is
long. Close below the prior low is short only when ``ctx.allow_short`` and the
EMA50/EMA200 regime is not long-only: when EMA50 > EMA200 the module stays
long-only by default.

SL 2×ATR(20) via ``atr14(bars, 20)``. No fixed TP (``tp_points=0``); trailing is
noted in features. max_bars 60. Skip entry when the breakout bar gaps more than
1×ATR versus the prior close.

Jev: B1 B2 B3 yes, V no.
"""
from __future__ import annotations

from ..indicators import atr14, donchian, ema
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
    cost_gate,
    entry_signal,
    hold_seconds_for,
    hold_signal,
    labels_for,
    require_yes_no,
    risk_equity_ok,
    stop_points,
)
from .mtf_ctx import (
    ctx_allow_short,
    ctx_limits,
    ctx_spread_points,
    ctx_stops_level,
    mtf_bars,
)

NAME = "h4_donchian"
TF = "H4"
B1 = "h4_dc_b1"
B2 = "h4_dc_b2"
B3 = "h4_dc_b3"
V = "h4_dc_v"
KEYS = (B1, B2, B3, V)
K_SL = 2.0
ATR_PERIOD = 20
DONCHIAN_N = 20
MAX_BARS = 60


def questions() -> dict[str, Question]:
    return {
        B1: noul(
            "Is the H4 close in `h4_donchian.breakout` a clean Donchian-20 breakout "
            "(beyond the prior channel extreme)?",
            "The close is a clean Donchian-20 breakout.",
            "The close is not a clean Donchian-20 breakout.",
        ),
        B2: noul(
            "Does the EMA50/EMA200 regime in `h4_donchian.regime` agree with the breakout side?",
            "EMA regime agrees with the breakout side.",
            "EMA regime disagrees with the breakout side.",
        ),
        B3: noul(
            "Is H4 follow-through in `h4_donchian.follow` supportive after the breakout "
            "(not an immediate reversal bar)?",
            "Follow-through supports the breakout.",
            "Follow-through does not support the breakout.",
        ),
        V: noul(
            "Is the breakout bar in `h4_donchian.breakout` a gap spike or news candle "
            "that should be skipped?",
            "The bar is a gap spike or news candle — skip.",
            "The bar is not a gap spike / news skip.",
        ),
    }


def _bars(ctx: MarketContext):
    return mtf_bars(ctx, TF)


def _allow_short(ctx: MarketContext) -> bool:
    return ctx_allow_short(ctx)


def compute(ctx: MarketContext) -> dict | None:
    bars = _bars(ctx)
    if len(bars) < DONCHIAN_N + 2 + ATR_PERIOD:
        return None
    hi, lo = donchian(bars, DONCHIAN_N)
    if hi is None or lo is None:
        return None
    last, prev = bars[-1], bars[-2]
    closes = [b.close for b in bars]
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    if e50[-1] is None or e200[-1] is None:
        return None
    atr = atr14(bars, ATR_PERIOD)
    if atr is None:
        return None
    gap = abs(last.open - prev.close)
    if gap > atr:
        return {
            "gap_block": True, "gap": gap, "atr": atr,
            "hi": hi, "lo": lo, "close": last.close,
            "ema50": e50[-1], "ema200": e200[-1],
            "side": 0, "long_setup": False, "short_setup": False,
        }
    regime_long = e50[-1] > e200[-1]
    long_break = last.close > hi
    short_break = last.close < lo
    # Long-only default when EMA50 > EMA200; shorts need allow_short and non-long regime.
    long_setup = long_break and regime_long
    short_setup = short_break and _allow_short(ctx) and not regime_long
    side = 1 if long_setup else -1 if short_setup else 0
    return {
        "gap_block": False,
        "gap": gap,
        "atr": atr,
        "hi": hi,
        "lo": lo,
        "close": last.close,
        "open": last.open,
        "ema50": e50[-1],
        "ema200": e200[-1],
        "regime_long": regime_long,
        "long_break": long_break,
        "short_break": short_break,
        "long_setup": long_setup,
        "short_setup": short_setup,
        "side": side,
        "last_t": last.t,
        "bullish_bar": last.close > last.open,
    }


def precondition(ctx: MarketContext) -> bool:
    feat = compute(ctx)
    return bool(feat and feat.get("side") and not feat.get("gap_block"))


def state_fields(ctx: MarketContext) -> dict:
    feat = compute(ctx) or {}
    return {
        NAME: {
            "breakout": {
                "prior_high": f"{feat.get('hi', 0):.2f}",
                "prior_low": f"{feat.get('lo', 0):.2f}",
                "close": f"{feat.get('close', 0):.2f}",
                "gap": f"{feat.get('gap', 0):.2f}",
                "gap_block": bool(feat.get("gap_block")),
                "proposed_side": (
                    "long" if feat.get("side", 0) > 0 else "short" if feat.get("side", 0) < 0 else "none"
                ),
            },
            "regime": {
                "ema50": f"{feat.get('ema50', 0):.2f}",
                "ema200": f"{feat.get('ema200', 0):.2f}",
                "long_only_default": bool(feat.get("regime_long")),
            },
            "follow": {
                "bullish_bar": bool(feat.get("bullish_bar")),
                "atr20": f"{feat.get('atr', 0) or 0:.2f}",
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
        min_sl=float(limits.min_sl_points.get(TF, 2000.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=spread,
    )
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
    hold = hold_seconds_for(TF, MAX_BARS, int(getattr(limits, "global_max_hold", 900) or 900))
    return sl, 0.0, hold


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    policy = policy or Policy()
    features: dict = {"strategy": NAME, "trailing": True, "trail_note": "no fixed TP; trail in live"}
    answers = _answers(response)
    feat = compute(ctx)
    if feat is None:
        return hold_signal(NAME, f"{NAME}: insufficient H4 history", features, answers)
    if feat.get("gap_block"):
        features["gap"] = feat.get("gap")
        return hold_signal(NAME, f"{NAME}: gap > 1 ATR", features, answers)
    side = int(feat.get("side") or 0)
    features.update({
        "hi": feat.get("hi"), "lo": feat.get("lo"),
        "regime_long": feat.get("regime_long"),
        "side": "long" if side > 0 else "short" if side < 0 else "none",
    })
    if side == 0:
        return hold_signal(NAME, f"{NAME}: no regime-aligned breakout", features, answers)
    if ctx.position and (ctx.position > 0) == (side > 0):
        return hold_signal(NAME, f"{NAME}: already positioned", features, answers)

    labels = labels_for(response, KEYS, policy)
    features["labels"] = labels
    features["nouls"] = {k: reading(response, k) for k in KEYS}
    why = require_yes_no(labels, (B1, B2, B3), (V,))
    if why:
        return hold_signal(NAME, f"{NAME}: {why}", features, answers)

    atr = feat.get("atr")
    if atr is None:
        return hold_signal(NAME, f"{NAME}: insufficient history for ATR20", features, answers)
    sized = _size(ctx, float(atr))
    if sized[0] is None:
        return hold_signal(NAME, f"{NAME}: {sized[1]}", features, answers)
    sl, tp, max_hold = sized
    features.update({
        "atr": round(float(atr), 4), "atr_period": ATR_PERIOD,
        "sl_points": sl, "tp_points": tp, "max_bars": MAX_BARS,
    })
    action = "buy" if side > 0 else "sell"
    features["table"] = action
    return entry_signal(
        action, NAME, f"{NAME}: {action} B1-B3 yes V no",
        sl_points=sl, tp_points=tp, max_hold_seconds=max_hold,
        features=features, answers=answers,
    )
