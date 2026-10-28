"""Deterministic indicators for multi-timeframe strategies. No Jev calls."""
from __future__ import annotations

from datetime import datetime, timezone

from .bars import Bar
from .strategies.common import require_zone


def true_range(bar: Bar, prev_close: float | None) -> float:
    if prev_close is None:
        return max(bar.high - bar.low, 0.0)
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


def atr14(bars: list[Bar], period: int = 14) -> float | None:
    """Wilder ATR, including gaps via true range."""
    if len(bars) < period + 1:
        return None
    trs = [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return float(atr)


def ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    total = sum(values[:period])
    out[period - 1] = total / period
    for i in range(period, len(values)):
        total += values[i] - values[i - period]
        out[i] = total / period
    return out


def rsi14(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    def _rsi(g, l):
        if l <= 1e-12:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out[period] = _rsi(avg_g, avg_l)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi(avg_g, avg_l)
    return out


def adx14(bars: list[Bar], period: int = 14) -> list[float | None]:
    """Wilder ADX. Returns one value per bar (None until seeded)."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < period * 2:
        return out
    plus_dm = [0.0]
    minus_dm = [0.0]
    tr = [true_range(bars[0], None)]
    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(true_range(bars[i], bars[i - 1].close))
    atr = sum(tr[1:period + 1]) / period
    sp = sum(plus_dm[1:period + 1]) / period
    sm = sum(minus_dm[1:period + 1]) / period
    dxs: list[float] = []
    for i in range(period, n):
        if i > period:
            atr = (atr * (period - 1) + tr[i]) / period
            sp = (sp * (period - 1) + plus_dm[i]) / period
            sm = (sm * (period - 1) + minus_dm[i]) / period
        plus_di = 100.0 * sp / atr if atr > 1e-12 else 0.0
        minus_di = 100.0 * sm / atr if atr > 1e-12 else 0.0
        denom = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denom if denom > 1e-12 else 0.0
        dxs.append(dx)
        if len(dxs) == period:
            out[i] = sum(dxs) / period
        elif len(dxs) > period:
            prev = out[i - 1] if out[i - 1] is not None else sum(dxs[-period:]) / period
            out[i] = (prev * (period - 1) + dx) / period
    return out


def efficiency_ratio(closes: list[float], period: int = 10) -> float | None:
    if len(closes) < period + 1:
        return None
    change = abs(closes[-1] - closes[-1 - period])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - period, len(closes)))
    if path <= 1e-12:
        return 0.0
    return change / path


def donchian(bars: list[Bar], period: int) -> tuple[float | None, float | None]:
    """Highest high / lowest low over the previous ``period`` bars (excludes current)."""
    if period <= 0 or len(bars) < period + 1:
        return None, None
    window = bars[-(period + 1):-1]
    return max(b.high for b in window), min(b.low for b in window)


def range_atr_ratio(bar: Bar, atr: float | None) -> float | None:
    if atr is None or atr <= 1e-12:
        return None
    return bar.span / atr


def session_vwap(bars: list[Bar], *, start_t: int) -> float | None:
    """VWAP from tick volume since ``start_t`` (inclusive)."""
    num = 0.0
    den = 0.0
    for b in bars:
        if b.t < start_t:
            continue
        typical = (b.high + b.low + b.close) / 3.0
        w = max(b.tick_vol, 1e-9)
        num += typical * w
        den += w
    if den <= 0:
        return None
    return num / den


def fractal_swings(bars: list[Bar], left: int = 3, right: int = 3) -> list[tuple[int, float, str]]:
    out: list[tuple[int, float, str]] = []
    n = len(bars)
    for i in range(left, n - right):
        h, l = bars[i].high, bars[i].low
        if all(h > bars[j].high for j in range(i - left, i)) and all(h > bars[j].high for j in range(i + 1, i + right + 1)):
            out.append((i, h, "high"))
        if all(l < bars[j].low for j in range(i - left, i)) and all(l < bars[j].low for j in range(i + 1, i + right + 1)):
            out.append((i, l, "low"))
    return out


def structure_hh_hl(bars: list[Bar], left: int = 2, right: int = 2) -> dict | None:
    swings = fractal_swings(bars, left=left, right=right)
    highs = [(i, p) for i, p, k in swings if k == "high"]
    lows = [(i, p) for i, p, k in swings if k == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    return {"hh": h2 > h1, "hl": l2 > l1, "lh": h2 < h1, "ll": l2 < l1, "highs": (h1, h2), "lows": (l1, l2)}


def local_now(unix_t: int, zone_name: str) -> datetime:
    return datetime.fromtimestamp(int(unix_t), tz=timezone.utc).astimezone(require_zone(zone_name))


def session_bounds(unix_t: int, zone_name: str, start_hm: tuple[int, int], end_hm: tuple[int, int]) -> tuple[int, int]:
    """Most recent completed or current local window as UTC unix (start, end)."""
    local = local_now(unix_t, zone_name)
    start = local.replace(hour=start_hm[0], minute=start_hm[1], second=0, microsecond=0)
    end = local.replace(hour=end_hm[0], minute=end_hm[1], second=0, microsecond=0)
    if end <= start:
        # wraps midnight
        if local < end:
            start = start.replace(day=start.day) - __import__("datetime").timedelta(days=1)
        else:
            end = end + __import__("datetime").timedelta(days=1)
    return int(start.astimezone(timezone.utc).timestamp()), int(end.astimezone(timezone.utc).timestamp())
