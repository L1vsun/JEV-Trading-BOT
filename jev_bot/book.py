"""Market data container and CSV loading.

Jev works on a regular grid (default 1 second) of top-of-book snapshots plus
signed traded volume. Deeper levels are optional.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Market:
    t: np.ndarray            # seconds
    bid: np.ndarray
    ask: np.ndarray
    bid_sz: np.ndarray       # (n, levels); level 0 = best
    ask_sz: np.ndarray
    buy_vol: np.ndarray      # aggressor-buy volume in the interval
    sell_vol: np.ndarray
    n_trades: np.ndarray
    tick: float = 0.01

    def __len__(self) -> int:
        return len(self.t)

    @property
    def mid(self) -> np.ndarray:
        return (self.bid + self.ask) / 2

    def slice(self, a: int, b: int) -> "Market":
        return Market(self.t[a:b], self.bid[a:b], self.ask[a:b], self.bid_sz[a:b], self.ask_sz[a:b],
                      self.buy_vol[a:b], self.sell_vol[a:b], self.n_trades[a:b], self.tick)

    def save_csv(self, path: str | Path) -> None:
        lv = self.bid_sz.shape[1]
        head = ["t", "bid", "ask"] + [f"bid_sz{i}" for i in range(lv)] + [f"ask_sz{i}" for i in range(lv)] + ["buy_vol", "sell_vol", "n_trades"]
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(head)
            for i in range(len(self)):
                w.writerow([f"{self.t[i]:.0f}", f"{self.bid[i]:.4f}", f"{self.ask[i]:.4f}",
                            *[f"{x:.0f}" for x in self.bid_sz[i]], *[f"{x:.0f}" for x in self.ask_sz[i]],
                            f"{self.buy_vol[i]:.0f}", f"{self.sell_vol[i]:.0f}", int(self.n_trades[i])])


def load_csv(path: str | Path, tick: float | None = None) -> Market:
    """Load a snapshot CSV.

    Required columns: ``t, bid, ask, bid_sz0, ask_sz0`` and either
    ``buy_vol, sell_vol`` (aggressor-signed volume) or ``volume`` (unsigned;
    it is then split with bulk volume classification, see
    :func:`jev.features.bulk_volume_classification`). Extra depth columns
    ``bid_sz1..`` / ``ask_sz1..`` are picked up automatically.
    """
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path}: no rows")
    cols = rows[0].keys()
    col = lambda k: np.array([float(r[k]) for r in rows])
    lv = 0
    while f"bid_sz{lv}" in cols and f"ask_sz{lv}" in cols:
        lv += 1
    if lv == 0:
        raise ValueError("need bid_sz0 and ask_sz0 columns")
    bid, ask = col("bid"), col("ask")
    if "buy_vol" in cols and "sell_vol" in cols:
        bv, sv = col("buy_vol"), col("sell_vol")
    elif "volume" in cols:
        from .features import bulk_volume_classification
        bv, sv = bulk_volume_classification((bid + ask) / 2, col("volume"))
    else:
        raise ValueError("need buy_vol/sell_vol or volume")
    nt = col("n_trades").astype(np.int64) if "n_trades" in cols else np.zeros(len(rows), dtype=np.int64)
    if tick is None:
        d = np.abs(np.diff(np.concatenate([bid, ask])))
        d = d[d > 1e-9]
        tick = float(np.round(d.min(), 8)) if len(d) else 0.01
    return Market(col("t"), bid, ask,
                  np.stack([col(f"bid_sz{i}") for i in range(lv)], 1),
                  np.stack([col(f"ask_sz{i}") for i in range(lv)], 1),
                  bv, sv, nt, tick)
