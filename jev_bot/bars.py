"""Multi-timeframe OHLC bars from ticks and MT5 rate backfill.

Candle boundaries follow broker server time: measure
``symbol_info_tick().time - utc`` at start and re-check hourly for DST.
Strategies read only closed candles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

# MT5 timeframe enums (stable values from the MetaTrader5 package).
TF_M1 = 1
TF_M5 = 5
TF_M15 = 15
TF_H1 = 16385
TF_H4 = 16388

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}
TF_ENUM = {
    "M1": TF_M1,
    "M5": TF_M5,
    "M15": TF_M15,
    "H1": TF_H1,
    "H4": TF_H4,
}
DEFAULT_BACKFILL = {"M1": 500, "M5": 500, "M15": 400, "H1": 300, "H4": 300}


@dataclass(frozen=True)
class Bar:
    """One OHLC candle. ``t`` is the unix open of the candle (UTC after offset)."""

    t: int
    open: float
    high: float
    low: float
    close: float
    tick_vol: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    spread_mean: float = 0.0
    spread_max: float = 0.0

    @property
    def span(self) -> float:
        return max(self.high - self.low, 0.0)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def bullish(self) -> bool:
        return self.close > self.open


@dataclass
class BrokerClock:
    """Maps broker server unix times onto UTC using a measured offset."""

    offset_seconds: int = 0
    last_check: float = 0.0

    def to_utc(self, server_unix: int) -> int:
        return int(server_unix) - int(self.offset_seconds)

    def to_server(self, utc_unix: int) -> int:
        return int(utc_unix) + int(self.offset_seconds)

    def maybe_refresh(self, server_unix: int, utc_now: float, *, hourly: bool = True) -> None:
        if hourly and self.last_check and (utc_now - self.last_check) < 3600:
            return
        self.offset_seconds = int(round(server_unix - utc_now))
        self.last_check = float(utc_now)


def candle_open(unix_t: int, tf_seconds: int) -> int:
    """Floor ``unix_t`` to the candle open for a fixed-width timeframe."""
    if tf_seconds <= 0:
        raise ValueError("tf_seconds must be positive")
    return (int(unix_t) // tf_seconds) * tf_seconds


def is_closed(bar_open: int, now_unix: int, tf_seconds: int, *, grace_seconds: int = 2) -> bool:
    """A candle is closed at the next open, or ``grace_seconds`` after the boundary."""
    end = int(bar_open) + int(tf_seconds)
    return int(now_unix) >= end + max(0, int(grace_seconds)) or int(now_unix) >= end and True


@dataclass
class BarBuilder:
    """Aggregate ticks into one timeframe. The forming candle is kept separate."""

    tf: str
    tf_seconds: int
    bars: list[Bar] = field(default_factory=list)
    forming: Bar | None = None
    _n_spread: int = 0
    _sum_spread: float = 0.0
    mismatches: int = 0

    @classmethod
    def for_tf(cls, tf: str) -> "BarBuilder":
        if tf not in TF_SECONDS:
            raise ValueError(f"unknown timeframe {tf}")
        return cls(tf=tf, tf_seconds=TF_SECONDS[tf])

    def closed(self) -> list[Bar]:
        return list(self.bars)

    def seed_from_rates(self, rows: Iterable) -> None:
        """Load closed bars from ``copy_rates_from_pos`` rows (already UTC opens)."""
        self.bars = []
        for row in rows:
            # MT5 rate: time, open, high, low, close, tick_volume, spread, real_volume
            t = int(row[0]) if not hasattr(row, "time") else int(row.time)
            o = float(row[1] if not hasattr(row, "open") else row.open)
            h = float(row[2] if not hasattr(row, "high") else row.high)
            l = float(row[3] if not hasattr(row, "low") else row.low)
            c = float(row[4] if not hasattr(row, "close") else row.close)
            tv = float(row[5] if not hasattr(row, "tick_volume") else row.tick_volume)
            spr = float(row[6] if not hasattr(row, "spread") else (row.spread or 0))
            self.bars.append(Bar(t, o, h, l, c, tick_vol=tv, spread_mean=spr, spread_max=spr))
        self.bars.sort(key=lambda b: b.t)

    def on_tick(self, utc_t: int, bid: float, ask: float, *,
                last: float | None = None, volume: float = 0.0,
                buy: bool = False, sell: bool = False) -> Bar | None:
        """Fold one tick into the forming candle. Returns a newly closed bar, if any."""
        mid = last if last and last > 0 else (bid + ask) / 2.0
        spread = max(0.0, ask - bid)
        open_t = candle_open(utc_t, self.tf_seconds)
        closed_bar: Bar | None = None
        if self.forming is not None and open_t > self.forming.t:
            closed_bar = self.forming
            self.bars.append(closed_bar)
            self.forming = None
            self._n_spread = 0
            self._sum_spread = 0.0
        if self.forming is None:
            self.forming = Bar(
                open_t, mid, mid, mid, mid,
                tick_vol=0.0, buy_vol=0.0, sell_vol=0.0,
                spread_mean=spread, spread_max=spread,
            )
            self._n_spread = 1
            self._sum_spread = spread
        else:
            f = self.forming
            self.forming = Bar(
                f.t, f.open,
                max(f.high, mid), min(f.low, mid), mid,
                tick_vol=f.tick_vol + max(0.0, volume),
                buy_vol=f.buy_vol + (volume if buy else 0.0),
                sell_vol=f.sell_vol + (volume if sell else 0.0),
                spread_mean=0.0, spread_max=max(f.spread_max, spread),
            )
            self._n_spread += 1
            self._sum_spread += spread
            self.forming = Bar(
                self.forming.t, self.forming.open, self.forming.high, self.forming.low,
                self.forming.close, self.forming.tick_vol, self.forming.buy_vol,
                self.forming.sell_vol,
                spread_mean=self._sum_spread / max(1, self._n_spread),
                spread_max=self.forming.spread_max,
            )
        return closed_bar

    def reconcile(self, mt5_bar: Bar) -> None:
        """MT5 wins on close: replace the last closed bar if it matches the open time."""
        if not self.bars:
            self.bars.append(mt5_bar)
            return
        if self.bars[-1].t == mt5_bar.t:
            prev = self.bars[-1]
            if (
                abs(prev.open - mt5_bar.open) > 1e-9
                or abs(prev.high - mt5_bar.high) > 1e-9
                or abs(prev.low - mt5_bar.low) > 1e-9
                or abs(prev.close - mt5_bar.close) > 1e-9
            ):
                self.mismatches += 1
            self.bars[-1] = mt5_bar
        elif mt5_bar.t > self.bars[-1].t:
            self.bars.append(mt5_bar)

    def enough(self, need: int) -> bool:
        return len(self.bars) >= int(need)


@dataclass
class MultiTFBooks:
    """Hold builders for M1/M5/M15/H1/H4 used by the higher-timeframe strategies."""

    clock: BrokerClock = field(default_factory=BrokerClock)
    books: dict[str, BarBuilder] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.books:
            self.books = {tf: BarBuilder.for_tf(tf) for tf in ("M1", "M5", "M15", "H1", "H4")}

    def get(self, tf: str) -> BarBuilder:
        return self.books[tf]

    def on_tick(self, utc_t: int, bid: float, ask: float, **kw) -> dict[str, Bar]:
        closed: dict[str, Bar] = {}
        for tf, builder in self.books.items():
            bar = builder.on_tick(utc_t, bid, ask, **kw)
            if bar is not None:
                closed[tf] = bar
        return closed
