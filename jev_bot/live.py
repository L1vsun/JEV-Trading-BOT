"""Live loop: a broker feed, the same decision path, always-send orders.

``features.py``, ``state.py`` and the strategy router are used as they are.
A lost terminal flattens and stops. A Jev error holds that bar and leaves the
loop running. There is no dry-run path.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .bars import DEFAULT_BACKFILL, Bar, MultiTFBooks, TF_ENUM, TF_SECONDS, candle_open
from .features import feature_matrix
from .mt5_feed import MarketBuffer, Tick, TickAggregator, depth_note
from .quant import FairValueModel
from .session_guard import (
    EntryWindow,
    allows_entry,
    is_sunday_stub,
    should_flatten_weekend,
)
from .state import StateBuilder
from .strategies import StrategyFlags, StrategyRouter, build_context
from .strategies.magics import (
    ENTRY_STRATEGIES,
    all_bot_magics,
    magic_for,
    magic_table,
    order_comment,
)
from .strategies.mtf_base import MtfLimits, default_mtf_limits
from .risk import (
    AvoidHours,
    ConnectionLost,
    KillSwitch,
    LOT_MODE_EQUITY_SCALED,
    LOT_MODE_FIXED,
    LotScaling,
    MarketHours,
    RiskError,
    RiskLimits,
    TradingHalted,
    assert_may_send,
    build_order,
    check_entry,
    format_intent,
    lot_stage_table,
    parse_avoid_hours,
    position_text,
    redact,
    require_berlin,
    require_new_york,
    scaled_lot,
    stop_exit,
)
from .typesafe import Answer, JevError, Response, payload_text

# Closed bars needed before ATR-based MTF strategies can leave hold.
_MTF_ATR_HISTORY_NEED = 30

# Strategy name → signal timeframe for the Jev candle-close cache.
# ``1s`` means every decision bar (consensus / vwap / orb / PA).
_STRATEGY_SIGNAL_TF: dict[str, str] = {
    "consensus": "1s",
    "vwap_fade": "1s",
    "orb_london": "1s",
    "pa_rejection": "1s",
    "pa_insidebar": "1s",
    "pa_structure": "1s",
    "m5_ny_orb": "M5",
    "m5_asianbox": "M5",
    "m5_vwap_pb": "M5",
    "m15_ny_orb": "M15",
    "m15_asia_squeeze": "M15",
    "m15_trend_pullback": "M15",
    "h1_trend_long": "H1",
    "h1_pullback": "H1",
    "h1_prevday_break": "H1",
    "h4_donchian": "H4",
    "h4_ema_pullback": "H4",
    "h4_zone_retest": "H4",
    "pullback_m1": "M1",
    "pullback_m5": "M5",
    "pullback_m15": "M15",
    "pullback_h1": "H1",
    "pullback_h4": "H4",
}


def signal_tf_for(strategy: str) -> str:
    """Map a strategy name to its signal TF (``M5``…``H4``, ``M1``, or ``1s``)."""
    if strategy in _STRATEGY_SIGNAL_TF:
        return _STRATEGY_SIGNAL_TF[strategy]
    if strategy.startswith("m5_"):
        return "M5"
    if strategy.startswith("m15_"):
        return "M15"
    if strategy.startswith("h1_"):
        return "H1"
    if strategy.startswith("h4_"):
        return "H4"
    if strategy.startswith("pullback_"):
        tag = strategy.split("_", 1)[-1].upper()
        if tag in TF_SECONDS:
            return tag
    return "1s"


@dataclass
class TrackedPosition:
    """Local SL/TP/hold bookkeeping for one bot magic."""

    side: int = 0
    qty: float = 0.0
    px_in: float = 0.0
    t_in: int = 0
    sl: float = 0.0
    tp: float = 0.0
    hold_seconds: int = 0
    strategy: str = ""

    def clear_stops(self) -> None:
        self.sl = 0.0
        self.tp = 0.0
        self.t_in = 0
        self.px_in = 0.0
        self.hold_seconds = 0


def describe_model(jev) -> str:
    """Header text for the model that will actually answer. Never includes a key."""
    name = getattr(jev, "model", "?")
    return f"{name} (TypeSafe Jev)"


def _shown(name: str, value: float) -> str:
    """Header text. Zero means that control is off."""
    if value <= 0:
        return f"{name} off"
    return f"{name} {value:g}"


def mark_depth(state: dict, available: bool) -> dict:
    """Record whether the terminal published a book. Queue fields are not sent.

    The decision questions do not read depth. On a level-1 book the omitted
    fields would have been placeholders, so they must not count as a signal.
    """
    if available:
        state["data_quality"] = {
            "market_depth": "available",
            "book_signals": "not a decision input",
            "note": "Queue depth is not included. The questions do not read it.",
        }
    else:
        state["data_quality"] = {
            "market_depth": "unavailable",
            "book_signals": "degraded",
            "note": (
                "No market depth. best_level, top_five_levels and micro_price are omitted "
                "from the request, so a level-1 placeholder is not a queue signal. "
                "order_book.spread is the quote. The aggressor tape, volatility, "
                "forming candle and fair value still come from bid, ask and signed tick volume."
            ),
        }
    return state


_MTF_FLAG_NAMES = (
    "m5_ny_orb", "m5_asianbox", "m5_vwap_pb",
    "m15_ny_orb", "m15_asia_squeeze", "m15_trend_pullback",
    "h1_trend_long", "h1_pullback", "h1_prevday_break",
    "h4_donchian", "h4_ema_pullback", "h4_zone_retest",
    "pullback_m1", "pullback_m5", "pullback_m15", "pullback_h1", "pullback_h4",
)


def flags_need_mtf(flags: StrategyFlags) -> bool:
    return any(getattr(flags, name, False) for name in _MTF_FLAG_NAMES)


def _any_entry_strategy(flags: StrategyFlags) -> bool:
    if flags.consensus or flags.vwap_fade or flags.orb:
        return True
    if flags.pa_rejection or flags.pa_insidebar:
        return True
    return flags_need_mtf(flags)


@dataclass
class LiveConfig:
    symbol: str = "XAUUSD"
    step: int = 10
    warmup: int = 1800
    history: int = 7200
    levels: int = 5
    fallback_size: float = 1.0
    allow_real: bool = False
    kill_file: str = "jev.KILL"
    magic: int = 20261027
    magic_offsets: dict[str, int] = field(default_factory=dict)
    no_hedge: bool = False
    max_silence: float = 30.0
    max_gap: int = 10
    hours: str = "gold"
    verbose: bool = False
    max_hold_seconds: int = 900
    avoid: AvoidHours | None = None
    limits: RiskLimits = field(default_factory=RiskLimits)
    strategies: StrategyFlags = field(default_factory=lambda: StrategyFlags(consensus=False))
    lot_scaling: LotScaling = field(default_factory=LotScaling)
    backfill: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BACKFILL))
    mtf_limits: MtfLimits = field(default_factory=default_mtf_limits)
    allow_short: bool = False
    h4_ema_allow_short: bool = False
    h1_use_sma50_200: bool = False
    hold_over_weekend: bool = False
    hold_overrides: dict[str, int] = field(default_factory=dict)


def parse_magic_offset_json(text: str | None) -> dict[str, int]:
    """Parse optional ``--magic-offset-json`` overrides onto the central table."""
    if not text:
        return {}
    import json
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise RiskError("--magic-offset-json must be a JSON object of strategy→offset")
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError) as exc:
            raise RiskError(f"invalid magic offset for {key!r}") from exc
    return out


def live_config_from_args(args) -> LiveConfig:
    lot_scaling = LotScaling(
        mode=getattr(args, "lot_mode", LOT_MODE_EQUITY_SCALED),
        base=float(getattr(args, "lot_base", 0.01)),
        start_equity=float(getattr(args, "lot_start_equity", 1000.0)),
        factor=float(getattr(args, "lot_factor", 1.5)),
    )
    lot_scaling.validate()
    lot = float(args.lot) if lot_scaling.mode == LOT_MODE_FIXED else float(lot_scaling.base)
    limits = RiskLimits(
        max_lots=args.max_lots, max_positions=args.max_positions,
        max_spread_points=args.max_spread, lot=lot, sl_points=args.sl_points, tp_points=args.tp_points,
        deviation_points=args.deviation,
    )
    limits.validate()
    hold = int(getattr(args, "max_hold_seconds", 900))
    if hold < 0:
        raise RiskError("max_hold_seconds must be >= 0")
    avoid = parse_avoid_hours(getattr(args, "avoid_hours", None))
    if args.step < 1 or args.warmup < 300:
        raise RiskError("step must be >= 1 and warmup must be at least 300 seconds")
    if args.history < args.warmup:
        raise RiskError("history must cover the warmup")
    if args.hours not in ("gold", "always"):
        raise RiskError("market hours must be 'gold' or 'always'")
    if not bool(getattr(args, "allow_real", False)):
        raise RiskError(
            "live always sends orders; pass --allow-real and set MT5_CONFIRM_REAL "
            "to the confirmation phrase"
        )

    # Start from --strategies if given, else all off (including consensus).
    base = getattr(args, "strategy_flags", None)
    if base is None:
        strategies = StrategyFlags(consensus=False)
    else:
        strategies = base

    # Individual --sig-* flags OR onto the base (consensus default is off).
    updates: dict[str, bool] = {}
    if bool(getattr(args, "sig_consensus", False)):
        updates["consensus"] = True
    if bool(getattr(args, "sig_vwap_fade", False)):
        updates["vwap_fade"] = True
    if bool(getattr(args, "sig_orb", False)):
        updates["orb"] = True
    if bool(getattr(args, "filter_session_dir", False)):
        updates["session_dir"] = True
    if bool(getattr(args, "sig_pa_rejection", False)):
        updates["pa_rejection"] = True
    if bool(getattr(args, "sig_pa_insidebar", False)):
        updates["pa_insidebar"] = True
    if bool(getattr(args, "sig_pa_structure", False)):
        updates["pa_structure"] = True
    for tf in ("m1", "m5", "m15", "h1", "h4"):
        attr = f"sig_pullback_{tf}"
        if bool(getattr(args, attr, False)):
            updates[f"pullback_{tf}"] = True
    allow_short = bool(getattr(args, "allow_short", False)) or bool(strategies.allow_short)
    if updates:
        strategies = replace(strategies, **updates)
    strategies = replace(strategies, allow_short=allow_short)

    if not _any_entry_strategy(strategies):
        raise RiskError(
            "enable at least one entry strategy via --strategies or "
            "--sig-consensus / --sig-vwap-fade / --sig-orb / "
            "--sig-pa-rejection / --sig-pa-insidebar / an MTF name"
        )

    hold_overrides = dict(getattr(args, "hold_overrides", None) or {})
    for name, secs in hold_overrides.items():
        if int(secs) < 0:
            raise RiskError(f"hold override for {name!r} must be >= 0")

    mtf = default_mtf_limits()
    min_sl = dict(mtf.min_sl_points)
    for tf, attr in (("M5", "min_sl_m5"), ("M15", "min_sl_m15"), ("H1", "min_sl_h1"), ("H4", "min_sl_h4")):
        val = getattr(args, attr, None)
        if val is not None:
            if float(val) < 0:
                raise RiskError(f"{attr} must be >= 0")
            min_sl[tf] = float(val)
    mtf = MtfLimits(
        min_sl_points=min_sl,
        k_sl=dict(mtf.k_sl),
        max_bars=dict(mtf.max_bars),
        max_cost_share=dict(mtf.max_cost_share),
        allow_short=allow_short,
        global_max_hold=hold,
        max_spread_points=float(limits.max_spread_points),
        hold_overrides=dict(hold_overrides),
    )

    backfill = getattr(args, "backfill", None) or dict(DEFAULT_BACKFILL)
    magic_offsets = parse_magic_offset_json(getattr(args, "magic_offset_json", None))

    return LiveConfig(
        symbol=args.symbol, step=args.step, warmup=args.warmup, history=args.history,
        levels=getattr(args, "levels", 5), allow_real=True,
        kill_file=args.kill_file, magic=args.magic, magic_offsets=magic_offsets,
        no_hedge=bool(getattr(args, "no_hedge", False)),
        hours=args.hours, verbose=bool(args.verbose),
        max_hold_seconds=hold, avoid=avoid, limits=limits, strategies=strategies,
        lot_scaling=lot_scaling, backfill=dict(backfill), mtf_limits=mtf,
        allow_short=allow_short,
        h4_ema_allow_short=bool(getattr(args, "h4_ema_allow_short", False)),
        h1_use_sma50_200=bool(getattr(args, "h1_sma50_200", False)),
        hold_over_weekend=bool(getattr(args, "hold_over_weekend", False)),
        hold_overrides=dict(hold_overrides),
    )


class LiveSession:
    def __init__(self, broker, jev, cfg: LiveConfig | None = None, *,
                 sleep=time.sleep, now=None, clock=None):
        self.broker = broker
        self.jev = jev
        self.cfg = cfg or LiveConfig()
        self.limits = self.cfg.limits
        self.hours = MarketHours(self.cfg.hours)
        self.kill = KillSwitch(self.cfg.kill_file)
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.clock = clock or time.time
        self.agg = TickAggregator(self.cfg.levels, self.cfg.fallback_size, self.cfg.max_gap)
        self.buffer = MarketBuffer(self.cfg.history, max_gap=self.cfg.max_gap)
        self.router = StrategyRouter(
            flags=self.cfg.strategies,
            base_magic=self.cfg.magic,
            magic_overrides=dict(self.cfg.magic_offsets),
        )
        self.mtf_books: MultiTFBooks | None = None
        if flags_need_mtf(self.cfg.strategies):
            self.mtf_books = MultiTFBooks()
        # Per-magic bookkeeping. Foreign magics are never written here.
        self.by_magic: dict[int, TrackedPosition] = {}
        self.magic_strategy: dict[int, str] = {}
        for name, offset in magic_table(self.cfg.magic_offsets).items():
            m = int(self.cfg.magic) + int(offset)
            self.magic_strategy[m] = name
            self.by_magic[m] = TrackedPosition(strategy=name)
        # Ensure broker knows the full bot magic family.
        bot_magics = all_bot_magics(self.cfg.magic, self.cfg.magic_offsets)
        if hasattr(self.broker, "magics"):
            self.broker.magics = set(bot_magics)
            self.broker.magic = int(self.cfg.magic)
        self.last_msc = 0
        self._contract = 0.0
        self._tick_value = 1.0
        self._tick_size = 0.01
        self._depth_logged = False
        self._warm_logged = False
        self._answered = False
        self._last_tick_wall: float | None = None
        self._session_lot: float | None = None
        self._lot_day = None
        self._lot_equity: float | None = None
        self.entry_window = EntryWindow()
        # Jev candle-close cache: (strategy, candle_open_t) → Response (answer subset).
        self._jev_cache: dict[tuple[str, int], Response] = {}
        self._mtf_hist_warned: set[str] = set()
        self._swap_logged = False

    def magic_of(self, strategy: str) -> int:
        return magic_for(strategy, self.cfg.magic, self.cfg.magic_offsets or None)

    def _tracked(self, magic: int) -> TrackedPosition:
        pos = self.by_magic.get(magic)
        if pos is None:
            pos = TrackedPosition(strategy=self.magic_strategy.get(magic, ""))
            self.by_magic[magic] = pos
        return pos

    def _positions_by_strategy(self) -> dict[str, int]:
        """Side map keyed by strategy name for the router."""
        out: dict[str, int] = {}
        for magic, tracked in self.by_magic.items():
            name = tracked.strategy or self.magic_strategy.get(magic, "")
            if name:
                out[name] = int(tracked.side)
        return out

    def _open_count(self) -> int:
        return sum(1 for p in self.by_magic.values() if p.side)

    def _any_open(self) -> bool:
        return self._open_count() > 0

    def _hedge_blocked(self, side: int) -> bool:
        if not self.cfg.no_hedge:
            return False
        for pos in self.by_magic.values():
            if pos.side and pos.side != side:
                return True
        return False

    def _primary_tracked(self) -> TrackedPosition:
        """Prefer consensus, else first open, else a flat placeholder for state text."""
        cons = self.magic_of("consensus") if "consensus" in magic_table(self.cfg.magic_offsets) else None
        if cons is not None and cons in self.by_magic:
            return self.by_magic[cons]
        for pos in self.by_magic.values():
            if pos.side:
                return pos
        return TrackedPosition()

    def ensure_allowed(self) -> None:
        if self.cfg.hours == "gold":
            require_new_york()
        if self.cfg.avoid is not None:
            require_berlin()
        self.limits.validate()
        self.cfg.lot_scaling.validate()
        if self.cfg.max_hold_seconds < 0:
            raise RiskError("max_hold_seconds must be >= 0")
        if self.cfg.step < 1 or self.cfg.warmup < 300 or self.cfg.history < self.cfg.warmup:
            raise RiskError("step must be >= 1, warmup at least 300 seconds, and history must cover it")
        if not 1 <= self.cfg.levels <= 10:
            raise RiskError("levels must be between 1 and 10")
        assert_may_send(
            self.broker.trade_mode(), dry_run=False, allow_real=True,
            confirm=os.environ.get("MT5_CONFIRM_REAL"),
        )
        from .mt5_broker import require_algo_trading
        require_algo_trading(self.broker)
        spec = self.broker.spec()
        self._refresh_lot(force=True)
        if self.effective_lot() < spec.volume_min - 1e-12:
            raise RiskError("lot is below the broker minimum")
        self.buffer.tick = spec.tick_size or spec.point or 0.01
        self._contract = spec.contract_size or 1.0
        self._tick_size = float(getattr(spec, "tick_size", 0) or spec.point or 0.01)
        self._tick_value = float(getattr(spec, "tick_value", 0) or self._tick_size)

    def effective_lot(self) -> float:
        if self._session_lot is not None:
            return float(self._session_lot)
        return float(self.limits.lot)

    def _refresh_lot(self, *, force: bool = False) -> None:
        """Recompute equity-scaled lot once per UTC day, never mid-position."""
        if self._any_open():
            return
        day = self.now().astimezone(timezone.utc).date()
        if not force and self._lot_day == day and self._session_lot is not None:
            return
        scaling = self.cfg.lot_scaling
        spec = self.broker.spec()
        if scaling.mode == LOT_MODE_FIXED:
            self._session_lot = float(self.limits.lot)
            self._lot_equity = None
        else:
            equity = None
            eq_fn = getattr(self.broker, "equity", None)
            if callable(eq_fn):
                try:
                    equity = eq_fn()
                except Exception:
                    equity = None
            self._lot_equity = float(equity) if equity is not None else None
            self._session_lot = scaled_lot(
                equity,
                base=scaling.base,
                start=scaling.start_equity,
                factor=scaling.factor,
                volume_min=float(spec.volume_min),
                volume_step=float(spec.volume_step),
                max_lots=float(self.limits.max_lots),
            )
        self._lot_day = day
        # Keep RiskLimits.lot in sync so strategies reading limits.lot see the stage.
        self.limits = replace(self.limits, lot=float(self._session_lot))

    def run(self, max_steps: int | None = None) -> None:
        try:
            if self.cfg.hours == "gold":
                require_new_york()
            if self.cfg.avoid is not None:
                require_berlin()
            if not getattr(self.broker, "connected", False):
                self.broker.connect()
            self.ensure_allowed()
            self.broker.ensure_clock(self.clock())
            self._announce()
            self._smoke_jev()
            self._backfill()
            self._backfill_mtf()
            steps = 0
            while max_steps is None or steps < max_steps:
                self._one_poll()
                steps += 1
                if max_steps is None:
                    self.sleep(self._wait())
        except KeyboardInterrupt:
            self.flatten("interrupt")
            raise
        except ConnectionLost:
            self.flatten("connection lost")
            raise
        finally:
            self.broker.shutdown()

    def _smoke_jev(self) -> None:
        """Abort before any order when Jev is unreachable at startup.

        During the running loop, Jev errors still hold that bar (existing behavior).
        """
        if not (os.environ.get("TYPESAFE_API_KEY") or "").strip():
            raise RiskError(
                "TYPESAFE_API_KEY is required at live start; aborting with no orders"
            )
        try:
            models_fn = getattr(self.jev, "models", None)
            if not callable(models_fn):
                raise JevError("Jev client has no models() smoke probe")
            listed = models_fn()
        except JevError as exc:
            raise RiskError(
                f"Jev unreachable at startup; aborting with no orders: {exc}"
            ) from exc
        except Exception as exc:
            raise RiskError(
                f"Jev unreachable at startup; aborting with no orders: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        n = len(listed) if listed is not None else 0
        self._log(f"jev smoke ok · {n} model(s) reachable")

    def _announce(self) -> None:
        spec = self.broker.spec()
        avoid = "off" if self.cfg.avoid is None else f"{self.cfg.avoid.text} Europe/Berlin"
        self._log(
            f"live · mt5 · {self.broker.symbol} · SEND · model {describe_model(self.jev)} · "
            f"{self.broker.describe()}"
        )
        names = self.router.enabled_names()
        mtf_note = (
            f" · allow_short={'on' if self.cfg.allow_short else 'off'}"
            f" · h4_ema_allow_short={'on' if self.cfg.h4_ema_allow_short else 'off'}"
            f" · h1_sma50_200={'on' if self.cfg.h1_use_sma50_200 else 'off'}"
        )
        hold_ow = "on" if self.cfg.hold_over_weekend else "off"
        self._log(
            f"limits · lot {self.effective_lot():g} · max_lots {self.limits.max_lots:g} · "
            f"max_positions {self.limits.max_positions} · no_hedge={'on' if self.cfg.no_hedge else 'off'} · "
            f"max_spread {self.limits.max_spread_points:g} · {_shown('sl_points', self.limits.sl_points)} · "
            f"{_shown('tp_points', self.limits.tp_points)} · deviation {self.limits.deviation_points} · "
            f"{_shown('max_hold_seconds', self.cfg.max_hold_seconds)} · avoid_hours {avoid} · "
            f"hours {self.cfg.hours} · hold_over_weekend {hold_ow} · "
            f"step {self.cfg.step}s · warmup {self.cfg.warmup}s · "
            f"contract {spec.contract_size:g} · point {spec.point:g} · magic_base {self.cfg.magic}"
        )
        self._log(f"strategies · {', '.join(names) if names else '(none)'}{mtf_note}")
        entry = [n for n in names if n in ENTRY_STRATEGIES]
        if entry:
            parts = [f"{n}={self.magic_of(n)}" for n in entry]
            self._log(f"magics · {', '.join(parts)}")
        if self.cfg.hold_overrides:
            parts = [f"{k}={v}s" for k, v in sorted(self.cfg.hold_overrides.items())]
            self._log(f"hold_overrides · {', '.join(parts)}")
        self._log_lot_table()
        self._log_swap()
        if self.broker.trade_mode() == "real":
            self._log("REAL MONEY: orders will be sent on this account")
        else:
            self._log(
                f"DEMO/CONTEST: orders will still be sent (--allow-real + confirm required)"
            )
        self._log(self.broker.clock_text())

    def _log_swap(self) -> None:
        """Dashboard-compatible swap line when the broker exposes the fields."""
        swap_fn = getattr(self.broker, "swap_rates", None)
        if not callable(swap_fn):
            return
        try:
            rates = swap_fn()
        except Exception:
            return
        if rates is None:
            return
        try:
            long_s, short_s = float(rates[0]), float(rates[1])
        except (TypeError, ValueError, IndexError):
            return
        self._swap_logged = True
        self._log(f"swap · long={long_s:g} short={short_s:g}")

    def _log_lot_table(self) -> None:
        scaling = self.cfg.lot_scaling
        if scaling.mode == LOT_MODE_FIXED:
            self._log(f"lot · mode fixed · lot {self.effective_lot():g} (--lot)")
            return
        from .risk import equity_lot_stage
        spec = self.broker.spec()
        rows = lot_stage_table(
            max_equity=20_000.0,
            base=scaling.base,
            start=scaling.start_equity,
            factor=scaling.factor,
            volume_min=float(spec.volume_min),
            volume_step=float(spec.volume_step),
            max_lots=float(self.limits.max_lots),
        )
        parts = [
            f"n={r['stage']} equity>={r['equity_from']:g} raw={r['raw_lot']:g} lot={r['lot']:g}"
            for r in rows
        ]
        eq = "n/a" if self._lot_equity is None else f"{self._lot_equity:g}"
        if self._lot_equity is None:
            stage = 0
        else:
            stage = equity_lot_stage(
                self._lot_equity, scaling.base, scaling.start_equity, scaling.factor,
            )
        self._log(
            f"lot · mode equity-scaled · equity {eq} · stage {stage} · "
            f"current {self.effective_lot():g} · --lot ignored · "
            f"table: {' · '.join(parts)}"
        )

    def _backfill(self) -> None:
        ticks = self.broker.history_ticks(self.cfg.history, now_unix=self.clock())
        bars = self.agg.bars(ticks, None) if ticks else []
        for bar in bars:
            self.buffer.append(bar)
            self._feed_mtf_bar(bar)
        if ticks:
            self.last_msc = max(t.time_msc for t in ticks)
            self._last_tick_wall = self.clock()
            self._log(f"backfill {len(self.buffer)}s from terminal tick history (no depth in history)")
        else:
            self._log(f"no tick history; collecting {self.cfg.warmup}s before the first decision")

    def _backfill_mtf(self) -> None:
        if self.mtf_books is None:
            return
        copy_fn = getattr(self.broker, "copy_rates_from_pos", None)
        if not callable(copy_fn):
            self._log("mtf · broker has no copy_rates_from_pos; higher-TF books stay empty until ticks fill them")
            return
        for tf, count in self.cfg.backfill.items():
            tf_enum = TF_ENUM.get(tf)
            if tf_enum is None:
                continue
            try:
                # start_pos=1 skips the still-forming candle (pos 0).
                rows = copy_fn(self.broker.symbol, tf_enum, 1, int(count))
            except Exception as exc:
                self._log(f"mtf · {tf} backfill failed: {redact(str(exc))}")
                continue
            if not rows:
                self._log(f"mtf · {tf} backfill empty")
                self._warn_mtf_history(tf, 0)
                continue
            builder = self.mtf_books.get(tf)
            builder.seed_from_rates(rows)
            self._log(f"mtf · {tf} backfill {len(builder.bars)} closed bars")
            self._warn_mtf_history(tf, len(builder.bars))

    def _warn_mtf_history(self, tf: str, n: int) -> None:
        if n >= _MTF_ATR_HISTORY_NEED:
            return
        if tf in self._mtf_hist_warned:
            return
        self._mtf_hist_warned.add(tf)
        self._log(f"mtf · {tf} insufficient history ({n}); strategies will hold")

    def _feed_mtf_bar(self, bar) -> None:
        if self.mtf_books is None:
            return
        # One-second book mid as a tick into higher-TF builders.
        closed = self.mtf_books.on_tick(int(bar.t), float(bar.bid), float(bar.ask))
        for tf, closed_bar in closed.items():
            self._reconcile_mtf_close(tf, closed_bar)
            tf_sec = TF_SECONDS.get(tf, 0)
            self.entry_window.note_closed_bar(tf, int(closed_bar.t), int(tf_sec))

    def _reconcile_mtf_close(self, tf: str, local_bar: Bar) -> None:
        """On each higher-TF close, pull MT5's closed bar (pos 1) and let MT5 win."""
        del local_bar  # identity comes from MT5; local only triggers the reconcile
        if self.mtf_books is None:
            return
        copy_fn = getattr(self.broker, "copy_rates_from_pos", None)
        tf_enum = TF_ENUM.get(tf)
        if not callable(copy_fn) or tf_enum is None:
            return
        try:
            rows = copy_fn(self.broker.symbol, tf_enum, 1, 1)
        except Exception as exc:
            self._log(f"mtf · {tf} reconcile failed: {redact(str(exc))}")
            return
        if not rows:
            return
        row = rows[0]
        mt5_bar = Bar(
            int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
            tick_vol=float(row[5]) if len(row) > 5 else 0.0,
            spread_mean=float(row[6]) if len(row) > 6 else 0.0,
            spread_max=float(row[6]) if len(row) > 6 else 0.0,
        )
        builder = self.mtf_books.get(tf)
        before = builder.mismatches
        builder.reconcile(mt5_bar)
        if builder.mismatches > before:
            self._log(f"mtf · {tf} reconcile mismatch · total={builder.mismatches}")
        self._warn_mtf_history(tf, len(builder.closed()))

    def _mtf_closed(self) -> dict | None:
        if self.mtf_books is None:
            return None
        return {tf: builder.closed() for tf, builder in self.mtf_books.books.items()}

    def _signal_candle_t(self, tf: str, bar_t: int) -> int | None:
        """Open time of the latest closed signal candle for ``tf``, or ``None``."""
        if tf == "1s":
            return int(bar_t)
        if self.mtf_books is not None and tf in self.mtf_books.books:
            closed = self.mtf_books.get(tf).closed()
            if closed:
                last = closed[-1]
                tf_sec = TF_SECONDS.get(tf, 0)
                if tf_sec and is_sunday_stub(int(last.t), int(tf_sec)):
                    return None
                return int(last.t)
        sec = TF_SECONDS.get(tf)
        if not sec:
            return int(bar_t)
        open_t = candle_open(int(bar_t), int(sec))
        # Forming candle: last closed opens one TF earlier.
        if int(bar_t) < open_t + sec:
            closed_open = open_t - sec
            return int(closed_open) if closed_open >= 0 else None
        return int(open_t)

    def _enabled_entry_names(self) -> list[str]:
        return [n for n in self.router.enabled_names() if n in ENTRY_STRATEGIES or n == "pa_structure"]

    def _jev_cache_plan(self, bar_t: int) -> tuple[set[str] | None, dict[str, Response]]:
        """Return (strategies to ask, cached responses to reuse).

        ``ask`` is ``None`` when every enabled strategy should be considered
        (no candle filter). Otherwise it is the set that needs a fresh Jev call.
        """
        ask: set[str] = set()
        cached: dict[str, Response] = {}
        for name in self._enabled_entry_names():
            tf = signal_tf_for(name)
            candle_t = self._signal_candle_t(tf, int(bar_t))
            if candle_t is None:
                continue
            key = (name, int(candle_t))
            hit = self._jev_cache.get(key)
            if hit is not None:
                self._log(f"jev cache hit · {name} · candle={candle_t}")
                cached[name] = hit
            else:
                ask.add(name)
        return ask, cached

    def _jev_cache_store(self, ask_names: set[str], bar_t: int, response: Response) -> None:
        if response is None:
            return
        for name in ask_names:
            tf = signal_tf_for(name)
            candle_t = self._signal_candle_t(tf, int(bar_t))
            if candle_t is None:
                continue
            # Store a copy keyed by this strategy's candle; answers may be shared
            # across strategies in one call, which is fine for reuse.
            self._jev_cache[(name, int(candle_t))] = Response(
                model=response.model,
                answers=dict(response.answers),
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms,
                cached=True,
            )
            # Keep only the latest candle per strategy (1s keys change every bar).
            stale = [k for k in self._jev_cache if k[0] == name and k[1] != int(candle_t)]
            for key in stale:
                del self._jev_cache[key]

    def _jev_merge_response(
        self, fresh: Response | None, cached: dict[str, Response],
    ) -> Response | None:
        if not cached and fresh is None:
            return None
        answers: dict[str, Answer] = {}
        model = "?"
        tokens_in = 0
        tokens_out = 0
        latency = 0.0
        for resp in cached.values():
            answers.update(resp.answers)
            model = resp.model or model
        if fresh is not None:
            answers.update(fresh.answers)
            model = fresh.model or model
            tokens_in = fresh.input_tokens
            tokens_out = fresh.output_tokens
            latency = fresh.latency_ms
        return Response(
            model=model, answers=answers,
            input_tokens=tokens_in, output_tokens=tokens_out, latency_ms=latency,
            cached=fresh is None and bool(cached),
        )

    def _one_poll(self) -> None:
        self._check_kill()
        if not self.broker.healthy():
            raise ConnectionLost("terminal disconnected")
        now = self.now()
        ticks = self.broker.poll_ticks(self.last_msc, now_unix=self.clock())
        book = self.broker.book()
        if not self._depth_logged:
            self._depth_logged = True
            both = bool(book) and any(lv.side == "bid" for lv in book) and any(lv.side == "ask" for lv in book)
            self._log(depth_note(available=both))
        if ticks:
            self.last_msc = max(self.last_msc, max(t.time_msc for t in ticks))
            self._last_tick_wall = self.clock()
            bars = self.agg.bars(ticks, book)
        elif self.hours.is_open(now) and self._silent_for() > self.cfg.max_silence:
            raise ConnectionLost("no ticks")
        elif self.hours.is_open(now):
            bars = self._quiet_bars(book, now)
        else:
            bars = []
        for bar in bars:
            self.on_bar(bar, now)

    def _quiet_bars(self, book, now: datetime):
        """One second of the current quote when no new tick arrived. Volume stays zero.

        The stamp is UTC. A synthetic second never clears the warmup: a quote
        that is still on the broker clock, or a poll that lands far from the
        last bar, is skipped instead of looking like a weekend gap.
        """
        q = self.broker.quote()
        if q is None or q.bid <= 0 or q.ask <= 0:
            raise ConnectionLost("symbol info unavailable")
        wall = int(now.timestamp() * 1000)
        stamp = wall
        if q.time_msc and abs(q.time_msc - wall) <= self.cfg.max_gap * 1000:
            stamp = q.time_msc
        if self.buffer.rows:
            last = self.buffer.rows[-1].t
            sec = stamp // 1000
            if sec <= last:
                stamp = (last + 1) * 1000
            elif sec > last + self.cfg.max_gap:
                return []
        return self.agg.bars([Tick(stamp, q.bid, q.ask)], book)

    def _silent_for(self) -> float:
        now = self.clock()
        if self._last_tick_wall is None:
            self._last_tick_wall = now
            return 0.0
        return now - self._last_tick_wall

    def _check_kill(self) -> None:
        if self.kill.tripped():
            self.flatten("kill switch")
            raise TradingHalted("kill switch")

    def on_bar(self, bar, now: datetime):
        self._check_kill()
        if self.buffer.rows:
            gap = int(bar.t) - int(self.buffer.rows[-1].t) - 1
            if gap > 0:
                self.entry_window.note_feed_gap(float(gap), float(bar.t))
        self._feed_mtf_bar(bar)
        # Post-gap recovery for 1s / M1 when higher-TF books are off.
        self.entry_window.note_closed_bar("1s", int(bar.t), 1)
        if int(bar.t) % 60 == 0 and int(bar.t) >= 60:
            self.entry_window.note_closed_bar("M1", int(bar.t) - 60, 60)
        if self.buffer.append(bar):
            self._warm_logged = False
            self._log("feed gap; warmup started again")
        if should_flatten_weekend(now, self.cfg.hold_over_weekend):
            if self._has_position():
                self.flatten("weekend flatten")
            return None
        if not self.hours.is_open(now):
            if self._has_position():
                self.flatten("market closed")
            return None
        if len(self.buffer) < self.cfg.warmup:
            return None
        if not self._warm_logged:
            self._warm_logged = True
            self._log(f"warmup done · {len(self.buffer)}s in the window")
        if self._protect(bar):
            return None
        if bar.t % self.cfg.step != 0:
            return None
        return self._decide(bar, now)

    def _account_equity(self) -> float | None:
        """Current account equity for the SL plausibility check, or ``None``."""
        eq_fn = getattr(self.broker, "equity", None)
        if callable(eq_fn):
            try:
                value = eq_fn()
            except Exception:
                value = None
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        if self._lot_equity is not None:
            return float(self._lot_equity)
        return None

    def _has_position(self) -> bool:
        self.sync()
        return self._any_open()

    def sync(self) -> None:
        """Refresh side/qty for every bot magic from the terminal. Foreign magics ignored."""
        for magic in list(self.by_magic):
            exp = self.broker.exposure(magic=magic)
            tracked = self._tracked(magic)
            if exp.count == 0 or abs(exp.net) < 1e-12:
                tracked.side = 0
                tracked.qty = 0.0
            else:
                tracked.side = 1 if exp.net > 0 else -1
                tracked.qty = abs(exp.net)

    def flatten(self, reason: str) -> None:
        """Close every bot magic. Kill file and session exits use this path."""
        self._log(f"flatten: {reason}")
        try:
            reports = self.broker.close_all()
        except ConnectionLost:
            self._log("flatten: terminal already gone")
            return
        for rep in reports:
            if not rep.ok:
                self._log(f"flatten: close rejected retcode={rep.retcode}")
        for tracked in self.by_magic.values():
            tracked.side = 0
            tracked.qty = 0.0
            tracked.clear_stops()
        try:
            self.sync()
        except ConnectionLost:
            self._log("flatten: terminal already gone")

    def _protect(self, bar) -> bool:
        """Per-magic stop / hold management. True means this bar is done.

        Live leaves broker stops on each position and only logs once the
        terminal reports that magic flat. The hold limit is ours, per magic.
        """
        snapshot = {
            magic: (tracked.side, tracked.sl, tracked.tp, tracked.strategy)
            for magic, tracked in self.by_magic.items()
        }
        self.sync()
        acted = False
        for magic, (side, sl, tp, strategy) in snapshot.items():
            tracked = self._tracked(magic)
            if side and not tracked.side:
                hit = stop_exit(side, bar.bid, bar.ask, sl, tp)
                reason = hit[0] if hit else "broker close"
                px = hit[1] if hit else None
                tracked.clear_stops()
                price = f" @ {px:.2f}" if px is not None else ""
                self._log(
                    f"EXIT{price} ({reason}) · magic={magic} · strategy={strategy or '-'}"
                )
                acted = True
                continue
            if not tracked.side:
                continue
            if tracked.t_in == 0:
                tracked.t_in = bar.t
                if not tracked.px_in:
                    tracked.px_in = bar.bid if tracked.side > 0 else bar.ask
                continue
            limit = tracked.hold_seconds or self.cfg.max_hold_seconds
            if limit and bar.t - tracked.t_in >= limit:
                self._log(self._close_magic(
                    magic, bar, f"max hold {limit}s", strategy=tracked.strategy,
                ))
                acted = True
        return acted

    def _decide(self, bar, now: datetime):
        self.sync()
        for tracked in self.by_magic.values():
            if tracked.side and tracked.t_in == 0:
                tracked.t_in = bar.t
                tracked.px_in = bar.bid if tracked.side > 0 else bar.ask
        if not self._any_open():
            self._refresh_lot()
        positions = self._positions_by_strategy()
        primary = self._primary_tracked()
        try:
            mkt = self.buffer.market()
            X, _, raw = feature_matrix(mkt)
            warm = min(self.cfg.warmup // 2, max(0, len(mkt) - 1))
            fair_z = FairValueModel(X.shape[1]).run(X, raw["mid"], raw["rv"], warmup=warm)
            state = StateBuilder(raw, fair_z).state(
                len(mkt) - 1,
                position_text(primary.side, bar.t, primary.t_in, primary.px_in, bar.bid, bar.ask),
            )
            mark_depth(state, self.buffer.depth_live)
            spec = self.broker.spec()
            self._tick_size = float(getattr(spec, "tick_size", 0) or spec.point or 0.01)
            self._tick_value = float(getattr(spec, "tick_value", 0) or self._tick_size)
            ctx = build_context(
                now=now, bar_t=bar.t, mid=float(raw["mid"][-1]), bid=bar.bid, ask=bar.ask,
                point=spec.point, digits=spec.digits, position=primary.side, semantic=state,
                times=[int(x) for x in mkt.t], mids=[float(x) for x in raw["mid"]],
                buy_vol=[float(x) for x in mkt.buy_vol], sell_vol=[float(x) for x in mkt.sell_vol],
                limits=self.limits, max_hold_seconds=self.cfg.max_hold_seconds,
                avoid=self.cfg.avoid,
                mtf=self._mtf_closed(),
                mtf_limits=self.cfg.mtf_limits,
                stops_level=int(spec.stops_level),
                spread_points=float(spec.spread_points),
                lot=float(self.effective_lot()),
                equity=self._account_equity(),
                tick_value=float(self._tick_value),
                tick_size=float(self._tick_size),
                allow_short=bool(self.cfg.allow_short),
                h4_ema_allow_short=bool(self.cfg.h4_ema_allow_short),
                h1_use_sma50_200=bool(self.cfg.h1_use_sma50_200),
                hold_overrides=dict(self.cfg.hold_overrides),
            )
            # Jev candle-close cache: ask only when a strategy has a NEW closed
            # signal candle. Between closes, reuse the cached Response for that
            # strategy (SL/TP/hold/kill still run every second in _protect).
            # Cost gates mostly run inside decide after Jev; cheap code
            # preconditions already skip question collection when no setup.
            ask_names, cached_by_strategy = self._jev_cache_plan(bar.t)
            questions = self.router.collect_questions(
                ctx, positions=positions, only=ask_names if ask_names is not None else None,
            )
            response = None
            if questions:
                for name in sorted(ask_names or ()):
                    tf = signal_tf_for(name)
                    candle_t = self._signal_candle_t(tf, int(bar.t))
                    if candle_t is not None:
                        self._log(f"jev ask · {name} · tf={tf} · candle={candle_t}")
                sent = self.router.state_for_request(ctx, questions, positions=positions)
                response = self.jev.system_one(sent, questions)
                self._jev_cache_store(ask_names or set(), int(bar.t), response)
            response = self._jev_merge_response(response, cached_by_strategy)
        except JevError as exc:
            # Retries and rate-limit backoff already ran inside the client.
            # A failed call is a hold. It is not stored as a Noul of zero.
            self._log(f"jev unavailable, holding: {exc}")
            payload = getattr(self.jev, "last_payload", None)
            if payload is not None:
                self._log("jev request payload: " + payload_text(payload))
            return []
        except (ConnectionLost, TradingHalted, KeyboardInterrupt):
            raise
        except Exception as exc:
            self.flatten("session error")
            raise TradingHalted(redact(f"{type(exc).__name__}: {exc}")) from exc
        if response is not None and not self._answered:
            self._answered = True
            self._log(f"first answer · model {response.model}")
        decisions = self.router.decide_all(ctx, response, positions=positions)
        buys = [d for d in decisions if d.action == "buy"]
        sells = [d for d in decisions if d.action == "sell"]
        if buys and sells:
            self._log(
                f"conflict · longs={[d.strategy for d in buys]} · "
                f"shorts={[d.strategy for d in sells]} · allowing both"
                + (" (no-hedge may block)" if self.cfg.no_hedge else "")
            )
        for decision in decisions:
            feat = decision.features or {}
            magic = decision.magic if decision.magic is not None else (
                self.magic_of(decision.strategy) if decision.strategy else None
            )
            if decision.strategy and (
                decision.action in ("buy", "sell", "exit")
                or feat.get("setup_id")
                or feat.get("nouls")
                or feat.get("labels")
            ):
                self._log(
                    f"signal · {decision.strategy} · magic={magic} · {decision.action} · "
                    f"setup={feat.get('setup_id', feat.get('session_id', '-'))} · {feat}"
                )
            line = self._act(decision, bar, now)
            if line and (self.cfg.verbose or not line.startswith("hold")):
                self._log(line)
        return decisions

    def _act(self, decision, bar, now: datetime) -> str:
        strategy = decision.strategy or ""
        if not strategy:
            return f"hold {decision.reason}"
        try:
            magic = decision.magic if decision.magic is not None else self.magic_of(strategy)
        except KeyError:
            return f"hold unknown strategy magic: {strategy}"
        tracked = self._tracked(magic)
        tracked.strategy = strategy or tracked.strategy
        lines = []
        closing = tracked.side and (
            decision.action == "exit"
            or (
                decision.action in ("buy", "sell")
                and (decision.action == "buy") != (tracked.side > 0)
            )
        )
        if closing:
            lines.append(self._close_magic(magic, bar, decision.reason, strategy=strategy))
        if decision.action in ("buy", "sell"):
            side = 1 if decision.action == "buy" else -1
            # Re-read after a possible flip-close on this magic.
            tracked = self._tracked(magic)
            if tracked.side == 0:
                lines.append(self._open(side, decision, bar, now, magic=magic, strategy=strategy))
            elif tracked.side == side:
                lines.append(
                    f"blocked: {strategy} already has a position · magic={magic}"
                )
        if not lines:
            return f"hold {decision.reason}"
        return " | ".join(x for x in lines if x)

    def _close_magic(
        self,
        magic: int,
        bar,
        reason: str,
        *,
        px: float | None = None,
        strategy: str = "",
    ) -> str:
        reports = self.broker.close_all(magic=magic)
        self.sync()
        tracked = self._tracked(magic)
        bad = [r for r in reports if not r.ok]
        name = strategy or tracked.strategy or self.magic_strategy.get(magic, "-")
        if tracked.side or bad:
            code = bad[0].retcode if bad else 0
            return (
                f"EXIT rejected retcode={code} ({reason}) · "
                f"magic={magic} · strategy={name}"
            )
        shown = f" @ {px:.2f}" if px is not None else ""
        tracked.clear_stops()
        return f"EXIT{shown} ({reason}) · magic={magic} · strategy={name}"

    def _trade_limits(self, decision) -> RiskLimits:
        """Global limits, with optional tighter strategy stops for this fill."""
        lim = self.limits
        sl = decision.sl_points if decision.sl_points is not None else lim.sl_points
        tp = decision.tp_points if decision.tp_points is not None else lim.tp_points
        # Use the session lot (equity-scaled or fixed).
        lim = replace(lim, lot=float(self.effective_lot()))
        if sl == lim.sl_points and tp == lim.tp_points:
            return lim
        return replace(lim, sl_points=sl, tp_points=tp)

    def _open(self, side: int, decision, bar, now: datetime, *, magic: int, strategy: str) -> str:
        if self.cfg.avoid is not None and self.cfg.avoid.blocks(now):
            return f"blocked: avoid hours {self.cfg.avoid.text} Europe/Berlin"
        tf = signal_tf_for(strategy)
        gap_token = self.entry_window.gap_block_token(tf)
        ok_entry, why_entry = allows_entry(
            now,
            hold_over_weekend=self.cfg.hold_over_weekend,
            last_gap_at=gap_token,
        )
        if not ok_entry:
            return f"blocked: {why_entry}"
        # Reject Sunday-stub signal bars for higher-TF strategies.
        if tf != "1s":
            candle_t = self._signal_candle_t(tf, int(bar.t))
            tf_sec = TF_SECONDS.get(tf, 0)
            if candle_t is not None and tf_sec and is_sunday_stub(int(candle_t), int(tf_sec)):
                return f"blocked: sunday stub candle · tf={tf}"
        if self._tracked(magic).side:
            return f"blocked: {strategy} already has a position · magic={magic}"
        if self._hedge_blocked(side):
            return (
                f"blocked: no-hedge · opposite bot position open · "
                f"magic={magic} · strategy={strategy}"
            )
        try:
            self._refresh_lot()
            spec = self.broker.spec()
            self._contract = spec.contract_size or self._contract or 1.0
            limits = self._trade_limits(decision)
            intent = build_order(side, self.effective_lot() * decision.size, spec, limits)
            exp = self.broker.exposure()  # all bot magics
            check_entry(self.limits, intent.volume, exp.count, float(exp.lots))
        except RiskError as exc:
            return f"blocked: {exc}"
        hold = decision.max_hold_seconds
        if hold is None:
            hold = self.cfg.hold_overrides.get(strategy, self.cfg.max_hold_seconds)
        comment = order_comment(strategy)
        self._log(
            f"order · magic={magic} · strategy={strategy} · "
            f"{'BUY' if side > 0 else 'SELL'} · comment={comment}"
        )
        try:
            report = self.broker.send(intent, magic=magic, comment=comment)
        except ConnectionLost:
            raise
        if not report.ok:
            return (
                f"order rejected retcode={report.retcode} · "
                f"magic={magic} · strategy={strategy}"
            )
        self.sync()
        tracked = self._tracked(magic)
        if tracked.side:
            tracked.strategy = strategy
            tracked.px_in = report.price or intent.price
            tracked.t_in = bar.t
            tracked.sl = intent.sl
            tracked.tp = intent.tp
            tracked.hold_seconds = int(hold or 0)
            self.router.note_fill(decision)
        if not self._swap_logged:
            self._log_swap()
        else:
            # Refresh swap on each open for the dashboard.
            self._log_swap()
        return (
            format_intent(self.broker.symbol, intent, spec.spread_points, dry_run=False)
            + f" retcode={report.retcode} · magic={magic} · strategy={strategy}"
        )

    def _log(self, msg: str) -> None:
        print(redact(msg), flush=True)

    def _wait(self) -> float:
        return max(0.05, 1.0 - (self.clock() % 1.0))
