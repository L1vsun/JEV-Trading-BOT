"""H1 trend pullback to EMA20 with H4 EMA50/EMA200 bias.

Thin wrapper around ``_pullback_common``. Shorts require ``--allow-short``.
Flag ``--sig-pullback-h1`` / ``--strategies pullback_h1`` (default off).
Legacy alias: ``h1_pullback``.
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pullback_common as pb
from .common import MarketContext, Policy, Signal

CFG = pb.spec_h1()
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
