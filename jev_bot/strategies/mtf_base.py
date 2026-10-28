"""Shared helpers for M5–H4 strategies: cost/equity gates, min SL, TypeSafe banding."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..bars import TF_SECONDS, Bar
from ..indicators import atr14
from ..typesafe import Response
from .common import Policy, Signal, band, clamp_hold, noul, reading

# Minimum stop distance in points (1 point = 0.01 USD on XAUUSD).
# Overridable via --min-sl-m5 / --min-sl-m15 / --min-sl-h1 / --min-sl-h4.
MIN_SL_POINTS = {"M1": 200.0, "M5": 300.0, "M15": 500.0, "H1": 1000.0, "H4": 2000.0}
# ATR multiples. H1 trend-following uses 2.5 in h1_trend_long; H4 default 2.5
# sits in the 2.0–3.0 band. Global --sl-points 200 clamps M1 / 1-s only.
DEFAULT_K_SL = {"M1": 1.2, "M5": 1.5, "M15": 1.5, "H1": 2.0, "H4": 2.5}
DEFAULT_MAX_BARS = {"M1": 30, "M5": 24, "M15": 20, "H1": 24, "H4": 30}
DEFAULT_COST_SHARE = {"M1": 0.10, "M5": 0.10, "M15": 0.10, "H1": 0.07, "H4": 0.05}


@dataclass(frozen=True)
class MtfLimits:
    min_sl_points: dict
    k_sl: dict
    max_bars: dict
    max_cost_share: dict
    allow_short: bool = False
    global_max_hold: int = 900
    max_spread_points: float = 30.0
    # Per-strategy hold seconds; may exceed global_max_hold when set via CLI.
    hold_overrides: dict = field(default_factory=dict)


def default_mtf_limits() -> MtfLimits:
    return MtfLimits(
        min_sl_points=dict(MIN_SL_POINTS),
        k_sl=dict(DEFAULT_K_SL),
        max_bars=dict(DEFAULT_MAX_BARS),
        max_cost_share=dict(DEFAULT_COST_SHARE),
        hold_overrides={},
    )


def labels_for(response: Response, keys: tuple[str, ...], policy: Policy | None = None) -> dict[str, str]:
    policy = policy or Policy()
    return {k: band(reading(response, k), policy) for k in keys}


def require_yes_no(labels: dict[str, str], yes_keys: tuple[str, ...], no_keys: tuple[str, ...]) -> str | None:
    """Return a hold reason, or None when the table passes."""
    for k in yes_keys:
        lab = labels.get(k, "missing")
        if lab == "missing":
            return f"{k} missing; not read as no"
        if lab == "mid":
            return f"{k} unsettled"
        if lab != "yes":
            return f"{k} no"
    for k in no_keys:
        lab = labels.get(k, "missing")
        if lab in ("missing", "mid"):
            return f"{k} {lab}; not a clear no"
        if lab != "no":
            return f"{k} yes"
    return None


def cost_pts(spread_points: float) -> float:
    return float(spread_points) + 6.0


def stop_points(*, tf: str, atr: float, point: float, k_sl: float, min_sl: float,
                stops_level: int, spread_points: float) -> float:
    """max(k_sl*ATR, min_sl, stops_level+spread) in points.

    Never clamp the result to the global M1 ``--sl-points`` (200). Higher-TF
    stops are gated by the cost share and equity-plausibility checks instead.
    """
    atr_pts = (k_sl * atr) / point if point > 0 else 0.0
    floor = float(min_sl)
    broker = float(stops_level) + float(spread_points)
    return max(atr_pts, floor, broker)


def cost_gate(sl_pts: float, spread_points: float, max_share: float) -> tuple[bool, str]:
    if sl_pts <= 0:
        return False, "cost: sl unset"
    share = cost_pts(spread_points) / sl_pts
    if share > max_share + 1e-12:
        return False, f"cost share {share:.3f} > {max_share:.3f}"
    return True, "cost ok"


def tp_from_rr(sl_pts: float, rr: float = 2.0) -> float:
    return float(sl_pts) * float(rr)


def risk_equity_ok(sl_pts: float, point: float, tick_value: float, tick_size: float,
                   lot: float, equity: float | None) -> tuple[bool, str]:
    """Hold when loss at SL for one new order would exceed current account equity.

    Plausibility only — not a daily budget. When equity is missing or unreadable
    the check is skipped so a brief ``account_info`` gap does not block entries.
    """
    if tick_size <= 0 or point <= 0:
        return False, "risk>equity: tick size"
    if equity is None:
        return True, "risk ok"
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        return True, "risk ok"
    if not (eq == eq) or eq <= 0:  # NaN or non-positive
        return False, "risk>equity: equity unusable"
    # Distance in price = sl_pts * point; loss ≈ (dist/tick_size)*tick_value*lot
    dist = sl_pts * point
    loss = (dist / tick_size) * tick_value * lot
    if loss > eq + 1e-9:
        return False, f"risk>equity loss={loss:.2f} > equity={eq:.2f}"
    return True, "risk ok"


def hold_seconds_for(tf: str, max_bars: int, global_hold: int, override: int | None = None) -> int:
    """Strategy hold from max_bars; may exceed global only with an explicit override."""
    tf_sec = TF_SECONDS.get(tf, 300)
    wanted = int(max_bars) * int(tf_sec)
    if override is not None and override > 0:
        return int(override)
    return clamp_hold(wanted, global_hold if global_hold > 0 else wanted)


def hold_override_for(strategy: str, *, limits: MtfLimits | None = None,
                      ctx=None) -> int | None:
    """CLI / ctx per-strategy hold override, or ``None`` when unset."""
    overs: dict = {}
    if limits is not None:
        overs.update(getattr(limits, "hold_overrides", None) or {})
    if ctx is not None:
        overs.update(getattr(ctx, "hold_overrides", None) or {})
        lim = getattr(ctx, "mtf_limits", None)
        if lim is not None:
            overs.update(getattr(lim, "hold_overrides", None) or {})
    if strategy not in overs:
        return None
    try:
        value = int(overs[strategy])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def entry_signal(action: str, strategy: str, reason: str, *, sl_points: float, tp_points: float,
                 max_hold_seconds: int, features: dict, answers: dict) -> Signal:
    return Signal(
        action=action, size=1.0, reason=reason, strategy=strategy,
        features=features, answers=answers,
        sl_points=sl_points, tp_points=tp_points, max_hold_seconds=max_hold_seconds,
    )


def hold_signal(strategy: str, reason: str, features: dict | None = None,
                answers: dict | None = None) -> Signal:
    return Signal("hold", reason, strategy, features=features or {}, answers=answers or {})


def atr_or_hold(bars: list[Bar], strategy: str) -> tuple[float | None, Signal | None]:
    atr = atr14(bars)
    if atr is None:
        return None, hold_signal(strategy, f"{strategy}: insufficient history for ATR")
    return atr, None
