"""Synthetic limit-order-book market with a hidden fair value.

The generator is a small agent model in the spirit of Kyle (1985) and
Glosten-Milgrom (1985):

* a latent *efficient price* ``v`` diffuses and occasionally jumps (news);
* *informed* traders see ``v`` and trade toward it, harder the larger the gap;
* *noise* traders buy and sell at random;
* a *market maker* only sees order flow. It moves its mid by a small
  immediate impact per net lot plus a lagged impact on the smoothed flow
  (it under-reacts, as real liquidity providers do), withdraws queue size on
  the side being hit, and widens the spread when flow turns one-sided.

So the mid tracks ``v`` with a lag of a few minutes, and the only observable
trace of the gap is in the order flow and the book. That is exactly the gap
Jev is built to measure. ``v`` is returned separately for diagnostics and is
never an input to any model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .book import Market

LEVELS = 5


@dataclass
class SimConfig:
    seconds: int = 6 * 3600            # one session at 1-second resolution
    seed: int = 7
    p0: float = 100.0
    tick: float = 0.01
    sigma_v: float = 0.006             # latent diffusion per sqrt(second)
    jump_rate: float = 1 / 1800        # news events per second (~every 30 min)
    jump_size: tuple = (0.35, 0.8)     # |jump| range, price units
    impact: float = 0.0007             # immediate price impact per net lot
    impact_lag: float = 0.0005         # impact per lot/sec of smoothed net flow
    flow_halflife: float = 30.0        # seconds; how slowly the maker digests flow
    informed_rate: float = 2.0         # informed trades/sec per unit of |gap|
    noise_rate: float = 1.1            # noise trades/sec per side
    mean_lot: float = 3.0              # geometric trade size mean (lots)
    mm_noise: float = 0.002            # market-maker quote noise per second
    base_depth: float = 40.0           # L1 queue size in lots
    toxicity_widen: float = 0.06       # spread ticks per unit of |EW net flow|


@dataclass
class SimTruth:
    """Latent quantities. For evaluation and plots only, never a model input."""
    v: np.ndarray
    jumps: list = field(default_factory=list)   # (second, size)


def simulate(cfg: SimConfig | None = None) -> tuple[Market, SimTruth]:
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.seed)
    n = cfg.seconds
    tick = cfg.tick

    v = np.empty(n)
    m = np.empty(n)
    bid = np.empty(n)
    ask = np.empty(n)
    bid_sz = np.empty((n, LEVELS))
    ask_sz = np.empty((n, LEVELS))
    buy_vol = np.zeros(n)
    sell_vol = np.zeros(n)
    n_trades = np.zeros(n, dtype=np.int64)
    jumps: list[tuple[int, float]] = []

    v[0] = m[0] = cfg.p0
    ew_buy = ew_sell = ew_net = 0.0
    a_net = 1 - 0.5 ** (1 / cfg.flow_halflife)
    depth_profile = np.array([1.0, 1.35, 1.7, 2.0, 2.3])

    def lots(k: int) -> float:
        return float(rng.geometric(1 / cfg.mean_lot, size=k).sum()) if k else 0.0

    for t in range(n):
        if t:
            dv = cfg.sigma_v * rng.standard_normal()
            if rng.random() < cfg.jump_rate:
                size = rng.uniform(*cfg.jump_size) * rng.choice((-1.0, 1.0))
                dv += size
                jumps.append((t, size))
            v[t] = v[t - 1] + dv
        gap = v[t] - (m[t - 1] if t else m[0])

        # order flow this second
        k_inf = rng.poisson(cfg.informed_rate * abs(gap))
        k_nb = rng.poisson(cfg.noise_rate)
        k_ns = rng.poisson(cfg.noise_rate)
        inf = lots(k_inf)
        b = lots(k_nb) + (inf if gap > 0 else 0.0)
        s = lots(k_ns) + (inf if gap < 0 else 0.0)
        buy_vol[t], sell_vol[t] = b, s
        n_trades[t] = k_inf + k_nb + k_ns

        # market maker learns only from flow
        ew_net += a_net * ((b - s) - ew_net)
        if t:
            m[t] = m[t - 1] + cfg.impact * (b - s) + cfg.impact_lag * ew_net + cfg.mm_noise * rng.standard_normal()
        ew_buy = 0.9 * ew_buy + 0.1 * b
        ew_sell = 0.9 * ew_sell + 0.1 * s
        net = ew_buy - ew_sell

        spread_ticks = 1 + int(min(4, cfg.toxicity_widen * abs(net) * 10))
        half = spread_ticks * tick / 2
        bid[t] = np.floor((m[t] - half) / tick + 1e-9) * tick
        ask[t] = max(bid[t] + tick, np.ceil((m[t] + half) / tick - 1e-9) * tick)

        # queues: the side being hit gets thinner
        d = cfg.base_depth
        bq = d * np.exp(-0.035 * ew_sell) * rng.gamma(6, 1 / 6)
        aq = d * np.exp(-0.035 * ew_buy) * rng.gamma(6, 1 / 6)
        bid_sz[t] = np.maximum(1, np.round(depth_profile * d * rng.gamma(8, 1 / 8, LEVELS)))
        ask_sz[t] = np.maximum(1, np.round(depth_profile * d * rng.gamma(8, 1 / 8, LEVELS)))
        bid_sz[t, 0] = max(1.0, round(bq))
        ask_sz[t, 0] = max(1.0, round(aq))

    mkt = Market(
        t=np.arange(n, dtype=float), bid=bid, ask=ask, bid_sz=bid_sz, ask_sz=ask_sz,
        buy_vol=buy_vol, sell_vol=sell_vol, n_trades=n_trades, tick=tick,
    )
    return mkt, SimTruth(v=v, jumps=jumps)
