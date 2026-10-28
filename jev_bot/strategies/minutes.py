"""Build closed M1 candles from the one-second live buffer."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MinuteBar:
    """One closed minute. ``t`` is the unix second of the minute open."""

    t: int
    open: float
    high: float
    low: float
    close: float
    buy_vol: float
    sell_vol: float

    @property
    def mid_close(self) -> float:
        return self.close


def closed_minutes(times: list[int], mids: list[float],
                   buy_vol: list[float], sell_vol: list[float]) -> list[MinuteBar]:
    """Aggregate one-second rows into closed minutes. The forming minute is dropped."""
    if not times:
        return []
    buckets: dict[int, list[int]] = {}
    for i, t in enumerate(times):
        minute = (int(t) // 60) * 60
        buckets.setdefault(minute, []).append(i)
    if len(buckets) < 2:
        return []
    keys = sorted(buckets)
    # Drop the last (still forming) minute.
    out: list[MinuteBar] = []
    for minute in keys[:-1]:
        idxs = buckets[minute]
        prices = [mids[i] for i in idxs]
        out.append(MinuteBar(
            t=minute,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            buy_vol=sum(buy_vol[i] for i in idxs),
            sell_vol=sum(sell_vol[i] for i in idxs),
        ))
    return out
