"""Command line: ``jev-bot <command>``.

    jev-bot demo                  simulate a session, trade it, write report + replay
    jev-bot backtest --csv f.csv  trade your own snapshot data
    jev-bot paper                 stream a simulated market in real time, decide live
    jev-bot live --broker mt5     same loop on a MetaTrader 5 terminal (dry-run unless --send)
    jev-bot ask state.json        send one state to Jev and print the typed answers
    jev-bot models                list the models your key can use
    jev-bot simulate out.csv      write a synthetic session to CSV

``--model surrogate`` (the default when TYPESAFE_API_KEY is unset) uses the
offline stand-in; ``--model jev-latest`` (or a pinned ``jev-1.13.0``) calls
the real API. ``--cache file.jsonl`` records every call so a run can be
replayed later with ``--replay-cache`` and no key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .book import load_csv
from .engine import RunConfig, run
from .questions import decision_questions
from .sim import SimConfig, simulate
from .surrogate import JevSurrogate
from .typesafe import JevClient, JevError, ResponseCache


def make_jev(args) -> object:
    name = args.model or ("jev-latest" if os.environ.get("TYPESAFE_API_KEY") else "surrogate")
    if name == "surrogate":
        return JevSurrogate()
    cache = ResponseCache(args.cache or args.replay_cache) if (args.cache or args.replay_cache) else None
    return JevClient(model=name, cache=cache, cache_only=bool(args.replay_cache))


def make_live_jev(args) -> object:
    """The real Jev model when ``TYPESAFE_API_KEY`` is set, otherwise the surrogate.

    The key is read from the environment and is not copied into the returned
    object beyond what ``JevClient`` already keeps for the Authorization
    header. This function never puts the key in an exception or a log line.
    A missing key does not raise: live trading falls back to the surrogate.
    """
    requested = getattr(args, "model", None)
    if requested == "surrogate" or not (os.environ.get("TYPESAFE_API_KEY") or "").strip():
        return JevSurrogate()
    return make_jev(args)


def report(res, out_dir: Path) -> str:
    m = res.metrics
    f = lambda v, d=2: "n/a" if v is None else f"{v:.{d}f}"
    lines = [
        f"# Jev Trading Bot · session report", "",
        f"model: `{res.model}`" + ("  (offline surrogate, not the real Jev)" if res.model.startswith("surrogate") else ""), "",
        "| metric | value |", "|---|---|",
        f"| Jev calls | {m['decisions']} ({m['holds']} holds) |",
        f"| trades | {m['trades']} |",
        f"| net PnL (after fees) | ${m['pnl']:.2f} |",
        f"| hit rate | {m['hit_rate'] * 100:.1f}% |",
        f"| max drawdown | ${m['max_drawdown']:.2f} |",
        f"| beats random traders (same trade count and hold times) | {f(m.get('beats_random_traders_pct'), 1)}% of {RunConfig().n_random} |",
        f"| divergence flags / followed by a 2σ move in 10 min | {m['flags']} / {m['flags_followed_by_2sigma_move']} |",
        f"| median time from flag to half the move | {f(m['median_lead_to_half_move_s'], 0)} s |",
        f"| forming-candle read, Jev (when not 'unclear') | {f(m['candle_read_jev_accuracy'] and m['candle_read_jev_accuracy'] * 100, 1)}% on {f(m['candle_read_jev_coverage'] and m['candle_read_jev_coverage'] * 100, 0)}% of ticks |",
        f"| forming-candle read, quant logistic model | {f(m['candle_read_quant_accuracy'] and m['candle_read_quant_accuracy'] * 100, 1)}% |",
        f"| forming-candle read, naive (keeps current colour) | {f(m['candle_read_naive_accuracy'] and m['candle_read_naive_accuracy'] * 100, 1)}% |",
    ]
    if res.usage:
        u = res.usage
        lines.append(f"| API usage | {u['calls']} calls · {u['input_tokens']:,} input tokens · ${u['cost_usd']:.4f} · p50 {f(u['p50_latency_ms'], 0)} ms |")
    lines += ["", "## trades", "", "| # | side | qty | in | out | pnl | entry reason | exit |", "|---|---|---|---|---|---|---|---|"]
    for i, t in enumerate(res.trades, 1):
        lines.append(f"| {i} | {'long' if t.side > 0 else 'short'} | {t.qty:.0f} | {t.px_in:.2f} | {t.px_out:.2f} | {t.pnl:+.2f} | {t.reason_in} | {t.reason_out} |")
    text = "\n".join(lines) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(text)
    with (out_dir / "decisions.jsonl").open("w") as fh:
        for d in res.decisions:
            fh.write(json.dumps({"t": d.t, "action": d.action, "size": d.size, "composite": round(d.composite, 4),
                                 "gates": d.gates, "reason": d.reason, "answers": d.answers}) + "\n")
    return text


def cmd_demo(args) -> None:
    m, _ = simulate(SimConfig(seconds=args.hours * 3600, seed=args.seed))
    _session(m, args)


def cmd_backtest(args) -> None:
    _session(load_csv(args.csv), args)


def _session(m, args) -> None:
    from .replay import build
    jev = make_jev(args)
    print(f"· {len(m):,} seconds of data · model {getattr(jev, 'model', '?')} · a call every {args.step}s")
    res = run(m, jev, RunConfig(step=args.step), progress=True)
    out = Path(args.out)
    print(report(res, out))
    html = build(m, res, out / "replay.html")
    print(f"· report   {out / 'report.md'}\n· log      {out / 'decisions.jsonl'}\n· replay   {html}")


def cmd_paper(args) -> None:
    """Real-time paper loop on a simulated feed: one Jev call every --step seconds."""
    from .decide import decide
    from .features import feature_matrix
    from .quant import FairValueModel
    from .state import StateBuilder
    jev = make_jev(args)
    m, _ = simulate(SimConfig(seconds=args.warmup + args.minutes * 60, seed=args.seed))
    X, _, raw = feature_matrix(m)
    fz = FairValueModel(X.shape[1]).run(X, raw["mid"], raw["rv"], warmup=args.warmup // 2)
    sb, qs = StateBuilder(raw, fz), decision_questions()
    pos, px_in, t_in, pnl = 0, 0.0, 0, 0.0
    print(f"paper trading · model {getattr(jev, 'model', '?')} · {args.minutes} min · Ctrl-C to stop")
    for t in range(args.warmup, len(m) - 1, args.step):
        t0 = time.perf_counter()
        pos_txt = "flat" if not pos else f"{'long' if pos > 0 else 'short'} for {(t - t_in) // 60} min, currently {'in profit' if (raw['mid'][t] - px_in) * pos > 0 else 'at a loss'}"
        r = jev.system_one(sb.state(t, pos_txt), qs)
        d = decide(t, r, pos)
        ms = (time.perf_counter() - t0) * 1000
        if d.action in ("exit", "buy", "sell") and pos and (d.action == "exit" or (d.action == "buy") != (pos > 0)):
            px = m.bid[t + 1] if pos > 0 else m.ask[t + 1]
            pnl += (px - px_in) * pos * 100
            print(f"{t:>6}s  EXIT  @ {px:.2f}   session pnl {pnl:+.2f}")
            pos = 0
        if d.action in ("buy", "sell") and not pos:
            pos = 1 if d.action == "buy" else -1
            px_in, t_in = (m.ask[t + 1] if pos > 0 else m.bid[t + 1]), t
            print(f"{t:>6}s  {d.action.upper():4}  @ {px_in:.2f}   {d.reason}")
        elif args.verbose:
            print(f"{t:>6}s  {d.action:5} {ms:6.0f} ms  {d.reason}")
        time.sleep(max(0.0, args.step / args.speed - (time.perf_counter() - t0)))
    print(f"done · session pnl {pnl:+.2f}")


def cmd_ask(args) -> None:
    jev = make_jev(args)
    state = json.loads(Path(args.state).read_text())
    r = jev.system_one(state, decision_questions())
    print(f"model {r.model} · {r.input_tokens} input tokens · {r.latency_ms:.0f} ms")
    for k, a in r.answers.items():
        if a.type == "noul":
            print(f"  {k:26} noul   {a.noul:.2f}")
        elif a.type == "choice":
            print(f"  {k:26} choice {a.choice:14} conf {a.confidence:.2f}  {a.probabilities}")
        else:
            print(f"  {k:26} score  {a.score:.2f}  conf {a.confidence:.2f}")


def cmd_models(args) -> None:
    for mdl in JevClient().models():
        print(f"{mdl.get('name'):16} {mdl.get('release_date', ''):12} {mdl.get('description', '')}")


def cmd_simulate(args) -> None:
    m, truth = simulate(SimConfig(seconds=args.hours * 3600, seed=args.seed))
    m.save_csv(args.out)
    print(f"wrote {args.out} · {len(m):,} rows · {len(truth.jumps)} hidden news jumps")


def cmd_live(args) -> None:
    """One-second MT5 book, the same features and Jev call as paper, real orders only with --send."""
    from .live import LiveSession, live_config_from_args
    from .mt5_broker import MT5Broker
    from .risk import ConnectionLost, RiskError, SymbolError, TradingHalted, redact
    try:
        cfg = live_config_from_args(args)
        broker = MT5Broker(symbol=cfg.symbol, magic=cfg.magic, deviation=cfg.limits.deviation_points)
        LiveSession(broker, make_live_jev(args), cfg).run()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    except TradingHalted as e:
        sys.exit(f"halted: {redact(str(e))}")
    except ConnectionLost as e:
        sys.exit(f"connection lost: {redact(str(e))}")
    except (RiskError, SymbolError) as e:
        sys.exit(f"live: {redact(str(e))}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jev-bot", description="Order-flow trading bot driven by Jev (TypeSafe System One).")
    p.add_argument("--model", help="surrogate | jev-latest | jev-1.13.0 (default: jev-latest if TYPESAFE_API_KEY is set)")
    p.add_argument("--cache", help="record API responses to this JSONL file")
    p.add_argument("--replay-cache", help="answer only from this JSONL cache (no key needed)")
    sp = p.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("demo"); d.add_argument("--hours", type=int, default=6); d.add_argument("--seed", type=int, default=7)
    d.add_argument("--step", type=int, default=10); d.add_argument("--out", default="runs/demo"); d.set_defaults(fn=cmd_demo)
    b = sp.add_parser("backtest"); b.add_argument("--csv", required=True); b.add_argument("--step", type=int, default=10)
    b.add_argument("--out", default="runs/backtest"); b.set_defaults(fn=cmd_backtest)
    pp = sp.add_parser("paper"); pp.add_argument("--minutes", type=int, default=30); pp.add_argument("--step", type=int, default=5)
    pp.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier"); pp.add_argument("--warmup", type=int, default=1800)
    pp.add_argument("--seed", type=int, default=11); pp.add_argument("-v", "--verbose", action="store_true"); pp.set_defaults(fn=cmd_paper)
    lv = sp.add_parser("live", help="trade a MetaTrader 5 feed; orders are logged unless --send is set")
    lv.add_argument("--broker", required=True, choices=("mt5",), help="mt5 uses the official MetaTrader5 package and a local terminal")
    lv.add_argument("--symbol", default="XAUUSD", help="broker symbol; a RoboForex suffix such as c, .c or m is resolved when the plain name is absent")
    lv.add_argument("--dry-run", action="store_true", help="log orders and do not send them (this is the default)")
    lv.add_argument("--send", action="store_true", help="send orders; a demo or contest account only, unless --allow-real is also set")
    lv.add_argument("--allow-real", action="store_true", help="allow a real-money or cent account; also set MT5_CONFIRM_REAL to the confirmation phrase")
    lv.add_argument("--step", type=int, default=10, help="seconds between decisions")
    lv.add_argument("--warmup", type=int, default=1800, help="seconds of book required before the first decision (at least 300)")
    lv.add_argument("--history", type=int, default=7200, help="seconds kept in the rolling window")
    lv.add_argument("--lot", type=float, default=0.01, help="full-size lot; 0.01 is the usual cent-account minimum")
    lv.add_argument("--max-lots", type=float, default=0.05)
    lv.add_argument("--max-positions", type=int, default=1)
    lv.add_argument("--max-daily-loss", type=float, default=20.0, help="stop for the day, in account currency (cents on a cent account)")
    lv.add_argument("--max-spread", type=float, default=30.0, help="skip a new entry when the spread is wider than this, in points")
    lv.add_argument("--sl-points", type=float, default=200.0, help="stop distance in points; 0 leaves the stop unset")
    lv.add_argument("--tp-points", type=float, default=400.0, help="take-profit distance in points; 0 leaves it unset")
    lv.add_argument("--deviation", type=int, default=15, help="slippage allowance in points")
    lv.add_argument("--max-hold-seconds", type=int, default=900, help="close an open position after this many seconds; 0 disables it")
    lv.add_argument("--avoid-hours", default=None, help="block new entries during HH:MM-HH:MM Europe/Berlin, for example 13:00-17:00; off by default")
    lv.add_argument("--kill-file", default="jev.KILL", help="when this file appears, every bot position is closed and the process stops")
    lv.add_argument("--magic", type=int, default=20261027)
    lv.add_argument("--hours", choices=("gold", "always"), default="gold", help="gold follows the New York spot session; always disables the filter")
    lv.add_argument("--levels", type=int, default=5, help="depth levels to keep when the terminal publishes a book")
    lv.add_argument("-v", "--verbose", action="store_true")
    lv.set_defaults(fn=cmd_live)
    a = sp.add_parser("ask"); a.add_argument("state"); a.set_defaults(fn=cmd_ask)
    sp.add_parser("models").set_defaults(fn=cmd_models)
    s = sp.add_parser("simulate"); s.add_argument("out"); s.add_argument("--hours", type=int, default=6); s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_simulate)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except JevError as e:
        from .risk import redact
        sys.exit(f"jev: {redact(str(e))}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
