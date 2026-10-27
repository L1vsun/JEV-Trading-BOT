"""The questions Jev answers on every decision tick.

One request carries all of them (speculative fan-out: they are evaluated in
parallel, so the exit question is asked even when flat and simply ignored).
Each question is atomic, literal and points at the state field it needs by
name in backticks, following the Jev 1.13 guidance on literal reading and
indirection. Arithmetic stays in code.
"""
from __future__ import annotations

from .typesafe import Choice, Noul, Question, Score

ACTION_OPTIONS = {
    "buy": "Order flow, the book and the fair value gap all point up, so a long position is justified now",
    "sell": "Order flow, the book and the fair value gap all point down, so a short position is justified now",
    "hold": "The evidence is mixed, weak or contradictory, so no new position",
}


def decision_questions() -> dict[str, Question]:
    return {
        # --- market regime and who is in control
        "regime": Choice(
            instructions="Which regime best describes `price_action` together with `volatility`?",
            criteria={
                "trending_up": "Price is moving higher in a sustained way",
                "trending_down": "Price is moving lower in a sustained way",
                "ranging": "Price is going sideways without direction",
                "disorderly": "Moves are violent and erratic with no clear direction",
            }),
        "flow_control": Choice(
            instructions="Who is in control of `order_flow` right now?",
            criteria={"buyers": "Buying aggression dominates", "sellers": "Selling aggression dominates",
                      "neither": "Buying and selling are roughly balanced"}),
        # --- the microstructure edge
        "informed_flow": Noul(
            instructions="Does `order_flow.aggressor_tape_30s_windows_oldest_first` show the same side "
                         "aggressing window after window, with `flow_toxicity` elevated or extreme?",
            criteria={"true": "Persistent one-sided aggression with elevated toxicity",
                      "false": "Aggression switches sides or toxicity is low or normal"}),
        "price_lagging_flow": Noul(
            instructions={
                "question": "Is the price lagging behind the order flow, so that `fair_value_gap` describes "
                            "a gap that the price has not closed yet?",
                "note": "A gap exists only when `fair_value_gap` is not 'price is in line with fair value'.",
            },
            criteria={"true": "Price is clearly away from where the flow says it should be",
                      "false": "Price is in line with the flow"}),
        "book_pressure": Choice(
            instructions="Which way does `order_book` lean, using `best_level`, `top_five_levels` and `micro_price`? "
                         "If `data_quality.book_signals` is degraded, those three fields are placeholders from a "
                         "level-1 book and do not show a real queue.",
            criteria={"up": "The book favours the price ticking up", "down": "The book favours the price ticking down",
                      "neutral": "The book is balanced"}),
        # --- reading the forming candle
        "candle_close": Choice(
            instructions="Judging only by the shape of `forming_candle`, how will this candle most likely close?",
            criteria={"up": "Close above its open", "down": "Close below its open", "unclear": "Too early or too mixed to tell"}),
        # --- risk
        "liquidity_stress": Score(
            instructions="How stressed is liquidity, from `order_book.spread`, `volatility` and `flow_toxicity`?",
            criteria=["Calm: tight spread, normal volatility, low or normal toxicity",
                      "Strained: one of spread, volatility or toxicity is elevated",
                      "Stressed: wide spread or very volatile with extreme toxicity"]),
        "setup_quality": Score(
            instructions="How strong is the case for opening a position now, if every field agrees on one direction?",
            criteria=["No setup: fields disagree", "Weak: a few fields agree", "Decent: most fields agree",
                      "Strong: flow, book and fair value gap agree", "Exceptional: everything agrees and the gap is large"]),
        # --- the decision itself, asked three ways for self-consistency
        "action": Choice(instructions="What should the trader do now?", criteria=ACTION_OPTIONS),
        "action_alt": Choice(
            instructions={"question": "Given `order_flow`, `order_book` and `fair_value_gap`, pick the trade.",
                          "rule": "Only choose buy or sell when all three point the same way."},
            criteria=ACTION_OPTIONS),
        "action_contrarian_check": Choice(
            instructions="A skeptical risk manager reviews this state. Which trade, if any, would they approve?",
            criteria=ACTION_OPTIONS),
        # --- speculative: only read when a position is open
        "exit_now": Noul(
            instructions="Given `position`, has the order flow turned against it or has the `fair_value_gap` closed?",
            criteria={"true": "Flow now opposes the position or the gap is gone", "false": "The reason for the position still holds"}),
    }
