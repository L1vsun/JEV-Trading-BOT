"""Quant divergence flags: the code-side twin of the fair-value gap.

The fair-value model (quant.FairValueModel) predicts where the mid should be
in a few minutes; when that prediction stays large for a while, Jev raises a
flag. The evaluation of those flags against the moves that followed lives in
backtest.flag_lead_times and is diagnostic only.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Flag:
    t: int
    side: int           # +1: fair value above the mid (expect up)
    z: float


def detect_flags(z: np.ndarray, threshold: float = 1.0, persist: int = 10, cooldown: int = 600) -> list[Flag]:
    """Fire when |z| stays above ``threshold`` on one side for ``persist``
    consecutive seconds, then stay quiet for ``cooldown`` seconds."""
    flags: list[Flag] = []
    run, side, last = 0, 0, -10 ** 9
    for t, v in enumerate(z):
        s = int(np.sign(v)) if abs(v) >= threshold else 0
        run = run + 1 if s and s == side else (1 if s else 0)
        side = s
        if run >= persist and t - last >= cooldown:
            flags.append(Flag(t, s, float(v)))
            last, run = t, 0
    return flags
