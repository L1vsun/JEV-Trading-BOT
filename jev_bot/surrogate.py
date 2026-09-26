"""Offline stand-in for Jev.

Answers the bot's own question set with the same typed shapes (Noul /
Choice / Score, probabilities, confidence) by reading the *semantic* state
back through the vocabularies in state.py. It never sees a raw number the
real model would not see.

It exists so the project runs, tests and renders without an API key. It is a
hand-written rule model, not Jev, and it does not know anything about
questions outside ``questions.decision_questions()``. Swap in ``JevClient``
for real decisions.
"""
from __future__ import annotations

import json
import math
from typing import Any

from . import state as S
from .typesafe import Answer, Choice, Noul, Question, Response, Score, confidence_from


def _softmax(logits: dict[str, float], temp: float = 1.0) -> dict[str, float]:
    m = max(logits.values())
    e = {k: math.exp((v - m) / temp) for k, v in logits.items()}
    z = sum(e.values())
    return {k: round(v / z, 4) for k, v in e.items()}


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


class JevSurrogate:
    model = "surrogate-local"

    def _read(self, st: dict) -> dict[str, float]:
        v = S.value_of
        of = st["order_flow"]
        seq = [v(w, S.FLOW) for w in of["aggressor_tape_30s_windows_oldest_first"]]
        same = sum(1 for x in seq if x * (seq[-1] or 1e-9) > 0.15) / len(seq)
        c = st["forming_candle"]
        el = int(c["progress"].split()[0]) / int(c["progress"].split()[2])
        path = {S.CANDLE_PATH[0]: -0.5, S.CANDLE_PATH[1]: 0.0, S.CANDLE_PATH[2]: 0.6}[c["path"]]
        return {
            "p1": v(st["price_action"]["last_1_minute"], S.PRICE), "p5": v(st["price_action"]["last_5_minutes"], S.PRICE),
            "f15": v(of["last_15_seconds"], S.FLOW), "f60": v(of["last_1_minute"], S.FLOW), "f180": v(of["last_3_minutes"], S.FLOW),
            "tape": sum(seq) / len(seq), "persist": same,
            "book": v(st["order_book"]["best_level"], S.BOOK), "depth": v(st["order_book"]["top_five_levels"], S.BOOK),
            "micro": v(st["order_book"]["micro_price"], S.MICRO),
            "spread": S.SPREAD.index(st["order_book"]["spread"]) / 3, "tox": S.TOX.index(st["flow_toxicity"]) / 3,
            "vol": S.VOL.index(st["volatility"]) / 4, "gap": v(st["fair_value_gap"], S.GAP),
            "c_ret": v(c["so_far"], S.CANDLE_SO_FAR), "c_loc": v(c["location_in_range"], S.CANDLE_LOC),
            "c_path": path, "c_ticks": v(c["ticks"], S.TICKS), "c_el": el,
            "pos": 1.0 if st["position"].startswith("long") else -1.0 if st["position"].startswith("short") else 0.0,
        }

    def system_one(self, state: Any, questions: dict[str, Question]) -> Response:
        f = self._read(state)
        flow = 0.2 * f["f15"] + 0.45 * f["f60"] + 0.35 * f["f180"]
        book = (f["book"] + f["depth"] + f["micro"]) / 3
        direction = 0.4 * flow + 0.2 * book + 0.4 * f["gap"]
        agree = [x for x in (flow, book, f["gap"]) if abs(x) > 0.15]
        aligned = len(agree) == 3 and (all(x > 0 for x in agree) or all(x < 0 for x in agree))
        candle = f["c_ret"] * (0.4 + 0.6 * f["c_el"]) + 0.3 * f["c_loc"] * f["c_el"] + 0.2 * f["c_ticks"] + 0.25 * f["c_path"] * f["c_ret"]
        stress = 0.4 * f["spread"] + 0.3 * f["vol"] + 0.3 * f["tox"]
        out: dict[str, Answer] = {}
        for qid, q in questions.items():
            if qid == "regime":
                pr = _softmax({"trending_up": 3 * f["p5"] + f["p1"], "trending_down": -3 * f["p5"] - f["p1"],
                               "ranging": 1.2 - 2 * abs(f["p5"]), "disorderly": 5 * (f["vol"] - 0.6)}, 0.8)
            elif qid == "flow_control":
                pr = _softmax({"buyers": 4 * flow, "sellers": -4 * flow, "neither": 1 - 3 * abs(flow)}, 0.8)
            elif qid == "book_pressure":
                pr = _softmax({"up": 4 * book, "down": -4 * book, "neutral": 1 - 3 * abs(book)}, 0.8)
            elif qid == "candle_close":
                pr = _softmax({"up": 3 * candle, "down": -3 * candle, "unclear": 1.3 - 2.5 * abs(candle) - f["c_el"]}, 0.8)
            elif qid == "informed_flow":
                out[qid] = Answer("noul", noul=round(_sig(6 * (f["persist"] * abs(f["tape"]) + 0.5 * f["tox"] - 0.55)), 4)); continue
            elif qid == "price_lagging_flow":
                out[qid] = Answer("noul", noul=round(_sig(7 * (abs(f["gap"]) - 0.4)), 4)); continue
            elif qid == "exit_now":
                against = -f["pos"] * direction
                out[qid] = Answer("noul", noul=round(_sig(6 * (against + 0.25 - abs(f["gap"]))) if f["pos"] else 0.5, 4)); continue
            elif qid == "liquidity_stress":
                pr = _softmax({"0": 2 - 5 * stress, "1": 0.6, "2": 5 * stress - 2.4}, 0.7)
            elif qid == "setup_quality":
                s = abs(direction) * (1.6 if aligned else 0.6)
                pr = _softmax({str(i): -abs(s * 5 - i) * 1.3 for i in range(5)}, 1.0)
            elif qid.startswith("action"):
                temp = {"action": 0.55, "action_alt": 0.5, "action_contrarian_check": 0.7}.get(qid, 0.6)
                bar = 0.18 if qid != "action_contrarian_check" else 0.28
                strength = abs(direction) - bar - (0.0 if aligned else 0.15) - 0.25 * stress
                d = 1 if direction > 0 else -1
                pr = _softmax({"buy": 5 * strength if d > 0 else -2, "sell": 5 * strength if d < 0 else -2, "hold": 0.3}, temp)
            else:
                pr = self._uniform(q)
            out[qid] = self._answer(q, pr)
        tokens = len(json.dumps({"state": state, "questions": {k: q.to_json() for k, q in questions.items()}})) // 4
        return Response(self.model, out, tokens, 8 * len(questions), 0.0)

    @staticmethod
    def _uniform(q: Question) -> dict[str, float]:
        if isinstance(q, Choice):
            keys = list(q.criteria)
        elif isinstance(q, Score):
            keys = [str(i) for i in range(len(q.criteria))]
        else:
            return {"true": 0.5}
        return {k: 1 / len(keys) for k in keys}

    @staticmethod
    def _answer(q: Question, pr: dict[str, float]) -> Answer:
        if isinstance(q, Noul):
            return Answer("noul", noul=pr.get("true", 0.5))
        conf = round(confidence_from(pr), 4)
        if isinstance(q, Choice):
            return Answer("choice", choice=max(pr, key=pr.get), probabilities=pr, confidence=conf)
        legend = {str(i): (c if isinstance(c, str) else str(c)) for i, c in enumerate(q.criteria)}
        score = round(sum(int(k) * p for k, p in pr.items()), 4)
        return Answer("score", score=score, probabilities=pr, confidence=conf, legend=legend)
