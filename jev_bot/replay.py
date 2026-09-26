"""Animated replay: writes a single self-contained HTML file (no network, no
dependencies) that plays back a window of the session: price vs Jev's fair
value, the order book, every typed answer from every Jev call, the decision
gates and the exact state Jev was sent."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .book import Market
from .engine import Result
from .features import ewm
from .questions import decision_questions

TEMPLATE = Path(__file__).parent / "assets" / "dashboard.html"


def best_window(res: Result, length: int = 900) -> tuple[int, int]:
    """Centre the replay on the most profitable trade (or the first flag)."""
    if res.trades:
        tr = max(res.trades, key=lambda x: x.pnl)
        a = max(0, tr.t_in - int(length * 0.45))
    elif res.flags:
        a = max(0, res.flags[0].t - length // 3)
    else:
        a = max(0, len(res.equity) // 2 - length // 2)
    return a, min(len(res.equity), a + length)


def build(m: Market, res: Result, out: str | Path, window: tuple[int, int] | None = None, speed: float = 30.0) -> Path:
    a, b = window or best_window(res)
    r = res.raw
    pos = np.zeros(len(m), dtype=int)
    for tr in res.trades:
        pos[tr.t_in:tr.t_out] = tr.side
    rnd = lambda x, d=4: [round(float(v), d) for v in x]
    decs = [d for d in res.decisions if a <= d.t < b]
    data = {
        "model": res.model, "surrogate": res.model.startswith("surrogate"), "speed": speed, "t0": int(m.t[a]) + 9 * 3600 + 30 * 60,
        "tick": m.tick, "mid": rnd(r["mid"][a:b]), "bid": rnd(m.bid[a:b]), "ask": rnd(m.ask[a:b]), "fair": rnd(ewm(res.fair, 10)[a:b]),
        "bsz": m.bid_sz[a:b].astype(int).tolist(), "asz": m.ask_sz[a:b].astype(int).tolist(), "vpin": rnd(r["vpin"][a:b], 3),
        "pos": pos[a:b].tolist(), "equity": rnd(res.equity[a:b], 2),
        "questions": [{"id": k, "type": q.to_json()["type"]} for k, q in decision_questions().items()],
        "call0": sum(1 for d in res.decisions if d.t < a),
        "latency_p50": (res.usage or {}).get("p50_latency_ms"),
        "decisions": [{"i": d.t - a, "action": d.action, "size": d.size, "composite": round(d.composite, 3), "reason": d.reason,
                       "gates": d.gates, "state": d.answers.get("_state"),
                       "answers": {k: v for k, v in d.answers.items() if not k.startswith("_")}} for d in decs],
        "trades": [{"i_in": t.t_in - a, "i_out": t.t_out - a, "px_in": t.px_in, "px_out": t.px_out, "side": t.side,
                    "qty": t.qty, "pnl": round(t.pnl, 2)} for t in res.trades if a <= t.t_in < b],
        "flags": [{"i": f.t - a, "side": f.side} for f in res.flags if a <= f.t < b],
    }
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out
