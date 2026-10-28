"""Live loop: a broker feed in place of the simulator, the same decision path.

``features.py``, ``state.py`` and ``decide.py`` are used as they are. The
only difference from ``cmd_paper`` is where the one-second book comes from
and where an order goes. A lost terminal flattens and stops. A Jev error
holds that bar and leaves the loop running.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .decide import Decision, decide
from .features import feature_matrix
from .mt5_feed import MarketBuffer, Tick, TickAggregator, depth_note
from .quant import FairValueModel
from .questions import decision_questions, state_for_model
from .state import StateBuilder
from .risk import (
    AvoidHours,
    ConnectionLost,
    KillSwitch,
    MarketHours,
    RiskError,
    RiskLimits,
    TradingHalted,
    assert_may_send,
    build_order,
    check_entry,
    format_intent,
    loss_breached,
    parse_avoid_hours,
    position_text,
    redact,
    require_berlin,
    require_new_york,
    stop_exit,
)
from .typesafe import JevError, payload_text


def describe_model(jev) -> str:
    """Header text for the model that will actually answer. Never includes a key."""
    name = getattr(jev, "model", "?")
    if str(name).startswith("surrogate"):
        return f"{name} (offline stand-in, not Jev)"
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


@dataclass
class LiveConfig:
    symbol: str = "XAUUSD"
    step: int = 10
    warmup: int = 1800
    history: int = 7200
    levels: int = 5
    fallback_size: float = 1.0
    dry_run: bool = True
    allow_real: bool = False
    kill_file: str = "jev.KILL"
    magic: int = 20261027
    max_silence: float = 30.0
    max_gap: int = 10
    hours: str = "gold"
    verbose: bool = False
    max_hold_seconds: int = 900
    avoid: AvoidHours | None = None
    limits: RiskLimits = field(default_factory=RiskLimits)


def live_config_from_args(args) -> LiveConfig:
    if getattr(args, "dry_run", False) and getattr(args, "send", False):
        raise RiskError("pass only one of --dry-run and --send")
    limits = RiskLimits(
        max_lots=args.max_lots, max_positions=args.max_positions, max_daily_loss=args.max_daily_loss,
        max_spread_points=args.max_spread, lot=args.lot, sl_points=args.sl_points, tp_points=args.tp_points,
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
    return LiveConfig(
        symbol=args.symbol, step=args.step, warmup=args.warmup, history=args.history,
        levels=args.levels, dry_run=not bool(args.send), allow_real=bool(args.allow_real),
        kill_file=args.kill_file, magic=args.magic, hours=args.hours, verbose=bool(args.verbose),
        max_hold_seconds=hold, avoid=avoid, limits=limits,
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
        self.qs = decision_questions()
        self.position = 0
        self.qty = 0.0
        self.px_in = 0.0
        self.t_in = 0
        self.sl = 0.0
        self.tp = 0.0
        self.realized = 0.0
        self.last_msc = 0
        self._contract = 0.0
        self._depth_logged = False
        self._warm_logged = False
        self._answered = False
        self._last_tick_wall: float | None = None

    def ensure_allowed(self) -> None:
        if self.cfg.hours == "gold":
            require_new_york()
        if self.cfg.avoid is not None:
            require_berlin()
        self.limits.validate()
        if self.cfg.max_hold_seconds < 0:
            raise RiskError("max_hold_seconds must be >= 0")
        if self.cfg.step < 1 or self.cfg.warmup < 300 or self.cfg.history < self.cfg.warmup:
            raise RiskError("step must be >= 1, warmup at least 300 seconds, and history must cover it")
        if not 1 <= self.cfg.levels <= 10:
            raise RiskError("levels must be between 1 and 10")
        assert_may_send(
            self.broker.trade_mode(), dry_run=self.cfg.dry_run, allow_real=self.cfg.allow_real,
            confirm=os.environ.get("MT5_CONFIRM_REAL"),
        )
        if not self.cfg.dry_run:
            from .mt5_broker import require_algo_trading
            require_algo_trading(self.broker)
        spec = self.broker.spec()
        if self.limits.lot < spec.volume_min - 1e-12:
            raise RiskError("lot is below the broker minimum")
        self.buffer.tick = spec.tick_size or spec.point or 0.01
        self._contract = spec.contract_size or 1.0

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
            self._backfill()
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

    def _announce(self) -> None:
        spec = self.broker.spec()
        mode = "DRY-RUN" if self.cfg.dry_run else "SEND"
        avoid = "off" if self.cfg.avoid is None else f"{self.cfg.avoid.text} Europe/Berlin"
        self._log(
            f"live · mt5 · {self.broker.symbol} · {mode} · model {describe_model(self.jev)} · "
            f"{self.broker.describe()}"
        )
        self._log(
            f"limits · lot {self.limits.lot:g} · max_lots {self.limits.max_lots:g} · "
            f"max_positions {self.limits.max_positions} · max_daily_loss {self.limits.max_daily_loss:g} · "
            f"max_spread {self.limits.max_spread_points:g} · {_shown('sl_points', self.limits.sl_points)} · "
            f"{_shown('tp_points', self.limits.tp_points)} · deviation {self.limits.deviation_points} · "
            f"{_shown('max_hold_seconds', self.cfg.max_hold_seconds)} · avoid_hours {avoid} · "
            f"hours {self.cfg.hours} · step {self.cfg.step}s · warmup {self.cfg.warmup}s · "
            f"contract {spec.contract_size:g} · point {spec.point:g}"
        )
        if str(getattr(self.jev, "model", "")).startswith("surrogate"):
            if (os.environ.get("TYPESAFE_API_KEY") or "").strip():
                self._log("WARNING: live is using the offline surrogate, not TypeSafe Jev.")
            else:
                self._log(
                    "WARNING: TYPESAFE_API_KEY is not set. Live decisions use the offline "
                    "surrogate (surrogate-local), not TypeSafe Jev."
                )
        if not self.cfg.dry_run and self.broker.trade_mode() == "real":
            self._log("REAL MONEY: orders will be sent on this account")
        self._log(self.broker.clock_text())

    def _backfill(self) -> None:
        ticks = self.broker.history_ticks(self.cfg.history, now_unix=self.clock())
        bars = self.agg.bars(ticks, None) if ticks else []
        for bar in bars:
            self.buffer.append(bar)
        if ticks:
            self.last_msc = max(t.time_msc for t in ticks)
            self._last_tick_wall = self.clock()
            self._log(f"backfill {len(self.buffer)}s from terminal tick history (no depth in history)")
        else:
            self._log(f"no tick history; collecting {self.cfg.warmup}s before the first decision")

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
        if self.buffer.append(bar):
            self._warm_logged = False
            self._log("feed gap; warmup started again")
        if loss_breached(self.pnl(bar), self.limits.max_daily_loss):
            self.flatten("daily loss limit")
            raise TradingHalted("daily loss limit")
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

    def pnl(self, bar) -> float:
        if not self.cfg.dry_run:
            return self.broker.daily_pnl(self.now())
        if self.position == 0:
            return self.realized
        px = bar.bid if self.position > 0 else bar.ask
        return self.realized + (px - self.px_in) * self.position * self.qty * (self._contract or 1.0)

    def _has_position(self) -> bool:
        self.sync()
        return self.position != 0

    def sync(self) -> None:
        if self.cfg.dry_run:
            return
        exp = self.broker.exposure()
        self.qty = abs(exp.net)
        if exp.count == 0 or abs(exp.net) < 1e-12:
            self.position = 0
            self.qty = 0.0
        else:
            self.position = 1 if exp.net > 0 else -1

    def _clear_stops(self) -> None:
        self.sl = 0.0
        self.tp = 0.0
        self.t_in = 0
        self.px_in = 0.0

    def flatten(self, reason: str) -> None:
        self._log(f"flatten: {reason}")
        if self.cfg.dry_run:
            if self.position:
                self.position = 0
                self.qty = 0.0
            self._clear_stops()
            return
        try:
            reports = self.broker.close_all()
        except ConnectionLost:
            self._log("flatten: terminal already gone")
            return
        for rep in reports:
            if not rep.ok:
                self._log(f"flatten: close rejected retcode={rep.retcode}")
        self._clear_stops()
        try:
            self.sync()
        except ConnectionLost:
            self._log("flatten: terminal already gone")

    def _protect(self, bar) -> bool:
        """Close on a stop or the hold limit. True means this bar is done.

        Dry-run fills the stop at the same rounded price the order would have
        carried. Live leaves that stop on the position and only logs it once
        the terminal reports the position gone, so a market close cannot race
        the broker fill. The hold limit is ours in both modes.
        """
        if not self.cfg.dry_run:
            side, sl, tp = self.position, self.sl, self.tp
            self.sync()
            if side and not self.position:
                hit = stop_exit(side, bar.bid, bar.ask, sl, tp)
                reason = hit[0] if hit else "broker close"
                px = hit[1] if hit else None
                self.qty = 0.0
                self._clear_stops()
                price = f" @ {px:.2f}" if px is not None else ""
                self._log(f"EXIT{price} ({reason})")
                return True
        if not self.position:
            return False
        if self.t_in == 0:
            self.t_in = bar.t
            if not self.px_in:
                self.px_in = bar.bid if self.position > 0 else bar.ask
            return False
        if self.cfg.dry_run:
            hit = stop_exit(self.position, bar.bid, bar.ask, self.sl, self.tp)
            if hit:
                reason, px = hit
                self._log(self._close(bar, reason, px=px))
                return True
        limit = self.cfg.max_hold_seconds
        if limit and bar.t - self.t_in >= limit:
            self._log(self._close(bar, f"max hold {limit}s"))
            return True
        return False

    def _decide(self, bar, now: datetime):
        self.sync()
        if not self.cfg.dry_run and self.position and self.t_in == 0:
            self.t_in = bar.t
            self.px_in = bar.bid if self.position > 0 else bar.ask
        try:
            mkt = self.buffer.market()
            X, _, raw = feature_matrix(mkt)
            warm = min(self.cfg.warmup // 2, max(0, len(mkt) - 1))
            fair_z = FairValueModel(X.shape[1]).run(X, raw["mid"], raw["rv"], warmup=warm)
            state = StateBuilder(raw, fair_z).state(
                len(mkt) - 1,
                position_text(self.position, bar.t, self.t_in, self.px_in, bar.bid, bar.ask),
            )
            mark_depth(state, self.buffer.depth_live)
            sent = state_for_model(state)
            response = self.jev.system_one(sent, self.qs)
        except JevError as exc:
            # Retries and rate-limit backoff already ran inside the client.
            # A failed call is a hold. It is not stored as a Noul of zero.
            self._log(f"jev unavailable, holding: {exc}")
            payload = getattr(self.jev, "last_payload", None)
            if payload is not None:
                self._log("jev request payload: " + payload_text(payload))
            return Decision(len(self.buffer) - 1, "hold", 0.0, 0.0, {}, "jev unavailable", {})
        except (ConnectionLost, TradingHalted, KeyboardInterrupt):
            raise
        except Exception as exc:
            self.flatten("session error")
            raise TradingHalted(redact(f"{type(exc).__name__}: {exc}")) from exc
        if not self._answered:
            self._answered = True
            self._log(f"first answer · model {response.model}")
        decision = decide(len(self.buffer) - 1, response, self.position)
        line = self._act(decision, bar, now)
        if line and (self.cfg.verbose or not line.startswith("hold")):
            self._log(line)
        if loss_breached(self.pnl(bar), self.limits.max_daily_loss):
            self.flatten("daily loss limit")
            raise TradingHalted("daily loss limit")
        return decision

    def _act(self, decision, bar, now: datetime) -> str:
        lines = []
        closing = self.position and (
            decision.action == "exit"
            or (decision.action in ("buy", "sell") and (decision.action == "buy") != (self.position > 0))
        )
        if closing:
            lines.append(self._close(bar, decision.reason))
        if decision.action in ("buy", "sell") and self.position == 0:
            lines.append(self._open(1 if decision.action == "buy" else -1, decision, bar, now))
        if not lines:
            return f"hold {decision.reason}"
        return " | ".join(x for x in lines if x)

    def _close(self, bar, reason: str, px: float | None = None) -> str:
        if self.cfg.dry_run:
            if px is None:
                px = bar.bid if self.position > 0 else bar.ask
            self.realized += (px - self.px_in) * self.position * self.qty * (self._contract or 1.0)
            self.position = 0
            self.qty = 0.0
            self._clear_stops()
            return f"DRY-RUN EXIT @ {px:.2f} ({reason}) pnl {self.realized:+.2f}"
        reports = self.broker.close_all()
        self.sync()
        bad = [r for r in reports if not r.ok]
        if self.position or bad:
            code = bad[0].retcode if bad else 0
            return f"EXIT rejected retcode={code} ({reason})"
        shown = f" @ {px:.2f}" if px is not None else ""
        self.qty = 0.0
        self._clear_stops()
        return f"EXIT{shown} ({reason})"

    def _open(self, side: int, decision, bar, now: datetime) -> str:
        if self.cfg.avoid is not None and self.cfg.avoid.blocks(now):
            return f"blocked: avoid hours {self.cfg.avoid.text} Europe/Berlin"
        try:
            spec = self.broker.spec()
            self._contract = spec.contract_size or self._contract or 1.0
            intent = build_order(side, self.limits.lot * decision.size, spec, self.limits)
            if self.cfg.dry_run:
                count, current = (1 if self.position else 0), 0.0
            else:
                exp = self.broker.exposure()
                count, current = exp.count, abs(exp.net)
            check_entry(self.limits, intent.volume, count, current)
        except RiskError as exc:
            return f"blocked: {exc}"
        if self.cfg.dry_run:
            self.position = side
            self.qty = intent.volume
            self.px_in = intent.price
            self.t_in = bar.t
            self.sl = intent.sl
            self.tp = intent.tp
            return format_intent(self.broker.symbol, intent, spec.spread_points, dry_run=True)
        try:
            report = self.broker.send(intent)
        except ConnectionLost:
            raise
        if not report.ok:
            return f"order rejected retcode={report.retcode}"
        self.sync()
        if self.position:
            self.px_in = report.price or intent.price
            self.t_in = bar.t
            self.sl = intent.sl
            self.tp = intent.tp
        return format_intent(self.broker.symbol, intent, spec.spread_points, dry_run=False) + f" retcode={report.retcode}"

    def _log(self, msg: str) -> None:
        print(redact(msg), flush=True)

    def _wait(self) -> float:
        return max(0.05, 1.0 - (self.clock() % 1.0))
