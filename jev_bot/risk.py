"""Hard limits for a live broker session.

Live always sends orders. Demo, contest and real-money accounts (including
cent accounts) all require ``--allow-real`` and the environment variable
``MT5_CONFIRM_REAL`` set to :data:`CONFIRM_PHRASE`. There is no silent demo
path. Login, password and server never pass through these messages.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIRM_PHRASE = "YES_TRADE_REAL"

# Official MetaTrader 5 enum values. SYMBOL_FILLING_* is a bitmask on the
# symbol; ORDER_FILLING_* is the integer written into ``order_send``.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
EXEC_REQUEST = 0
EXEC_INSTANT = 1
EXEC_MARKET = 2
EXEC_EXCHANGE = 3
SYMBOL_TRADE_DISABLED = 0
SYMBOL_TRADE_LONGONLY = 1
SYMBOL_TRADE_SHORTONLY = 2
SYMBOL_TRADE_CLOSEONLY = 3
SYMBOL_TRADE_FULL = 4

_SECRET_ENV = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "TYPESAFE_API_KEY")


class RiskError(RuntimeError):
    """This order must not be sent. The session keeps running."""


class TradingHalted(RuntimeError):
    """A hard stop. Flatten and leave the session."""


class ConnectionLost(RuntimeError):
    """The terminal is gone or has stopped answering."""


class SymbolError(RuntimeError):
    """The configured symbol is not in the terminal."""


def redact(text: str) -> str:
    """Remove credential values that happen to be in the environment."""
    out = text
    for key in _SECRET_ENV:
        val = os.environ.get(key) or ""
        if len(val) >= 4 and val in out:
            out = out.replace(val, "***")
    return out


def read_mt5_credentials() -> dict[str, str]:
    """Login, password and server come from the environment, or not at all.

    All three must be set together. When none are set the terminal's existing
    login is used. The returned password is for ``initialize`` only; callers
    must not log the dict.
    """
    login = os.environ.get("MT5_LOGIN", "").strip()
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "").strip()
    path = os.environ.get("MT5_PATH", "").strip()
    present = (bool(login), bool(password), bool(server))
    if any(present) and not all(present):
        raise RiskError("set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER together, or leave all three unset")
    if login and not login.isdigit():
        raise RiskError("MT5_LOGIN must be the numeric account id")
    out = {"login": login, "password": password, "server": server, "path": path}
    return out


def trade_mode_name(mode: int) -> str:
    """Map an MT5 account trade mode. Anything unknown is treated as real."""
    return {0: "demo", 1: "contest", 2: "real"}.get(int(mode), "real")


def assert_may_send(trade_mode: str, *, allow_real: bool, confirm: str | None) -> None:
    """Refuse to send orders unless ``--allow-real`` and the confirm phrase match.

    Live-only: demo, contest and real accounts all require both the flag and
    :data:`CONFIRM_PHRASE`. There is no dry-run escape hatch.
    """
    _ = trade_mode  # demo and real are gated the same way
    if allow_real and confirm == CONFIRM_PHRASE:
        return
    raise RiskError(
        "refusing to send orders; pass --allow-real and set MT5_CONFIRM_REAL "
        "to the confirmation phrase (required for demo and real accounts)"
    )


@dataclass(frozen=True)
class RiskLimits:
    max_lots: float = 0.05
    max_positions: int = 5                # across all bot magics; 1 per strategy
    max_spread_points: float = 30.0
    lot: float = 0.01                      # full-size order; half size is lot/2
    sl_points: float = 200.0               # 0 = no stop
    tp_points: float = 400.0               # 0 = no take profit
    deviation_points: int = 15             # slippage allowance, in points

    def validate(self) -> None:
        if self.max_lots <= 0:
            raise RiskError("max_lots must be positive")
        if self.max_positions < 0:
            raise RiskError("max_positions must be >= 0")
        if self.lot <= 0:
            raise RiskError("lot must be positive")
        if self.lot > self.max_lots + 1e-12:
            raise RiskError("lot is above max_lots")
        if self.sl_points < 0 or self.tp_points < 0:
            raise RiskError("sl/tp points must be >= 0")
        if self.deviation_points < 0:
            raise RiskError("deviation must be >= 0")
        if self.max_spread_points <= 0:
            raise RiskError("max_spread_points must be positive")


# Equity-scaled lot sizing defaults (CLI: --lot-mode / --lot-base / …).
LOT_MODE_FIXED = "fixed"
LOT_MODE_EQUITY_SCALED = "equity-scaled"
DEFAULT_LOT_MODE = LOT_MODE_EQUITY_SCALED
DEFAULT_LOT_BASE = 0.01
DEFAULT_LOT_START_EQUITY = 1000.0
DEFAULT_LOT_FACTOR = 1.5


@dataclass(frozen=True)
class LotScaling:
    """Parameters for fixed or equity-scaled order volume.

    ``mode`` is ``fixed`` (use :attr:`RiskLimits.lot`) or ``equity-scaled``
    (see :func:`scaled_lot`). Staging is independent of ``RiskLimits``; the
    result is still capped by ``max_lots``.
    """

    mode: str = DEFAULT_LOT_MODE
    base: float = DEFAULT_LOT_BASE
    start_equity: float = DEFAULT_LOT_START_EQUITY
    factor: float = DEFAULT_LOT_FACTOR

    def validate(self) -> None:
        if self.mode not in (LOT_MODE_FIXED, LOT_MODE_EQUITY_SCALED):
            raise RiskError("lot mode must be 'fixed' or 'equity-scaled'")
        if self.base <= 0:
            raise RiskError("lot base must be positive")
        if self.start_equity <= 0:
            raise RiskError("lot start equity must be positive")
        if self.factor <= 1.0:
            raise RiskError("lot factor must be greater than 1")


def _equity_readable(equity: float | None) -> bool:
    if equity is None:
        return False
    try:
        value = float(equity)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value)


def equity_lot_stage(
    equity: float,
    base: float = DEFAULT_LOT_BASE,
    start: float = DEFAULT_LOT_START_EQUITY,
    factor: float = DEFAULT_LOT_FACTOR,
) -> int:
    """Return stage index ``n`` for ``equity``.

    Stage ``n`` begins at ``start * factor**n``. Equity below ``start`` (or
    unreadable) is stage ``0``, which uses the base lot. ``base`` is accepted
    for a stable call signature with :func:`scaled_lot`; staging itself depends
    only on equity, start and factor.
    """
    _ = base  # kept for call-signature parity with scaled_lot / CLI flags
    if not _equity_readable(equity) or float(equity) < start:
        return 0
    # Largest n with start * factor**n <= equity (avoid log for float edges).
    n = 0
    equity_f = float(equity)
    while start * (factor ** (n + 1)) <= equity_f + 1e-12:
        n += 1
        if n > 10_000:
            break
    return n


def _floor_volume_to_step(volume: float, step: float) -> float:
    """Always floor ``volume`` onto the broker step grid (from zero)."""
    if step <= 0:
        raise RiskError("volume_step must be positive")
    steps = math.floor(volume / step + 1e-12)
    return round(steps * step, _decimals(step))


def scaled_lot(
    equity: float | None,
    *,
    base: float = DEFAULT_LOT_BASE,
    start: float = DEFAULT_LOT_START_EQUITY,
    factor: float = DEFAULT_LOT_FACTOR,
    volume_min: float = 0.01,
    volume_step: float = 0.01,
    max_lots: float = 0.05,
) -> float:
    """Equity-scaled lot, floored to ``volume_step`` and capped by ``max_lots``.

    While equity is below ``start``, the lot is ``base``. At stage ``n``
    (equity ``>= start * factor**n``) the raw lot is ``base * factor**n``, then
    floored to ``volume_step``, raised to at least ``volume_min``, and capped
    at ``max_lots``. If equity is ``None`` or unreadable, return ``volume_min``
    (never a larger speculative size).
    """
    if volume_min <= 0 or volume_step <= 0:
        raise RiskError("volume_min and volume_step must be positive")
    if max_lots <= 0:
        raise RiskError("max_lots must be positive")
    if not _equity_readable(equity):
        return float(volume_min)
    equity_f = float(equity)
    if equity_f < start:
        raw = float(base)
    else:
        n = equity_lot_stage(equity_f, base, start, factor)
        raw = float(base) * (float(factor) ** n)
    floored = _floor_volume_to_step(raw, volume_step)
    if floored < volume_min - 1e-12:
        floored = float(volume_min)
    return float(min(floored, max_lots))


def lot_stage_table(
    max_equity: float = 20_000.0,
    *,
    base: float = DEFAULT_LOT_BASE,
    start: float = DEFAULT_LOT_START_EQUITY,
    factor: float = DEFAULT_LOT_FACTOR,
    volume_min: float = 0.01,
    volume_step: float = 0.01,
    max_lots: float = 0.05,
) -> list[dict[str, float | int]]:
    """Rows of ``{stage, equity_from, raw_lot, lot}`` up to ``max_equity``.

    Useful for README examples and session logs. Includes stage ``0`` at
    ``start`` and every higher stage whose threshold is ``<= max_equity``.
    """
    if max_equity < 0:
        raise RiskError("max_equity must be >= 0")
    rows: list[dict[str, float | int]] = []
    n = 0
    while n <= 10_000:
        equity_from = float(start) * (float(factor) ** n)
        if n > 0 and equity_from > max_equity + 1e-12:
            break
        raw_lot = float(base) * (float(factor) ** n)
        lot = scaled_lot(
            equity_from,
            base=base,
            start=start,
            factor=factor,
            volume_min=volume_min,
            volume_step=volume_step,
            max_lots=max_lots,
        )
        rows.append(
            {
                "stage": n,
                "equity_from": equity_from,
                "raw_lot": raw_lot,
                "lot": lot,
            }
        )
        if equity_from >= max_equity - 1e-12:
            break
        n += 1
    return rows


def check_entry(limits: RiskLimits, volume: float, open_positions: int, current_lots: float) -> None:
    """Gate a new entry against global position count and total open lots.

    ``current_lots`` is the sum of abs lots across all bot magics; ``volume``
    is the new order. Together they must stay at or under ``max_lots``.
    """
    if open_positions >= limits.max_positions:
        raise RiskError("max open positions reached")
    if volume > limits.max_lots + 1e-12:
        raise RiskError("volume above max_lots")
    if current_lots + volume > limits.max_lots + 1e-12:
        raise RiskError("position would exceed max_lots")


def _decimals(step: float) -> int:
    text = f"{step:.8f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return min(8, len(text.split(".")[1]))


def normalize_volume(volume: float, vmin: float, vmax: float, step: float) -> float:
    """Snap ``volume`` down onto the broker's step grid.

    Values under the minimum are rejected. Values over the broker maximum are
    rejected rather than silently reduced.
    """
    if vmin <= 0 or step <= 0 or vmax <= 0:
        raise RiskError("broker volume constraints are invalid")
    if volume < vmin - 1e-9:
        raise RiskError("volume below broker minimum")
    if volume > vmax + 1e-9:
        raise RiskError("volume above broker maximum")
    steps = math.floor((volume - vmin) / step + 1e-8)
    snapped = round(vmin + steps * step, _decimals(step))
    if snapped < vmin - 1e-9:
        raise RiskError("volume below broker minimum")
    if snapped > vmax + 1e-9:
        raise RiskError("volume above broker maximum")
    return snapped


def select_filling(filling_mode: int, trade_exemode: int) -> int:
    """Pick the ``type_filling`` value ``order_send`` will accept.

    Market and exchange execution use RETURN. Instant and request execution
    use FOK when the symbol allows it, otherwise IOC, otherwise RETURN.
    ``filling_mode`` is the symbol bitmask (FOK=1, IOC=2), which is not the
    same numbering as the order enum.
    """
    if trade_exemode in (EXEC_MARKET, EXEC_EXCHANGE):
        return ORDER_FILLING_RETURN
    if filling_mode & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    if filling_mode & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    return ORDER_FILLING_RETURN


@dataclass(frozen=True)
class SymbolSpec:
    bid: float
    ask: float
    point: float
    digits: int
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    freeze_level: int
    filling_mode: int
    trade_exemode: int
    symbol_trade_mode: int = SYMBOL_TRADE_FULL
    contract_size: float = 100.0
    tick_size: float = 0.01
    tick_value: float = 1.0

    @property
    def spread_points(self) -> float:
        if self.point <= 0:
            return float("inf")
        return (self.ask - self.bid) / self.point


@dataclass(frozen=True)
class OrderIntent:
    side: int
    volume: float
    price: float
    sl: float
    tp: float
    deviation: int
    filling: int
    digits: int
    volume_digits: int
    stops_adjusted: bool = False

    @property
    def filling_name(self) -> str:
        return {ORDER_FILLING_FOK: "FOK", ORDER_FILLING_IOC: "IOC", ORDER_FILLING_RETURN: "RETURN"}.get(
            self.filling, str(self.filling))


def _round_px(px: float, digits: int) -> float:
    return round(px, max(0, digits))


def build_order(side: int, volume: float, spec: SymbolSpec, limits: RiskLimits) -> OrderIntent:
    """Turn a sized decision into a market-deal request the terminal can accept.

    Buys pay the ask and sells hit the bid. Stops closer than the broker's
    minimum distance are widened to that distance. A spread wider than the
    configured cap refuses the entry.
    """
    if side not in (1, -1):
        raise RiskError("side must be buy or sell")
    if spec.point <= 0 or spec.bid <= 0 or spec.ask <= 0:
        raise RiskError("broker quote is not usable")
    if spec.ask < spec.bid:
        raise RiskError("crossed quote")
    mode = spec.symbol_trade_mode
    if mode in (SYMBOL_TRADE_DISABLED, SYMBOL_TRADE_CLOSEONLY):
        raise RiskError("symbol is closed for new trades")
    if mode == SYMBOL_TRADE_LONGONLY and side < 0:
        raise RiskError("symbol is long only")
    if mode == SYMBOL_TRADE_SHORTONLY and side > 0:
        raise RiskError("symbol is short only")
    if spec.spread_points > limits.max_spread_points + 1e-9:
        raise RiskError(f"spread {spec.spread_points:.0f}pts above max")
    if volume > limits.max_lots + 1e-12:
        raise RiskError("volume above max_lots")
    snapped = normalize_volume(volume, spec.volume_min, spec.volume_max, spec.volume_step)
    if snapped > limits.max_lots + 1e-12:
        raise RiskError("volume above max_lots")
    price = spec.ask if side > 0 else spec.bid
    min_points = max(int(spec.stops_level), int(spec.freeze_level), 0)
    adjusted = False

    def dist(points: float) -> float:
        nonlocal adjusted
        if points <= 0:
            return 0.0
        if points < min_points:
            adjusted = True
            points = float(min_points)
        return points * spec.point

    sl_d, tp_d = dist(limits.sl_points), dist(limits.tp_points)
    if side > 0:
        sl = price - sl_d if sl_d else 0.0
        tp = price + tp_d if tp_d else 0.0
    else:
        sl = price + sl_d if sl_d else 0.0
        tp = price - tp_d if tp_d else 0.0
    return OrderIntent(
        side=side, volume=snapped, price=_round_px(price, spec.digits),
        sl=_round_px(sl, spec.digits) if sl else 0.0,
        tp=_round_px(tp, spec.digits) if tp else 0.0,
        deviation=int(limits.deviation_points),
        filling=select_filling(spec.filling_mode, spec.trade_exemode),
        digits=spec.digits, volume_digits=_decimals(spec.volume_step),
        stops_adjusted=adjusted,
    )


def format_intent(symbol: str, intent: OrderIntent, spread_points: float) -> str:
    side = "BUY" if intent.side > 0 else "SELL"
    vd, pd = intent.volume_digits, intent.digits
    sl = f"{intent.sl:.{pd}f}" if intent.sl else "off"
    tp = f"{intent.tp:.{pd}f}" if intent.tp else "off"
    note = " stops widened to the broker minimum" if intent.stops_adjusted else ""
    return (
        f"{side} {symbol} {intent.volume:.{vd}f} @ {intent.price:.{pd}f} "
        f"sl={sl} tp={tp} deviation={intent.deviation}pts filling={intent.filling_name} "
        f"spread={spread_points:.0f}pts{note}"
    )


class KillSwitch:
    """A file in the working directory. Creating it flattens and stops."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def tripped(self) -> bool:
        return self.path.exists()


def stop_exit(side: int, bid: float, ask: float, sl: float, tp: float) -> tuple[str, float] | None:
    """Return ``(reason, price)`` when the exit side has traded through a stop.

    A long is sold on the bid and a short is covered on the ask, which is how
    a broker stop on that position fills. Stop loss wins when one quote crosses
    both levels. A price of 0 means that side is off.
    """
    if side > 0:
        if sl > 0 and bid <= sl:
            return "stop loss", sl
        if tp > 0 and bid >= tp:
            return "take profit", tp
    elif side < 0:
        if sl > 0 and ask >= sl:
            return "stop loss", sl
        if tp > 0 and ask <= tp:
            return "take profit", tp
    return None


@dataclass(frozen=True)
class AvoidHours:
    """A clock window in Europe/Berlin during which new entries are refused.

    The end minute is exclusive, so ``13:00-17:00`` blocks 13:00 and allows
    17:00. A window that passes midnight, such as ``22:00-02:00``, wraps.
    Existing positions are not closed because of this window.
    """

    start: int
    end: int
    text: str

    def blocks(self, now: datetime) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(require_berlin())
        minute = local.hour * 60 + local.minute
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end


def parse_avoid_hours(text: str | None) -> AvoidHours | None:
    """Parse ``HH:MM-HH:MM``. Empty, ``off`` and ``none`` mean the filter is off."""
    raw = (text or "").strip()
    if not raw or raw.lower() in ("off", "none"):
        return None
    parts = raw.split("-")
    if len(parts) != 2:
        raise RiskError("avoid-hours must look like 13:00-17:00")

    def mins(piece: str) -> int:
        piece = piece.strip()
        if piece.count(":") != 1:
            raise RiskError("avoid-hours must look like 13:00-17:00")
        hour, minute = piece.split(":", 1)
        if not hour.isdigit() or not minute.isdigit() or len(minute) != 2:
            raise RiskError("avoid-hours must look like 13:00-17:00")
        hh, mm = int(hour), int(minute)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise RiskError("avoid-hours must look like 13:00-17:00")
        return hh * 60 + mm

    start, end = mins(parts[0]), mins(parts[1])
    if start == end:
        raise RiskError("avoid-hours start and end are the same")

    def fmt(n: int) -> str:
        return f"{n // 60:02d}:{n % 60:02d}"

    return AvoidHours(start, end, f"{fmt(start)}-{fmt(end)}")


def require_berlin() -> ZoneInfo:
    """Load ``Europe/Berlin`` for ``--avoid-hours``.

    A missing database raises :class:`RiskError`. The filter is not replaced
    with a fixed UTC offset, because Berlin daylight saving moves the window.
    """
    try:
        return ZoneInfo("Europe/Berlin")
    except ZoneInfoNotFoundError:
        raise RiskError(
            "cannot load Europe/Berlin, so --avoid-hours cannot be checked. "
            "Install the IANA time zone database and start again (on Windows: pip install tzdata)."
        ) from None


def require_new_york() -> ZoneInfo:
    """Load ``America/New_York``.

    The gold session follows that clock, including US daylight saving. A
    missing database raises :class:`RiskError`. Callers must not replace it
    with a fixed UTC offset.
    """
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        raise RiskError(
            "cannot load America/New_York, so the gold session cannot be checked. "
            "Install the IANA time zone database and start again (on Windows: pip install tzdata)."
        ) from None


def gold_session_open(now: datetime) -> bool:
    """XAUUSD session in New York time: Sunday 17:05 through Friday 16:55.

    The daily break is 16:55–17:05 New York. ``zoneinfo`` follows US daylight
    saving, which is the clock the gold session actually uses.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(require_new_york())
    wd = local.weekday()
    minute = local.hour * 60 + local.minute
    open_at, shut = 17 * 60 + 5, 16 * 60 + 55
    if wd == 5:
        return False
    if wd == 6:
        return minute >= open_at
    if wd == 4 and minute >= shut:
        return False
    if wd in (0, 1, 2, 3) and shut <= minute < open_at:
        return False
    return True


@dataclass(frozen=True)
class MarketHours:
    """``name`` is ``gold`` or ``always``. ``always`` is an explicit override."""

    name: str = "gold"

    def is_open(self, now: datetime) -> bool:
        if self.name == "always":
            return True
        if self.name != "gold":
            raise RiskError("market hours must be 'gold' or 'always'")
        return gold_session_open(now)


def position_text(side: int, t: int, t_in: int, px_in: float, bid: float, ask: float) -> str:
    """Plain-language ``position`` field for Jev state."""
    if not side:
        return "flat"
    held = max(0, t - t_in)
    mid = (bid + ask) / 2
    upnl = (mid - px_in) * side
    state = "in profit" if upnl > 0 else "at a loss" if upnl < 0 else "flat"
    return f"{'long' if side > 0 else 'short'} for {held // 60} min {held % 60} s, currently {state}"
