"""Online models. Both learn walk-forward: a label is used for training only
after it has been fully observed, so no prediction ever sees its own future.
"""
from __future__ import annotations

import math

import numpy as np


class OnlineScaler:
    """Running mean/std (Welford) so features are standardised causally."""

    def __init__(self, k: int):
        self.n = 0
        self.mu = np.zeros(k)
        self.m2 = np.zeros(k)

    def update(self, x: np.ndarray) -> None:
        self.n += 1
        d = x - self.mu
        self.mu += d / self.n
        self.m2 += d * (x - self.mu)

    def transform(self, x: np.ndarray) -> np.ndarray:
        sd = np.sqrt(self.m2 / max(1, self.n - 1)) if self.n > 1 else np.ones_like(x)
        return (x - self.mu) / np.maximum(sd, 1e-9)


class RLS:
    """Recursive least squares with exponential forgetting and a ridge prior."""

    def __init__(self, k: int, lam: float = 0.9999, ridge: float = 10.0):
        self.w = np.zeros(k)
        self.P = np.eye(k) / ridge
        self.lam = lam

    def predict(self, x: np.ndarray) -> float:
        return float(self.w @ x)

    def update(self, x: np.ndarray, y: float) -> None:
        Px = self.P @ x
        g = Px / (self.lam + x @ Px)
        self.w += g * (y - self.w @ x)
        self.P = (self.P - np.outer(g, Px)) / self.lam


class FairValueModel:
    """"What the market *should* be doing".

    Predicts the mid's move over the next ``horizon`` seconds (in units of
    realised volatility) from order flow, book and candle-shape features.
    fair value = mid + prediction * rv * sqrt(horizon).
    """

    def __init__(self, k: int, horizon: int = 180, lam: float = 0.99995, ridge: float = 30.0):
        self.h = horizon
        self.scaler = OnlineScaler(k)
        self.rls = RLS(k + 1, lam, ridge)

    def run(self, X: np.ndarray, mid: np.ndarray, rv: np.ndarray, warmup: int = 1800) -> np.ndarray:
        n, h = len(X), self.h
        pred = np.zeros(n)
        xs = np.zeros((n, X.shape[1] + 1))
        scale = rv * math.sqrt(h)
        for t in range(n):
            self.scaler.update(X[t])
            xs[t, :-1] = np.clip(self.scaler.transform(X[t]), -6, 6)
            xs[t, -1] = 1.0
            s = t - h                                  # label for s is known at t
            if s >= 0:
                y = (mid[t] - mid[s]) / scale[s]
                self.rls.update(xs[s], float(np.clip(y, -8, 8)))
            pred[t] = self.rls.predict(xs[t]) if t >= warmup else 0.0
        return pred

    @property
    def weights(self) -> np.ndarray:
        return self.rls.w[:-1]


class CandleModel:
    """Reads the shape of the forming candle and estimates P(close > open).

    Online logistic regression (SGD). Every second of a bar is a sample; the
    samples of a bar are trained on only after that bar has closed.
    """

    def __init__(self, k: int, bar: int = 60, lr: float = 0.02, l2: float = 1e-4):
        self.bar, self.lr, self.l2 = bar, lr, l2
        self.w = np.zeros(k + 1)

    def _p(self, x: np.ndarray) -> float:
        z = float(np.clip(self.w @ x, -30, 30))
        return 1 / (1 + math.exp(-z))

    def run(self, F: np.ndarray, mid: np.ndarray) -> np.ndarray:
        n, bar = len(F), self.bar
        p = np.full(n, 0.5)
        Fx = np.column_stack([F, np.ones(n)])
        for start in range(0, n, bar):
            end = min(n, start + bar)
            for i in range(start, end):
                p[i] = self._p(Fx[i])
            if end - start < bar:
                break
            y = 1.0 if mid[end - 1] > mid[start] else 0.0
            for i in range(start, end - 1):             # the closing tick itself is not a forecast
                g = self._p(Fx[i]) - y
                self.w -= self.lr * (g * Fx[i] + self.l2 * self.w)
        return p


CANDLE_KEYS = ("ret", "pos", "wick", "upticks", "levy", "range")


def candle_inputs(raw: dict[str, np.ndarray]) -> np.ndarray:
    """Candle-model inputs: shape features, each also interacted with the
    elapsed fraction so the model can trust the shape more as the bar ages."""
    el = raw["candle_elapsed"]
    cols = []
    for k in CANDLE_KEYS:
        x = raw[f"candle_{k}"]
        cols += [x, x * el]
    cols += [raw["ofi_60"], raw["qimb"]]
    return np.column_stack(cols)
