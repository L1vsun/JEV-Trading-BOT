"""M1 strategy modules: truth tables, cost gates, flags, clamps, time zones."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jev_bot.cli import build_parser
from jev_bot.live import LiveConfig, live_config_from_args
from jev_bot.risk import RiskLimits
from jev_bot.strategies import StrategyFlags, StrategyRouter, build_context
from jev_bot.strategies.common import clamp_hold, clamp_points
from jev_bot.strategies.minutes import MinuteBar, closed_minutes
from jev_bot.strategies import orb_london, session_dir, vwap_fade
from jev_bot.typesafe import Answer, JevClient, Response

UTC = timezone.utc
_SECRET = ("TYPESAFE_API_KEY", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "MT5_CONFIRM_REAL")


def _noul(value: float) -> Answer:
    return Answer("noul", noul=value)


def _response(mapping: dict[str, float]) -> Response:
    return Response("mock", {k: _noul(v) for k, v in mapping.items()})


def _limits(**kw) -> RiskLimits:
    base = dict(max_spread_points=30, sl_points=200, tp_points=400, deviation_points=15, lot=0.01)
    base.update(kw)
    return RiskLimits(**base)


def _ctx(*, now, times, mids, buy=None, sell=None, bid=None, ask=None, position=0,
         limits=None, max_hold=900, point=0.01) -> object:
    buy = buy or [1.0] * len(mids)
    sell = sell or [0.0] * len(mids)
    mid = mids[-1]
    spread = 0.2
    return build_context(
        now=now, bar_t=times[-1], mid=mid,
        bid=bid if bid is not None else mid - spread / 2,
        ask=ask if ask is not None else mid + spread / 2,
        point=point, digits=2, position=position,
        semantic={
            "price_action": {"last_5_minutes": "flat"},
            "order_flow": {
                "last_1_minute": "light buying",
                "aggressor_tape_30s_windows_oldest_first": ["light buying"] * 5,
            },
            "order_book": {"spread": "two ticks, normal"},
            "volatility": "normal",
            "fair_value_gap": "price is in line with fair value",
            "forming_candle": {"so_far": "up slightly"},
            "position": "flat",
        },
        times=times, mids=mids, buy_vol=buy, sell_vol=sell,
        limits=limits or _limits(), max_hold_seconds=max_hold,
    )


class Helpers(unittest.TestCase):
    def test_clamp_only_tightens(self):
        self.assertEqual(clamp_points(500, 200), 200)
        self.assertEqual(clamp_points(100, 200), 100)
        self.assertEqual(clamp_points(100, 0), 100)
        self.assertEqual(clamp_points(0, 200), 0)
        self.assertEqual(clamp_hold(600, 900), 600)
        self.assertEqual(clamp_hold(600, 300), 300)
        self.assertEqual(clamp_hold(600, 0), 600)

    def test_closed_minutes_drop_forming(self):
        # Two full minutes plus one second of a third.
        base = (1_700_000_000 // 60) * 60
        times, mids, buy, sell = [], [], [], []
        for m in range(3):
            for s in range(60 if m < 2 else 1):
                t = base + m * 60 + s
                times.append(t)
                mids.append(2000.0 + m + s * 0.01)
                buy.append(1.0)
                sell.append(0.0)
        bars = closed_minutes(times, mids, buy, sell)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].t, base)
        self.assertAlmostEqual(bars[0].open, 2000.0)
        self.assertAlmostEqual(bars[1].close, mids[119])


class VwapFade(unittest.TestCase):
    def _session_series(self, *, z_target: float, n: int = 400):
        # Build a session around a known NY open so VWAP is defined.
        open_local = datetime(2026, 10, 27, 17, 5, tzinfo=ZoneInfo("America/New_York"))
        start = int(open_local.astimezone(UTC).timestamp())
        times = [start + i for i in range(n)]
        # Constant price near vwap, then a stretch.
        base = 2000.0
        mids = [base] * (n - 50) + [base + z_target * 2.0] * 50
        # Heavy equal volume so sigma is driven by the stretch.
        buy = [10.0] * n
        sell = [0.0] * n
        now = datetime.fromtimestamp(times[-1], tz=UTC)
        # Tight quote so the cost gate can pass on a large stretch.
        ctx = _ctx(now=now, times=times, mids=mids, buy=buy, sell=sell,
                   bid=mids[-1] - 0.01, ask=mids[-1] + 0.01,
                   limits=_limits(deviation_points=1))
        return ctx

    def test_long_short_hold_and_cost_gate(self):
        ctx = self._session_series(z_target=-5.0)
        self.assertTrue(vwap_fade.precondition(ctx))
        feat = vwap_fade.compute(ctx)
        self.assertTrue(feat["cost_ok"])
        self.assertLessEqual(feat["z"], -1.0)
        sig = vwap_fade.decide(ctx, _response({"vwap_fade_long": 0.9, "vwap_fade_short": 0.05}))
        self.assertEqual(sig.action, "buy")
        self.assertEqual(sig.strategy, "vwap_fade")
        self.assertIsNotNone(sig.sl_points)
        self.assertLessEqual(sig.sl_points, 200)
        self.assertEqual(sig.max_hold_seconds, 600)

        mid = vwap_fade.decide(ctx, _response({"vwap_fade_long": 0.5, "vwap_fade_short": 0.05}))
        self.assertEqual(mid.action, "hold")
        self.assertIn("unsettled", mid.reason)

        missing = vwap_fade.decide(ctx, _response({"vwap_fade_short": 0.05}))
        self.assertEqual(missing.action, "hold")
        self.assertIn("not read as no", missing.reason)

        ctx_up = self._session_series(z_target=5.0)
        sell = vwap_fade.decide(ctx_up, _response({"vwap_fade_long": 0.05, "vwap_fade_short": 0.9}))
        self.assertEqual(sell.action, "sell")

        # Wide spread → cost gate blocks, no Jev needed.
        wide = self._session_series(z_target=-5.0)
        wide.bid, wide.ask = wide.mid - 5.0, wide.mid + 5.0
        self.assertFalse(vwap_fade.precondition(wide))
        blocked = vwap_fade.decide(wide, _response({"vwap_fade_long": 0.99}))
        self.assertEqual(blocked.action, "hold")
        self.assertIn("cost", blocked.reason)

    def test_hold_clamped_to_global(self):
        ctx = self._session_series(z_target=-5.0)
        ctx.max_hold_seconds = 300
        sig = vwap_fade.decide(ctx, _response({"vwap_fade_long": 0.9, "vwap_fade_short": 0.05}))
        self.assertEqual(sig.max_hold_seconds, 300)


class OrbLondon(unittest.TestCase):
    def _box_and_break(self, *, side: str, london_date: datetime):
        # london_date is a UTC instant inside the trade window after 08:30 London.
        zone = ZoneInfo("Europe/London")
        open_local = london_date.astimezone(zone).replace(hour=8, minute=0, second=0, microsecond=0)
        open_utc = int(open_local.astimezone(UTC).timestamp())
        minutes = []
        # 30-minute box: high 2010, low 2000.
        for i in range(30):
            minutes.append(MinuteBar(open_utc + i * 60, 2005, 2010, 2000, 2005, 1, 0))
        # Breakout minute.
        if side == "up":
            minutes.append(MinuteBar(open_utc + 30 * 60, 2010, 2015, 2009, 2012, 2, 0))
        else:
            minutes.append(MinuteBar(open_utc + 30 * 60, 2000, 2001, 1995, 1996, 0, 2))
        now = datetime.fromtimestamp(minutes[-1].t + 30, tz=UTC)
        times = list(range(minutes[0].t, minutes[-1].t + 60))
        mids = [2005.0] * len(times)
        mids[-1] = minutes[-1].close
        ctx = _ctx(now=now, times=times, mids=mids, bid=mids[-1] - 0.1, ask=mids[-1] + 0.1)
        ctx.minutes = minutes
        return ctx

    def test_breakout_hold_dst_and_first_of_day(self):
        # BST (summer): 08:00 London = 07:00 UTC.
        summer = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
        ctx = self._box_and_break(side="up", london_date=summer)
        self.assertTrue(orb_london.precondition(ctx))
        buy = orb_london.decide(ctx, _response({"orb_break_up": 0.9, "orb_break_down": 0.05}))
        self.assertEqual(buy.action, "buy")
        self.assertLessEqual(buy.sl_points or 0, 200)
        self.assertLessEqual(buy.tp_points or 0, 400)

        hold = orb_london.decide(ctx, _response({"orb_break_up": 0.4, "orb_break_down": 0.05}))
        self.assertEqual(hold.action, "hold")

        # GMT (winter): 08:00 London = 08:00 UTC.
        winter = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
        ctx_w = self._box_and_break(side="down", london_date=winter)
        sell = orb_london.decide(ctx_w, _response({"orb_break_up": 0.05, "orb_break_down": 0.85}))
        self.assertEqual(sell.action, "sell")

        day = orb_london.compute(ctx)["day"]
        blocked = orb_london.decide(
            ctx, _response({"orb_break_up": 0.99, "orb_break_down": 0.05}),
            broken_days={day},
        )
        self.assertEqual(blocked.action, "hold")
        self.assertIn("already taken", blocked.reason)


class SessionDir(unittest.TestCase):
    def test_filters_against_opening_drive(self):
        open_local = datetime(2026, 10, 27, 17, 5, tzinfo=ZoneInfo("America/New_York"))
        start = int(open_local.astimezone(UTC).timestamp())
        minutes = [
            MinuteBar(start + i * 60, 2000, 2010, 1999, 2000 + i * 0.5, 1, 0)
            for i in range(30)
        ]
        now = datetime.fromtimestamp(start + 40 * 60, tz=UTC)
        times = list(range(start, start + 40 * 60))
        ctx = _ctx(now=now, times=times, mids=[2005.0] * len(times))
        ctx.minutes = minutes
        feat = session_dir.compute(ctx)
        self.assertIsNotNone(feat)
        self.assertEqual(feat["direction"], 1)
        from jev_bot.strategies.common import Signal
        buy = Signal("buy", "x", "consensus", size=1.0)
        sell = Signal("sell", "x", "consensus", size=1.0)
        self.assertEqual(session_dir.filter_signal(ctx, buy).action, "buy")
        self.assertEqual(session_dir.filter_signal(ctx, sell).action, "hold")


class Router(unittest.TestCase):
    def test_conflict_is_hold_and_flags_gate_questions(self):
        open_local = datetime(2026, 10, 27, 17, 5, tzinfo=ZoneInfo("America/New_York"))
        start = int(open_local.astimezone(UTC).timestamp())
        n = 400
        times = [start + i for i in range(n)]
        mids = [2000.0] * (n - 50) + [1990.0] * 50
        now = datetime.fromtimestamp(times[-1], tz=UTC)
        ctx = _ctx(now=now, times=times, mids=mids, buy=[10] * n, sell=[0] * n,
                   bid=mids[-1] - 0.01, ask=mids[-1] + 0.01, limits=_limits(deviation_points=1))
        router = StrategyRouter(StrategyFlags(consensus=True, vwap_fade=True, orb=False))
        qs = router.collect_questions(ctx)
        self.assertIn("tape_buys_up", qs)
        self.assertIn("vwap_fade_long", qs)

        # Force a buy from consensus and a sell from vwap by crafting answers.
        answers = {k: 0.05 for k in qs}
        for k in ("tape_buys_up", "trend_up", "flow_up", "candle_up", "price_below_flow"):
            answers[k] = 0.9
        answers["vwap_fade_long"] = 0.05
        answers["vwap_fade_short"] = 0.05
        # With z negative, vwap wants long when yes — make consensus sell instead.
        answers = {k: 0.05 for k in qs}
        for k in ("tape_sells_down", "trend_down", "flow_down", "candle_down", "price_above_flow"):
            answers[k] = 0.9
        answers["vwap_fade_long"] = 0.95
        answers["vwap_fade_short"] = 0.05
        d = router.decide(ctx, _response(answers))
        self.assertEqual(d.action, "hold")
        self.assertIn("conflict", d.reason)

        off = StrategyRouter(StrategyFlags(consensus=False, vwap_fade=False, orb=False))
        # live_config_from_args rejects this; router alone returns hold.
        d2 = off.decide(ctx, None)
        self.assertEqual(d2.action, "hold")


class CliFlags(unittest.TestCase):
    def tearDown(self):
        for k in _SECRET:
            os.environ.pop(k, None)

    def test_defaults_and_documentation(self):
        args = build_parser().parse_args(["live", "--broker", "mt5"])
        cfg = live_config_from_args(args)
        self.assertTrue(cfg.strategies.consensus)
        self.assertFalse(cfg.strategies.vwap_fade)
        self.assertFalse(cfg.strategies.orb)
        self.assertFalse(cfg.strategies.session_dir)
        on = build_parser().parse_args([
            "live", "--broker", "mt5", "--no-sig-consensus", "--sig-vwap-fade", "--sig-orb",
            "--filter-session-dir",
        ])
        cfg2 = live_config_from_args(on)
        self.assertFalse(cfg2.strategies.consensus)
        self.assertTrue(cfg2.strategies.vwap_fade)
        self.assertTrue(cfg2.strategies.orb)
        self.assertTrue(cfg2.strategies.session_dir)
        with self.assertRaises(Exception):
            live_config_from_args(build_parser().parse_args([
                "live", "--broker", "mt5", "--no-sig-consensus",
            ]))
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("--sig-vwap-fade", readme)
        self.assertIn("--sig-orb", readme)
        self.assertIn("no proven edge", readme.lower())


class MockedApi(unittest.TestCase):
    def tearDown(self):
        for k in _SECRET:
            os.environ.pop(k, None)

    def test_batched_request_hides_the_key(self):
        import json
        import urllib.error
        token = "unit-test-key"
        os.environ["TYPESAFE_API_KEY"] = token
        jev = JevClient(model="jev-latest", base_url="http://jev.invalid", timeout=1, max_retries=0)
        open_local = datetime(2026, 10, 27, 17, 5, tzinfo=ZoneInfo("America/New_York"))
        start = int(open_local.astimezone(UTC).timestamp())
        n = 400
        times = [start + i for i in range(n)]
        mids = [2000.0] * (n - 50) + [1990.0] * 50
        now = datetime.fromtimestamp(times[-1], tz=UTC)
        ctx = _ctx(now=now, times=times, mids=mids, buy=[10] * n, sell=[0] * n,
                   bid=mids[-1] - 0.01, ask=mids[-1] + 0.01, limits=_limits(deviation_points=1))
        router = StrategyRouter(StrategyFlags(consensus=True, vwap_fade=True))
        qs = router.collect_questions(ctx)
        captured = {}

        class _Resp:
            def __init__(self, payload):
                self._payload = payload
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        def open_url(req, timeout=0):
            body = json.loads(req.data.decode())
            captured["body"] = body
            answers = {k: {"type": "noul", "noul": 0.05} for k in body["questions"]}
            for k in ("tape_buys_up", "trend_up", "flow_up", "candle_up", "price_below_flow", "vwap_fade_long"):
                if k in answers:
                    answers[k] = {"type": "noul", "noul": 0.9}
            return _Resp(json.dumps({
                "model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }).encode())

        with patch("urllib.request.urlopen", side_effect=open_url):
            sent = router.state_for_request(ctx, qs)
            response = jev.system_one(sent, qs)
        self.assertIn("vwap_fade_long", captured["body"]["questions"])
        self.assertIn("tape_buys_up", captured["body"]["questions"])
        self.assertIn("vwap_fade", captured["body"]["state"])
        self.assertNotIn(token, json.dumps(captured["body"]))
        decision = router.decide(ctx, response)
        # Consensus buy and vwap fade long agree → buy.
        self.assertEqual(decision.action, "buy")


if __name__ == "__main__":
    unittest.main()
