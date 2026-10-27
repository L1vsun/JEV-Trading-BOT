"""MT5 adapter and live risk limits, against a fake MetaTrader5 module.

The real ``MetaTrader5`` package is Windows-only and is never imported here.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from jev_bot import features as F
from jev_bot.cli import build_parser
from jev_bot.live import LiveConfig, LiveSession, live_config_from_args
from jev_bot.mt5_broker import MT5Broker, resolve_symbol
from jev_bot.mt5_feed import (
    DEGRADED_WITHOUT_DEPTH,
    BookLevel,
    MarketBuffer,
    SecondBar,
    Tick,
    TickAggregator,
    aggressor,
    book_sizes,
    depth_note,
    broker_offset_seconds,
    parse_ticks,
    server_unix_from_tick,
)
from jev_bot.risk import (
    CONFIRM_PHRASE,
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ConnectionLost,
    MarketHours,
    RiskError,
    RiskLimits,
    SymbolError,
    SymbolSpec,
    TradingHalted,
    assert_may_send,
    build_order,
    check_entry,
    gold_session_open,
    loss_breached,
    normalize_volume,
    read_mt5_credentials,
    redact,
    select_filling,
    trade_mode_name,
)
from jev_bot.state import StateBuilder
from jev_bot.typesafe import Answer, JevError, Response

UTC = timezone.utc
_SECRET_KEYS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "MT5_CONFIRM_REAL", "TYPESAFE_API_KEY")


def _response(confidence: float = 0.85, exit_noul: float = 0.1) -> Response:
    buy = {"type": "choice", "choice": "buy", "probabilities": {"buy": 0.9, "sell": 0.02, "hold": 0.08}, "confidence": confidence}
    raw = {
        "regime": {"type": "choice", "choice": "trending_up", "probabilities": {"trending_up": .8, "trending_down": .05, "ranging": .1, "disorderly": .05}, "confidence": .7},
        "flow_control": {"type": "choice", "choice": "buyers", "probabilities": {"buyers": .9, "sellers": .05, "neither": .05}, "confidence": .85},
        "book_pressure": {"type": "choice", "choice": "up", "probabilities": {"up": .8, "down": .1, "neutral": .1}, "confidence": .7},
        "candle_close": {"type": "choice", "choice": "up", "probabilities": {"up": .7, "down": .1, "unclear": .2}, "confidence": .55},
        "informed_flow": {"type": "noul", "noul": .8}, "price_lagging_flow": {"type": "noul", "noul": .9},
        "exit_now": {"type": "noul", "noul": exit_noul},
        "liquidity_stress": {"type": "score", "score": .4, "probabilities": {"0": .6, "1": .4, "2": 0}, "confidence": .4},
        "setup_quality": {"type": "score", "score": 3.1, "probabilities": {"0": 0, "1": 0, "2": .1, "3": .7, "4": .2}, "confidence": .6},
        "action": buy, "action_alt": buy, "action_contrarian_check": buy,
    }
    return Response("scripted", {k: Answer.from_json(v) for k, v in raw.items()})


class ScriptedJev:
    model = "scripted"

    def __init__(self, items):
        self.items = list(items)
        self.states: list = []

    def system_one(self, state, questions):
        self.states.append(state)
        if not self.items:
            raise AssertionError("no scripted response left")
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


class BoomJev:
    model = "boom"

    def system_one(self, state, questions):
        raise JevError("api down")


def _symbol(name="XAUUSD", **kw):
    base = dict(
        name=name, point=0.01, digits=2, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        trade_stops_level=10, trade_freeze_level=0, filling_mode=1, trade_exemode=1, trade_mode=4,
        trade_contract_size=100.0, trade_tick_size=0.01, visible=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _position(magic=20261027, ticket=1, volume=0.01, side=0, profit=0.0, symbol="XAUUSD"):
    return SimpleNamespace(ticket=ticket, symbol=symbol, magic=magic, volume=volume, type=side, profit=profit, swap=0.0)


class FakeMT5:
    """Stand-in for the official module. Constants match the real enum values."""

    COPY_TICKS_ALL = 3
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008

    def __init__(self):
        self.fail_init = False
        self.init_kwargs = None
        self.init_args = None
        self.initialized = False
        self.up = True
        self.trade_allowed = True
        self.trade_expert = True
        self.trade_mode = 0
        self.margin_mode = 0
        self.currency = "USD"
        self.login = 55667788
        self.server = "RoboForex-Demo"
        self.symbols = {"XAUUSD": _symbol()}
        self.quote = {"bid": 2000.0, "ask": 2000.2, "last": 0.0, "volume": 1.0, "volume_real": 0.0, "flags": 0, "time_msc": 1_700_000_000_000}
        self.batches: list = []
        self.tick_from = None
        self.range_window = None
        self.time_filter = False
        self.history = []
        self.book_levels = None
        self.positions: list = []
        self.deals: list = []
        self.orders: list = []
        self.retcode = 10009
        self.return_none = False
        self.raise_keyboard = False
        self.shutdown_calls = 0

    def initialize(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = dict(kwargs)
        self.initialized = not self.fail_init
        return self.initialized

    def shutdown(self):
        self.shutdown_calls += 1
        self.initialized = False

    def last_error(self):
        return (-6, "Authorization failed")

    def terminal_info(self):
        if not self.initialized or not self.up:
            return None
        return SimpleNamespace(connected=True, trade_allowed=self.trade_allowed)

    def account_info(self):
        if not self.initialized:
            return None
        return SimpleNamespace(
            trade_mode=self.trade_mode, margin_mode=self.margin_mode, currency=self.currency,
            login=self.login, server=self.server, balance=10_000.0, equity=10_000.0, trade_expert=self.trade_expert,
        )

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable=True):
        return name in self.symbols

    def symbols_get(self):
        return [SimpleNamespace(name=name) for name in self.symbols]

    def symbol_info_tick(self, name):
        if name not in self.symbols or not self.up:
            return None
        q = self.quote
        return SimpleNamespace(time=q["time_msc"] // 1000, time_msc=q["time_msc"], **{k: q[k] for k in ("bid", "ask", "last", "volume", "volume_real", "flags")})

    def _unix(self, value) -> float:
        if isinstance(value, datetime):
            return value.timestamp()
        return float(value)

    def copy_ticks_from(self, symbol, date_from, count, flags):
        self.tick_from = date_from
        if self.raise_keyboard:
            raise KeyboardInterrupt
        if self.time_filter:
            start = self._unix(date_from)
            rows = [row for row in self.history if row["time_msc"] / 1000.0 >= start - 0.001]
            return rows[:count]
        if not self.batches:
            return []
        return self.batches.pop(0)

    def copy_ticks_range(self, symbol, start, end, flags):
        self.range_window = (start, end)
        if not self.time_filter:
            return self.history
        s, e = self._unix(start), self._unix(end)
        return [row for row in self.history if s <= row["time_msc"] / 1000.0 <= e + 0.001]

    def market_book_add(self, symbol):
        return self.book_levels is not None

    def market_book_get(self, symbol):
        return self.book_levels

    def market_book_release(self, symbol):
        return True

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or getattr(p, "symbol", None) == symbol]

    def history_deals_get(self, start, end):
        self.deal_window = (start, end)
        return list(self.deals)

    def order_send(self, request):
        self.orders.append(dict(request))
        if self.return_none:
            return None
        if self.retcode != 10009:
            return SimpleNamespace(retcode=self.retcode, order=0, price=0.0, volume=0.0)
        self._apply(request)
        return SimpleNamespace(retcode=self.retcode, order=len(self.orders), price=request["price"], volume=request["volume"])

    def _apply(self, request):
        ticket = request.get("position")
        vol = float(request["volume"])
        buy = int(request["type"]) == 0
        if ticket:
            self.positions = [p for p in self.positions if int(p.ticket) != int(ticket)]
            return
        opposite = [p for p in self.positions if int(p.magic) == int(request["magic"]) and (int(p.type) == 0) != buy]
        if opposite:
            opposite[0].volume = float(opposite[0].volume) - vol
            if opposite[0].volume <= 1e-9:
                self.positions = [p for p in self.positions if p is not opposite[0]]
            return
        self.positions.append(_position(
            magic=int(request["magic"]), ticket=100 + len(self.orders), volume=vol, side=0 if buy else 1, symbol=request["symbol"],
        ))


def _spec(**kw) -> SymbolSpec:
    base = dict(
        bid=2000.0, ask=2000.2, point=0.01, digits=2, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        stops_level=10, freeze_level=0, filling_mode=1, trade_exemode=1, symbol_trade_mode=4,
        contract_size=100.0, tick_size=0.01,
    )
    base.update(kw)
    return SymbolSpec(**base)


def _bar(t, bid=2000.0, ask=2000.2, buy=1.0, sell=0.0) -> SecondBar:
    b = np.zeros(5)
    a = np.zeros(5)
    b[0] = a[0] = 1.0
    return SecondBar(t, bid, ask, b, a, buy, sell, 1 if buy or sell else 0, False)


def _cfg(**kw) -> LiveConfig:
    lim = {f.name: getattr(RiskLimits(), f.name) for f in fields(RiskLimits)}
    for key in list(kw):
        if key in lim:
            lim[key] = kw.pop(key)
    kill = kw.pop("kill_file", None) or str(Path(tempfile.mkdtemp()) / "jev.KILL")
    # feature_matrix momentum uses a 300-second window, so the first decision needs that much book.
    defaults = dict(
        symbol="XAUUSD", step=1, warmup=320, history=800, hours="always", dry_run=True,
        allow_real=False, kill_file=kill, magic=20261027, verbose=False,
    )
    defaults.update(kw)
    defaults["limits"] = RiskLimits(**lim)
    return LiveConfig(**defaults)


def _session(fake, cfg, jev=None, **kw) -> LiveSession:
    broker = MT5Broker(symbol=cfg.symbol, magic=cfg.magic, deviation=cfg.limits.deviation_points, mt5=fake)
    return LiveSession(broker, jev or ScriptedJev([_response()]), cfg, sleep=lambda *_: None, **kw)


def _ready(fake, cfg, jev=None, **kw) -> LiveSession:
    session = _session(fake, cfg, jev, **kw)
    session.broker.connect()
    session.ensure_allowed()
    return session


def _prefill(session, n=None, start=1) -> int:
    """Fill every second before the warmup elapses. Return the next timestamp."""
    n = session.cfg.warmup - 1 if n is None else n
    for i in range(n):
        session.buffer.append(_bar(start + i))
    return start + n


class _CleanEnv(unittest.TestCase):
    def setUp(self):
        self._old = {k: os.environ.get(k) for k in _SECRET_KEYS}
        for key in _SECRET_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, val in self._old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class Feed(_CleanEnv):
    def test_flags_beat_the_quote_rule_and_volume_is_signed(self):
        sell_at_ask = Tick(1_000, 2000.0, 2000.2, last=2000.2, volume=4, flags=64)
        self.assertEqual(aggressor(sell_at_ask, None, 0), -1)
        self.assertEqual(aggressor(Tick(1_000, 2000.0, 2000.2, flags=32), None, 0), 1)
        first = Tick(1_000, 2000.0, 2000.2, volume=2)
        second = Tick(2_000, 2000.1, 2000.3, volume=2)          # mid up
        third = Tick(3_000, 2000.1, 2000.3, volume=2)           # unchanged, carry the buy
        self.assertEqual(aggressor(first, None, 0), 0)
        self.assertEqual(aggressor(second, 2000.1, 0), 1)
        self.assertEqual(aggressor(third, 2000.2, 1), 1)
        bars = TickAggregator().bars([
            Tick(5_000, 2000.0, 2000.2, volume=9, volume_real=2, flags=32),
            Tick(5_400, 2000.0, 2000.2, volume=1, flags=64),
            Tick(5_800, 2000.0, 2000.2, flags=2),               # quote only
        ], None)
        self.assertEqual(len(bars), 1)
        self.assertAlmostEqual(bars[0].buy_vol, 2)
        self.assertAlmostEqual(bars[0].sell_vol, 1)
        self.assertEqual(bars[0].n_trades, 2)

    def test_gaps_and_level1_fallback(self):
        far = TickAggregator(max_gap=10).bars([
            Tick(1_000, 2000.0, 2000.2, volume=1, flags=32),
            Tick(30_000, 2001.0, 2001.2, volume=1, flags=32),
        ], None)
        self.assertEqual([b.t for b in far], [30])
        filled = TickAggregator(max_gap=10).bars([
            Tick(1_000, 2000.0, 2000.2, volume=1, flags=32),
            Tick(4_000, 2000.1, 2000.3, volume=1, flags=64),
        ], None)
        self.assertEqual([b.t for b in filled], [1, 2, 3, 4])
        self.assertFalse(filled[0].depth)
        self.assertEqual(filled[0].bid_sz[0], 1.0)
        self.assertEqual(filled[0].ask_sz[0], 1.0)
        one_sided = TickAggregator().bars(
            [Tick(1_000, 2000.0, 2000.2, volume=1, flags=32)],
            [BookLevel(2000.0, 8, "bid")],
        )
        self.assertFalse(one_sided[0].depth)
        buf = MarketBuffer(10, max_gap=10)
        buf.append(_bar(1, buy=1))
        buf.append(_bar(1, buy=2))
        self.assertAlmostEqual(buf.rows[0].buy_vol, 3)
        self.assertTrue(buf.append(_bar(100)))
        self.assertEqual([r.t for r in buf.rows], [100])

    def test_depth_is_sorted_and_fallback_degrades_the_book_jevs_reads(self):
        levels = [
            BookLevel(1999.5, 3, "bid"), BookLevel(2000.0, 8, "bid"),
            BookLevel(2000.5, 4, "ask"), BookLevel(2000.2, 1, "ask"),
        ]
        bid, ask, depth = book_sizes(levels, 5, 1.0)
        self.assertTrue(depth)
        self.assertEqual((bid[0], bid[1], ask[0], ask[1]), (8, 3, 1, 4))
        ticks = [
            Tick((1_700_000_000 + i) * 1000, 2000 + i * 0.01, 2000.2 + i * 0.01, volume=1, flags=32 if i % 2 == 0 else 64)
            for i in range(320)
        ]
        bars = TickAggregator().bars(ticks, None)
        buf = MarketBuffer(400, tick=0.01)
        for bar in bars:
            buf.append(bar)
        m = buf.market()
        self.assertTrue(np.allclose(F.queue_imbalance(m), 0))
        self.assertTrue(np.allclose(F.depth_imbalance(m), 0))
        self.assertTrue(np.allclose(F.weighted_mid(m), m.mid))
        self.assertGreater(F.ofi(m)[1], 0)                      # a higher bid still scores as buying
        self.assertGreater(m.buy_vol.sum(), 0)
        _, _, raw = F.feature_matrix(m)
        state = StateBuilder(raw, np.zeros(len(m))).state(310)
        self.assertEqual(state["order_book"]["best_level"], "roughly even queues")
        self.assertEqual(state["order_book"]["top_five_levels"], state["order_book"]["best_level"])
        self.assertEqual(state["order_book"]["micro_price"], "centered")
        note = depth_note(available=False)
        for line in DEGRADED_WITHOUT_DEPTH:
            self.assertIn(line.split(":")[0], note)

    def test_numpy_tick_records_parse(self):
        arr = np.zeros(1, dtype=[
            ("time", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"),
            ("volume", "f8"), ("time_msc", "i8"), ("flags", "i8"), ("volume_real", "f8"),
        ])
        arr[0] = (10, 2000.0, 2000.2, 0.0, 3, 10_000, 32, 1.5)
        ticks = parse_ticks(arr)
        self.assertEqual(ticks[0].flags, 32)
        self.assertAlmostEqual(ticks[0].volume_real, 1.5)
        self.assertEqual(parse_ticks(None), [])


class Orders(_CleanEnv):
    def test_volume_step_spread_stops_and_filling(self):
        self.assertAlmostEqual(normalize_volume(0.03, 0.01, 50, 0.01), 0.03)
        self.assertAlmostEqual(normalize_volume(0.029, 0.01, 50, 0.01), 0.02)
        self.assertAlmostEqual(normalize_volume(0.015, 0.01, 50, 0.01), 0.01)
        with self.assertRaises(RiskError):
            normalize_volume(0.009, 0.01, 50, 0.01)
        with self.assertRaises(RiskError):
            normalize_volume(0.2, 0.01, 0.1, 0.01)
        buy = build_order(1, 0.01, _spec(), RiskLimits(sl_points=100, tp_points=200, deviation_points=30))
        self.assertEqual((buy.price, buy.sl, buy.tp, buy.deviation, buy.filling), (2000.2, 1999.2, 2002.2, 30, ORDER_FILLING_FOK))
        sell = build_order(-1, 0.02, _spec(), RiskLimits(sl_points=100, tp_points=50, lot=0.02))
        self.assertEqual((sell.price, sell.sl, sell.tp), (2000.0, 2001.0, 1999.5))
        wide = build_order(1, 0.01, _spec(stops_level=20), RiskLimits(sl_points=5))
        self.assertTrue(wide.stops_adjusted)
        self.assertAlmostEqual(wide.sl, 2000.0)
        self.assertEqual(wide.tp, 0.0)
        with self.assertRaises(RiskError) as cm:
            build_order(1, 0.01, _spec(ask=2001.0), RiskLimits(max_spread_points=80))
        self.assertIn("spread", str(cm.exception))
        with self.assertRaises(RiskError):
            build_order(1, 0.10, _spec(), RiskLimits(max_lots=0.05))
        with self.assertRaises(RiskError):
            build_order(1, 0.005, _spec(), RiskLimits())
        self.assertEqual(select_filling(1, 1), ORDER_FILLING_FOK)
        self.assertEqual(select_filling(2, 1), ORDER_FILLING_IOC)
        self.assertEqual(select_filling(3, 1), ORDER_FILLING_FOK)
        self.assertEqual(select_filling(1, 2), ORDER_FILLING_RETURN)
        self.assertEqual(select_filling(2, 3), ORDER_FILLING_RETURN)
        returned = build_order(1, 0.01, _spec(trade_exemode=2, filling_mode=1), RiskLimits())
        self.assertEqual(returned.filling_name, "RETURN")
        with self.assertRaises(RiskError):
            build_order(-1, 0.01, _spec(symbol_trade_mode=1), RiskLimits())
        with self.assertRaises(RiskError):
            build_order(1, 0.01, _spec(symbol_trade_mode=3), RiskLimits())

    def test_entry_caps_and_account_gate(self):
        limits = RiskLimits(max_positions=1, max_lots=0.05)
        check_entry(limits, 0.01, 0, 0)
        with self.assertRaises(RiskError):
            check_entry(limits, 0.01, 1, 0)
        with self.assertRaises(RiskError):
            check_entry(limits, 0.02, 0, 0.04)
        self.assertTrue(loss_breached(-20, 20))
        self.assertFalse(loss_breached(-19.99, 20))
        assert_may_send("demo", dry_run=True, allow_real=False, confirm=None)
        assert_may_send("real", dry_run=True, allow_real=False, confirm=None)
        assert_may_send("demo", dry_run=False, allow_real=False, confirm=None)
        assert_may_send("contest", dry_run=False, allow_real=False, confirm=None)
        assert_may_send("real", dry_run=False, allow_real=True, confirm=CONFIRM_PHRASE)
        for kwargs in (
            dict(trade_mode="real", dry_run=False, allow_real=False, confirm=None),
            dict(trade_mode="real", dry_run=False, allow_real=True, confirm=None),
            dict(trade_mode="real", dry_run=False, allow_real=True, confirm="nope"),
            dict(trade_mode="real", dry_run=False, allow_real=False, confirm=CONFIRM_PHRASE),
            dict(trade_mode="weird", dry_run=False, allow_real=False, confirm=None),
        ):
            with self.assertRaises(RiskError):
                assert_may_send(**kwargs)
        self.assertEqual(trade_mode_name(0), "demo")
        self.assertEqual(trade_mode_name(99), "real")

    def test_credentials_are_complete_and_redacted(self):
        os.environ["MT5_PASSWORD"] = "s3cret-password"
        self.assertEqual(redact("password is s3cret-password"), "password is ***")
        os.environ["MT5_LOGIN"] = "not-a-login-xx"
        os.environ["MT5_SERVER"] = "srv"
        with self.assertRaises(RiskError) as cm:
            read_mt5_credentials()
        self.assertNotIn("not-a-login-xx", str(cm.exception))
        os.environ.pop("MT5_PASSWORD")
        with self.assertRaises(RiskError) as cm:
            read_mt5_credentials()
        self.assertNotIn("not-a-login-xx", str(cm.exception))


class Hours(_CleanEnv):
    def test_gold_session_follows_new_york(self):
        def at(y, mo, d, hh, mm=0):
            return datetime(y, mo, d, hh, mm, tzinfo=UTC)
        self.assertFalse(gold_session_open(at(2026, 10, 24, 15)))          # Saturday
        self.assertTrue(gold_session_open(at(2026, 10, 26, 16)))            # Monday noon NY
        self.assertFalse(gold_session_open(at(2026, 10, 23, 21, 30)))       # Friday after the close
        self.assertTrue(gold_session_open(at(2026, 10, 25, 21, 30)))        # Sunday after the open
        self.assertFalse(gold_session_open(at(2026, 10, 25, 20)))           # Sunday before the open
        self.assertFalse(gold_session_open(at(2026, 10, 26, 20, 58)))       # daily break, daylight time
        self.assertTrue(gold_session_open(at(2026, 10, 26, 21, 10)))
        self.assertFalse(gold_session_open(at(2026, 1, 5, 22)))             # daily break, standard time
        self.assertTrue(gold_session_open(at(2026, 1, 5, 22, 30)))
        self.assertTrue(MarketHours("always").is_open(at(2026, 10, 24, 15)))

    def test_broker_offset_is_server_minus_utc_and_ignores_a_stale_quote(self):
        real = datetime(2026, 10, 27, 22, 24, 42, tzinfo=UTC)
        server_wall = datetime(2026, 10, 28, 0, 24, 39)  # naive, as symbol_info_tick().time
        server_unix = server_unix_from_tick({"time": server_wall, "time_msc": 0})
        self.assertEqual(server_unix, datetime(2026, 10, 28, 0, 24, 39, tzinfo=UTC).timestamp())
        self.assertEqual(broker_offset_seconds(server_unix, real.timestamp()), 7197)
        msc = int(round((real.timestamp() + 7200) * 1000))
        self.assertEqual(broker_offset_seconds(msc / 1000.0, real.timestamp()), 7200)
        self.assertIsNone(broker_offset_seconds(real.timestamp() - 3 * 86400, real.timestamp()))
        parsed = parse_ticks([{"time": server_wall, "time_msc": 0, "bid": 2000.0, "ask": 2000.2}])
        self.assertEqual(parsed[0].time_msc, int(round(server_unix * 1000)))

    def test_missing_time_zone_stops_the_session(self):
        when = datetime(2026, 10, 26, 16, tzinfo=UTC)
        with patch("jev_bot.risk.ZoneInfo", side_effect=ZoneInfoNotFoundError("America/New_York")):
            with self.assertRaises(RiskError) as cm:
                gold_session_open(when)
            self.assertIn("America/New_York", str(cm.exception))
            self.assertIn("tzdata", str(cm.exception))
            self.assertTrue(MarketHours("always").is_open(when))
            fake = FakeMT5()
            session = _session(fake, _cfg(hours="gold", dry_run=False))
            with self.assertRaises(RiskError) as cm:
                session.run(max_steps=1)
            self.assertIn("tzdata", str(cm.exception))
            self.assertFalse(fake.initialized)
            self.assertEqual(fake.orders, [])
            session.broker.connect()
            with self.assertRaises(RiskError) as cm:
                session.ensure_allowed()
            self.assertIn("America/New_York", str(cm.exception))
            self.assertEqual(fake.orders, [])
            always = _session(fake, _cfg(hours="always"))
            always.broker.connect()
            always.ensure_allowed()


class Adapter(_CleanEnv):
    def test_symbol_suffix_and_exact_name(self):
        fake = FakeMT5()
        fake.symbols = {"XAUUSD.c": _symbol("XAUUSD.c")}
        broker = MT5Broker(symbol="XAUUSD", mt5=fake)
        broker.connect()
        self.assertEqual(broker.symbol, "XAUUSD.c")
        fake.symbols = {"XAUUSD": _symbol(), "XAUUSD.c": _symbol("XAUUSD.c")}
        broker = MT5Broker(symbol="XAUUSD", mt5=fake)
        broker.connect()
        self.assertEqual(broker.symbol, "XAUUSD")
        fake.symbols = {"XAUUSD.r": _symbol("XAUUSD.r")}
        broker = MT5Broker(symbol="XAUUSD", mt5=fake)
        broker.connect()
        self.assertEqual(broker.symbol, "XAUUSD.r")
        fake.symbols = {"EURUSD": _symbol("EURUSD")}
        broker = MT5Broker(symbol="XAUUSD", mt5=fake)
        with self.assertRaises(SymbolError) as cm:
            broker.connect()
        self.assertNotIn(fake.server, str(cm.exception))
        self.assertNotIn(str(fake.login), str(cm.exception))

    def test_initialize_hides_credentials_and_attaches_without_them(self):
        fake = FakeMT5()
        os.environ.update({"MT5_LOGIN": "55667788", "MT5_PASSWORD": "s3cret-password", "MT5_SERVER": "RoboForex-Demo"})
        fake.fail_init = True
        broker = MT5Broker(mt5=fake)
        with self.assertRaises(ConnectionLost) as cm:
            broker.connect()
        self.assertNotIn("s3cret-password", str(cm.exception))
        self.assertNotIn("55667788", str(cm.exception))
        self.assertNotIn("RoboForex-Demo", str(cm.exception))
        self.assertEqual(fake.init_kwargs["password"], "s3cret-password")
        own = {k: v for k, v in broker.__dict__.items() if k != "_mod"}
        self.assertNotIn("s3cret-password", repr(own))
        fake.fail_init = False
        for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
            os.environ.pop(key, None)
        broker = MT5Broker(mt5=fake)
        broker.connect()
        self.assertEqual(fake.init_kwargs, {})
        self.assertEqual(fake.init_args, ())
        os.environ["MT5_LOGIN"] = "1"
        fresh = FakeMT5()
        with self.assertRaises(RiskError):
            MT5Broker(mt5=fresh).connect()
        self.assertFalse(fresh.initialized)

        os.environ.pop("MT5_LOGIN", None)
        def boom():
            raise ImportError("no")
        with self.assertRaises(ConnectionLost) as cm:
            MT5Broker(loader=boom).connect()
        self.assertIn("MetaTrader5", str(cm.exception))

    def test_order_request_book_pnl_and_hedging_close(self):
        fake = FakeMT5()
        broker = MT5Broker(mt5=fake, deviation=15)
        broker.connect()
        intent = build_order(1, 0.01, broker.spec(), RiskLimits(sl_points=100, tp_points=200, deviation_points=15))
        report = broker.send(intent)
        self.assertTrue(report.ok)
        req = fake.orders[-1]
        self.assertEqual(req["action"], 1)
        self.assertEqual(req["type"], 0)
        self.assertAlmostEqual(req["volume"], 0.01)
        self.assertAlmostEqual(req["price"], 2000.2)
        self.assertAlmostEqual(req["sl"], 1999.2)
        self.assertAlmostEqual(req["tp"], 2002.2)
        self.assertEqual(req["deviation"], 15)
        self.assertEqual(req["type_filling"], ORDER_FILLING_FOK)
        self.assertEqual(req["magic"], 20261027)
        self.assertEqual(req["comment"], "jev")
        self.assertNotIn("password", req)
        self.assertNotIn("login", req)
        fake.retcode = 10006
        bad = broker.send(intent)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.retcode, 10006)
        fake.retcode = 10009
        arr = np.zeros(1, dtype=[
            ("time", "i8"), ("bid", "f8"), ("ask", "f8"), ("last", "f8"),
            ("volume", "f8"), ("time_msc", "i8"), ("flags", "i8"), ("volume_real", "f8"),
        ])
        arr[0] = (10, 2000.0, 2000.2, 0.0, 3, 10_000, 32, 1.5)
        fake.batches = [arr]
        polled = broker.poll_ticks(0)
        self.assertEqual(polled[0].flags, 32)
        self.assertAlmostEqual(polled[0].volume_real, 1.5)
        fake.book_levels = [
            SimpleNamespace(type=2, price=1999.5, volume=8, volume_real=0),
            SimpleNamespace(type=2, price=2000.0, volume=0, volume_real=5),
            SimpleNamespace(type=1, price=2000.5, volume=4, volume_real=0),
            SimpleNamespace(type=1, price=2000.2, volume=1, volume_real=0),
        ]
        bid, ask, depth = book_sizes(broker.book(), 5, 1)
        self.assertTrue(depth)
        self.assertEqual((bid[0], ask[0]), (5, 1))
        fake.book_levels = []
        broker._book_ok = False
        self.assertIsNone(broker.book())
        fake.deals = [
            SimpleNamespace(symbol="XAUUSD", magic=20261027, profit=-1, commission=-2, swap=-0.5, fee=-0.25),
            SimpleNamespace(symbol="XAUUSD", magic=1, profit=-100, commission=0, swap=0, fee=0),
            SimpleNamespace(symbol="EURUSD", magic=20261027, profit=-50, commission=0, swap=0, fee=0),
        ]
        fake.positions = [_position(profit=-4), _position(magic=5, ticket=2, profit=-80)]
        when = datetime(2026, 10, 26, 16, tzinfo=UTC)
        self.assertAlmostEqual(broker.daily_pnl(when), -7.75)
        start, end = fake.deal_window
        self.assertEqual(start, datetime(2026, 10, 26, tzinfo=UTC).astimezone().replace(tzinfo=None))
        self.assertEqual(end, when.astimezone().replace(tzinfo=None))
        # A live quote two hours ahead of UTC moves the deal query onto the broker clock.
        ahead = datetime(2026, 10, 27, 22, 24, 42, tzinfo=UTC)
        fake.quote["time_msc"] = int(round((ahead.timestamp() + 7200) * 1000))
        broker.clock = lambda: ahead.timestamp()
        broker.clock_locked = False
        broker.daily_pnl(ahead)
        start, end = fake.deal_window
        self.assertAlmostEqual(start.timestamp(), datetime(2026, 10, 27, tzinfo=UTC).timestamp() + 7200, delta=1)
        self.assertAlmostEqual(end.timestamp(), ahead.timestamp() + 7200, delta=1)
        self.assertEqual(broker.server_offset, 7200)
        fake.positions = [_position(ticket=7), _position(ticket=8, magic=999, volume=1.0)]
        fake.orders.clear()
        broker.close_all()
        self.assertEqual(len(fake.orders), 1)
        self.assertEqual(fake.orders[0]["type"], 1)
        self.assertAlmostEqual(fake.orders[0]["volume"], 0.01)
        self.assertNotIn("position", fake.orders[0])
        self.assertTrue(any(p.magic == 999 for p in fake.positions))
        fake.margin_mode = 2
        fake.positions = [_position(ticket=42)]
        fake.orders.clear()
        broker.close_all()
        self.assertEqual(fake.orders[0]["position"], 42)
        self.assertEqual(fake.orders[0]["deviation"], 15)
        text = broker.describe()
        self.assertIn("mode=demo", text)
        self.assertNotIn(str(fake.login), text)
        self.assertNotIn(fake.server, text)

    def test_import_does_not_load_the_real_package(self):
        import sys
        import jev_bot.cli
        import jev_bot.live
        import jev_bot.mt5_broker
        self.assertNotIn("MetaTrader5", sys.modules)
        self.assertTrue(jev_bot.cli.cmd_live)
        self.assertTrue(resolve_symbol)


class Session(_CleanEnv):
    def _now(self):
        return datetime(2026, 10, 26, 16, tzinfo=UTC)

    def test_dry_run_uses_the_pipeline_and_sends_nothing(self):
        fake = FakeMT5()
        os.environ.update({"MT5_LOGIN": "55667788", "MT5_PASSWORD": "s3cret-password", "MT5_SERVER": "RoboForex-Demo"})
        jev = ScriptedJev([_response()])
        n = 320
        cfg = _cfg(warmup=n, step=1, history=800)
        session = _session(fake, cfg, jev)
        base = 1_700_000_000
        fake.batches = [[{
            "time": base + i, "time_msc": (base + i) * 1000, "bid": 2000 + (i % 7) * 0.01, "ask": 2000.2 + (i % 7) * 0.01,
            "last": 0.0, "volume": 1.0, "volume_real": 0.0, "flags": 32,
        }] for i in range(n)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.run(max_steps=n)
        text = buf.getvalue()
        self.assertIn("DRY-RUN BUY", text)
        self.assertIn("level-1 fallback", text)
        self.assertIn("queue imbalance", text)
        self.assertNotIn("s3cret-password", text)
        self.assertNotIn("55667788", text)
        self.assertNotIn("RoboForex-Demo", text)
        self.assertEqual(fake.orders, [])
        self.assertEqual(jev.states[0]["order_book"]["best_level"], "roughly even queues")
        self.assertTrue(all(isinstance(v, str) for v in _leaves(jev.states[0])))
        self.assertEqual(fake.init_kwargs["password"], "s3cret-password")
        own = {k: v for k, v in session.broker.__dict__.items() if k != "_mod"}
        self.assertNotIn("s3cret-password", repr(own))

    def test_demo_send_shapes_the_order(self):
        fake = FakeMT5()
        cfg = _cfg(dry_run=False, sl_points=100, tp_points=200, deviation_points=30)
        session = _ready(fake, cfg, ScriptedJev([_response()]))
        t = _prefill(session)
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(len(fake.orders), 1)
        req = fake.orders[0]
        self.assertEqual(req["symbol"], "XAUUSD")
        self.assertEqual(req["type"], 0)
        self.assertAlmostEqual(req["volume"], 0.01)
        self.assertAlmostEqual(req["price"], 2000.2)
        self.assertAlmostEqual(req["sl"], 1999.2)
        self.assertAlmostEqual(req["tp"], 2002.2)
        self.assertEqual(req["deviation"], 30)
        self.assertEqual(req["type_filling"], ORDER_FILLING_FOK)
        self.assertNotIn("DRY-RUN", buf.getvalue())
        self.assertEqual(session.position, 1)

    def test_half_size_below_the_minimum_and_a_wide_spread_are_blocked(self):
        fake = FakeMT5()
        session = _ready(fake, _cfg(dry_run=False), ScriptedJev([_response(confidence=0.5)]))
        t = _prefill(session)
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(fake.orders, [])
        self.assertIn("minimum", buf.getvalue())
        fake.quote["ask"] = 2001.0
        session = _ready(fake, _cfg(dry_run=False), ScriptedJev([_response()]))
        t = _prefill(session)
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(fake.orders, [])
        self.assertIn("spread", buf.getvalue())

    def test_max_positions_daily_loss_kill_file_and_closed_market(self):
        fake = FakeMT5()
        session = _ready(fake, _cfg(dry_run=False, max_positions=0), ScriptedJev([_response()]))
        t = _prefill(session)
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(fake.orders, [])
        self.assertIn("max open positions", buf.getvalue())

        fake = FakeMT5()
        fake.deals = [SimpleNamespace(symbol="XAUUSD", magic=20261027, profit=-30, commission=0, swap=0, fee=0)]
        fake.positions = [_position()]
        session = _ready(fake, _cfg(dry_run=False, max_daily_loss=20), ScriptedJev([_response()]))
        with redirect_stdout(io.StringIO()), self.assertRaises(TradingHalted):
            session.on_bar(_bar(4), self._now())
        self.assertEqual(fake.orders[0]["type"], 1)
        self.assertEqual(session.jev.states, [])

        fake = FakeMT5()
        fake.positions = [_position(ticket=1), _position(ticket=2, magic=999, volume=1)]
        cfg = _cfg(dry_run=False)
        session = _ready(fake, cfg, ScriptedJev([_response()]))
        Path(cfg.kill_file).write_text("stop")
        with redirect_stdout(io.StringIO()), self.assertRaises(TradingHalted):
            session.on_bar(_bar(4), self._now())
        self.assertEqual(len(fake.orders), 1)
        self.assertAlmostEqual(fake.orders[0]["volume"], 0.01)
        self.assertEqual(fake.orders[0]["comment"], "jev")
        self.assertTrue(any(p.magic == 999 for p in fake.positions))

        fake = FakeMT5()
        fake.positions = [_position()]
        jev = ScriptedJev([_response()])
        session = _ready(fake, _cfg(dry_run=False, hours="gold"), jev)
        saturday = datetime(2026, 10, 24, 15, tzinfo=UTC)
        with redirect_stdout(io.StringIO()):
            session.on_bar(_bar(4), saturday)
        self.assertEqual(fake.orders[0]["type"], 1)
        self.assertEqual(jev.states, [])
        fake.orders.clear()
        fake.positions.clear()
        with redirect_stdout(io.StringIO()):
            session.on_bar(_bar(5), saturday)
        self.assertEqual(fake.orders, [])

    def test_real_account_needs_flag_and_confirmation(self):
        fake = FakeMT5()
        fake.trade_mode = 2
        session = _session(fake, _cfg(dry_run=False))
        session.broker.connect()
        with self.assertRaises(RiskError) as cm:
            session.ensure_allowed()
        self.assertNotIn(str(fake.login), str(cm.exception))
        self.assertEqual(fake.orders, [])
        session.cfg.allow_real = True
        with self.assertRaises(RiskError):
            session.ensure_allowed()
        os.environ["MT5_CONFIRM_REAL"] = CONFIRM_PHRASE
        session.ensure_allowed()
        fake.trade_mode = 0
        fake.trade_allowed = False
        session = _session(fake, _cfg(dry_run=False))
        session.broker.connect()
        with self.assertRaises(RiskError) as cm:
            session.ensure_allowed()
        self.assertIn("Algo Trading", str(cm.exception))
        session.cfg.dry_run = True
        session.ensure_allowed()

    def test_first_poll_asks_for_recent_ticks(self):
        from datetime import timedelta
        fake = FakeMT5()
        broker = MT5Broker(mt5=fake)
        broker.connect()
        broker.poll_ticks(0)
        self.assertIsNotNone(fake.tick_from)
        self.assertGreater(fake.tick_from, datetime.now() - timedelta(minutes=1))
        quote = broker.quote()
        self.assertAlmostEqual(quote.bid, 2000.0)
        self.assertAlmostEqual(quote.ask, 2000.2)

    def test_server_clock_offset_keeps_warmup_and_decides(self):
        # The observed RoboForex quote: broker wall 00:24, PC UTC 22:24.
        frozen = datetime(2026, 10, 27, 22, 24, 42, tzinfo=UTC)
        unix = frozen.timestamp()
        offset = 7200
        fake = FakeMT5()
        fake.time_filter = True
        rows = []
        for i in range(420):
            server = unix + offset - 399 + i
            rows.append({
                "time": int(server), "time_msc": int(round(server * 1000)),
                "bid": 2000 + (i % 7) * 0.01, "ask": 2000.2 + (i % 7) * 0.01,
                "last": 0.0, "volume": 1.0, "volume_real": 0.0, "flags": 32,
            })
        fake.history = rows
        fake.quote["time_msc"] = int(round((unix + offset) * 1000))
        cfg = _cfg(warmup=320, step=1, history=800, hours="gold")
        session = _session(
            fake, cfg, ScriptedJev([_response()]),
            now=lambda: frozen, clock=lambda: unix,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.run(max_steps=2)
        text = buf.getvalue()
        self.assertIn("broker clock +7200s vs UTC", text)
        self.assertNotIn("feed gap", text)
        self.assertIn("DRY-RUN BUY", text)
        self.assertGreaterEqual(len(session.buffer), 320)
        self.assertLess(abs(session.buffer.rows[-1].t - unix), 30)
        self.assertAlmostEqual(fake.range_window[1].timestamp(), unix + offset, delta=1)

    def test_gold_session_uses_utc_not_the_broker_clock(self):
        # Sunday 20:00 UTC is before the New York open. The same instant on a
        # clock two hours ahead is already inside the session.
        closed = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
        self.assertFalse(gold_session_open(closed))
        self.assertTrue(gold_session_open(closed + timedelta(hours=2)))
        unix = closed.timestamp()
        offset = 7200
        fake = FakeMT5()
        fake.time_filter = True
        fake.history = [{
            "time": int(unix + offset - 399 + i),
            "time_msc": int(round((unix + offset - 399 + i) * 1000)),
            "bid": 2000 + (i % 5) * 0.01, "ask": 2000.2 + (i % 5) * 0.01,
            "last": 0.0, "volume": 1.0, "volume_real": 0.0, "flags": 32,
        } for i in range(400)]
        fake.quote["time_msc"] = int(round((unix + offset) * 1000))
        cfg = _cfg(warmup=320, step=1, history=800, hours="gold", dry_run=False)
        session = _session(
            fake, cfg, ScriptedJev([_response()]),
            now=lambda: closed, clock=lambda: unix,
        )
        with redirect_stdout(io.StringIO()):
            session.run(max_steps=2)
        self.assertEqual(fake.orders, [])
        self.assertEqual(session.position, 0)
        self.assertGreaterEqual(len(session.buffer), 320)
        self.assertLess(abs(session.buffer.rows[-1].t - unix), 30)

    def test_jev_error_connection_loss_and_interrupt_flatten(self):
        fake = FakeMT5()
        fake.positions = [_position()]
        session = _ready(fake, _cfg(dry_run=False), BoomJev())
        t = _prefill(session)
        with redirect_stdout(io.StringIO()), self.assertRaises(JevError):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(fake.orders[0]["type"], 1)

        fake = FakeMT5()
        fake.positions = [_position()]
        cfg = _cfg(dry_run=False)
        session = _session(fake, cfg)
        orig = session._backfill
        def drop():
            orig()
            fake.up = False
        session._backfill = drop
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(ConnectionLost):
            session.run(max_steps=1)
        self.assertEqual(fake.orders, [])
        self.assertIn("terminal already gone", buf.getvalue())

        class Clock:
            def __init__(self):
                self.t = 0.0
            def __call__(self):
                self.t += 100
                return self.t

        fake = FakeMT5()
        fake.positions = [_position()]
        session = _session(fake, _cfg(dry_run=False, max_silence=30), clock=Clock())
        with redirect_stdout(io.StringIO()), self.assertRaises(ConnectionLost):
            session.run(max_steps=3)
        self.assertEqual(fake.orders[0]["type"], 1)

        fake = FakeMT5()
        fake.positions = [_position()]
        fake.raise_keyboard = True
        session = _session(fake, _cfg(dry_run=False))
        with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            session.run(max_steps=1)
        self.assertEqual(fake.orders[0]["type"], 1)
        self.assertGreaterEqual(fake.shutdown_calls, 1)

    def test_dry_run_daily_loss_uses_contract_size(self):
        fake = FakeMT5()
        session = _ready(fake, _cfg(max_daily_loss=20), ScriptedJev([_response()]))
        t = _prefill(session)
        with redirect_stdout(io.StringIO()):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(session.position, 1)
        self.assertEqual(fake.orders, [])
        with redirect_stdout(io.StringIO()), self.assertRaises(TradingHalted):
            session.on_bar(_bar(t + 1, bid=1980.0, ask=1980.2), self._now())
        self.assertEqual(session.position, 0)
        self.assertEqual(fake.orders, [])

    def test_rejected_order_does_not_invent_a_position(self):
        fake = FakeMT5()
        fake.retcode = 10006
        session = _ready(fake, _cfg(dry_run=False), ScriptedJev([_response()]))
        t = _prefill(session)
        buf = io.StringIO()
        with redirect_stdout(buf):
            session.on_bar(_bar(t), self._now())
        self.assertEqual(session.position, 0)
        self.assertIn("rejected", buf.getvalue())
        self.assertEqual(fake.retcode, 10006)


class Cli(_CleanEnv):
    def test_live_dry_run_is_the_default_and_documented(self):
        args = build_parser().parse_args(["live", "--broker", "mt5", "--dry-run"])
        cfg = live_config_from_args(args)
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.symbol, "XAUUSD")
        self.assertFalse(cfg.allow_real)
        plain = build_parser().parse_args(["live", "--broker", "mt5"])
        self.assertTrue(live_config_from_args(plain).dry_run)
        sent = build_parser().parse_args(["live", "--broker", "mt5", "--send", "--symbol", "XAUUSD.c", "--allow-real"])
        cfg = live_config_from_args(sent)
        self.assertFalse(cfg.dry_run)
        self.assertTrue(cfg.allow_real)
        self.assertEqual(cfg.symbol, "XAUUSD.c")
        both = build_parser().parse_args(["live", "--broker", "mt5", "--dry-run", "--send"])
        with self.assertRaises(RiskError):
            live_config_from_args(both)
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            build_parser().parse_args(["live", "--help"])
        self.assertIn("--dry-run", buf.getvalue())
        self.assertIn("--broker", buf.getvalue())
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("jev-bot live --broker mt5 --dry-run", readme)
        self.assertIn("level-1 fallback", readme)
        self.assertIn("queue imbalance", readme)
        self.assertIn("RoboForex", readme)


def _leaves(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _leaves(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _leaves(value)
    else:
        yield obj


if __name__ == "__main__":
    unittest.main()
