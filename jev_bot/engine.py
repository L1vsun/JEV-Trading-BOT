"""The bot: features -> fair value -> state -> Jev -> decision -> fills.

``run()`` walks the session second by second. Every ``step`` seconds it
builds the state from data up to that second, asks Jev every question in one
call, turns the answers into an order, and fills it one second later at the
far side of the book plus fees. Nothing looks ahead: the evaluation block at
the end is the only code that reads the future, and it only grades.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from .book import Market
from .decide import Decision, Policy, decide
from .features import feature_matrix
from .quant import CandleModel, FairValueModel, candle_inputs
from .questions import decision_questions
from .signals import Flag, detect_flags
from .state import StateBuilder
from .typesafe import JevError, SystemOne


@dataclass
class RunConfig:
    step: int = 10              # seconds between Jev calls
    warmup: int = 1800          # let the online models learn first
    horizon: int = 180          # fair-value horizon (s)
    latency: int = 1            # decision -> fill (s); Jev answers in ~0.1 s
    qty: float = 100.0          # units per full-size position
    fee_per_unit: float = 0.0005
    max_hold: int = 900
    n_random: int = 1000        # random-trader benchmark size
    seed: int = 1


@dataclass
class Trade:
    side: int
    qty: float
    t_in: int
    px_in: float
    t_out: int = -1
    px_out: float = float("nan")
    pnl: float = 0.0
    reason_in: str = ""
    reason_out: str = ""


@dataclass
class Result:
    model: str
    trades: list[Trade]
    decisions: list[Decision]
    flags: list[Flag]
    equity: np.ndarray
    fair: np.ndarray
    fair_z: np.ndarray
    candle_p_up: np.ndarray
    raw: dict
    metrics: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)


def _position_text(pos: int, t: int, tr: Trade | None, mid: float) -> str:
    if not pos or tr is None:
        return "flat"
    held = t - tr.t_in
    upnl = (mid - tr.px_in) * pos
    return f"{'long' if pos > 0 else 'short'} for {held // 60} min {held % 60} s, currently {'in profit' if upnl > 0 else 'at a loss' if upnl < 0 else 'flat'}"


def run(m: Market, jev: SystemOne, cfg: RunConfig | None = None, policy: Policy | None = None,
        progress: bool = False) -> Result:
    cfg = cfg or RunConfig()
    n = len(m)
    X, names, raw = feature_matrix(m)
    mid, rv = raw["mid"], raw["rv"]
    fv = FairValueModel(X.shape[1], horizon=cfg.horizon)
    fair_z = fv.run(X, mid, rv, warmup=cfg.warmup // 2)
    fair = mid + fair_z * rv * math.sqrt(cfg.horizon)
    p_up = CandleModel(candle_inputs(raw).shape[1]).run(candle_inputs(raw), mid)
    flags = detect_flags(fair_z)
    sb = StateBuilder(raw, fair_z)
    qs = decision_questions()

    pos, open_tr = 0, None
    trades: list[Trade] = []
    decisions: list[Decision] = []
    pending: tuple[int, Decision] | None = None
    cash = 0.0
    equity = np.zeros(n)
    t0 = time.time()
    ticks = list(range(cfg.warmup, n - cfg.latency - 1, cfg.step))
    tick_set = set(ticks)

    def close(t: int, why: str) -> None:
        nonlocal pos, open_tr, cash
        px = m.bid[t] if pos > 0 else m.ask[t]
        open_tr.t_out, open_tr.px_out, open_tr.reason_out = t, px, why
        open_tr.pnl = (px - open_tr.px_in) * pos * open_tr.qty - 2 * cfg.fee_per_unit * open_tr.qty
        cash += (px - open_tr.px_in) * pos * open_tr.qty - cfg.fee_per_unit * open_tr.qty
        trades.append(open_tr)
        pos, open_tr = 0, None

    for t in range(n):
        # fills from the previous decision
        if pending and pending[0] == t:
            d = pending[1]
            if d.action == "exit" and pos:
                close(t, d.reason)
            elif d.action in ("buy", "sell"):
                side = 1 if d.action == "buy" else -1
                if pos and pos != side:
                    close(t, "reversed")
                if not pos:
                    px = m.ask[t] if side > 0 else m.bid[t]
                    pos = side
                    open_tr = Trade(side, cfg.qty * d.size, t, px, reason_in=d.reason)
                    cash -= cfg.fee_per_unit * open_tr.qty
            pending = None
        if pos and t - open_tr.t_in >= cfg.max_hold:
            close(t, "max hold")
        # decision tick
        if t in tick_set:
            st = sb.state(t, _position_text(pos, t, open_tr, mid[t]))
            try:
                r = jev.system_one(st, qs)
            except JevError as e:
                if progress:
                    print(f"  t={t}: Jev error {e}; holding")
                continue
            d = decide(t, r, pos, policy)
            d.answers["_state"] = st
            decisions.append(d)
            if d.action != "hold":
                pending = (t + cfg.latency, d)
            if progress and len(decisions) % 200 == 0:
                print(f"  {len(decisions)}/{len(ticks)} decisions · {len(trades)} trades · {time.time() - t0:.0f}s")
        equity[t] = cash + (((m.bid[t] if pos > 0 else m.ask[t]) - open_tr.px_in) * pos * open_tr.qty if pos else 0.0)
    if pos:
        close(n - 1, "session end")
        equity[-1] = cash

    res = Result(getattr(jev, "model", "?"), trades, decisions, flags, equity, fair, fair_z, p_up, raw)
    res.metrics = evaluate(m, res, cfg)
    u = getattr(jev, "usage", None)
    if u is not None:
        res.usage = {"calls": u.calls, "input_tokens": u.input_tokens, "cost_usd": round(u.cost_usd, 4),
                     "p50_latency_ms": float(np.median(u.latency_ms)) if u.latency_ms else None}
    return res


# ---------------------------------------------------------------- grading (reads the future)
def evaluate(m: Market, res: Result, cfg: RunConfig) -> dict:
    mid, rv = res.raw["mid"], res.raw["rv"]
    pnl = np.array([t.pnl for t in res.trades])
    eq = res.equity
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    out = {
        "trades": len(pnl), "pnl": float(pnl.sum()), "hit_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "avg_trade": float(pnl.mean()) if len(pnl) else 0.0, "max_drawdown": dd,
        "fees": 2 * cfg.fee_per_unit * sum(t.qty for t in res.trades),
        "decisions": len(res.decisions),
        "holds": sum(d.action == "hold" for d in res.decisions),
    }
    # random traders: same number of trades, same holding times, same sizes, random entries and sides
    rng = np.random.default_rng(cfg.seed)
    if len(res.trades):
        holds = np.array([t.t_out - t.t_in for t in res.trades])
        qtys = np.array([t.qty for t in res.trades])
        lo, hi = cfg.warmup, len(m) - int(holds.max()) - 2
        rand = np.zeros(cfg.n_random)
        for k in range(cfg.n_random):
            ti = rng.integers(lo, hi, len(holds))
            sd = rng.choice((-1, 1), len(holds))
            px_in = np.where(sd > 0, m.ask[ti], m.bid[ti])
            px_out = np.where(sd > 0, m.bid[ti + holds], m.ask[ti + holds])
            rand[k] = np.sum((px_out - px_in) * sd * qtys - 2 * cfg.fee_per_unit * qtys)
        out["random_traders_mean_pnl"] = float(rand.mean())
        out["beats_random_traders_pct"] = float((rand < out["pnl"]).mean() * 100)
    # divergence flags: did a >= 2 sigma move follow in the flagged direction within 10 minutes?
    leads, hits = [], 0
    for f in res.flags:
        end = min(len(m) - 1, f.t + 600)
        path = (mid[f.t:end + 1] - mid[f.t]) * f.side
        sig = rv[f.t] * math.sqrt(600)
        if path.max() >= 2 * sig:
            hits += 1
            leads.append(int(np.argmax(path >= path.max() * 0.5)))
    out["flags"] = len(res.flags)
    out["flags_followed_by_2sigma_move"] = hits
    out["median_lead_to_half_move_s"] = float(np.median(leads)) if leads else None
    # reading the forming candle: Jev vs the quant logistic model, 20-50 s into the bar
    j_ok = j_n = q_ok = q_n = n_ok = 0
    for d in res.decisions:
        el = res.raw["candle_elapsed"][d.t] * 60
        if not 20 <= el <= 50:
            continue
        start = d.t - int(round(el)) + 1
        end = start + 59
        if end >= len(m):
            continue
        up = mid[end] > mid[start]
        c = d.answers.get("candle_close", {}).get("choice")
        if c in ("up", "down"):
            j_n += 1
            j_ok += (c == "up") == up
        q_n += 1
        q_ok += (res.candle_p_up[d.t] > 0.5) == up
        n_ok += (mid[d.t] > mid[start]) == up          # naive: the candle keeps its current colour
    out["candle_read_jev_accuracy"] = j_ok / j_n if j_n else None
    out["candle_read_jev_coverage"] = j_n / q_n if q_n else None
    out["candle_read_quant_accuracy"] = q_ok / q_n if q_n else None
    out["candle_read_naive_accuracy"] = n_ok / q_n if q_n else None
    return out
