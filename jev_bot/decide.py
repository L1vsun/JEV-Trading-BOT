"""From Jev's typed answers to an order.

Three documented TypeSafe patterns do the work, all in code:

* **composite scoring**: atomic answers are combined with weights we own;
* **confidence-gated routing**: act at full size only on high confidence,
  half size on medium, not at all on low;
* **self-consistency**: the action is asked three ways and all three must
  agree before anything is sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .typesafe import Response


@dataclass
class Policy:
    min_action_conf: float = 0.35      # below this the answer is "I don't know" -> no trade
    full_size_conf: float = 0.65
    min_lagging: float = 0.55          # Noul: price lagging the flow
    max_stress: float = 1.4            # Score 0..2
    min_setup: float = 1.6             # Score 0..4
    min_composite: float = 0.30
    exit_noul: float = 0.65
    weights: dict = field(default_factory=lambda: {
        "action": 0.35, "flow": 0.2, "book": 0.15, "candle": 0.1, "regime": 0.1, "lag": 0.1})


@dataclass
class Decision:
    t: int
    action: str                  # buy | sell | hold | exit
    size: float                  # 0, 0.5 or 1
    composite: float             # signed conviction, -1..1
    gates: dict[str, bool]
    reason: str
    answers: dict = field(default_factory=dict)


def composite_score(r: Response, w: dict) -> float:
    acts = [r[k] for k in ("action", "action_alt", "action_contrarian_check") if k in r.answers]
    a = sum(x.p("buy") - x.p("sell") for x in acts) / max(1, len(acts))
    flow = r["flow_control"].p("buyers") - r["flow_control"].p("sellers")
    book = r["book_pressure"].p("up") - r["book_pressure"].p("down")
    candle = r["candle_close"].p("up") - r["candle_close"].p("down")
    regime = r["regime"].p("trending_up") - r["regime"].p("trending_down")
    lag = (r["price_lagging_flow"].noul or 0) * (1 if a >= 0 else -1)
    return (w["action"] * a + w["flow"] * flow + w["book"] * book + w["candle"] * candle
            + w["regime"] * regime + w["lag"] * lag)


def decide(t: int, r: Response, position: int, p: Policy | None = None) -> Decision:
    p = p or Policy()
    ans = {k: v.to_json() for k, v in r.answers.items()}
    if position:
        ex = r["exit_now"].noul or 0
        if ex >= p.exit_noul:
            return Decision(t, "exit", 0, 0, {"exit_now": True}, f"exit_now {ex:.2f}", ans)
    comp = composite_score(r, p.weights)
    picks = [r[k].choice for k in ("action", "action_alt", "action_contrarian_check")]
    conf = min(r[k].confidence or 0 for k in ("action", "action_alt", "action_contrarian_check"))
    side = picks[0]
    gates = {
        "consistent": len(set(picks)) == 1 and side != "hold",
        "confident": conf >= p.min_action_conf,
        "lagging": (r["price_lagging_flow"].noul or 0) >= p.min_lagging,
        "calm_enough": (r["liquidity_stress"].score or 0) <= p.max_stress,
        "setup": (r["setup_quality"].score or 0) >= p.min_setup,
        "composite": abs(comp) >= p.min_composite and (comp > 0) == (side == "buy"),
    }
    if not all(gates.values()):
        failed = ",".join(k for k, v in gates.items() if not v)
        return Decision(t, "hold", 0, comp, gates, f"blocked: {failed}", ans)
    if position and (position > 0) == (side == "buy"):
        return Decision(t, "hold", 0, comp, gates, "already positioned", ans)
    size = 1.0 if conf >= p.full_size_conf else 0.5
    return Decision(t, side, size, comp, gates, f"{side} conf {conf:.2f} composite {comp:+.2f}", ans)
