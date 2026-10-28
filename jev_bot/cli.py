"""Command line: ``jev-bot <command>``.

    jev-bot live --broker mt5 --allow-real   send real orders on a local MT5 terminal
    jev-bot ask state.json                   send one state to Jev and print the typed answers
    jev-bot models                           list the models your key can use

Live always sends orders. It requires ``--allow-real``, ``MT5_CONFIRM_REAL`` set to
the confirmation phrase, and ``TYPESAFE_API_KEY``. There is no dry-run, paper,
demo, backtest or simulate path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .bars import DEFAULT_BACKFILL
from .risk import (
    CONFIRM_PHRASE,
    DEFAULT_LOT_BASE,
    DEFAULT_LOT_FACTOR,
    DEFAULT_LOT_MODE,
    DEFAULT_LOT_START_EQUITY,
    LOT_MODE_EQUITY_SCALED,
    LOT_MODE_FIXED,
    RiskError,
    redact,
)
from .strategies.router import parse_strategy_flags
from .typesafe import JevClient, JevError, ResponseCache


def _require_api_key(for_cmd: str) -> None:
    if not (os.environ.get("TYPESAFE_API_KEY") or "").strip():
        raise JevError(
            f"TYPESAFE_API_KEY is required for {for_cmd}; export it before calling"
        )


def make_jev(args) -> object:
    """Jev client for ask/models. Requires TYPESAFE_API_KEY."""
    _require_api_key("ask/models")
    requested = getattr(args, "model", None)
    if requested == "surrogate":
        raise JevError(
            "--model surrogate was removed; use a Jev model (jev-latest or a pinned version)"
        )
    name = requested or "jev-latest"
    cache = ResponseCache(args.cache or args.replay_cache) if (args.cache or args.replay_cache) else None
    return JevClient(model=name, cache=cache, cache_only=bool(args.replay_cache))


def make_live_jev(args) -> object:
    """Real Jev only. Live refuses a missing key and the removed surrogate."""
    _require_api_key("live")
    requested = getattr(args, "model", None)
    if requested == "surrogate":
        raise RiskError(
            "live refuses --model surrogate; use a Jev model (jev-latest or a pinned version)"
        )
    name = requested or "jev-latest"
    cache = ResponseCache(args.cache) if getattr(args, "cache", None) else None
    return JevClient(model=name, cache=cache, cache_only=False)


def cmd_ask(args) -> None:
    from .questions import decision_questions, state_for_model
    jev = make_jev(args)
    state = json.loads(Path(args.state).read_text())
    r = jev.system_one(state_for_model(state), decision_questions())
    print(f"model {r.model} · {r.input_tokens} input tokens · {r.latency_ms:.0f} ms")
    for k, a in r.answers.items():
        if a.type == "noul":
            print(f"  {k:26} noul   {a.noul:.2f}")
        elif a.type == "choice":
            print(f"  {k:26} choice {a.choice:14} conf {a.confidence:.2f}  {a.probabilities}")
        else:
            print(f"  {k:26} score  {a.score:.2f}  conf {a.confidence:.2f}")


def cmd_models(args) -> None:
    _require_api_key("models")
    for mdl in JevClient().models():
        print(f"{mdl.get('name'):16} {mdl.get('release_date', ''):12} {mdl.get('description', '')}")


def cmd_live(args) -> None:
    """Always-send MT5 loop. Requires --allow-real and MT5_CONFIRM_REAL."""
    from .live import LiveSession, live_config_from_args
    from .mt5_broker import MT5Broker
    from .risk import ConnectionLost, SymbolError, TradingHalted
    from .strategies.magics import all_bot_magics
    try:
        cfg = live_config_from_args(args)
        broker = MT5Broker(
            symbol=cfg.symbol,
            magic=cfg.magic,
            deviation=cfg.limits.deviation_points,
            magics=all_bot_magics(cfg.magic, cfg.magic_offsets),
        )
        LiveSession(broker, make_live_jev(args), cfg).run()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    except TradingHalted as e:
        sys.exit(f"halted: {redact(str(e))}")
    except ConnectionLost as e:
        sys.exit(f"connection lost: {redact(str(e))}")
    except (RiskError, SymbolError) as e:
        sys.exit(f"live: {redact(str(e))}")


def _parse_backfill(text: str | None) -> dict[str, int]:
    """Default DEFAULT_BACKFILL, or one int applied to every higher TF."""
    if text is None or str(text).strip() == "":
        return dict(DEFAULT_BACKFILL)
    raw = str(text).strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        n = int(raw)
        if n < 1:
            raise RiskError("backfill-bars must be a positive integer")
        return {tf: n for tf in DEFAULT_BACKFILL}
    # Optional M5:500,H1:300 form.
    out = dict(DEFAULT_BACKFILL)
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise RiskError("backfill-bars must be an int or TF:count pairs (e.g. M5:500,H1:300)")
        tf, count = part.split(":", 1)
        tf = tf.strip().upper()
        if tf not in DEFAULT_BACKFILL:
            raise RiskError(f"unknown backfill timeframe {tf!r}")
        n = int(count.strip())
        if n < 1:
            raise RiskError("backfill-bars counts must be positive")
        out[tf] = n
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jev-bot",
        description=(
            "Live-only order-flow trading bot driven by Jev (TypeSafe System One). "
            "Commands: live, ask, models."
        ),
    )
    p.add_argument("--model", help="jev-latest | jev-1.13.0 (TYPESAFE_API_KEY required)")
    p.add_argument("--cache", help="record API responses to this JSONL file")
    p.add_argument("--replay-cache", help="answer only from this JSONL cache (ask only)")
    sp = p.add_subparsers(dest="cmd", required=True)

    lv = sp.add_parser(
        "live",
        help="trade a MetaTrader 5 feed; always sends real orders (requires --allow-real)",
    )
    lv.add_argument(
        "--broker", required=True, choices=("mt5",),
        help="mt5 uses the official MetaTrader5 package and a local terminal",
    )
    lv.add_argument(
        "--symbol", default="XAUUSD",
        help="broker symbol; a RoboForex suffix such as c, .c or m is resolved when the plain name is absent",
    )
    lv.add_argument(
        "--allow-real", action="store_true", required=False,
        help=(
            f"required for live on demo and real accounts; also set MT5_CONFIRM_REAL="
            f"{CONFIRM_PHRASE}"
        ),
    )
    lv.add_argument("--step", type=int, default=10, help="seconds between decisions")
    lv.add_argument(
        "--warmup", type=int, default=1800,
        help="seconds of book required before the first decision (at least 300)",
    )
    lv.add_argument("--history", type=int, default=7200, help="seconds kept in the rolling window")
    lv.add_argument(
        "--lot-mode", choices=(LOT_MODE_FIXED, LOT_MODE_EQUITY_SCALED),
        default=DEFAULT_LOT_MODE,
        help="fixed uses --lot; equity-scaled stages lot from account equity (default)",
    )
    lv.add_argument("--lot", type=float, default=0.01, help="full-size lot in fixed mode")
    lv.add_argument("--lot-base", type=float, default=DEFAULT_LOT_BASE, help="base lot for equity scaling")
    lv.add_argument(
        "--lot-start-equity", type=float, default=DEFAULT_LOT_START_EQUITY,
        help="equity at which stage 0 begins",
    )
    lv.add_argument(
        "--lot-factor", type=float, default=DEFAULT_LOT_FACTOR,
        help="equity and lot multiply by this each stage",
    )
    lv.add_argument("--max-lots", type=float, default=0.05,
                    help="cap on sum of open bot lots plus the new order")
    lv.add_argument(
        "--max-positions", type=int, default=5,
        help="max open positions across all bot magics (1 per strategy)",
    )
    lv.add_argument(
        "--no-hedge", action="store_true", default=False,
        help="block opening a side opposite any existing bot position (default off)",
    )
    lv.add_argument(
        "--max-spread", type=float, default=30.0,
        help="skip a new entry when the spread is wider than this, in points",
    )
    lv.add_argument(
        "--sl-points", type=float, default=200.0,
        help="global stop distance in points (M1/consensus); 0 leaves the stop unset",
    )
    lv.add_argument(
        "--tp-points", type=float, default=400.0,
        help="take-profit distance in points; 0 leaves it unset",
    )
    lv.add_argument("--deviation", type=int, default=15, help="slippage allowance in points")
    lv.add_argument(
        "--max-hold-seconds", type=int, default=900,
        help="close an open position after this many seconds; 0 disables it",
    )
    lv.add_argument(
        "--avoid-hours", default=None,
        help="block new entries during HH:MM-HH:MM Europe/Berlin, for example 13:00-17:00; off by default",
    )
    lv.add_argument(
        "--kill-file", default="jev.KILL",
        help="when this file appears, every bot position is closed and the process stops",
    )
    lv.add_argument(
        "--magic", type=int, default=20261027,
        help="base MT5 magic; each strategy adds its offset from strategies/magics.py",
    )
    lv.add_argument(
        "--magic-offset-json", default=None,
        help='optional JSON object overriding strategy→offset, e.g. {"vwap_fade":1}',
    )
    lv.add_argument(
        "--hours", choices=("gold", "always"), default="gold",
        help="gold follows the New York spot session; always disables the filter",
    )
    lv.add_argument("--levels", type=int, default=5, help="depth levels when the terminal publishes a book")
    lv.add_argument(
        "--strategies", default=None,
        help="comma list of strategy or group names (m5,m15,h1,h4,pullback,consensus,…); all default off",
    )
    lv.add_argument(
        "--sig-consensus", dest="sig_consensus", action="store_true", default=False,
        help="enable the 14-Noul consensus truth table (default off)",
    )
    lv.add_argument(
        "--no-sig-consensus", dest="sig_consensus", action="store_false",
        help="turn the consensus strategy off",
    )
    lv.add_argument(
        "--sig-vwap-fade", dest="sig_vwap_fade", action="store_true", default=False,
        help="enable VWAP fade (default off)",
    )
    lv.add_argument(
        "--sig-orb", dest="sig_orb", action="store_true", default=False,
        help="enable London opening-range breakout (default off)",
    )
    lv.add_argument(
        "--filter-session-dir", dest="filter_session_dir", action="store_true", default=False,
        help="keep only entries that match the first 30 minutes of the gold session (default off)",
    )
    lv.add_argument(
        "--sig-pa-rejection", dest="sig_pa_rejection", action="store_true", default=False,
        help="enable M5 pin-bar rejection at a zone (default off)",
    )
    lv.add_argument(
        "--sig-pa-insidebar", dest="sig_pa_insidebar", action="store_true", default=False,
        help="enable M5 inside-bar breakout in mother direction (default off)",
    )
    lv.add_argument(
        "--sig-pa-structure", dest="sig_pa_structure", action="store_true", default=False,
        help="filter PA entries by M5 HH/HL vs LH/LL structure (default off; never fires alone)",
    )
    lv.add_argument(
        "--sig-pullback-m1", dest="sig_pullback_m1", action="store_true", default=False,
        help="enable unified M1 EMA pullback (default off)",
    )
    lv.add_argument(
        "--sig-pullback-m5", dest="sig_pullback_m5", action="store_true", default=False,
        help="enable unified M5 EMA pullback (default off)",
    )
    lv.add_argument(
        "--sig-pullback-m15", dest="sig_pullback_m15", action="store_true", default=False,
        help="enable unified M15 EMA pullback (default off)",
    )
    lv.add_argument(
        "--sig-pullback-h1", dest="sig_pullback_h1", action="store_true", default=False,
        help="enable unified H1 EMA pullback (default off; shorts need --allow-short)",
    )
    lv.add_argument(
        "--sig-pullback-h4", dest="sig_pullback_h4", action="store_true", default=False,
        help="enable unified H4 EMA pullback (default off; shorts need --allow-short)",
    )
    lv.add_argument(
        "--allow-short", action="store_true", default=False,
        help="allow short entries from MTF strategies that support them",
    )
    lv.add_argument(
        "--backfill-bars", default=None,
        help=(
            "higher-TF history count: one int for every TF, or TF:count pairs "
            f"(default {DEFAULT_BACKFILL})"
        ),
    )
    lv.add_argument("--min-sl-m5", type=float, default=None, help="override M5 minimum stop in points")
    lv.add_argument("--min-sl-m15", type=float, default=None, help="override M15 minimum stop in points")
    lv.add_argument("--min-sl-h1", type=float, default=None, help="override H1 minimum stop in points")
    lv.add_argument("--min-sl-h4", type=float, default=None, help="override H4 minimum stop in points")
    lv.add_argument(
        "--h4-ema-allow-short", action="store_true", default=False,
        help="allow shorts on h4_ema_pullback when EMA50 < EMA200",
    )
    lv.add_argument(
        "--h1-sma50-200", action="store_true", default=False,
        help="use SMA50/SMA200 instead of EMA9/EMA20 for h1_trend_long",
    )
    lv.add_argument("-v", "--verbose", action="store_true")
    lv.set_defaults(fn=cmd_live)

    a = sp.add_parser("ask", help="send one state JSON file to Jev")
    a.add_argument("state")
    a.set_defaults(fn=cmd_ask)

    sp.add_parser("models", help="list models your TypeSafe key can use").set_defaults(fn=cmd_models)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if getattr(args, "cmd", None) == "live":
        # Attach parsed backfill onto args for live_config_from_args.
        try:
            args.backfill = _parse_backfill(getattr(args, "backfill_bars", None))
        except RiskError as e:
            sys.exit(f"live: {redact(str(e))}")
        if getattr(args, "strategies", None):
            try:
                args.strategy_flags = parse_strategy_flags(
                    [part.strip() for part in str(args.strategies).split(",") if part.strip()]
                )
            except ValueError as e:
                sys.exit(f"live: {redact(str(e))}")
        else:
            args.strategy_flags = None
    try:
        args.fn(args)
    except JevError as e:
        sys.exit(f"jev: {redact(str(e))}")
    except RiskError as e:
        sys.exit(f"live: {redact(str(e))}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
