"""M5 structure direction filter. Never emits a trade on its own.

Flag ``--sig-pa-structure`` (default off). Blocks ``pa_rejection`` and
``pa_insidebar`` against the structure; other strategies are untouched.
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pa_common as pa
from .common import MarketContext, Policy, Signal, band, noul, reading

NAME = "pa_structure"
S1 = "pa_struct_s1"
S2 = "pa_struct_s2"


def questions() -> dict[str, Question]:
    return {
        S1: noul(
            "Does the M5 structure in `pa_structure.swings` show higher highs and higher lows?",
            "Higher highs and higher lows.",
            "The structure is not a clear sequence of higher highs and higher lows.",
        ),
        S2: noul(
            "Does the M5 structure in `pa_structure.swings` show lower highs and lower lows?",
            "Lower highs and lower lows.",
            "The structure is not a clear sequence of lower highs and lower lows.",
        ),
    }


def precondition(ctx: MarketContext) -> bool:
    m5 = pa.closed_m5(ctx.minutes)
    return pa.structure_swings(m5) is not None


def state_fields(ctx: MarketContext) -> dict:
    m5 = pa.closed_m5(ctx.minutes)
    struct = pa.structure_swings(m5)
    if struct is None:
        return {}
    return {
        "pa_structure": {
            "swings": {
                "highs": [f"{struct['highs'][0]:.2f}", f"{struct['highs'][1]:.2f}"],
                "lows": [f"{struct['lows'][0]:.2f}", f"{struct['lows'][1]:.2f}"],
                "hh": struct["hh"], "hl": struct["hl"], "lh": struct["lh"], "ll": struct["ll"],
            }
        }
    }


def allowed_side(ctx: MarketContext, response: Response, policy: Policy | None = None) -> tuple[int, dict]:
    """Return ``+1``, ``-1`` or ``0`` (blocked both ways) plus feature notes."""
    policy = policy or Policy()
    features: dict = {"strategy": NAME}
    m5 = pa.closed_m5(ctx.minutes)
    struct = pa.structure_swings(m5)
    if struct is None:
        features["table"] = "blocked"
        return 0, features
    features["swings"] = {
        "hh": struct["hh"], "hl": struct["hl"], "lh": struct["lh"], "ll": struct["ll"],
    }
    s1 = band(reading(response, S1), policy)
    s2 = band(reading(response, S2), policy)
    features["labels"] = {S1: s1, S2: s2}
    features["nouls"] = {S1: reading(response, S1), S2: reading(response, S2)}
    if s1 == "yes" and s2 == "no":
        features["table"] = "long_only"
        return 1, features
    if s2 == "yes" and s1 == "no":
        features["table"] = "short_only"
        return -1, features
    features["table"] = "blocked"
    return 0, features


def filter_signal(ctx: MarketContext, signal: Signal, response: Response | None,
                  policy: Policy | None = None) -> Signal:
    """Block rejection/inside-bar entries that fight the structure."""
    if signal.strategy not in ("pa_rejection", "pa_insidebar"):
        return signal
    if signal.action not in ("buy", "sell"):
        return signal
    if response is None:
        return Signal(
            "hold", f"{signal.strategy}: blocked by {NAME} (no response)",
            signal.strategy, features=dict(signal.features), answers=signal.answers,
        )
    side, feat = allowed_side(ctx, response, policy)
    features = dict(signal.features)
    features["pa_structure"] = feat
    want = 1 if signal.action == "buy" else -1
    if side == want:
        return Signal(
            action=signal.action, size=signal.size,
            reason=signal.reason + f" | {NAME}: aligned",
            strategy=signal.strategy, features=features,
            sl_points=signal.sl_points, tp_points=signal.tp_points,
            max_hold_seconds=signal.max_hold_seconds,
            composite=signal.composite, answers=signal.answers,
        )
    return Signal(
        action="hold",
        reason=f"{signal.strategy}: blocked by {NAME}",
        strategy=signal.strategy, features=features, answers=signal.answers,
        composite=signal.composite,
    )
