"""Legacy flag name for the unified H4 EMA pullback.

Delegates to ``_pullback_common`` with ``name=h4_ema_pullback`` so existing
``--strategies h4_ema_pullback`` / ``h4`` group wiring keeps working.
Prefer ``pullback_h4`` for new configs. Shorts need ``--allow-short`` or
``--h4-ema-allow-short``.
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pullback_common as pb
from .common import MarketContext, Policy, Signal

CFG = pb.spec_h4(name="h4_ema_pullback", also_h4_ema_allow_short=True)
NAME = CFG.name


def questions() -> dict[str, Question]:
    return pb.questions_for(CFG)


def find_setup(ctx: MarketContext, **kw) -> dict | None:
    return pb.find_setup(ctx, CFG)


def precondition(ctx: MarketContext, **kw) -> bool:
    return pb.precondition(ctx, CFG, **kw)


def state_fields(ctx: MarketContext, **kw) -> dict:
    return pb.state_fields(ctx, CFG, **kw)


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None, **kw) -> Signal:
    return pb.decide(ctx, response, CFG, policy=policy, **kw)
