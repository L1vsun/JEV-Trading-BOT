"""Consensus strategy: the existing 14 Noul questions and truth table.

Flag ``--sig-consensus`` (default on). Always asks its questions when enabled.
"""
from __future__ import annotations

from ..questions import (
    QUESTION_IDS,
    Decision,
    Policy,
    decision_questions,
    decide as consensus_decide,
    state_for_model,
)
from ..typesafe import Question, Response
from .common import MarketContext, Signal


NAME = "consensus"


def questions() -> dict[str, Question]:
    return decision_questions()


def question_ids() -> tuple[str, ...]:
    return QUESTION_IDS


def precondition(ctx: MarketContext) -> bool:
    """Always ready. The exit questions are speculative when flat."""
    return True


def state_fields(ctx: MarketContext) -> dict:
    return state_for_model(ctx.semantic)


def decide(ctx: MarketContext, response: Response, policy: Policy | None = None) -> Signal:
    d: Decision = consensus_decide(ctx.bar_t, response, ctx.position, policy)
    return Signal(
        action=d.action,
        size=d.size,
        reason=f"{NAME}: {d.reason}",
        strategy=NAME,
        composite=d.composite,
        answers=d.answers or {},
        features={"composite_trace": d.composite},
    )
