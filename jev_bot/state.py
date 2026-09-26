"""Turns numbers into a state Jev can judge.

Jev's documentation is explicit: it is not a calculator and reads semantic
descriptions better than raw numbers. So every quantity is computed in code
(features.py, quant.py) and then written into the state as a *named bucket*
("heavy buying", "bid queue much thicker than the ask"), with the structure
kept as JSON so each question can point at the field it needs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .features import rolling_std

# Ordered vocabularies, strongest-negative -> strongest-positive. The
# surrogate model inverts these, so it only ever sees what Jev sees.
FLOW = ["heavy selling", "moderate selling", "light selling", "balanced", "light buying", "moderate buying", "heavy buying"]
PRICE = ["falling fast", "falling steadily", "drifting lower", "flat", "drifting higher", "rising steadily", "rising fast"]
BOOK = ["ask queue much thicker than the bid", "ask queue thicker than the bid", "roughly even queues",
        "bid queue thicker than the ask", "bid queue much thicker than the ask"]
MICRO = ["leans strongly toward the bid", "leans toward the bid", "centered", "leans toward the ask", "leans strongly toward the ask"]
GAP = ["price is far above where the order flow says it should be", "price is above where the order flow says it should be",
       "price is slightly above fair value", "price is in line with fair value", "price is slightly below fair value",
       "price is below where the order flow says it should be", "price is far below where the order flow says it should be"]
CANDLE_SO_FAR = ["down sharply", "down moderately", "down slightly", "unchanged", "up slightly", "up moderately", "up sharply"]
CANDLE_LOC = ["pinned at the low", "in the lower part of its range", "mid-range", "in the upper part of its range", "pinned at the high"]
CANDLE_PATH = ["the move came early and has stalled", "the move was spread evenly", "the move is happening late and still building"]
TICKS = ["mostly downticks", "more downticks than upticks", "mixed ticks", "more upticks than downticks", "mostly upticks"]
TOX = ["low", "normal", "elevated", "extreme"]
VOL = ["very calm", "calm", "normal", "volatile", "very volatile"]
SPREAD = ["one tick, tight", "two ticks, normal", "wide", "very wide"]

LEXICON = {name: vocab for name, vocab in {
    "FLOW": FLOW, "PRICE": PRICE, "BOOK": BOOK, "MICRO": MICRO, "GAP": GAP, "CANDLE_SO_FAR": CANDLE_SO_FAR,
    "CANDLE_LOC": CANDLE_LOC, "CANDLE_PATH": CANDLE_PATH, "TICKS": TICKS, "TOX": TOX, "VOL": VOL, "SPREAD": SPREAD}.items()}


def bucket(x: float, vocab: list[str], edges: tuple[float, ...]) -> str:
    """Symmetric bucketing: ``edges`` are the positive thresholds, low to high."""
    k = len(vocab) // 2
    a = abs(x)
    lvl = sum(a >= e for e in edges[:k])
    return vocab[k + lvl] if x >= 0 else vocab[k - lvl]


def value_of(word: str, vocab: list[str]) -> float:
    """Inverse of :func:`bucket`: -1..1 position of ``word`` in its vocabulary."""
    i = vocab.index(word)
    return (i - (len(vocab) - 1) / 2) / ((len(vocab) - 1) / 2)


def causal_z(x: np.ndarray, w: int = 1800) -> np.ndarray:
    sd = rolling_std(x, w)
    sd[: min(len(sd), 60)] = np.nan
    return np.nan_to_num(x / np.where(sd > 1e-12, sd, np.nan))


@dataclass
class StateBuilder:
    raw: dict
    fair_z: np.ndarray            # quant fair-value prediction, vol units
    bar: int = 60

    def __post_init__(self) -> None:
        r = self.raw
        self.z = {k: causal_z(r[k]) for k in ("ofi_15", "ofi_60", "ofi_180", "timb_30", "timb_120", "qimb", "dimb", "wmid_dev")}
        mid, rv = r["mid"], r["rv"]
        self.mom = {}
        for w in (60, 300):
            prev = np.concatenate([np.full(w, mid[0]), mid[:-w]])
            self.mom[w] = (mid - prev) / (rv * math.sqrt(w))
        v = r["vpin"]
        self.vpin_rank = np.array([np.mean(v[max(0, i - 3600): i + 1] <= v[i]) for i in range(len(v))])
        rvs = rolling_std(np.diff(mid, prepend=mid[0]), 60)
        base = np.maximum(rolling_std(np.diff(mid, prepend=mid[0]), 3600), 1e-12)
        self.vol_ratio = rvs / base
        self.spread_ticks = np.round(r["spread"] / np.maximum(np.min(r["spread"][r["spread"] > 0]), 1e-12))

    def state(self, t: int, position: str = "flat") -> dict:
        z, r = self.z, self.raw
        seq = []
        for k in range(4, -1, -1):                     # five 30-second windows, oldest first
            i = max(0, t - 30 * k)
            seq.append(bucket(z["timb_30"][i], FLOW, (0.5, 1.2, 2.0)))
        el = int(round(r["candle_elapsed"][t] * self.bar))
        levy = r["candle_levy"][t] * (1 if r["candle_ret"][t] >= 0 else -1)   # >0: move came late
        return {
            "market": "one instrument, 1-second limit order book snapshots",
            "price_action": {
                "last_1_minute": bucket(self.mom[60][t], PRICE, (0.4, 1.0, 2.0)),
                "last_5_minutes": bucket(self.mom[300][t], PRICE, (0.4, 1.0, 2.0)),
            },
            "order_flow": {
                "last_15_seconds": bucket(z["ofi_15"][t], FLOW, (0.5, 1.2, 2.0)),
                "last_1_minute": bucket(z["ofi_60"][t], FLOW, (0.5, 1.2, 2.0)),
                "last_3_minutes": bucket(z["ofi_180"][t], FLOW, (0.5, 1.2, 2.0)),
                "aggressor_tape_30s_windows_oldest_first": seq,
            },
            "order_book": {
                "best_level": bucket(z["qimb"][t], BOOK, (0.6, 1.5)),
                "top_five_levels": bucket(z["dimb"][t], BOOK, (0.6, 1.5)),
                "micro_price": bucket(z["wmid_dev"][t], MICRO, (0.6, 1.5)),
                "spread": SPREAD[int(min(3, max(0, self.spread_ticks[t] - 1)))],
            },
            "flow_toxicity": TOX[int(min(3, self.vpin_rank[t] * 4))],
            "volatility": VOL[int(np.clip(np.digitize(self.vol_ratio[t], (0.6, 0.85, 1.25, 1.8)), 0, 4))],
            "fair_value_gap": bucket(self.fair_z[t], GAP, (0.25, 0.6, 1.1)),
            "forming_candle": {
                "progress": f"{el} of {self.bar} seconds",
                "so_far": bucket(r["candle_ret"][t], CANDLE_SO_FAR, (0.25, 0.7, 1.4)),
                "location_in_range": bucket(r["candle_pos"][t], CANDLE_LOC, (0.35, 0.8)),
                "path": CANDLE_PATH[0 if levy < -0.08 else 2 if levy > 0.08 else 1],
                "ticks": bucket(r["candle_upticks"][t], TICKS, (0.2, 0.5)),
            },
            "position": position,
        }
