"""Shared trend-pullback building blocks for M1/M5/M15/H1/H4.

Code finds the higher-TF trend, EMA pullback touch, 38–62% impulse retracement,
and continuation close. Jev answers PB1–PB5 (long/short). Entry needs PB1–PB3
yes and PB4–PB5 no. Exit on code structure break or PB4 yes.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..bars import Bar
from ..indicators import (
    adx14,
    atr14,
    efficiency_ratio,
    ema,
    fractal_swings,
    structure_hh_hl,
)
from ..typesafe import Question, Response
from .common import MarketContext, Policy, Signal, noul, reading
from .mtf_base import (
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

TOUCH_ATR = 0.25
MIN_ADX = 20.0
MIN_ER = 0.3
K_SL = 1.2
TP_RR = 2.0
RETR_LO = 0.38
RETR_HI = 0.62
LOOKBACK_TOUCH = 8
DEFAULT_MIN_SL = {"M1": 200.0, "M5": 300.0, "M15": 500.0, "H1": 1000.0, "H4": 2000.0}
DEFAULT_COST_SHARE = {"M1": 0.10, "M5": 0.10, "M15": 0.10, "H1": 0.07, "H4": 0.05}


@dataclass(frozen=True)
class PullbackConfig:
    """Per-timeframe knobs for the unified pullback template."""

    name: str
    tf: str
    higher_tf: str | None
    # ema50_slope | ema50_ema200 | self_ema50_200
    higher_mode: str
    pullback_ema: int
    max_bars: int
    shorts_need_allow: bool
    also_h4_ema_allow_short: bool = False


def tf_tag(tf: str) -> str:
    return tf.lower()


def key_ids(cfg: PullbackConfig, side: int) -> tuple[str, str, str, str, str]:
    tag = tf_tag(cfg.tf)
    side_s = "long" if side > 0 else "short"
    keys = tuple(f"pb_{tag}_pb{i}_{side_s}" for i in range(1, 6))
    assert len(keys) == 5
    return keys[0], keys[1], keys[2], keys[3], keys[4]


def all_question_ids(cfg: PullbackConfig) -> tuple[str, ...]:
    return key_ids(cfg, 1) + key_ids(cfg, -1)


def questions_for(cfg: PullbackConfig) -> dict[str, Question]:
    name = cfg.name
    tag = cfg.tf
    out: dict[str, Question] = {}
    for side, label in ((1, "long"), (-1, "short")):
        pb1, pb2, pb3, pb4, pb5 = key_ids(cfg, side)
        struct = "HH/HL" if side > 0 else "LH/LL"
        out[pb1] = noul(
            f"Does higher-TF / regime structure in `{name}.trend` show {struct} "
            f"aligned with the proposed {label} on {tag}?",
            f"Structure confirms the {label} trend.",
            f"Structure does not confirm the {label} trend.",
        )
        out[pb2] = noul(
            f"Is the {tag} pullback into the EMA in `{name}.pullback` orderly "
            f"(weaker than the prior impulse, roughly a 38–62% retracement)?",
            "The pullback looks orderly versus the prior impulse.",
            "The pullback is chaotic, too deep, or too shallow.",
        )
        out[pb3] = noul(
            f"Is the last closed {tag} candle in `{name}.continuation` a clear "
            f"continuation beyond the pullback extreme for the {label}?",
            "Clear continuation candle beyond the pullback extreme.",
            "No clear continuation beyond the pullback extreme.",
        )
        out[pb4] = noul(
            f"Has the trend structure already been broken against the {label} setup "
            f"in `{name}`?",
            "Structure is broken against the setup.",
            "Structure is intact for the setup.",
        )
        out[pb5] = noul(
            f"Is the {tag} tape in `{name}.tape` too news-driven or chaotic for a "
            f"{label} pullback entry?",
            "Conditions are too news-driven or chaotic.",
            "Conditions are calm enough for a pullback entry.",
        )
    return out


def _minutes_as_bars(ctx: MarketContext) -> list[Bar]:
    minutes = getattr(ctx, "minutes", None) or []
    out: list[Bar] = []
    for m in minutes:
        buy = float(getattr(m, "buy_vol", 0.0) or 0.0)
        sell = float(getattr(m, "sell_vol", 0.0) or 0.0)
        out.append(Bar(
            t=int(m.t), open=float(m.open), high=float(m.high),
            low=float(m.low), close=float(m.close),
            tick_vol=buy + sell, buy_vol=buy, sell_vol=sell,
        ))
    return out


def entry_bars(ctx: MarketContext, tf: str) -> list[Bar]:
    bars = mtf_bars(ctx, tf)
    if bars:
        return bars
    if tf == "M1":
        return _minutes_as_bars(ctx)
    return []


def _ema_slope(closes: list[float], period: int) -> tuple[float | None, int]:
    series = ema(closes, period)
    vals = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(vals) < 2:
        return None, 0
    (_, a), (_, b) = vals[-2], vals[-1]
    if b > a:
        return float(b), 1
    if b < a:
        return float(b), -1
    return float(b), 0


def _allow_short(ctx: MarketContext, cfg: PullbackConfig) -> bool:
    if not cfg.shorts_need_allow:
        return True
    if ctx_allow_short(ctx):
        return True
    if cfg.also_h4_ema_allow_short and bool(getattr(ctx, "h4_ema_allow_short", False)):
        return True
    return False


def _higher_bias(ctx: MarketContext, cfg: PullbackConfig) -> dict | None:
    """Return ``{side, ...}`` from the higher-TF (or self) trend filter."""
    if cfg.higher_mode == "self_ema50_200":
        bars = entry_bars(ctx, cfg.tf)
        if len(bars) < 210:
            return None
        closes = [b.close for b in bars]
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        if e50[-1] is None or e200[-1] is None:
            return None
        last = bars[-1]
        ema50, ema200 = float(e50[-1]), float(e200[-1])
        if ema50 > ema200 and last.close > ema200:
            side = 1
        elif ema50 < ema200 and last.close < ema200:
            side = -1
        else:
            return None
        struct = structure_hh_hl(bars) or {}
        return {
            "side": side,
            "ema50": ema50,
            "ema200": ema200,
            "close": last.close,
            "hh": bool(struct.get("hh")),
            "hl": bool(struct.get("hl")),
            "lh": bool(struct.get("lh")),
            "ll": bool(struct.get("ll")),
            "source": cfg.tf,
        }

    htf = cfg.higher_tf
    if not htf:
        return None
    bars = mtf_bars(ctx, htf)
    if cfg.higher_mode == "ema50_slope":
        if len(bars) < 55:
            return None
        ema50, slope = _ema_slope([b.close for b in bars], 50)
        if ema50 is None or slope == 0:
            return None
        struct = structure_hh_hl(bars) or {}
        return {
            "side": slope,
            "ema50": ema50,
            "ema200": None,
            "close": bars[-1].close,
            "hh": bool(struct.get("hh")),
            "hl": bool(struct.get("hl")),
            "lh": bool(struct.get("lh")),
            "ll": bool(struct.get("ll")),
            "source": htf,
        }

    if cfg.higher_mode == "ema50_ema200":
        if len(bars) < 210:
            return None
        closes = [b.close for b in bars]
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        if e50[-1] is None or e200[-1] is None:
            return None
        last = bars[-1]
        ema50, ema200 = float(e50[-1]), float(e200[-1])
        if ema50 > ema200 and last.close > ema200:
            side = 1
        elif ema50 < ema200 and last.close < ema200:
            side = -1
        else:
            return None
        struct = structure_hh_hl(bars) or {}
        return {
            "side": side,
            "ema50": ema50,
            "ema200": ema200,
            "close": last.close,
            "hh": bool(struct.get("hh")),
            "hl": bool(struct.get("hl")),
            "lh": bool(struct.get("lh")),
            "ll": bool(struct.get("ll")),
            "source": htf,
        }
    return None


def _touched(bar: Bar, ema_v: float, atr: float) -> bool:
    band = TOUCH_ATR * atr
    return min(
        abs(bar.low - ema_v), abs(bar.high - ema_v),
        abs(bar.close - ema_v), abs(bar.open - ema_v),
    ) <= band + 1e-12


def _impulse_retracement(bars: list[Bar], side: int, touch_idx: int) -> float | None:
    """Retracement fraction of the last impulse into the pullback touch (0–1)."""
    if touch_idx < 5:
        return None
    window = bars[: touch_idx + 1]
    swings = fractal_swings(window, left=2, right=2)
    highs = [(i, p) for i, p, k in swings if k == "high"]
    lows = [(i, p) for i, p, k in swings if k == "low"]
    if side > 0:
        # Impulse up into a swing high, then pullback down to touch low.
        prior_highs = [h for h in highs if h[0] < touch_idx]
        if not prior_highs:
            return None
        peak_i, peak_p = prior_highs[-1]
        prior_lows = [l for l in lows if l[0] < peak_i]
        if not prior_lows:
            return None
        start_p = prior_lows[-1][1]
        impulse = peak_p - start_p
        if impulse <= 1e-12:
            return None
        pb_extreme = min(b.low for b in bars[peak_i: touch_idx + 1])
        return (peak_p - pb_extreme) / impulse
    prior_lows = [l for l in lows if l[0] < touch_idx]
    if not prior_lows:
        return None
    trough_i, trough_p = prior_lows[-1]
    prior_highs = [h for h in highs if h[0] < trough_i]
    if not prior_highs:
        return None
    start_p = prior_highs[-1][1]
    impulse = start_p - trough_p
    if impulse <= 1e-12:
        return None
    pb_extreme = max(b.high for b in bars[trough_i: touch_idx + 1])
    return (pb_extreme - trough_p) / impulse


def _structure_broken(bias: dict, side: int) -> bool:
    if side > 0:
        return bool(bias.get("lh") and bias.get("ll"))
    return bool(bias.get("hh") and bias.get("hl"))


def find_setup(ctx: MarketContext, cfg: PullbackConfig) -> dict | None:
    if getattr(ctx, "mtf", None) is None and cfg.tf != "M1":
        return None
    bars = entry_bars(ctx, cfg.tf)
    need = max(cfg.pullback_ema + 20, 40)
    if len(bars) < need:
        return None

    atr = atr14(bars)
    if atr is None or atr <= 0:
        return None
    lim = ctx_limits(ctx)
    if ctx_spread_points(ctx) > float(lim.max_spread_points) + 1e-12:
        return None

    bias = _higher_bias(ctx, cfg)
    if bias is None:
        return None
    side = int(bias["side"])
    if side < 0 and not _allow_short(ctx, cfg):
        return None

    adx_series = adx14(bars)
    adx = adx_series[-1] if adx_series else None
    er = efficiency_ratio([b.close for b in bars], period=10)
    trend_ok = (adx is not None and adx >= MIN_ADX) or (er is not None and er >= MIN_ER)
    if not trend_ok:
        return None

    ema_series = ema([b.close for b in bars], cfg.pullback_ema)
    last = bars[-1]
    last_ema = ema_series[-1]
    if last_ema is None:
        return None

    touch_idx = None
    touch_ema = None
    start = max(0, len(bars) - LOOKBACK_TOUCH - 1)
    band = TOUCH_ATR * atr
    for i in range(len(bars) - 2, start - 1, -1):
        ev = ema_series[i]
        if ev is None:
            continue
        if not _touched(bars[i], float(ev), atr):
            continue
        if side > 0 and bars[i].low <= float(ev) + band:
            touch_idx, touch_ema = i, float(ev)
            break
        if side < 0 and bars[i].high >= float(ev) - band:
            touch_idx, touch_ema = i, float(ev)
            break
    if touch_idx is None:
        return None

    retr = _impulse_retracement(bars, side, touch_idx)
    if retr is None or not (RETR_LO - 1e-12 <= retr <= RETR_HI + 1e-12):
        return None

    touch = bars[touch_idx]
    if side > 0:
        pullback_extreme = touch.low
        # Continuation: closed bar after the touch beyond the pullback high.
        if last.t == touch.t or last.close <= touch.high + 1e-12:
            return None
    else:
        pullback_extreme = touch.high
        if last.t == touch.t or last.close >= touch.low - 1e-12:
            return None

    return {
        "side": side,
        "atr": atr,
        "adx": float(adx) if adx is not None else None,
        "er": float(er) if er is not None else None,
        "retracement": float(retr),
        "bias": bias,
        "entry_ema": float(last_ema),
        "touch_t": touch.t,
        "touch_high": touch.high,
        "touch_low": touch.low,
        "touch_close": touch.close,
        "touch_ema": touch_ema,
        "pullback_extreme": pullback_extreme,
        "cont_t": last.t,
        "cont_open": last.open,
        "cont_high": last.high,
        "cont_low": last.low,
        "cont_close": last.close,
        "structure_broken": _structure_broken(bias, side),
        "setup_id": f"{cfg.name}_{'long' if side > 0 else 'short'}_{touch.t}",
    }


def precondition(ctx: MarketContext, cfg: PullbackConfig, **_kw) -> bool:
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        # Still allow exit evaluation when flat filter blocks entries.
        if not ctx.position:
            return False
    if ctx.position:
        # Ask PB keys while in a position so PB4 can exit.
        return True
    if getattr(ctx, "mtf", None) is None and cfg.tf != "M1":
        return False
    return find_setup(ctx, cfg) is not None


def state_fields(ctx: MarketContext, cfg: PullbackConfig, **_kw) -> dict:
    setup = find_setup(ctx, cfg)
    if setup is None:
        # Minimal tape while positioned for exit questions.
        if not ctx.position:
            return {}
        bias = _higher_bias(ctx, cfg) or {}
        side = 1 if ctx.position > 0 else -1
        return {
            cfg.name: {
                "side": "long" if side > 0 else "short",
                "trend": {
                    "source": bias.get("source"),
                    "ema50": f"{bias['ema50']:.2f}" if bias.get("ema50") is not None else "n/a",
                    "ema200": f"{bias['ema200']:.2f}" if bias.get("ema200") is not None else "n/a",
                    "hh": bool(bias.get("hh")),
                    "hl": bool(bias.get("hl")),
                    "lh": bool(bias.get("lh")),
                    "ll": bool(bias.get("ll")),
                    "structure_broken": _structure_broken(bias, side) if bias else False,
                },
                "pullback": {},
                "continuation": {},
                "tape": {"positioned": True, "adx": None, "er": None},
            }
        }
    side = "long" if setup["side"] > 0 else "short"
    bias = setup["bias"]
    return {
        cfg.name: {
            "side": side,
            "trend": {
                "source": bias.get("source"),
                "ema50": f"{bias['ema50']:.2f}" if bias.get("ema50") is not None else "n/a",
                "ema200": f"{bias['ema200']:.2f}" if bias.get("ema200") is not None else "n/a",
                "close": f"{bias.get('close', 0):.2f}",
                "hh": bias.get("hh"),
                "hl": bias.get("hl"),
                "lh": bias.get("lh"),
                "ll": bias.get("ll"),
                "structure_broken": setup["structure_broken"],
            },
            "pullback": {
                "t": setup["touch_t"],
                f"ema{cfg.pullback_ema}": f"{setup['touch_ema']:.2f}",
                "high": f"{setup['touch_high']:.2f}",
                "low": f"{setup['touch_low']:.2f}",
                "close": f"{setup['touch_close']:.2f}",
                "extreme": f"{setup['pullback_extreme']:.2f}",
                "retracement": f"{setup['retracement']:.2f}",
            },
            "continuation": {
                "t": setup["cont_t"],
                "open": f"{setup['cont_open']:.2f}",
                "high": f"{setup['cont_high']:.2f}",
                "low": f"{setup['cont_low']:.2f}",
                "close": f"{setup['cont_close']:.2f}",
            },
            "tape": {
                "adx": setup.get("adx"),
                "er": setup.get("er"),
                "atr": f"{setup['atr']:.2f}",
            },
            "setup_id": setup["setup_id"],
        }
    }


def _risk(ctx: MarketContext, cfg: PullbackConfig, setup: dict) -> tuple[float, float, int] | Signal:
    lim = ctx_limits(ctx)
    point = float(ctx.point)
    if point <= 0:
        return hold_signal(cfg.name, f"{cfg.name}: bad point")
    atr = float(setup["atr"])
    spread = ctx_spread_points(ctx)
    min_sl = float(lim.min_sl_points.get(cfg.tf, DEFAULT_MIN_SL.get(cfg.tf, 200.0)))
    atr_sl = stop_points(
        tf=cfg.tf, atr=atr, point=point, k_sl=K_SL,
        min_sl=min_sl, stops_level=ctx_stops_level(ctx), spread_points=spread,
    )
    entry = float(ctx.ask if setup["side"] > 0 else ctx.bid)
    extreme = float(setup["pullback_extreme"])
    if setup["side"] > 0:
        beyond = extreme - K_SL * atr
        dist_pts = (entry - beyond) / point
    else:
        beyond = extreme + K_SL * atr
        dist_pts = (beyond - entry) / point
    sl_pts = max(atr_sl, dist_pts)
    max_share = float(lim.max_cost_share.get(cfg.tf, DEFAULT_COST_SHARE.get(cfg.tf, 0.10)))
    ok, why = cost_gate(sl_pts, spread, max_share)
    if not ok:
        return hold_signal(cfg.name, f"{cfg.name}: {why}")
    ok, why = risk_equity_ok(
        sl_pts, point,
        float(getattr(ctx, "tick_value", 0.0) or 0.0),
        float(getattr(ctx, "tick_size", 0.0) or 0.0),
        float(getattr(ctx, "lot", 0.01) or 0.01),
        getattr(ctx, "equity", None),
    )
    if not ok:
        return hold_signal(cfg.name, f"{cfg.name}: {why}")
    tp_pts = tp_from_rr(sl_pts, TP_RR)
    global_hold = int(getattr(ctx, "max_hold_seconds", 0) or getattr(lim, "global_max_hold", 900) or 0)
    override = hold_override_for(cfg.name, limits=lim, ctx=ctx)
    hold = hold_seconds_for(cfg.tf, cfg.max_bars, global_hold, override=override)
    return sl_pts, tp_pts, hold


def _exit_signal(
    ctx: MarketContext, cfg: PullbackConfig, response: Response, policy: Policy,
    features: dict, answers: dict, side: int,
) -> Signal | None:
    """Exit when code sees structure break or PB4 is yes."""
    if not ctx.position or (ctx.position > 0) != (side > 0):
        # Prefer position side when flat-filter path is not a matching setup side.
        if not ctx.position:
            return None
        side = 1 if ctx.position > 0 else -1
    pb1, pb2, pb3, pb4, pb5 = key_ids(cfg, side)
    labels = labels_for(response, (pb4,), policy)
    features["labels"] = {**(features.get("labels") or {}), **labels}
    features["nouls"] = {
        **(features.get("nouls") or {}),
        pb4: reading(response, pb4),
    }
    bias = _higher_bias(ctx, cfg)
    code_break = bool(bias and _structure_broken(bias, side))
    if code_break:
        features["table"] = "exit_structure"
        return Signal(
            "exit", f"{cfg.name}: structure broken (code)", cfg.name,
            features=features, answers=answers,
        )
    if labels.get(pb4) == "yes":
        features["table"] = "exit_pb4"
        return Signal(
            "exit", f"{cfg.name}: {pb4} yes", cfg.name,
            features=features, answers=answers,
        )
    if labels.get(pb4) == "mid":
        return hold_signal(cfg.name, f"{cfg.name}: {pb4} unsettled", features, answers)
    return hold_signal(cfg.name, f"{cfg.name}: hold position", features, answers)


def decide(
    ctx: MarketContext, response: Response, cfg: PullbackConfig,
    policy: Policy | None = None, **_kw,
) -> Signal:
    policy = policy or Policy()
    features: dict = {"strategy": cfg.name}
    answers: dict = {}

    # Positioned: evaluate exit first (code + PB4).
    if ctx.position:
        side = 1 if ctx.position > 0 else -1
        for key in key_ids(cfg, side):
            value = reading(response, key)
            if value is not None:
                answers[key] = {"type": "noul", "noul": value}
        features["side"] = "long" if side > 0 else "short"
        exited = _exit_signal(ctx, cfg, response, policy, features, answers, side)
        if exited is not None:
            return exited

    if getattr(ctx, "mtf", None) is None and cfg.tf != "M1":
        return hold_signal(cfg.name, f"{cfg.name}: no mtf", features, answers)

    setup = find_setup(ctx, cfg)
    if setup is None:
        return hold_signal(cfg.name, f"{cfg.name}: no setup", features, answers)

    side = int(setup["side"])
    pb_keys = key_ids(cfg, side)
    pb1, pb2, pb3, pb4, pb5 = pb_keys
    for key in pb_keys:
        value = reading(response, key)
        if value is not None:
            answers[key] = {"type": "noul", "noul": value}

    features.update({
        "setup_id": setup["setup_id"],
        "side": "long" if side > 0 else "short",
        "atr": round(setup["atr"], 4),
        "retracement": round(setup["retracement"], 4),
        "pullback_extreme": round(setup["pullback_extreme"], 4),
        "structure_broken": setup["structure_broken"],
    })
    if getattr(ctx, "avoid", None) is not None and ctx.avoid.blocks(ctx.now):
        return hold_signal(cfg.name, f"{cfg.name}: avoid hours", features, answers)

    labels = labels_for(response, pb_keys, policy)
    features["nouls"] = {k: reading(response, k) for k in pb_keys}
    features["labels"] = labels
    blocked = require_yes_no(labels, (pb1, pb2, pb3), (pb4, pb5))
    if blocked:
        return hold_signal(cfg.name, f"{cfg.name}: {blocked}", features, answers)

    if setup["structure_broken"]:
        return hold_signal(cfg.name, f"{cfg.name}: structure broken (code)", features, answers)

    risk = _risk(ctx, cfg, setup)
    if isinstance(risk, Signal):
        risk.features = {**features, **(risk.features or {})}
        risk.answers = answers
        return risk
    sl_pts, tp_pts, hold = risk
    features["sl_points"] = sl_pts
    features["tp_points"] = tp_pts
    if ctx.position and (ctx.position > 0) == (side > 0):
        return hold_signal(cfg.name, f"{cfg.name}: already positioned", features, answers)
    action = "buy" if side > 0 else "sell"
    features["table"] = action
    return entry_signal(
        action, cfg.name, f"{cfg.name}: {setup['setup_id']} PB1-3 yes PB4-5 no",
        sl_points=sl_pts, tp_points=tp_pts, max_hold_seconds=hold,
        features=features, answers=answers,
    )


# ---------------------------------------------------------------------------
# Specs used by thin wrappers / legacy aliases
# ---------------------------------------------------------------------------

def spec_m1(name: str = "pullback_m1") -> PullbackConfig:
    return PullbackConfig(
        name=name, tf="M1", higher_tf="M5", higher_mode="ema50_slope",
        pullback_ema=20, max_bars=30, shorts_need_allow=False,
    )


def spec_m5(name: str = "pullback_m5") -> PullbackConfig:
    return PullbackConfig(
        name=name, tf="M5", higher_tf="M15", higher_mode="ema50_slope",
        pullback_ema=20, max_bars=24, shorts_need_allow=False,
    )


def spec_m15(name: str = "pullback_m15") -> PullbackConfig:
    return PullbackConfig(
        name=name, tf="M15", higher_tf="H1", higher_mode="ema50_slope",
        pullback_ema=20, max_bars=16, shorts_need_allow=False,
    )


def spec_h1(name: str = "pullback_h1") -> PullbackConfig:
    return PullbackConfig(
        name=name, tf="H1", higher_tf="H4", higher_mode="ema50_ema200",
        pullback_ema=20, max_bars=24, shorts_need_allow=True,
    )


def spec_h4(name: str = "pullback_h4", *, also_h4_ema_allow_short: bool = False) -> PullbackConfig:
    return PullbackConfig(
        name=name, tf="H4", higher_tf=None, higher_mode="self_ema50_200",
        pullback_ema=50, max_bars=30, shorts_need_allow=True,
        also_h4_ema_allow_short=also_h4_ema_allow_short,
    )
