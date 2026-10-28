"""Consensus strategy: the existing 14 Noul questions and truth table.

Flag ``--sig-consensus`` (default on). Always asks its questions when enabled.
Exit Nouls are omitted when the position is flat.
"""
from __future__ import annotations

from ..questions import (
    EXIT_LONG,
    EXIT_SHORT,
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


def questions(ctx: MarketContext | None = None) -> dict[str, Question]:
    qs = dict(decision_questions())
    if ctx is not None and int(getattr(ctx, "position", 0) or 0) == 0:
        qs.pop(EXIT_LONG, None)
        qs.pop(EXIT_SHORT, None)
    return qs


def question_ids(ctx: MarketContext | None = None) -> tuple[str, ...]:
    if ctx is not None and int(getattr(ctx, "position", 0) or 0) == 0:
        return tuple(k for k in QUESTION_IDS if k not in (EXIT_LONG, EXIT_SHORT))
    return QUESTION_IDS


def precondition(ctx: MarketContext) -> bool:
    """Always ready. Exit questions are omitted when flat."""
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
