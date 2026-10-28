"""M15 trend pullback to EMA20 with H1 EMA50 bias.

Thin wrapper around ``_pullback_common``. Shorts are always allowed on M1–M15.
Flag ``--sig-pullback-m15`` / ``--strategies pullback_m15`` (default off).
Legacy alias: ``m15_trend_pullback``.
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pullback_common as pb
from .common import MarketContext, Policy, Signal

CFG = pb.spec_m15()
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
