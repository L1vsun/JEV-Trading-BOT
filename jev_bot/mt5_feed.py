"""One-second bars from MetaTrader 5 ticks and an optional depth-of-market.

The bars are a :class:`jev_bot.book.Market`, so ``features.py``, ``state.py``
and ``decide.py`` run on them unchanged. When ``market_book_get`` has no
depth, each second is a level-1 book with equal placeholder size on both
sides. That keeps the feature math defined and makes the book signals below
go quiet instead of inventing a queue.

Degraded without depth (the state field Jev reads is in parentheses):

* queue imbalance (``order_book.best_level``) — both queues are the
  placeholder, so the imbalance is zero;
* depth imbalance (``order_book.top_five_levels``) — a single level copies
  the best level;
* micro-price (``order_book.micro_price``) — equal queues put the weighted
  mid on the mid;
* order-flow imbalance magnitude — the Cont–Kukanov–Stoikov size terms are
  1. The sign of a quote change is still there.

Still computed from bid, ask and signed tick volume: aggressor trade
imbalance, VPIN, Kyle's λ, spread, realised volatility, momentum, the
forming candle, and the fair-value model. The fair-value fit still runs;
its book inputs are flat for as long as the placeholder book is in the
window, so that part of the fit stays idle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .book import Market

# MetaTrader 5 tick flags.
TICK_FLAG_BID = 2
TICK_FLAG_ASK = 4
TICK_FLAG_LAST = 8
TICK_FLAG_VOLUME = 16
TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64
_TRADE_FLAGS = TICK_FLAG_LAST | TICK_FLAG_VOLUME | TICK_FLAG_BUY | TICK_FLAG_SELL

# Book sides. Market-by-market variants count as the same side.
BOOK_BID = 2
BOOK_BID_MARKET = 4
BOOK_ASK = 1
BOOK_ASK_MARKET = 3

DEGRADED_WITHOUT_DEPTH: tuple[str, ...] = (
    "queue imbalance (order_book.best_level): both queues are the placeholder size, so the imbalance is zero",
    "depth imbalance (order_book.top_five_levels): one level copies the best level",
    "micro-price (order_book.micro_price): equal queues put the weighted mid on the mid",
    "order-flow imbalance magnitude: the size terms are 1; the sign of a quote change remains",
)

UNAFFECTED_WITHOUT_DEPTH: tuple[str, ...] = (
    "aggressor trade imbalance",
    "VPIN",
    "Kyle's lambda",
    "spread",
    "realised volatility and momentum",
    "forming-candle shape",
    "fair value (the model still runs; its book inputs are flat while the placeholder book is in the window)",
)


def depth_note(*, available: bool) -> str:
    if available:
        return "market depth is on the book"
    degraded = "; ".join(DEGRADED_WITHOUT_DEPTH)
    alive = "; ".join(UNAFFECTED_WITHOUT_DEPTH)
    return f"level-1 fallback (no market depth). Degraded: {degraded}. Still live: {alive}."


@dataclass(frozen=True)
class Tick:
    time_msc: int
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0
    volume_real: float = 0.0
    flags: int = 0


@dataclass(frozen=True)
class BookLevel:
    price: float
    volume: float
    side: str                          # "bid" or "ask"


@dataclass
class SecondBar:
    t: int
    bid: float
    ask: float
    bid_sz: np.ndarray
    ask_sz: np.ndarray
    buy_vol: float
    sell_vol: float
    n_trades: int
    depth: bool


def _field(row, name: str, default=0):
    if isinstance(row, dict):
        return row.get(name, default)
    if hasattr(row, "dtype") and getattr(row.dtype, "names", None) and name in row.dtype.names:
        return row[name]
    return getattr(row, name, default)


def parse_ticks(raw) -> list[Tick]:
    """Accept the numpy records ``copy_ticks_*`` returns, or a list of mappings."""
    if raw is None:
        return []
    try:
        n = len(raw)
    except TypeError:
        return []
    if n == 0:
        return []
    out: list[Tick] = []
    for row in raw:
        msc = int(_field(row, "time_msc", 0) or 0)
        if not msc:
            msc = int(_field(row, "time", 0) or 0) * 1000
        out.append(Tick(
            time_msc=msc,
            bid=float(_field(row, "bid", 0) or 0),
            ask=float(_field(row, "ask", 0) or 0),
            last=float(_field(row, "last", 0) or 0),
            volume=float(_field(row, "volume", 0) or 0),
            volume_real=float(_field(row, "volume_real", 0) or 0),
            flags=int(_field(row, "flags", 0) or 0),
        ))
    out.sort(key=lambda t: t.time_msc)
    return out


def parse_book(raw) -> list[BookLevel]:
    if raw is None:
        return []
    try:
        n = len(raw)
    except TypeError:
        return []
    out: list[BookLevel] = []
    for row in raw:
        typ = int(_field(row, "type", 0) or 0)
        if typ in (BOOK_BID, BOOK_BID_MARKET):
            side = "bid"
        elif typ in (BOOK_ASK, BOOK_ASK_MARKET):
            side = "ask"
        else:
            continue
        vol_real = float(_field(row, "volume_real", 0) or 0)
        vol = vol_real if vol_real > 0 else float(_field(row, "volume", 0) or 0)
        price = float(_field(row, "price", 0) or 0)
        if vol <= 0 or price <= 0:
            continue
        out.append(BookLevel(price, vol, side))
    return out


def tick_volume(tick: Tick) -> float:
    """Tick volume for one print. Quote-only updates contribute nothing."""
    if tick.volume_real > 0:
        return float(tick.volume_real)
    if tick.volume > 0:
        return float(tick.volume)
    if tick.flags & _TRADE_FLAGS:
        return 1.0
    return 0.0


def aggressor(tick: Tick, prev_mid: float | None, prev_sign: int) -> int:
    """+1 buy, -1 sell, 0 when the print has no side.

    Tick flags win. A last price through the quote is next. Otherwise the
    tick rule on the mid, carried forward when the mid does not move.
    """
    buy = bool(tick.flags & TICK_FLAG_BUY)
    sell = bool(tick.flags & TICK_FLAG_SELL)
    if buy and not sell:
        return 1
    if sell and not buy:
        return -1
    last = tick.last
    if last > 0 and (tick.flags & TICK_FLAG_LAST or last):
        if tick.ask > 0 and last >= tick.ask:
            return 1
        if tick.bid > 0 and last <= tick.bid:
            return -1
    if tick.bid > 0 and tick.ask > 0:
        mid = (tick.bid + tick.ask) / 2
        if prev_mid is not None:
            if mid > prev_mid + 1e-12:
                return 1
            if mid < prev_mid - 1e-12:
                return -1
    return prev_sign if prev_sign in (1, -1) else 0


def book_sizes(levels: list[BookLevel] | None, n_levels: int, fallback: float) -> tuple[np.ndarray, np.ndarray, bool]:
    """Best ``n_levels`` on each side, or a neutral level-1 placeholder.

    Depth counts only when both sides have at least one positive level, so a
    one-sided read does not look like a real imbalance.
    """
    bids = sorted((lv for lv in levels or [] if lv.side == "bid"), key=lambda lv: -lv.price)
    asks = sorted((lv for lv in levels or [] if lv.side == "ask"), key=lambda lv: lv.price)
    bid_sz = np.zeros(n_levels)
    ask_sz = np.zeros(n_levels)
    if not bids or not asks:
        bid_sz[0] = ask_sz[0] = fallback
        return bid_sz, ask_sz, False
    for i, lv in enumerate(bids[:n_levels]):
        bid_sz[i] = lv.volume
    for i, lv in enumerate(asks[:n_levels]):
        ask_sz[i] = lv.volume
    return bid_sz, ask_sz, True


@dataclass
class _Acc:
    t: int
    bid: float
    ask: float
    buy: float = 0.0
    sell: float = 0.0
    n: int = 0


class TickAggregator:
    """Fold a tick stream into contiguous one-second bars.

    A hole longer than ``max_gap`` seconds ends the current segment. Only the
    latest segment is returned, so a weekend gap is not filled with a flat
    book.
    """

    def __init__(self, n_levels: int = 5, fallback: float = 1.0, max_gap: int = 10):
        self.n_levels = n_levels
        self.fallback = fallback
        self.max_gap = max_gap
        self.prev_mid: float | None = None
        self.prev_sign: int = 0

    def bars(self, ticks: list[Tick], book: list[BookLevel] | None) -> list[SecondBar]:
        bid_sz, ask_sz, depth = book_sizes(book, self.n_levels, self.fallback)
        accs: list[_Acc] = []
        index: dict[int, _Acc] = {}
        for tick in ticks:
            if tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid or tick.time_msc <= 0:
                continue
            sec = tick.time_msc // 1000
            vol = tick_volume(tick)
            sign = aggressor(tick, self.prev_mid, self.prev_sign)
            self.prev_mid = (tick.bid + tick.ask) / 2
            if sign in (1, -1):
                self.prev_sign = sign
            acc = index.get(sec)
            if acc is None:
                acc = _Acc(sec, tick.bid, tick.ask)
                index[sec] = acc
                accs.append(acc)
            acc.bid, acc.ask = tick.bid, tick.ask
            if vol > 0:
                acc.n += 1
                if sign > 0:
                    acc.buy += vol
                elif sign < 0:
                    acc.sell += vol
                else:
                    acc.buy += vol / 2
                    acc.sell += vol / 2
        if not accs:
            return []
        accs.sort(key=lambda a: a.t)
        segments: list[list[_Acc]] = [[accs[0]]]
        for prev, cur in zip(accs, accs[1:]):
            if cur.t - prev.t > self.max_gap:
                segments.append([cur])
            else:
                segments[-1].append(cur)
        chosen = segments[-1]
        filled: list[_Acc] = []
        for prev, cur in zip(chosen, chosen[1:]):
            filled.append(prev)
            for sec in range(prev.t + 1, cur.t):
                filled.append(_Acc(sec, prev.bid, prev.ask))
        filled.append(chosen[-1])
        return [
            SecondBar(a.t, a.bid, a.ask, bid_sz.copy(), ask_sz.copy(), a.buy, a.sell, a.n, depth)
            for a in filled
        ]


@dataclass
class MarketBuffer:
    """Rolling one-second history. Rows are one second apart, oldest first."""

    capacity: int
    tick: float = 0.01
    max_gap: int = 10
    rows: list[SecondBar] = field(default_factory=list)

    def clear(self) -> None:
        self.rows.clear()

    def append(self, bar: SecondBar) -> bool:
        """Append one second. Return True when a gap dropped the older bars."""
        reset = False
        if self.rows:
            last = self.rows[-1]
            if bar.t < last.t:
                return False
            if bar.t == last.t:
                self.rows[-1] = _merge(last, bar)
                return False
            gap = bar.t - last.t - 1
            if gap > self.max_gap:
                self.rows.clear()
                reset = True
            else:
                for sec in range(last.t + 1, bar.t):
                    self.rows.append(_flat(sec, last))
        self.rows.append(bar)
        if len(self.rows) > self.capacity:
            self.rows = self.rows[-self.capacity:]
        return reset

    def __len__(self) -> int:
        return len(self.rows)

    def market(self) -> Market:
        rows = self.rows
        if not rows:
            raise ValueError("no bars")
        return Market(
            t=np.array([r.t for r in rows], dtype=float),
            bid=np.array([r.bid for r in rows], dtype=float),
            ask=np.array([r.ask for r in rows], dtype=float),
            bid_sz=np.stack([r.bid_sz for r in rows]),
            ask_sz=np.stack([r.ask_sz for r in rows]),
            buy_vol=np.array([r.buy_vol for r in rows], dtype=float),
            sell_vol=np.array([r.sell_vol for r in rows], dtype=float),
            n_trades=np.array([r.n_trades for r in rows], dtype=np.int64),
            tick=self.tick,
        )

    @property
    def depth_live(self) -> bool:
        return bool(self.rows and self.rows[-1].depth)


def _flat(t: int, prev: SecondBar) -> SecondBar:
    return SecondBar(t, prev.bid, prev.ask, prev.bid_sz.copy(), prev.ask_sz.copy(), 0.0, 0.0, 0, prev.depth)


def _merge(a: SecondBar, b: SecondBar) -> SecondBar:
    return SecondBar(
        b.t, b.bid, b.ask, b.bid_sz, b.ask_sz,
        a.buy_vol + b.buy_vol, a.sell_vol + b.sell_vol, a.n_trades + b.n_trades,
        a.depth or b.depth,
    )
