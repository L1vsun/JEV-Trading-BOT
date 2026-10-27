"""MetaTrader 5 terminal adapter.

The official ``MetaTrader5`` package is imported only when a session connects,
and only on the Windows machine where the terminal is installed. Tests pass a
fake module in. Credentials are read from the environment inside
:func:`jev_bot.risk.read_mt5_credentials` and are never stored on this object
and never placed in an exception message.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .mt5_feed import (
    BookLevel,
    Tick,
    broker_offset_seconds,
    parse_book,
    parse_ticks,
    server_unix_from_tick,
)
from .risk import (
    ORDER_FILLING_RETURN,
    ConnectionLost,
    OrderIntent,
    RiskError,
    SymbolError,
    SymbolSpec,
    read_mt5_credentials,
    select_filling,
    trade_mode_name,
)

# RoboForex (and several other MT5 brokers) append one of these to XAUUSD.
SYMBOL_SUFFIXES: tuple[str, ...] = ("", "c", ".c", "m", ".m", "_i", ".pro", ".ecn", "micro", ".micro")
_SYMBOL_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")

# Retcodes that mean the deal was accepted.
_DONE = (10009, 10010, 10008)          # DONE, DONE_PARTIAL, PLACED
_POSITION_BUY = 0
_HEDGING = 2


def load_mt5():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise ConnectionLost(
            "MetaTrader5 is not installed. On the Windows machine that runs the terminal: "
            'pip install -e ".[mt5]"'
        ) from None
    return mt5


def _const(mt5, name: str, default: int) -> int:
    return int(getattr(mt5, name, default))


def _error_code(mt5) -> int:
    try:
        err = mt5.last_error()
    except Exception:
        return -1
    if isinstance(err, tuple) and err:
        return int(err[0])
    return -1


@dataclass
class Exposure:
    net: float                         # signed lots, this magic and symbol only
    count: int


@dataclass
class OrderReport:
    ok: bool
    retcode: int
    price: float
    volume: float


class MT5Broker:
    def __init__(self, symbol: str = "XAUUSD", magic: int = 20261027, deviation: int = 30,
                 mt5=None, loader=None, clock=None):
        if not _SYMBOL_RE.fullmatch(symbol or ""):
            raise SymbolError("invalid symbol")
        self.requested_symbol = symbol
        self.symbol = symbol
        self.magic = int(magic)
        self.deviation = int(deviation)
        self._mod = mt5
        self._loader = loader or load_mt5
        self.clock = clock or time.time
        self.server_offset = 0
        self.clock_locked = False
        self._book_added = False
        self._book_ok = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _mt5(self):
        if self._mod is None:
            try:
                self._mod = self._loader()
            except ImportError:
                raise ConnectionLost(
                    "MetaTrader5 is not installed. On the Windows machine that runs the terminal: "
                    'pip install -e ".[mt5]"'
                ) from None
        return self._mod

    def connect(self) -> None:
        creds = read_mt5_credentials()
        mt5 = self._mt5()
        kwargs: dict = {}
        if creds["path"]:
            kwargs["path"] = creds["path"]
        if creds["login"]:
            kwargs["login"] = int(creds["login"])
            kwargs["password"] = creds["password"]
            kwargs["server"] = creds["server"]
        try:
            try:
                ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
            except Exception:
                raise ConnectionLost(f"MT5 initialize failed (error {_error_code(mt5)})") from None
        finally:
            creds["password"] = ""
            kwargs.pop("password", None)
        if not ok:
            raise ConnectionLost(f"MT5 initialize failed (error {_error_code(mt5)})")
        self._connected = True
        self.symbol = resolve_symbol(mt5, self.requested_symbol)
        self._book_added = False

    def shutdown(self) -> None:
        mt5 = self._mod
        if mt5 is None:
            return
        rel = getattr(mt5, "market_book_release", None)
        if rel is not None and self._book_added:
            try:
                rel(self.symbol)
            except Exception:
                pass
        try:
            mt5.shutdown()
        except Exception:
            pass
        self._connected = False

    def healthy(self) -> bool:
        mt5 = self._mod
        if mt5 is None or not self._connected:
            return False
        try:
            info = mt5.terminal_info()
        except Exception:
            return False
        if info is None:
            return False
        return bool(getattr(info, "connected", True))

    def trade_mode(self) -> str:
        info = self._account()
        return trade_mode_name(int(getattr(info, "trade_mode", 2)))

    def describe(self) -> str:
        """Account facts that are safe to print. No login, server or path."""
        info = self._account()
        currency = getattr(info, "currency", "?")
        algo = "on" if self.algo_trading() else "off"
        return f"mode={self.trade_mode()} currency={currency} algo={algo}"

    def algo_trading(self) -> bool:
        mt5 = self._mt5()
        term = mt5.terminal_info()
        if term is not None and not bool(getattr(term, "trade_allowed", True)):
            return False
        info = self._account()
        return bool(getattr(info, "trade_expert", True))

    def _account(self):
        mt5 = self._mt5()
        info = mt5.account_info()
        if info is None:
            raise ConnectionLost("no account info")
        return info

    def spec(self) -> SymbolSpec:
        mt5 = self._mt5()
        info = mt5.symbol_info(self.symbol)
        tick = mt5.symbol_info_tick(self.symbol)
        if info is None or tick is None:
            raise ConnectionLost("symbol info unavailable")
        point = float(info.point)
        tick_size = float(getattr(info, "trade_tick_size", 0) or point or 0.01)
        return SymbolSpec(
            bid=float(tick.bid), ask=float(tick.ask), point=point,
            digits=int(info.digits),
            volume_min=float(info.volume_min), volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
            freeze_level=int(getattr(info, "trade_freeze_level", 0) or 0),
            filling_mode=int(getattr(info, "filling_mode", 0) or 0),
            trade_exemode=int(getattr(info, "trade_exemode", 1)),
            symbol_trade_mode=int(getattr(info, "trade_mode", 4) or 0),
            contract_size=float(getattr(info, "trade_contract_size", 100.0) or 100.0),
            tick_size=tick_size,
        )

    def ensure_clock(self, now_unix: float | None = None) -> int:
        """Lock the broker-clock offset from the latest quote.

        The offset stays 0 until a quote is within 18 hours of UTC, so a
        Friday tick cannot be mistaken for a timezone. Later calls keep the
        first lock: a one-second change would split the warmup.
        """
        if self.clock_locked:
            return self.server_offset
        now_unix = self.clock() if now_unix is None else now_unix
        raw = self._mt5().symbol_info_tick(self.symbol)
        server = server_unix_from_tick(raw) if raw is not None else None
        if server is None:
            return self.server_offset
        offset = broker_offset_seconds(server, now_unix)
        if offset is None:
            return self.server_offset
        self.server_offset = offset
        self.clock_locked = True
        return offset

    def clock_text(self) -> str:
        if not self.clock_locked:
            return "broker clock unknown (last quote is stale)"
        if self.server_offset == 0:
            return "broker clock matches UTC"
        return f"broker clock {self.server_offset:+d}s vs UTC"

    def _broker_datetime(self, real_unix: float) -> datetime:
        """Naive local datetime the terminal reads as this UTC instant on the broker clock.

        ``datetime.timestamp`` of the result is ``real_unix + server_offset``.
        That is the number stored in tick times.
        """
        return datetime.fromtimestamp(real_unix + self.server_offset)

    def _to_utc(self, ticks: list[Tick]) -> list[Tick]:
        delta = self.server_offset * 1000
        if not delta:
            return ticks
        return [replace(t, time_msc=int(round(t.time_msc - delta))) for t in ticks]

    def history_ticks(self, seconds: int, now_unix: float | None = None) -> list[Tick]:
        mt5 = self._mt5()
        fn = getattr(mt5, "copy_ticks_range", None)
        if fn is None:
            return []
        now_unix = self.clock() if now_unix is None else now_unix
        self.ensure_clock(now_unix)
        # The terminal compares the datetime's timestamp with tick times, and
        # those tick times use the broker clock. Ask for that clock, then
        # shift the rows back to UTC.
        end = self._broker_datetime(now_unix)
        start = self._broker_datetime(now_unix - max(1, int(seconds)))
        raw = fn(self.symbol, start, end, _const(mt5, "COPY_TICKS_ALL", 3))
        return self._to_utc(parse_ticks(raw))

    def poll_ticks(self, since_msc: int, now_unix: float | None = None) -> list[Tick]:
        """Ticks newer than ``since_msc`` (UTC milliseconds). The first call asks for the last few seconds."""
        if not self.healthy():
            raise ConnectionLost("terminal disconnected")
        mt5 = self._mt5()
        now_unix = self.clock() if now_unix is None else now_unix
        self.ensure_clock(now_unix)
        if since_msc > 0:
            start_real = since_msc / 1000.0
        else:
            start_real = now_unix - 5
        raw = mt5.copy_ticks_from(
            self.symbol, self._broker_datetime(start_real), 50_000, _const(mt5, "COPY_TICKS_ALL", 3),
        )
        if raw is None:
            if not self.healthy():
                raise ConnectionLost("terminal disconnected")
            return []
        ticks = self._to_utc(parse_ticks(raw))
        if since_msc:
            ticks = [t for t in ticks if t.time_msc > since_msc]
        return ticks

    def quote(self) -> Tick | None:
        mt5 = self._mt5()
        raw = mt5.symbol_info_tick(self.symbol)
        if raw is None:
            return None
        ticks = self._to_utc(parse_ticks([raw]))
        return ticks[0] if ticks else None

    def book(self) -> list[BookLevel] | None:
        mt5 = self._mt5()
        if not self._book_ok:
            add = getattr(mt5, "market_book_add", None)
            try:
                self._book_ok = bool(add(self.symbol)) if add else False
            except Exception:
                self._book_ok = False
            self._book_added = True
            if not self._book_ok:
                return None
        get = getattr(mt5, "market_book_get", None)
        if get is None:
            return None
        try:
            raw = get(self.symbol)
        except Exception:
            return None
        levels = parse_book(raw)
        return levels or None

    def exposure(self) -> Exposure:
        net = 0.0
        rows = self._our_positions()
        for pos in rows:
            vol = float(getattr(pos, "volume", 0) or 0)
            if int(getattr(pos, "type", 0)) == _POSITION_BUY:
                net += vol
            else:
                net -= vol
        return Exposure(net=net, count=len(rows))

    def daily_pnl(self, now: datetime | None = None) -> float:
        """Realised deals since 00:00 UTC plus floating profit, this magic only.

        The terminal compares the datetime against deal times on the broker
        clock. The UTC midnight instant is shifted by the measured offset and
        then written as the machine's local time, which is what the package
        reads back into a timestamp.
        """
        mt5 = self._mt5()
        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.ensure_clock(self.clock())
        # Deal times use the broker clock too. 00:00 UTC is that instant plus
        # the offset, not the UTC fields written through as a naive datetime.
        start = self._broker_datetime(start_utc.timestamp())
        end = self._broker_datetime(now_utc.timestamp())
        fn = getattr(mt5, "history_deals_get", None)
        deals = fn(start, end) if fn else None
        realised = 0.0
        for deal in deals or []:
            if getattr(deal, "symbol", None) != self.symbol:
                continue
            if int(getattr(deal, "magic", -1)) != self.magic:
                continue
            realised += float(getattr(deal, "profit", 0) or 0)
            realised += float(getattr(deal, "commission", 0) or 0)
            realised += float(getattr(deal, "swap", 0) or 0)
            realised += float(getattr(deal, "fee", 0) or 0)
        floating = 0.0
        for pos in self._our_positions():
            floating += float(getattr(pos, "profit", 0) or 0)
            floating += float(getattr(pos, "swap", 0) or 0)
        return realised + floating

    def send(self, intent: OrderIntent) -> OrderReport:
        if not self.healthy():
            raise ConnectionLost("terminal disconnected")
        mt5 = self._mt5()
        request = self._request(intent)
        result = mt5.order_send(request)
        if result is None:
            if not self.healthy():
                raise ConnectionLost("terminal disconnected")
            return OrderReport(False, _error_code(mt5), 0.0, 0.0)
        ret = int(getattr(result, "retcode", 0))
        done = {
            _const(mt5, "TRADE_RETCODE_DONE", _DONE[0]),
            _const(mt5, "TRADE_RETCODE_DONE_PARTIAL", _DONE[1]),
            _const(mt5, "TRADE_RETCODE_PLACED", _DONE[2]),
        }
        price = float(getattr(result, "price", 0) or intent.price)
        volume = float(getattr(result, "volume", 0) or intent.volume)
        return OrderReport(ret in done, ret, price, volume)

    def close_all(self) -> list[OrderReport]:
        """Close positions this bot opened on its symbol. Other magics stay."""
        if not self.healthy():
            raise ConnectionLost("terminal disconnected")
        mt5 = self._mt5()
        hedging = self._hedging()
        reports: list[OrderReport] = []
        for pos in self._our_positions():
            vol = float(getattr(pos, "volume", 0) or 0)
            if vol <= 0:
                continue
            buy = int(getattr(pos, "type", 0)) == _POSITION_BUY
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                raise ConnectionLost("symbol info unavailable")
            side = -1 if buy else 1
            price = float(tick.bid if buy else tick.ask)
            info = mt5.symbol_info(self.symbol)
            filling = ORDER_FILLING_RETURN
            digits = 2
            if info is not None:
                filling = select_filling(int(getattr(info, "filling_mode", 0) or 0),
                                          int(getattr(info, "trade_exemode", 0) or 0))
                digits = int(info.digits)
            intent = OrderIntent(
                side=side, volume=vol, price=round(price, digits), sl=0.0, tp=0.0,
                deviation=self.deviation, filling=filling, digits=digits, volume_digits=2,
            )
            request = self._request(intent)
            if hedging:
                request["position"] = int(getattr(pos, "ticket", 0))
            result = mt5.order_send(request)
            if result is None:
                if not self.healthy():
                    raise ConnectionLost("terminal disconnected")
                reports.append(OrderReport(False, _error_code(mt5), 0.0, 0.0))
                continue
            ret = int(getattr(result, "retcode", 0))
            reports.append(OrderReport(ret in _DONE or ret in (
                _const(mt5, "TRADE_RETCODE_DONE", _DONE[0]),
                _const(mt5, "TRADE_RETCODE_DONE_PARTIAL", _DONE[1]),
                _const(mt5, "TRADE_RETCODE_PLACED", _DONE[2]),
            ), ret, float(getattr(result, "price", 0) or price), vol))
        return reports

    def _request(self, intent: OrderIntent) -> dict:
        mt5 = self._mt5()
        return {
            "action": _const(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": self.symbol,
            "volume": float(intent.volume),
            "type": _const(mt5, "ORDER_TYPE_BUY", 0) if intent.side > 0 else _const(mt5, "ORDER_TYPE_SELL", 1),
            "price": float(intent.price),
            "sl": float(intent.sl),
            "tp": float(intent.tp),
            "deviation": int(intent.deviation),
            "magic": self.magic,
            "comment": "jev",
            "type_time": _const(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": int(intent.filling),
        }

    def _hedging(self) -> bool:
        info = self._account()
        return int(getattr(info, "margin_mode", 0)) == _HEDGING

    def _our_positions(self) -> list:
        mt5 = self._mt5()
        fn = getattr(mt5, "positions_get", None)
        rows = fn(symbol=self.symbol) if fn else None
        out = []
        for pos in rows or []:
            if int(getattr(pos, "magic", -1)) != self.magic:
                continue
            if getattr(pos, "symbol", self.symbol) != self.symbol:
                continue
            out.append(pos)
        return out


def _enable(mt5, name: str) -> bool:
    info = mt5.symbol_info(name)
    if info is None:
        return False
    select = getattr(mt5, "symbol_select", None)
    if select is not None and not select(name, True):
        # Already visible is good enough; a False select on a known symbol is a failure.
        if not bool(getattr(info, "visible", False)):
            return False
    return True


def resolve_symbol(mt5, requested: str) -> str:
    """Use the exact name when the terminal has it, otherwise a broker suffix.

    RoboForex cent and micro symbols often look like ``XAUUSDc`` or ``XAUUSD.c``.
    """
    if not _SYMBOL_RE.fullmatch(requested or ""):
        raise SymbolError("invalid symbol")
    if _enable(mt5, requested):
        return requested
    bases = [requested]
    for suf in SYMBOL_SUFFIXES:
        if suf and requested.endswith(suf) and requested[: -len(suf)]:
            bases.append(requested[: -len(suf)])
    tried: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for suf in SYMBOL_SUFFIXES:
            name = f"{base}{suf}"
            if name in seen:
                continue
            seen.add(name)
            tried.append(name)
            if _enable(mt5, name):
                return name
    matches: list[str] = []
    getter = getattr(mt5, "symbols_get", None)
    for row in (getter() if getter else None) or []:
        name = row.name if hasattr(row, "name") else row["name"]
        if requested.upper() in name.upper():
            matches.append(name)
    if len(matches) == 1 and _enable(mt5, matches[0]):
        return matches[0]
    extra = f" Candidates: {', '.join(matches[:8])}." if matches else ""
    raise SymbolError(
        f"symbol {requested!r} is not in the terminal. "
        f"Pass --symbol with the broker name (a suffix such as c, .c or m is common).{extra}"
    )


def require_algo_trading(broker: MT5Broker) -> None:
    if not broker.algo_trading():
        raise RiskError(
            "Algo Trading is off in the MT5 terminal. Enable it under Tools, Options, Expert Advisors, "
            "and press the AutoTrading button."
        )
