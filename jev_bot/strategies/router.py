"""Collect enabled strategies; live uses one Decision per actionable vote.

``decide_all`` returns every buy/sell/exit that fired. Long and short from
different strategies are allowed (logged by live). ``decide`` keeps the
legacy OR-combine for callers that still want a single Decision.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field, fields, replace

from ..questions import Decision, Policy
from ..typesafe import Question, Response
from . import (
    _pa_common as pa,
    consensus,
    h1_prevday_break,
    h1_pullback,
    h1_trend_long,
    h4_donchian,
    h4_ema_pullback,
    h4_zone_retest,
    m5_asianbox,
    m5_ny_orb,
    m5_vwap_pb,
    m15_asia_squeeze,
    m15_ny_orb,
    m15_trend_pullback,
    orb_london,
    pa_insidebar,
    pa_rejection,
    pa_structure,
    pullback_h1,
    pullback_h4,
    pullback_m1,
    pullback_m15,
    pullback_m5,
    session_dir,
    vwap_fade,
)
from .common import MarketContext, Signal, to_decision
from .magics import magic_for
from .minutes import closed_minutes

# Multi-timeframe entry modules wired through the router. Flag name == module NAME.
_MTF_MODULES: tuple[tuple[str, object], ...] = (
    ("m5_ny_orb", m5_ny_orb),
    ("m5_asianbox", m5_asianbox),
    ("m5_vwap_pb", m5_vwap_pb),
    ("m15_ny_orb", m15_ny_orb),
    ("m15_asia_squeeze", m15_asia_squeeze),
    ("m15_trend_pullback", m15_trend_pullback),
    ("h1_trend_long", h1_trend_long),
    ("h1_pullback", h1_pullback),
    ("h1_prevday_break", h1_prevday_break),
    ("h4_donchian", h4_donchian),
    ("h4_ema_pullback", h4_ema_pullback),
    ("h4_zone_retest", h4_zone_retest),
    ("pullback_m1", pullback_m1),
    ("pullback_m5", pullback_m5),
    ("pullback_m15", pullback_m15),
    ("pullback_h1", pullback_h1),
    ("pullback_h4", pullback_h4),
)

# Group aliases for parse_strategy_flags. h4_zone_retest stays off unless named.
# Legacy pullback flag names stay in m15/h1/h4; use group ``pullback`` for the
# unified pullback_m1..h4 modules (avoids enabling both alias and new name).
_FLAG_GROUPS: dict[str, tuple[str, ...]] = {
    "m5": ("m5_ny_orb", "m5_asianbox", "m5_vwap_pb"),
    "m15": ("m15_ny_orb", "m15_asia_squeeze", "m15_trend_pullback"),
    "h1": ("h1_trend_long", "h1_pullback", "h1_prevday_break"),
    "h4": ("h4_donchian", "h4_ema_pullback"),
    "pullback": ("pullback_m1", "pullback_m5", "pullback_m15", "pullback_h1", "pullback_h4"),
}

# Same-direction merge priority. First listed wins among buys (or among sells).
# Independent of StrategyFlags field order and of --strategies token order.
STRATEGY_PRIORITY: tuple[str, ...] = (
    "consensus",
    "vwap_fade",
    "orb_london",
    "pa_rejection",
    "pa_insidebar",
    "m5_ny_orb",
    "m5_asianbox",
    "m5_vwap_pb",
    "m15_ny_orb",
    "m15_asia_squeeze",
    "m15_trend_pullback",
    "h1_trend_long",
    "h1_pullback",
    "h1_prevday_break",
    "h4_donchian",
    "h4_ema_pullback",
    "h4_zone_retest",
    "pullback_m1",
    "pullback_m5",
    "pullback_m15",
    "pullback_h1",
    "pullback_h4",
)
_PRIORITY_RANK = {name: i for i, name in enumerate(STRATEGY_PRIORITY)}


def _priority_key(sig: Signal) -> tuple[int, str]:
    return (_PRIORITY_RANK.get(sig.strategy, len(STRATEGY_PRIORITY)), sig.strategy)


@dataclass
class StrategyFlags:
    consensus: bool = False  # live default: strategies only when explicitly enabled
    vwap_fade: bool = False
    orb: bool = False
    session_dir: bool = False
    pa_rejection: bool = False
    pa_insidebar: bool = False
    pa_structure: bool = False
    orb_zone: str = "Europe/London"
    m5_ny_orb: bool = False
    m5_asianbox: bool = False
    m5_vwap_pb: bool = False
    m15_ny_orb: bool = False
    m15_asia_squeeze: bool = False
    m15_trend_pullback: bool = False
    h1_trend_long: bool = False
    h1_pullback: bool = False
    h1_prevday_break: bool = False
    h4_donchian: bool = False
    h4_ema_pullback: bool = False
    h4_zone_retest: bool = False
    pullback_m1: bool = False
    pullback_m5: bool = False
    pullback_m15: bool = False
    pullback_h1: bool = False
    pullback_h4: bool = False
    allow_short: bool = False


def parse_strategy_flags(names: list[str] | str | None) -> StrategyFlags:
    """Map strategy / group names to a StrategyFlags with those fields enabled.

    Recognized groups: ``m5``, ``m15``, ``h1``, ``h4``, ``pullback``.
    ``h4_zone_retest`` is not part of the ``h4`` group and stays off unless
    named explicitly. Also accepts existing flag names (``consensus``,
    ``vwap_fade``, ``orb``, ``session_dir``, ``pa_*``, ``pullback_m1``…
    ``pullback_h4``, ``allow_short``) and comma-separated tokens inside list
    entries. Starts from all-off (including consensus).
    """
    if names is None:
        return StrategyFlags(consensus=False)
    if isinstance(names, str):
        raw_items: list[str] = [names]
    else:
        raw_items = list(names)
    tokens: list[str] = []
    for raw in raw_items:
        for part in str(raw).split(","):
            tok = part.strip()
            if tok:
                tokens.append(tok)

    updates: dict[str, bool] = {}
    field_names = {f.name for f in fields(StrategyFlags) if f.name != "orb_zone"}
    aliases = {
        "orb_london": "orb",
        "filter_session_dir": "session_dir",
    }
    for tok in tokens:
        key = aliases.get(tok, tok)
        if key in _FLAG_GROUPS:
            for name in _FLAG_GROUPS[key]:
                updates[name] = True
            continue
        if key in field_names:
            updates[key] = True
            continue
        raise ValueError(f"unknown strategy flag: {tok}")
    return replace(StrategyFlags(consensus=False), **updates)


def _accepts_param(fn, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _accepts_spent(fn) -> bool:
    return _accepts_param(fn, "spent")


@dataclass
class StrategyRouter:
    flags: StrategyFlags = field(default_factory=StrategyFlags)
    policy: Policy = field(default_factory=Policy)
    orb_broken_days: set[str] = field(default_factory=set)
    spent_setup_ids: set[str] = field(default_factory=set)
    traded_days: set[str] = field(default_factory=set)
    base_magic: int = 20261027
    magic_overrides: dict[str, int] = field(default_factory=dict)

    def _mtf_kwargs(self, fn) -> dict:
        kwargs: dict = {}
        if _accepts_param(fn, "spent"):
            kwargs["spent"] = self.spent_setup_ids
        if _accepts_param(fn, "traded_days"):
            kwargs["traded_days"] = self.traded_days
        return kwargs

    def _mtf_precondition(self, mod):
        """Build precondition(ctx) that passes spent / traded_days when accepted."""
        def check(ctx: MarketContext) -> bool:
            return bool(mod.precondition(ctx, **self._mtf_kwargs(mod.precondition)))
        return check

    def _mtf_state(self, mod, ctx: MarketContext) -> dict:
        return dict(mod.state_fields(ctx, **self._mtf_kwargs(mod.state_fields)) or {})

    def _mtf_decide(self, mod, ctx: MarketContext, response: Response, name: str) -> Signal:
        return mod.decide(ctx, response, self.policy, **self._mtf_kwargs(mod.decide))

    def _ctx_for(self, ctx: MarketContext, name: str,
                 positions: dict[str, int] | None) -> MarketContext:
        if positions is None:
            return ctx
        return replace(ctx, position=int(positions.get(name, 0)))

    def magic_of(self, strategy: str) -> int:
        return magic_for(strategy, self.base_magic, self.magic_overrides or None)

    def enabled_names(self) -> list[str]:
        names = []
        if self.flags.consensus:
            names.append("consensus")
        if self.flags.vwap_fade:
            names.append("vwap_fade")
        if self.flags.orb:
            names.append("orb_london")
        if self.flags.session_dir:
            names.append("session_dir")
        if self.flags.pa_rejection:
            names.append("pa_rejection")
        if self.flags.pa_insidebar:
            names.append("pa_insidebar")
        if self.flags.pa_structure:
            names.append("pa_structure")
        for name, _mod in _MTF_MODULES:
            if getattr(self.flags, name):
                names.append(name)
        return names

    def collect_questions(
        self,
        ctx: MarketContext,
        positions: dict[str, int] | None = None,
    ) -> dict[str, Question]:
        """Only the strategies whose code precondition is true contribute questions."""
        qs: dict[str, Question] = {}
        c_cons = self._ctx_for(ctx, "consensus", positions)
        if self.flags.consensus and consensus.precondition(c_cons):
            qs.update(consensus.questions(c_cons))
        c_vwap = self._ctx_for(ctx, "vwap_fade", positions)
        if self.flags.vwap_fade and vwap_fade.precondition(c_vwap):
            qs.update(vwap_fade.questions())
        c_orb = self._ctx_for(ctx, "orb_london", positions)
        if self.flags.orb and orb_london.precondition(
            c_orb, zone_name=self.flags.orb_zone, broken_days=self.orb_broken_days
        ):
            qs.update(orb_london.questions())
        pa_ready = False
        c_rej = self._ctx_for(ctx, "pa_rejection", positions)
        if self.flags.pa_rejection and pa_rejection.precondition(c_rej, spent=self.spent_setup_ids):
            pin = pa_rejection.find_setup(c_rej, spent=self.spent_setup_ids)
            if pin is not None:
                qs.update(pa_rejection.questions_for_side(pin.side))
                qs.update(pa.liquidity_questions())
                pa_ready = True
        c_ib = self._ctx_for(ctx, "pa_insidebar", positions)
        if self.flags.pa_insidebar and pa_insidebar.precondition(c_ib, spent=self.spent_setup_ids):
            setup = pa_insidebar.find_setup(c_ib, spent=self.spent_setup_ids)
            if setup is not None:
                qs.update(pa_insidebar.questions_for_side(setup.side))
                qs.update(pa.liquidity_questions())
                pa_ready = True
        if self.flags.pa_structure and pa_ready and pa_structure.precondition(ctx):
            qs.update(pa_structure.questions())
        for name, mod in _MTF_MODULES:
            if not getattr(self.flags, name):
                continue
            c_mtf = self._ctx_for(ctx, name, positions)
            if self._mtf_precondition(mod)(c_mtf):
                qs.update(mod.questions())
        # Flat aggregate: never ask exit Nouls (also filtered inside consensus.questions).
        flat = int(getattr(ctx, "position", 0) or 0) == 0
        if positions is not None:
            flat = all(int(v or 0) == 0 for v in positions.values())
        if flat:
            from ..questions import EXIT_LONG, EXIT_SHORT
            qs.pop(EXIT_LONG, None)
            qs.pop(EXIT_SHORT, None)
        return qs

    def state_for_request(
        self,
        ctx: MarketContext,
        questions: dict[str, Question],
        positions: dict[str, int] | None = None,
    ) -> dict:
        """Merge the semantic state with strategy-specific named fields."""
        if self.flags.consensus:
            state = consensus.state_fields(self._ctx_for(ctx, "consensus", positions))
        else:
            from ..questions import state_for_model
            state = state_for_model(ctx.semantic)
        if any(k.startswith("vwap_") for k in questions):
            state.update(vwap_fade.state_fields(self._ctx_for(ctx, "vwap_fade", positions)))
        if any(k.startswith("orb_") for k in questions):
            state.update(orb_london.state_fields(
                self._ctx_for(ctx, "orb_london", positions), zone_name=self.flags.orb_zone,
            ))
        if any(k.startswith("pa_rej_") for k in questions):
            state.update(pa_rejection.state_fields(
                self._ctx_for(ctx, "pa_rejection", positions), spent=self.spent_setup_ids,
            ))
        if any(k.startswith("pa_ib_") for k in questions):
            state.update(pa_insidebar.state_fields(
                self._ctx_for(ctx, "pa_insidebar", positions), spent=self.spent_setup_ids,
            ))
        if any(k.startswith("pa_struct_") for k in questions):
            state.update(pa_structure.state_fields(ctx))
        for name, mod in _MTF_MODULES:
            if not getattr(self.flags, name):
                continue
            mod_keys = set(mod.questions())
            if mod_keys.intersection(questions):
                state.update(self._mtf_state(mod, self._ctx_for(ctx, name, positions)))
        return state

    def _collect_signals(
        self,
        ctx: MarketContext,
        response: Response | None,
        positions: dict[str, int] | None = None,
    ) -> list[Signal]:
        signals: list[Signal] = []
        if self.flags.consensus:
            c = self._ctx_for(ctx, "consensus", positions)
            if response is None:
                signals.append(Signal("hold", "consensus: no response", "consensus"))
            else:
                signals.append(consensus.decide(c, response, self.policy))
        if self.flags.vwap_fade:
            c = self._ctx_for(ctx, "vwap_fade", positions)
            if vwap_fade.precondition(c):
                if response is None:
                    signals.append(Signal("hold", "vwap_fade: no response", "vwap_fade"))
                else:
                    signals.append(vwap_fade.decide(c, response, self.policy))
            else:
                feat = vwap_fade.compute(c) or {}
                reason = "vwap_fade: precondition false"
                if feat and not feat.get("cost_ok"):
                    reason = "vwap_fade: cost gate blocked"
                signals.append(Signal("hold", reason, "vwap_fade", features=feat))
        if self.flags.orb:
            c = self._ctx_for(ctx, "orb_london", positions)
            if orb_london.precondition(
                c, zone_name=self.flags.orb_zone, broken_days=self.orb_broken_days
            ):
                if response is None:
                    signals.append(Signal("hold", "orb_london: no response", "orb_london"))
                else:
                    signals.append(orb_london.decide(
                        c, response, self.policy,
                        zone_name=self.flags.orb_zone,
                        broken_days=self.orb_broken_days,
                    ))
            else:
                signals.append(Signal("hold", "orb_london: precondition false", "orb_london"))

        if self.flags.pa_rejection:
            c = self._ctx_for(ctx, "pa_rejection", positions)
            if pa_rejection.precondition(c, spent=self.spent_setup_ids):
                if response is None:
                    signals.append(Signal("hold", "pa_rejection: no response", "pa_rejection"))
                else:
                    signals.append(pa_rejection.decide(
                        c, response, self.policy, spent=self.spent_setup_ids,
                    ))
            else:
                signals.append(Signal(
                    "hold", "pa_rejection: precondition false", "pa_rejection",
                ))
        if self.flags.pa_insidebar:
            c = self._ctx_for(ctx, "pa_insidebar", positions)
            if pa_insidebar.precondition(c, spent=self.spent_setup_ids):
                if response is None:
                    signals.append(Signal("hold", "pa_insidebar: no response", "pa_insidebar"))
                else:
                    signals.append(pa_insidebar.decide(
                        c, response, self.policy, spent=self.spent_setup_ids,
                    ))
            else:
                signals.append(Signal(
                    "hold", "pa_insidebar: precondition false", "pa_insidebar",
                ))

        for name, mod in _MTF_MODULES:
            if not getattr(self.flags, name):
                continue
            c = self._ctx_for(ctx, name, positions)
            if self._mtf_precondition(mod)(c):
                if response is None:
                    signals.append(Signal("hold", f"{name}: no response", name))
                else:
                    signals.append(self._mtf_decide(mod, c, response, name))
            else:
                signals.append(Signal("hold", f"{name}: precondition false", name))

        if self.flags.pa_structure:
            signals = [
                pa_structure.filter_signal(ctx, s, response, self.policy) for s in signals
            ]
        if self.flags.session_dir:
            signals = [session_dir.filter_signal(ctx, s) for s in signals]

        return signals

    def _stamp(self, decision: Decision) -> Decision:
        if decision.magic is not None or not decision.strategy:
            return decision
        try:
            decision.magic = self.magic_of(decision.strategy)
        except KeyError:
            pass
        return decision

    def decide_all(
        self,
        ctx: MarketContext,
        response: Response | None,
        positions: dict[str, int] | None = None,
    ) -> list[Decision]:
        """One Decision per strategy that voted buy / sell / exit.

        Holds are omitted. Conflicting long and short votes are all returned;
        live logs the conflict and applies ``--no-hedge`` if set.
        """
        out: list[Decision] = []
        for sig in self._collect_signals(ctx, response, positions):
            if sig.action not in ("buy", "sell", "exit"):
                continue
            out.append(self._stamp(to_decision(sig, ctx.bar_t)))
        return out

    def decide(
        self,
        ctx: MarketContext,
        response: Response | None,
        positions: dict[str, int] | None = None,
    ) -> Decision:
        """Legacy OR-combine into a single Decision (conflicts become hold)."""
        return self._stamp(self._combine(ctx, self._collect_signals(ctx, response, positions)))

    def note_fill(self, decision: Decision) -> None:
        """First ORB breakout of the day is spent once a buy/sell is taken.

        Price-action and MTF setup / session IDs are also spent so a 1 s tick
        cannot re-enter. M15 day keys go into ``traded_days``.
        """
        if decision.action not in ("buy", "sell"):
            return
        feat = decision.features or {}
        if decision.strategy == "orb_london":
            day = feat.get("day")
            if day:
                self.orb_broken_days.add(str(day))
        for key in ("setup_id", "session_id"):
            val = feat.get(key)
            if val:
                self.spent_setup_ids.add(str(val))
        day = feat.get("day")
        if day:
            self.traded_days.add(str(day))

    def _combine(self, ctx: MarketContext, signals: list[Signal]) -> Decision:
        if not signals:
            return Decision(ctx.bar_t, "hold", 0.0, 0.0, {}, "hold: no strategy enabled", {})

        # Exit wins when any strategy says exit (consensus); priority picks among exits.
        exits = [s for s in signals if s.action == "exit"]
        if exits:
            return to_decision(min(exits, key=_priority_key), ctx.bar_t)

        buys = [s for s in signals if s.action == "buy"]
        sells = [s for s in signals if s.action == "sell"]
        # Order-independent: any buy + any sell among entries → hold (conflict).
        if buys and sells:
            names = ", ".join(sorted({s.strategy for s in buys + sells}))
            return Decision(
                ctx.bar_t, "hold", 0.0, 0.0, {"conflict": True},
                f"hold: conflict ({names})",
                features={"buys": [s.strategy for s in buys], "sells": [s.strategy for s in sells]},
            )
        if buys:
            return to_decision(min(buys, key=_priority_key), ctx.bar_t)
        if sells:
            return to_decision(min(sells, key=_priority_key), ctx.bar_t)

        # Prefer a reason that names a blocked setup over a bare hold.
        interesting = [
            s for s in signals
            if any(tok in s.reason for tok in ("blocked", "cost", "unsettled", "sl window", "spent", "veto"))
        ]
        pick = min(interesting, key=_priority_key) if interesting else min(signals, key=_priority_key)
        return to_decision(pick, ctx.bar_t)


def build_context(*, now, bar_t, mid, bid, ask, point, digits, position, semantic,
                  times, mids, buy_vol, sell_vol, limits, max_hold_seconds,
                  avoid=None, **kwargs) -> MarketContext:
    minutes = closed_minutes(times, mids, buy_vol, sell_vol)
    return MarketContext(
        now=now, bar_t=bar_t, mid=mid, bid=bid, ask=ask, point=point, digits=digits,
        position=position, semantic=semantic, mids=list(mids), volumes=[b + s for b, s in zip(buy_vol, sell_vol)],
        times=list(times), buy_vol=list(buy_vol), sell_vol=list(sell_vol),
        limits=limits, max_hold_seconds=max_hold_seconds, minutes=minutes, avoid=avoid,
        **kwargs,
    )
