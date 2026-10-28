"""H4 EMA50 pullback in an EMA50/EMA200 trend regime.

Thin wrapper around ``_pullback_common``. Shorts require ``--allow-short``.
Flag ``--sig-pullback-h4`` / ``--strategies pullback_h4`` (default off).
Legacy alias: ``h4_ema_pullback`` (also honours ``--h4-ema-allow-short``).
"""
from __future__ import annotations

from ..typesafe import Question, Response
from . import _pullback_common as pb
from .common import MarketContext, Policy, Signal

CFG = pb.spec_h4()
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
