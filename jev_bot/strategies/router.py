"""OR-combine enabled strategies and batch their TypeSafe questions."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..questions import Decision, Policy
from ..typesafe import Question, Response
from . import (
    _pa_common as pa,
    consensus,
    orb_london,
    pa_insidebar,
    pa_rejection,
    pa_structure,
    session_dir,
    vwap_fade,
)
from .common import MarketContext, Signal, to_decision
from .minutes import closed_minutes


@dataclass
class StrategyFlags:
    consensus: bool = True
    vwap_fade: bool = False
    orb: bool = False
    session_dir: bool = False
    pa_rejection: bool = False
    pa_insidebar: bool = False
    pa_structure: bool = False
    orb_zone: str = "Europe/London"


@dataclass
class StrategyRouter:
    flags: StrategyFlags = field(default_factory=StrategyFlags)
    policy: Policy = field(default_factory=Policy)
    orb_broken_days: set[str] = field(default_factory=set)
    spent_setup_ids: set[str] = field(default_factory=set)

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
        return names

    def collect_questions(self, ctx: MarketContext) -> dict[str, Question]:
        """Only the strategies whose code precondition is true contribute questions."""
        qs: dict[str, Question] = {}
        if self.flags.consensus and consensus.precondition(ctx):
            qs.update(consensus.questions())
        if self.flags.vwap_fade and vwap_fade.precondition(ctx):
            qs.update(vwap_fade.questions())
        if self.flags.orb and orb_london.precondition(
            ctx, zone_name=self.flags.orb_zone, broken_days=self.orb_broken_days
        ):
            qs.update(orb_london.questions())
        pa_ready = False
        if self.flags.pa_rejection and pa_rejection.precondition(ctx, spent=self.spent_setup_ids):
            pin = pa_rejection.find_setup(ctx, spent=self.spent_setup_ids)
            if pin is not None:
                qs.update(pa_rejection.questions_for_side(pin.side))
                qs.update(pa.liquidity_questions())
                pa_ready = True
        if self.flags.pa_insidebar and pa_insidebar.precondition(ctx, spent=self.spent_setup_ids):
            setup = pa_insidebar.find_setup(ctx, spent=self.spent_setup_ids)
            if setup is not None:
                qs.update(pa_insidebar.questions_for_side(setup.side))
                qs.update(pa.liquidity_questions())
                pa_ready = True
        if self.flags.pa_structure and pa_ready and pa_structure.precondition(ctx):
            qs.update(pa_structure.questions())
        return qs

    def state_for_request(self, ctx: MarketContext, questions: dict[str, Question]) -> dict:
        """Merge the semantic state with strategy-specific named fields."""
        if self.flags.consensus:
            state = consensus.state_fields(ctx)
        else:
            from ..questions import state_for_model
            state = state_for_model(ctx.semantic)
        if any(k.startswith("vwap_") for k in questions):
            state.update(vwap_fade.state_fields(ctx))
        if any(k.startswith("orb_") for k in questions):
            state.update(orb_london.state_fields(ctx, zone_name=self.flags.orb_zone))
        if any(k.startswith("pa_rej_") for k in questions):
            state.update(pa_rejection.state_fields(ctx, spent=self.spent_setup_ids))
        if any(k.startswith("pa_ib_") for k in questions):
            state.update(pa_insidebar.state_fields(ctx, spent=self.spent_setup_ids))
        if any(k.startswith("pa_struct_") for k in questions):
            state.update(pa_structure.state_fields(ctx))
        return state

    def decide(self, ctx: MarketContext, response: Response | None) -> Decision:
        signals: list[Signal] = []
        if self.flags.consensus:
            if response is None:
                signals.append(Signal("hold", "consensus: no response", "consensus"))
            else:
                signals.append(consensus.decide(ctx, response, self.policy))
        if self.flags.vwap_fade:
            if vwap_fade.precondition(ctx):
                if response is None:
                    signals.append(Signal("hold", "vwap_fade: no response", "vwap_fade"))
                else:
                    signals.append(vwap_fade.decide(ctx, response, self.policy))
            else:
                feat = vwap_fade.compute(ctx) or {}
                reason = "vwap_fade: precondition false"
                if feat and not feat.get("cost_ok"):
                    reason = "vwap_fade: cost gate blocked"
                signals.append(Signal("hold", reason, "vwap_fade", features=feat))
        if self.flags.orb:
            if orb_london.precondition(
                ctx, zone_name=self.flags.orb_zone, broken_days=self.orb_broken_days
            ):
                if response is None:
                    signals.append(Signal("hold", "orb_london: no response", "orb_london"))
                else:
                    signals.append(orb_london.decide(
                        ctx, response, self.policy,
                        zone_name=self.flags.orb_zone,
                        broken_days=self.orb_broken_days,
                    ))
            else:
                signals.append(Signal("hold", "orb_london: precondition false", "orb_london"))

        if self.flags.pa_rejection:
            if pa_rejection.precondition(ctx, spent=self.spent_setup_ids):
                if response is None:
                    signals.append(Signal("hold", "pa_rejection: no response", "pa_rejection"))
                else:
                    signals.append(pa_rejection.decide(
                        ctx, response, self.policy, spent=self.spent_setup_ids,
                    ))
            else:
                signals.append(Signal(
                    "hold", "pa_rejection: precondition false", "pa_rejection",
                ))
        if self.flags.pa_insidebar:
            if pa_insidebar.precondition(ctx, spent=self.spent_setup_ids):
                if response is None:
                    signals.append(Signal("hold", "pa_insidebar: no response", "pa_insidebar"))
                else:
                    signals.append(pa_insidebar.decide(
                        ctx, response, self.policy, spent=self.spent_setup_ids,
                    ))
            else:
                signals.append(Signal(
                    "hold", "pa_insidebar: precondition false", "pa_insidebar",
                ))

        if self.flags.pa_structure:
            signals = [
                pa_structure.filter_signal(ctx, s, response, self.policy) for s in signals
            ]
        if self.flags.session_dir:
            signals = [session_dir.filter_signal(ctx, s) for s in signals]

        return self._combine(ctx, signals)

    def note_fill(self, decision: Decision) -> None:
        """First ORB breakout of the day is spent once a buy/sell is taken.

        Price-action setup IDs are also spent so a 1 s tick cannot re-enter.
        """
        if decision.strategy == "orb_london" and decision.action in ("buy", "sell"):
            day = (decision.features or {}).get("day")
            if day:
                self.orb_broken_days.add(str(day))
        if decision.action in ("buy", "sell"):
            setup_id = (decision.features or {}).get("setup_id")
            if setup_id:
                self.spent_setup_ids.add(str(setup_id))

    def _combine(self, ctx: MarketContext, signals: list[Signal]) -> Decision:
        if not signals:
            return Decision(ctx.bar_t, "hold", 0.0, 0.0, {}, "hold: no strategy enabled", {})

        # Exit wins when any strategy says exit (consensus).
        exits = [s for s in signals if s.action == "exit"]
        if exits:
            return to_decision(exits[0], ctx.bar_t)

        buys = [s for s in signals if s.action == "buy"]
        sells = [s for s in signals if s.action == "sell"]
        if buys and sells:
            names = ", ".join(sorted({s.strategy for s in buys + sells}))
            return Decision(
                ctx.bar_t, "hold", 0.0, 0.0, {"conflict": True},
                f"hold: conflict ({names})",
                features={"buys": [s.strategy for s in buys], "sells": [s.strategy for s in sells]},
            )
        if buys:
            return to_decision(buys[0], ctx.bar_t)
        if sells:
            return to_decision(sells[0], ctx.bar_t)

        # Prefer a reason that names a blocked setup over a bare hold.
        interesting = [
            s for s in signals
            if any(tok in s.reason for tok in ("blocked", "cost", "unsettled", "sl window", "spent", "veto"))
        ]
        pick = interesting[0] if interesting else signals[0]
        return to_decision(pick, ctx.bar_t)


def build_context(*, now, bar_t, mid, bid, ask, point, digits, position, semantic,
                  times, mids, buy_vol, sell_vol, limits, max_hold_seconds,
                  avoid=None) -> MarketContext:
    minutes = closed_minutes(times, mids, buy_vol, sell_vol)
    return MarketContext(
        now=now, bar_t=bar_t, mid=mid, bid=bid, ask=ask, point=point, digits=digits,
        position=position, semantic=semantic, mids=list(mids), volumes=[b + s for b, s in zip(buy_vol, sell_vol)],
        times=list(times), buy_vol=list(buy_vol), sell_vol=list(sell_vol),
        limits=limits, max_hold_seconds=max_hold_seconds, minutes=minutes, avoid=avoid,
    )
