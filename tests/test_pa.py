"""Price-action strategies: pins, inside bars, structure filter, gates."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jev_bot.cli import build_parser
from jev_bot.live import live_config_from_args
from jev_bot.risk import RiskLimits
from jev_bot.strategies import StrategyFlags, StrategyRouter, build_context
from jev_bot.strategies import _pa_common as pa
from jev_bot.strategies import pa_insidebar, pa_rejection, pa_structure
from jev_bot.strategies.minutes import MinuteBar
from jev_bot.typesafe import Answer, Response

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


def _liq(**extra) -> dict[str, float]:
    out = {"spread_wide": 0.05, "volatility_violent": 0.05}
    out.update(extra)
    return out


def _m5_series(bars: list[tuple[float, float, float, float]], *, t0: int | None = None,
               buy: float = 2.0, sell: float = 0.5) -> list[MinuteBar]:
    """One MinuteBar per M5 slot so ``closed_m5`` keeps each as its own candle."""
    if t0 is None:
        # Mid-week UTC afternoon so Asia/prev-day helpers stay defined.
        t0 = int(datetime(2026, 10, 27, 14, 0, tzinfo=UTC).timestamp())
        t0 = (t0 // 300) * 300
    out = []
    for i, (o, h, l, c) in enumerate(bars):
        out.append(MinuteBar(t0 + i * 300, o, h, l, c, buy, sell))
    return out


def _ctx_from_m5(m5: list[MinuteBar], *, mid=None, bid=None, ask=None, position=0,
                 point=0.01, max_hold=900, limits=None, pad_forming: bool = True):
    bars = list(m5)
    if pad_forming:
        last = bars[-1]
        bars.append(MinuteBar(last.t + 300, last.close, last.close, last.close, last.close, 1, 0))
    mid = mid if mid is not None else bars[-2].close
    spread = 0.02
    now = datetime.fromtimestamp(bars[-2].t + 60, tz=UTC)
    # Dummy 1 s rows so build_context can run; minutes are overwritten.
    times = [bars[-2].t + i for i in range(120)]
    mids = [mid] * len(times)
    ctx = build_context(
        now=now, bar_t=bars[-2].t + 60, mid=mid,
        bid=bid if bid is not None else mid - spread / 2,
        ask=ask if ask is not None else mid + spread / 2,
        point=point, digits=2, position=position,
        semantic={
            "price_action": {"last_5_minutes": "rising steadily"},
            "order_flow": {
                "last_1_minute": "moderate buying",
                "aggressor_tape_30s_windows_oldest_first": ["moderate buying"] * 5,
            },
            "order_book": {"spread": "two ticks, normal"},
            "volatility": "normal",
            "fair_value_gap": "price is in line with fair value",
            "forming_candle": {"so_far": "up slightly"},
            "position": "flat",
        },
        times=times, mids=mids, buy_vol=[1.0] * len(times), sell_vol=[0.0] * len(times),
        limits=limits or _limits(), max_hold_seconds=max_hold,
    )
    ctx.minutes = bars
    return ctx


def _atr_warmup(n: int = 20, base: float = 2650.0, span: float = 1.6) -> list[tuple]:
    """Quiet bars that leave ATR near ``span``."""
    out = []
    px = base
    for i in range(n):
        o = px
        h = px + span * 0.55
        l = px - span * 0.45
        c = px + (0.1 if i % 2 == 0 else -0.1)
        out.append((o, h, l, c))
        px = c
    return out


class PaCommon(unittest.TestCase):
    def test_m5_atr_fractals_zones_deterministic(self):
        warm = _atr_warmup(24, base=2650.0, span=1.5)
        # Clear fractal high/low in the middle.
        warm[10] = (2650.0, 2654.0, 2649.5, 2651.0)
        warm[16] = (2651.0, 2651.5, 2646.0, 2648.0)
        bars = _m5_series(warm)
        m5 = pa.closed_m5(bars + [MinuteBar(bars[-1].t + 300, 2650, 2650, 2650, 2650, 1, 0)])
        self.assertGreaterEqual(len(m5), 20)
        atr = pa.atr14(m5)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0.5)
        swings = pa.fractal_swings(m5)
        self.assertTrue(any(k == "high" for _, _, k in swings))
        self.assertTrue(any(k == "low" for _, _, k in swings))
        clustered = pa.cluster_levels([100.0, 100.1, 105.0], atr=2.0, tol_atr=0.25)
        self.assertEqual(len(clustered), 2)
        now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        zones = pa.build_zones(m5, now, atr, mid=2650.0)
        kinds = {z.kind for z in zones}
        self.assertIn("round", kinds)
        self.assertIn("swing", kinds)
        a = pa.atr14(m5)
        b = pa.atr14(m5)
        self.assertEqual(a, b)


class PinRejection(unittest.TestCase):
    def _pin_long_ctx(self):
        warm = _atr_warmup(18, base=2648.0, span=1.55)
        # Approach with shrinking bodies, then a pin into 2650 support.
        warm.append((2651.0, 2651.4, 2650.6, 2650.9))
        warm.append((2650.9, 2651.0, 2650.5, 2650.7))
        # Pin: long lower wick under ~2650, close back above.
        warm.append((2651.2, 2651.55, 2649.95, 2651.35))
        # Trigger bar breaks pin high + 5 pts.
        warm.append((2651.4, 2651.70, 2651.2, 2651.60))
        bars = _m5_series(warm)
        mid = 2651.62
        return _ctx_from_m5(bars, mid=mid, bid=mid - 0.01, ask=mid + 0.01)

    def test_detects_pin_and_truth_table(self):
        ctx = self._pin_long_ctx()
        pin = pa_rejection.find_setup(ctx)
        self.assertIsNotNone(pin)
        self.assertEqual(pin.side, 1)
        yes = _response(_liq(
            pa_rej_r1_long=0.9, pa_rej_r2_long=0.85, pa_rej_r3_long=0.8, pa_rej_r4_long=0.1,
        ))
        sig = pa_rejection.decide(ctx, yes)
        self.assertEqual(sig.action, "buy", sig.reason)
        self.assertEqual(sig.strategy, "pa_rejection")
        self.assertIn("setup_id", sig.features)
        self.assertGreaterEqual(sig.sl_points or 0, 120)
        self.assertLessEqual(sig.sl_points or 999, 200)
        self.assertLessEqual(sig.tp_points or 999, 400)

        mid = pa_rejection.decide(ctx, _response(_liq(
            pa_rej_r1_long=0.5, pa_rej_r2_long=0.85, pa_rej_r3_long=0.8, pa_rej_r4_long=0.1,
        )))
        self.assertEqual(mid.action, "hold")
        self.assertIn("unsettled", mid.reason)

        missing = pa_rejection.decide(ctx, _response(_liq(
            pa_rej_r2_long=0.85, pa_rej_r3_long=0.8, pa_rej_r4_long=0.1,
        )))
        self.assertEqual(missing.action, "hold")
        self.assertIn("missing", missing.reason)

        veto = pa_rejection.decide(ctx, _response(_liq(
            pa_rej_r1_long=0.9, pa_rej_r2_long=0.85, pa_rej_r3_long=0.8, pa_rej_r4_long=0.9,
        )))
        self.assertEqual(veto.action, "hold")
        self.assertIn("spent", veto.reason)

        err = pa_rejection.decide(ctx, Response("mock", {}))
        self.assertEqual(err.action, "hold")

    def test_sl_window_and_cost_gate(self):
        ctx = self._pin_long_ctx()
        # Huge ATR forces SL outside 120-200.
        pin = pa_rejection.find_setup(ctx)
        self.assertIsNotNone(pin)
        # Monkey with a far ask so |entry-extreme| blows the window.
        ctx.ask = pin.pin_low + 5.0
        ctx.bid = ctx.ask - 0.02
        ctx.mid = (ctx.ask + ctx.bid) / 2
        sig = pa_rejection.decide(ctx, _response(_liq(
            pa_rej_r1_long=0.95, pa_rej_r2_long=0.95, pa_rej_r3_long=0.95, pa_rej_r4_long=0.05,
        )))
        self.assertEqual(sig.action, "hold")
        self.assertTrue("sl window" in sig.reason or "cost" in sig.reason)


class InsideBar(unittest.TestCase):
    def _inside_long_ctx(self):
        warm = _atr_warmup(18, base=2645.0, span=1.7)
        # Mother: span >= 2.0 (200 pts), body ~70% so body-extreme SL fits 120-200.
        warm.append((2648.5, 2650.05, 2648.0, 2650.0))  # span 2.05, body 1.5 → ~73%
        # Inside: fully contained, quiet.
        warm.append((2649.2, 2649.6, 2649.0, 2649.3))
        # Breakout through mother high + 5 pts.
        warm.append((2649.5, 2650.20, 2649.4, 2650.15))
        bars = _m5_series(warm)
        mid = 2650.16
        return _ctx_from_m5(bars, mid=mid, bid=mid - 0.01, ask=mid + 0.01)

    def test_detects_inside_and_truth_table(self):
        ctx = self._inside_long_ctx()
        setup = pa_insidebar.find_setup(ctx)
        self.assertIsNotNone(setup)
        self.assertEqual(setup.side, 1)
        yes = _response(_liq(
            pa_ib_i1_long=0.9, pa_ib_i2_long=0.85, pa_ib_i3_long=0.8, pa_ib_i4_long=0.1,
        ))
        sig = pa_insidebar.decide(ctx, yes)
        self.assertEqual(sig.action, "buy", sig.reason)
        self.assertGreaterEqual(sig.sl_points or 0, 120)
        self.assertLessEqual(sig.sl_points or 999, 200)

        no = pa_insidebar.decide(ctx, _response(_liq(
            pa_ib_i1_long=0.2, pa_ib_i2_long=0.85, pa_ib_i3_long=0.8, pa_ib_i4_long=0.1,
        )))
        self.assertEqual(no.action, "hold")

        overhead = pa_insidebar.decide(ctx, _response(_liq(
            pa_ib_i1_long=0.9, pa_ib_i2_long=0.85, pa_ib_i3_long=0.8, pa_ib_i4_long=0.9,
        )))
        self.assertEqual(overhead.action, "hold")
        self.assertIn("veto", overhead.reason)


class StructureFilter(unittest.TestCase):
    def _trend_up_ctx(self):
        # Rising swing highs and lows.
        bars = []
        base = 2600.0
        for i in range(30):
            o = base + i * 0.4
            # Alternate local extremes so 2/2 fractals print higher highs/lows.
            if i % 5 == 2:
                h, l, c = o + 2.0, o - 0.3, o + 1.0
            elif i % 5 == 4:
                h, l, c = o + 0.3, o - 2.0, o - 0.5
            else:
                h, l, c = o + 0.5, o - 0.5, o + 0.1
            bars.append((o, h, l, c))
        m5 = _m5_series(bars)
        return _ctx_from_m5(m5, mid=bars[-1][3])

    def test_blocks_counter_trend_pa(self):
        ctx = self._trend_up_ctx()
        self.assertTrue(pa_structure.precondition(ctx))
        long_ok = _response({
            pa_structure.S1: 0.9, pa_structure.S2: 0.1,
        })
        side, feat = pa_structure.allowed_side(ctx, long_ok)
        self.assertEqual(side, 1)
        self.assertEqual(feat["table"], "long_only")

        from jev_bot.strategies.common import Signal
        buy = Signal("buy", "x", "pa_rejection", size=1.0, features={"setup_id": "a"})
        sell = Signal("sell", "x", "pa_insidebar", size=1.0, features={"setup_id": "b"})
        self.assertEqual(pa_structure.filter_signal(ctx, buy, long_ok).action, "buy")
        self.assertEqual(pa_structure.filter_signal(ctx, sell, long_ok).action, "hold")
        # Other strategies are untouched.
        cons = Signal("sell", "x", "consensus", size=1.0)
        self.assertEqual(pa_structure.filter_signal(ctx, cons, long_ok).action, "sell")
        # Ambiguous → both blocked.
        amb = _response({pa_structure.S1: 0.5, pa_structure.S2: 0.5})
        self.assertEqual(pa_structure.filter_signal(ctx, buy, amb).action, "hold")


class RouterPa(unittest.TestCase):
    def test_conflict_debounce_and_flags(self):
        # Build a pin-long context and force another strategy to sell.
        warm = _atr_warmup(18, base=2648.0, span=1.55)
        warm.append((2651.0, 2651.4, 2650.6, 2650.9))
        warm.append((2650.9, 2651.0, 2650.5, 2650.7))
        warm.append((2651.2, 2651.55, 2649.95, 2651.35))
        warm.append((2651.4, 2651.70, 2651.2, 2651.60))
        ctx = _ctx_from_m5(_m5_series(warm), mid=2651.62, bid=2651.61, ask=2651.63)

        router = StrategyRouter(StrategyFlags(
            consensus=False, pa_rejection=True, pa_insidebar=False, pa_structure=False,
        ))
        self.assertTrue(pa_rejection.precondition(ctx))
        qs = router.collect_questions(ctx)
        self.assertTrue(any(k.startswith("pa_rej_r1_long") for k in qs))
        self.assertIn("spread_wide", qs)

        answers = {k: 0.05 for k in qs}
        answers.update(_liq(
            pa_rej_r1_long=0.95, pa_rej_r2_long=0.95, pa_rej_r3_long=0.95, pa_rej_r4_long=0.05,
        ))
        d = router.decide(ctx, _response(answers))
        self.assertEqual(d.action, "buy", d.reason)
        setup_id = d.features["setup_id"]
        router.note_fill(d)
        self.assertIn(setup_id, router.spent_setup_ids)
        # Same setup cannot fire again.
        self.assertIsNone(pa_rejection.find_setup(ctx, spent=router.spent_setup_ids))

        # Conflict: rejection buy + synthetic sell from a second flag.
        router2 = StrategyRouter(StrategyFlags(
            consensus=False, pa_rejection=True, pa_insidebar=True, pa_structure=False,
        ))
        # Pretend insidebar also buys/sells by patching decide.
        buy = pa_rejection.decide(ctx, _response(_liq(
            pa_rej_r1_long=0.95, pa_rej_r2_long=0.95, pa_rej_r3_long=0.95, pa_rej_r4_long=0.05,
        )))
        from jev_bot.strategies.common import Signal
        sell = Signal("sell", "forced", "pa_insidebar", size=1.0, features={"setup_id": "x"})
        combined = router2._combine(ctx, [buy, sell])
        self.assertEqual(combined.action, "hold")
        self.assertIn("conflict", combined.reason)


class CliPaFlags(unittest.TestCase):
    def tearDown(self):
        for k in _SECRET:
            os.environ.pop(k, None)

    def test_defaults_off_and_readme(self):
        args = build_parser().parse_args(["live", "--broker", "mt5"])
        cfg = live_config_from_args(args)
        self.assertFalse(cfg.strategies.pa_rejection)
        self.assertFalse(cfg.strategies.pa_insidebar)
        self.assertFalse(cfg.strategies.pa_structure)
        # Live send + allow-real still defaults PA off.
        sent = build_parser().parse_args([
            "live", "--broker", "mt5", "--send", "--allow-real",
        ])
        cfg_send = live_config_from_args(sent)
        self.assertFalse(cfg_send.strategies.pa_rejection)
        on = build_parser().parse_args([
            "live", "--broker", "mt5", "--dry-run", "--no-sig-consensus",
            "--sig-pa-rejection", "--sig-pa-insidebar", "--sig-pa-structure",
        ])
        cfg2 = live_config_from_args(on)
        self.assertTrue(cfg2.strategies.pa_rejection)
        self.assertTrue(cfg2.strategies.pa_insidebar)
        self.assertTrue(cfg2.strategies.pa_structure)
        # Structure alone is not enough.
        with self.assertRaises(Exception):
            live_config_from_args(build_parser().parse_args([
                "live", "--broker", "mt5", "--no-sig-consensus", "--sig-pa-structure",
            ]))
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("Price-Action-Strategien", readme)
        self.assertIn("--sig-pa-rejection", readme)
        self.assertIn("--sig-pa-insidebar", readme)
        self.assertIn("--sig-pa-structure", readme)
        self.assertIn("Engulfing", readme)
        self.assertIn("no proven edge after costs", readme.lower())


class SurrogatePa(unittest.TestCase):
    def test_answers_pa_keys(self):
        from jev_bot.surrogate import JevSurrogate
        from jev_bot.strategies.pa_rejection import questions_for_side
        jev = JevSurrogate()
        state = {
            "price_action": {"last_5_minutes": "rising steadily"},
            "order_flow": {
                "last_1_minute": "moderate buying",
                "aggressor_tape_30s_windows_oldest_first": ["moderate buying"] * 4,
            },
            "order_book": {"spread": "two ticks, normal"},
            "volatility": "normal",
            "fair_value_gap": "price is in line with fair value",
            "forming_candle": {"so_far": "up slightly"},
            "position": "flat",
        }
        qs = questions_for_side(1)
        qs.update(pa.liquidity_questions())
        res = jev.system_one(state, qs)
        self.assertGreaterEqual(res.answers["pa_rej_r1_long"].noul, 0.7)
        self.assertLessEqual(res.answers["pa_rej_r4_long"].noul, 0.3)


if __name__ == "__main__":
    unittest.main()
