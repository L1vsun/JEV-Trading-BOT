"""H1 trend-long: EMA9/20 (or SMA50/200) cross up on closed H1.

Gold is traded with a bull bias here: this module is LONG ONLY. Shorts are
never emitted. Code detects the cross and the opposite-cross exit; Jev answers
T1–T4. BUY when the cross fires and T1 T2 are yes with T3 no. EXIT on the
opposite cross (code) or when T4 reads yes.

Optional ``ctx.h1_use_sma50_200`` switches the cross to SMA50 above SMA200.
"""
from __future__ import annotations

from ..indicators import atr14, ema, sma, structure_hh_hl
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
    DEFAULT_MAX_BARS,
    atr_or_hold,
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
    ctx_limits,
    ctx_spread_points,
    ctx_stops_level,
    mtf_bars,
)

NAME = "h1_trend_long"
TF = "H1"
T1 = "h1_tl_t1"
T2 = "h1_tl_t2"
T3 = "h1_tl_t3"
T4 = "h1_tl_t4"
KEYS = (T1, T2, T3, T4)
K_SL = 2.5


def questions() -> dict[str, Question]:
    return {
        T1: noul(
            "Do the last 20 closed H1 bars in `h1_trend_long.structure` show a sequence of "
            "higher highs and higher lows?",
            "Last 20 H1 bars show higher highs and higher lows.",
            "The last 20 H1 bars do not show clear HH/HL structure.",
        ),
        T2: noul(
            "Did the last closed H1 bar in `h1_trend_long.last_bar` close in the upper half of its range?",
            "Last H1 close is in the upper half of the bar.",
            "Last H1 close is not in the upper half of the bar.",
        ),
        T3: noul(
            "Was the last three closed H1 bars in `h1_trend_long.stretch` a vertical stretch "
            "(range much larger than ATR, impulsive spike)?",
            "The last three H1 bars are a vertical stretch.",
            "The last three H1 bars are not a vertical stretch.",
        ),
        T4: noul(
            "Do the recent H1 swings in `h1_trend_long.structure` show lower highs and lower lows "
            "(trend failure against the long)?",
            "H1 structure shows lower highs and lower lows.",
            "H1 structure does not show LH/LL failure.",
        ),
    }


def _bars(ctx: MarketContext):
    return mtf_bars(ctx, TF)


def _use_sma(ctx: MarketContext) -> bool:
    return bool(getattr(ctx, "h1_use_sma50_200", False))


def compute(ctx: MarketContext) -> dict | None:
    bars = _bars(ctx)
    closes = [b.close for b in bars]
    use_sma = _use_sma(ctx)
    need = 210 if use_sma else 25
    if len(closes) < need:
        return None
    if use_sma:
        a = sma(closes, 50)
        b = sma(closes, 200)
        fast_n, slow_n = 50, 200
        kind = "sma50_200"
    else:
        a = ema(closes, 9)
        b = ema(closes, 20)
        fast_n, slow_n = 9, 20
        kind = "ema9_20"
    if a[-1] is None or b[-1] is None or a[-2] is None or b[-2] is None:
        return None
    cross_up = a[-2] <= b[-2] and a[-1] > b[-1]
    cross_down = a[-2] >= b[-2] and a[-1] < b[-1]
    last = bars[-1]
    upper_half = last.close >= (last.high + last.low) / 2.0
    window = bars[-20:] if len(bars) >= 20 else bars
    struct = structure_hh_hl(window) or structure_hh_hl(bars)
    atr = atr14(bars)
    stretch_range = 0.0
    if len(bars) >= 3:
        stretch_range = max(b.high for b in bars[-3:]) - min(b.low for b in bars[-3:])
    stretch_vs_atr = (stretch_range / atr) if atr and atr > 1e-12 else None
    return {
        "kind": kind,
        "fast": a[-1],
        "slow": b[-1],
        "fast_n": fast_n,
        "slow_n": slow_n,
        "cross_up": cross_up,
        "cross_down": cross_down,
        "upper_half": upper_half,
        "structure": struct,
        "stretch_range": stretch_range,
        "stretch_vs_atr": stretch_vs_atr,
        "atr": atr,
        "last_t": last.t,
        "last_close": last.close,
        "last_high": last.high,
        "last_low": last.low,
    }


def precondition(ctx: MarketContext) -> bool:
    feat = compute(ctx)
    if feat is None:
        return False
    if feat["cross_up"]:
        return True
    # Exit path: long open and opposite cross or T4 may fire.
    if ctx.position > 0 and (feat["cross_down"] or feat.get("structure") is not None):
        return True
    return False


def state_fields(ctx: MarketContext) -> dict:
    feat = compute(ctx) or {}
    struct = feat.get("structure") or {}
    return {
        NAME: {
            "kind": feat.get("kind", "ema9_20"),
            "fast": f"{feat.get('fast', 0):.2f}",
            "slow": f"{feat.get('slow', 0):.2f}",
            "cross": (
                "up" if feat.get("cross_up") else "down" if feat.get("cross_down") else "none"
            ),
            "last_bar": {
                "close": f"{feat.get('last_close', 0):.2f}",
                "high": f"{feat.get('last_high', 0):.2f}",
                "low": f"{feat.get('last_low', 0):.2f}",
                "upper_half": bool(feat.get("upper_half")),
            },
            "structure": {
                "hh": bool(struct.get("hh")),
                "hl": bool(struct.get("hl")),
                "lh": bool(struct.get("lh")),
                "ll": bool(struct.get("ll")),
            },
            "stretch": {
                "last_3_range": f"{feat.get('stretch_range', 0):.2f}",
                "vs_atr": (
                    f"{feat['stretch_vs_atr']:.2f}" if feat.get("stretch_vs_atr") is not None else "n/a"
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


def _sl_tp_hold(ctx: MarketContext, atr: float) -> tuple[float, float, int] | tuple[None, str]:
    limits = ctx_limits(ctx)
    sl = stop_points(
        tf=TF, atr=atr, point=ctx.point, k_sl=K_SL,
        min_sl=float(limits.min_sl_points.get(TF, 1000.0)),
        stops_level=ctx_stops_level(ctx),
        spread_points=ctx_spread_points(ctx),
    )
    spread = ctx_spread_points(ctx)
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
    max_bars = int(limits.max_bars.get(TF, DEFAULT_MAX_BARS.get(TF, 24)))
    hold = hold_seconds_for(TF, max_bars, int(getattr(limits, "global_max_hold", 900) or 900))
    # No fixed RR target — trail / opposite-cross / T4 manage the exit.
    return sl, 0.0, hold


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    policy = policy or Policy()
    features: dict = {"strategy": NAME, "side": "long"}
    answers = _answers(response)
    feat = compute(ctx)
    if feat is None:
        return hold_signal(NAME, f"{NAME}: insufficient H1 history", features, answers)
    features.update({
        "kind": feat["kind"], "cross_up": feat["cross_up"], "cross_down": feat["cross_down"],
        "stretch_vs_atr": feat.get("stretch_vs_atr"),
    })
    labels = labels_for(response, KEYS, policy)
    features["labels"] = labels
    features["nouls"] = {k: reading(response, k) for k in KEYS}

    # Exit first when long.
    if ctx.position > 0:
        if feat["cross_down"]:
            features["table"] = "exit_cross"
            return Signal("exit", f"{NAME}: opposite cross", NAME, features=features, answers=answers)
        if labels[T4] == "yes":
            features["table"] = "exit_t4"
            return Signal("exit", f"{NAME}: {T4} yes", NAME, features=features, answers=answers)
        if labels[T4] == "mid":
            return hold_signal(NAME, f"{NAME}: {T4} unsettled", features, answers)
        # Stay in the trade.
        return hold_signal(NAME, f"{NAME}: hold long", features, answers)

    if not feat["cross_up"]:
        return hold_signal(NAME, f"{NAME}: no bullish cross", features, answers)

    why = require_yes_no(labels, (T1, T2), (T3,))
    if why:
        return hold_signal(NAME, f"{NAME}: {why}", features, answers)

    atr, hold = atr_or_hold(_bars(ctx), NAME)
    if hold is not None:
        hold.features.update(features)
        hold.answers = answers
        return hold
    sized = _sl_tp_hold(ctx, atr)
    if sized[0] is None:
        return hold_signal(NAME, f"{NAME}: {sized[1]}", features, answers)
    sl, tp, max_hold = sized
    features.update({"atr": round(atr, 4), "sl_points": sl, "tp_points": tp, "k_sl": K_SL})
    features["table"] = "buy"
    return entry_signal(
        "buy", NAME, f"{NAME}: cross + {T1}/{T2} yes {T3} no",
        sl_points=sl, tp_points=tp, max_hold_seconds=max_hold,
        features=features, answers=answers,
    )
