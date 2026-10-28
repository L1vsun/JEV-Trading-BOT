"""Offline stand-in for Jev.

Answers the bot's own Noul questions with the same typed shape by reading the
semantic state back through the vocabularies in state.py. It never sees a raw
number the real model would not see, and it does not read queue depth.

It exists so the project runs, tests and renders without an API key. It is a
hand-written rule model, not Jev. Swap in ``JevClient`` for real decisions.
"""
from __future__ import annotations

import json
import math
from typing import Any

from . import state as S
from .questions import QUESTION_IDS
from .typesafe import Answer, Noul, Question, Response


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _n(x: float) -> float:
    """Map a signed reading near ±1 onto a Noul. Zero stays near 0.5."""
    return round(_sig(5 * x), 4)


class JevSurrogate:
    model = "surrogate-local"

    def _read(self, st: dict) -> dict[str, float]:
        v = S.value_of
        flow = st["order_flow"]
        seq = [v(word, S.FLOW) for word in flow.get("aggressor_tape_30s_windows_oldest_first") or []]
        candle = st.get("forming_candle") or {}
        book = st.get("order_book") or {}
        position = str(st.get("position") or "")
        spread = book.get("spread", S.SPREAD[1])
        return {
            "p5": v(st["price_action"]["last_5_minutes"], S.PRICE),
            "f60": v(flow["last_1_minute"], S.FLOW),
            "tape": sum(seq) / len(seq) if seq else 0.0,
            "spread": S.SPREAD.index(spread) / 3,
            "vol": S.VOL.index(st["volatility"]) / 4,
            "gap": v(st["fair_value_gap"], S.GAP),
            "c_ret": v(candle.get("so_far", S.CANDLE_SO_FAR[3]), S.CANDLE_SO_FAR),
            "pos": 1.0 if position.startswith("long") else -1.0 if position.startswith("short") else 0.0,
        }

    def system_one(self, state: Any, questions: dict[str, Question]) -> Response:
        f = self._read(state)
        readings = {
            "tape_buys_up": _n(2 * f["tape"]),
            "tape_sells_down": _n(-2 * f["tape"]),
            "trend_up": _n(2 * f["p5"]),
            "trend_down": _n(-2 * f["p5"]),
            "flow_up": _n(2 * f["f60"]),
            "flow_down": _n(-2 * f["f60"]),
            "candle_up": _n(2 * f["c_ret"]),
            "candle_down": _n(-2 * f["c_ret"]),
            "price_below_flow": _n(2 * f["gap"]),
            "price_above_flow": _n(-2 * f["gap"]),
            "spread_wide": round(_sig(8 * (f["spread"] - 0.5)), 4),
            "volatility_violent": round(_sig(20 * (f["vol"] - 0.9)), 4),
        }
        if f["pos"] > 0:
            readings["exit_against_long"] = _n(-2 * f["f60"])
            readings["exit_against_short"] = _n(-2)
        elif f["pos"] < 0:
            readings["exit_against_long"] = _n(-2)
            readings["exit_against_short"] = _n(2 * f["f60"])
        else:
            readings["exit_against_long"] = _n(-2)
            readings["exit_against_short"] = _n(-2)
        out: dict[str, Answer] = {}
        for qid, q in questions.items():
            if isinstance(q, Noul):
                out[qid] = Answer("noul", noul=readings.get(qid, 0.5))
            else:
                out[qid] = Answer("noul", noul=0.5)
        tokens = len(json.dumps({"state": state, "questions": {k: q.to_json() for k, q in questions.items()}})) // 4
        return Response(self.model, out, tokens, 8 * len(questions or QUESTION_IDS), 0.0)
