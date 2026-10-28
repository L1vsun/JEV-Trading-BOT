"""Questions, thresholds and the truth table for one live decision.

Jev answers one observable statement per question. This module maps those
answers onto buy, sell, hold or exit. Confidence is not a gate. A missing
answer is an error and holds; it is not read as a no.

Noul bands, from the TypeSafe starting point:

* ``noul >= 0.7`` is yes
* ``noul <= 0.3`` is no
* anything between is unsettled, and the row for that band is hold
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .typesafe import Noul, Question, Response

YES = 0.7
NO = 0.3

BUY_SIDE = (
    "tape_buys_up",
    "trend_up",
    "flow_up",
    "candle_up",
    "price_below_flow",
)
SELL_SIDE = (
    "tape_sells_down",
    "trend_down",
    "flow_down",
    "candle_down",
    "price_above_flow",
)
VETOES = ("spread_wide", "volatility_violent")
EXIT_LONG = "exit_against_long"
EXIT_SHORT = "exit_against_short"
QUESTION_IDS = BUY_SIDE + SELL_SIDE + VETOES + (EXIT_LONG, EXIT_SHORT)


def _noul(question: str, yes: str, no: str) -> Noul:
    return Noul(question, {"true": yes, "false": no})


def decision_questions() -> dict[str, Question]:
    """One judgment each. No question asks what the trader should do."""
    return {
        "tape_buys_up": _noul(
            "Do the later windows in `order_flow.aggressor_tape_30s_windows_oldest_first` show aggressive buying?",
            "Those windows are light buying, moderate buying or heavy buying.",
            "Those windows are balanced or any kind of selling.",
        ),
        "tape_sells_down": _noul(
            "Do the later windows in `order_flow.aggressor_tape_30s_windows_oldest_first` show aggressive selling?",
            "Those windows are light selling, moderate selling or heavy selling.",
            "Those windows are balanced or any kind of buying.",
        ),
        "trend_up": _noul(
            "Is price rising in `price_action.last_5_minutes`?",
            "It is drifting higher, rising steadily or rising fast.",
            "It is flat, drifting lower, falling steadily or falling fast.",
        ),
        "trend_down": _noul(
            "Is price falling in `price_action.last_5_minutes`?",
            "It is drifting lower, falling steadily or falling fast.",
            "It is flat, drifting higher, rising steadily or rising fast.",
        ),
        "flow_up": _noul(
            "Is `order_flow.last_1_minute` buying?",
            "It is light buying, moderate buying or heavy buying.",
            "It is balanced, light selling, moderate selling or heavy selling.",
        ),
        "flow_down": _noul(
            "Is `order_flow.last_1_minute` selling?",
            "It is light selling, moderate selling or heavy selling.",
            "It is balanced, light buying, moderate buying or heavy buying.",
        ),
        "candle_up": _noul(
            "Is `forming_candle.so_far` up?",
            "It is up slightly, up moderately or up sharply.",
            "It is unchanged, down slightly, down moderately or down sharply.",
        ),
        "candle_down": _noul(
            "Is `forming_candle.so_far` down?",
            "It is down slightly, down moderately or down sharply.",
            "It is unchanged, up slightly, up moderately or up sharply.",
        ),
        "price_below_flow": _noul(
            "Does `fair_value_gap` place the price below fair value?",
            "It says slightly below fair value, below where the order flow says it should be, or far below that.",
            "It says the price is in line with fair value or above it.",
        ),
        "price_above_flow": _noul(
            "Does `fair_value_gap` place the price above fair value?",
            "It says slightly above fair value, above where the order flow says it should be, or far above that.",
            "It says the price is in line with fair value or below it.",
        ),
        "spread_wide": _noul(
            "Is `order_book.spread` wide?",
            "The spread is wide or very wide.",
            "The spread is one tick, tight, or two ticks, normal.",
        ),
        "volatility_violent": _noul(
            "Is `volatility` very volatile?",
            "Volatility is very volatile.",
            "Volatility is very calm, calm, normal or volatile.",
        ),
        "exit_against_long": _noul(
            "Is `order_flow.last_1_minute` selling while `position` is a long?",
            "The position starts with long, and the one-minute flow is light, moderate or heavy selling.",
            "The position is flat or short, or the one-minute flow is balanced or buying.",
        ),
        "exit_against_short": _noul(
            "Is `order_flow.last_1_minute` buying while `position` is a short?",
            "The position starts with short, and the one-minute flow is light, moderate or heavy buying.",
            "The position is flat or long, or the one-minute flow is balanced or selling.",
        ),
    }


def state_for_model(state: dict) -> dict:
    """The fields the questions name, and nothing else.

    Queue imbalance, depth and micro-price are left out. On a level-1 book
    those strings are placeholders, and a placeholder must not count as a
    queue. Spread stays, because it is the bid-ask quote.
    """
    flow = state.get("order_flow") or {}
    price = state.get("price_action") or {}
    book = state.get("order_book") or {}
    candle = state.get("forming_candle") or {}
    out = {
        "price_action": {"last_5_minutes": price.get("last_5_minutes", "")},
        "order_flow": {
            "last_1_minute": flow.get("last_1_minute", ""),
            "aggressor_tape_30s_windows_oldest_first": list(flow.get("aggressor_tape_30s_windows_oldest_first") or []),
        },
        "order_book": {"spread": book.get("spread", "")},
        "volatility": state.get("volatility", ""),
        "fair_value_gap": state.get("fair_value_gap", ""),
        "forming_candle": {"so_far": candle.get("so_far", "")},
        "position": state.get("position", "flat"),
    }
    if "data_quality" in state:
        out["data_quality"] = state["data_quality"]
    return out


@dataclass(frozen=True)
class Policy:
    """The two Noul cuts. The band between them is its own row: hold."""

    yes: float = YES
    no: float = NO


@dataclass
class Decision:
    t: int
    action: str                  # buy | sell | hold | exit
    size: float                  # 0 or 1
    composite: float             # trace of the pairs, not a gate
    gates: dict[str, bool]
    reason: str
    answers: dict | None = None
    strategy: str = ""
    features: dict = field(default_factory=dict)
    sl_points: float | None = None
    tp_points: float | None = None
    max_hold_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.answers is None:
            self.answers = {}



def _reading(response: Response, key: str) -> float | None:
    """The Noul, or None when the answer is missing or not a Noul.

    None is an error. Callers must not treat it as zero.
    """
    answer = response.answers.get(key)
    if answer is None or answer.type != "noul" or answer.noul is None:
        return None
    return float(answer.noul)


def _band(value: float | None, policy: Policy) -> str:
    if value is None:
        return "missing"
    if value >= policy.yes:
        return "yes"
    if value <= policy.no:
        return "no"
    return "mid"


def composite_trace(labels: dict[str, str]) -> float:
    """Mean of +1 / -1 / 0 across the five pairs. Visible in the log, not a gate."""
    score = 0
    for up, down in zip(BUY_SIDE, SELL_SIDE):
        if labels.get(up) == "yes" and labels.get(down) == "no":
            score += 1
        elif labels.get(down) == "yes" and labels.get(up) == "no":
            score -= 1
    return score / len(BUY_SIDE)


def decide(t: int, response: Response, position: int, policy: Policy | None = None) -> Decision:
    """Every band combination has a row. The action is never read off confidence."""
    policy = policy or Policy()
    labels = {key: _band(_reading(response, key), policy) for key in QUESTION_IDS}
    answers = {key: answer.to_json() for key, answer in response.answers.items()}
    trace = composite_trace(labels)
    missing = [key for key, band in labels.items() if band == "missing"]

    def row(action: str, size: float, reason: str, **gates: bool) -> Decision:
        base = {
            "answers_present": not missing,
            "exit_clear": True,
            "settled": True,
            "liquidity_clear": True,
            "one_side": action in ("buy", "sell"),
        }
        base.update(gates)
        return Decision(t, action, size, trace, base, reason, answers)

    if missing:
        return row("hold", 0.0, "hold: answer missing (" + ", ".join(missing) + "); not read as no",
                   answers_present=False, exit_clear=False, settled=False, liquidity_clear=False)

    exit_key = EXIT_LONG if position > 0 else EXIT_SHORT if position < 0 else None
    if exit_key is not None:
        band = labels[exit_key]
        if band == "yes":
            return row("exit", 0.0, f"exit: {exit_key} >= {policy.yes:g}", exit_clear=False, one_side=False)
        if band == "mid":
            return row("hold", 0.0, f"hold: {exit_key} unsettled ({policy.no:g} to {policy.yes:g})",
                       exit_clear=False, one_side=False)

    directional = BUY_SIDE + SELL_SIDE + VETOES
    if any(labels[key] == "mid" for key in directional):
        names = [key for key in directional if labels[key] == "mid"]
        return row("hold", 0.0, "hold: unsettled (" + ", ".join(names) + ")", settled=False, one_side=False)

    if any(labels[key] == "yes" for key in VETOES):
        names = [key for key in VETOES if labels[key] == "yes"]
        return row("hold", 0.0, "hold: liquidity veto (" + ", ".join(names) + ")", liquidity_clear=False, one_side=False)

    buy = all(labels[key] == "yes" for key in BUY_SIDE) and all(labels[key] == "no" for key in SELL_SIDE)
    sell = all(labels[key] == "yes" for key in SELL_SIDE) and all(labels[key] == "no" for key in BUY_SIDE)
    if buy and sell:
        return row("hold", 0.0, "hold: both sides", one_side=False)
    if buy:
        if position > 0:
            return row("hold", 0.0, "hold: already long", one_side=False)
        return row("buy", 1.0, f"buy: buy-side >= {policy.yes:g}, sell-side <= {policy.no:g}, vetoes no")
    if sell:
        if position < 0:
            return row("hold", 0.0, "hold: already short", one_side=False)
        return row("sell", 1.0, f"sell: sell-side >= {policy.yes:g}, buy-side <= {policy.no:g}, vetoes no")
    return row("hold", 0.0, "hold: no setup", one_side=False)
