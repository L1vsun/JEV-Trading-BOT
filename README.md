# Jev Trading Bot

An order-flow trading bot whose decisions are made by **Jev**, TypeSafe's
*System One* model: a model that doesn't write text, but returns typed,
calibrated answers (a choice, a score, a yes/no probability) in ~100 ms.

The split follows Jev's own documentation. **Code does the math, and Jev
does the judgment.** A quant engine reads the limit order book second by
second and measures what the market is doing (order-flow imbalance,
micro-price, VPIN, a fair-value model, the shape of the forming candle). The
bot turns those numbers into a plain-language market state, asks Jev atomic
questions about it in one parallel call, and only trades when the answers are
confident, agree with each other, and agree with the flow.

This repository is **live-only** on MetaTrader 5. There is no dry-run, paper,
demo, backtest or simulate CLI path. Orders are always sent when `live` runs.

---

## What it does

```
limit order book (1 s snapshots) + signed trade volume
        │
        ▼
quant engine                    order-flow imbalance, micro-price, VPIN, Kyle's λ
  jev_bot/features.py           fair value (online RLS), forming-candle shape
  jev_bot/quant.py
        │
        ▼
state builder                   numbers → named buckets Jev reads well
  jev_bot/state.py
        │
        ▼
strategy router                 optional M1 / PA / M5–H4 setups OR'd together
  jev_bot/strategies/           each enabled strategy owns its Nouls + truth table
        │
        ▼
Jev  POST /v1/systemone         typed answers, one call per decision tick
        │
        ▼
execution                       MT5 order_send with hard risk limits
  jev_bot/live.py · mt5_broker.py · risk.py
```

### Every Jev feature, and where it's used

| Jev / TypeSafe feature | Where |
|---|---|
| **Noul** (yes/no probability, with `true`/`false` criteria) | consensus and each optional strategy's own questions |
| **Parallel questions in one call** | all ready strategies share one request |
| **Named state fields** | every question points at one field with backticks |
| **Truth table** | yes ≥ 0.7, no ≤ 0.3, mid or missing = hold |
| **Model aliases / pinning** | `--model jev-latest` or pin `--model jev-1.13.0` |
| **`GET /v1/models`** | `jev-bot models` |
| **Retries on 429 / 529** | exponential backoff honouring `retry-after` |

And the Jev 1.13 [jaggedness notes](https://docs.typesafe.ai/model-jaggedness/jev-1.13) are
designed in: no arithmetic or counting is asked of the model, numbers arrive
as named buckets, each question is literal and single-hop, and the state
carries only what the questions need.

---

## Quick start

```bash
git clone <this repo> && cd jev-trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[mt5]"          # on the Windows machine next to the terminal

export TYPESAFE_API_KEY=...      # https://console.typesafe.ai/keys  (required for live)
export MT5_CONFIRM_REAL=YES_TRADE_REAL

jev-bot models                   # which models your key can use
jev-bot ask examples/state.json  # one call, typed answers
jev-bot live --broker mt5 --allow-real --strategies consensus
```

### Which model decides

`live`, `ask`, and `models` require `TYPESAFE_API_KEY`. There is no offline
surrogate. Live calls `POST /v1/systemone`, model `jev-latest` unless you pin
one with `--model jev-1.13.0`. Missing key or unreachable Jev at startup aborts
before any order. The key is read only from the environment and is never
written to the repo, the log, or an error message.

The client times out, retries with backoff, and honours `retry-after` on HTTP
429 and 529. If the API fails mid-session after those retries, that bar is a
hold: nothing new is opened and the process stays up (SL/TP, hold time, and
the kill file still run). The failed call is not stored as a Noul of zero.

---

## Live trading (MetaTrader 5)

`jev-bot live` always sends orders. There is no `--dry-run` and no `--send`.
You must pass `--allow-real` and set `MT5_CONFIRM_REAL=YES_TRADE_REAL` for
**both demo and real** accounts. Enable at least one strategy
(`--strategies` or a `--sig-*` flag); consensus defaults **off**.

```bash
pip install -e ".[mt5]"          # on that Windows machine, next to the terminal
export TYPESAFE_API_KEY=...
export MT5_CONFIRM_REAL=YES_TRADE_REAL
jev-bot live --broker mt5 --allow-real --strategies consensus
```

### Terminal setup

1. Install MetaTrader 5 and open a RoboForex account in the terminal.
2. Turn on **Algo Trading** (Tools → Options → Expert Advisors, and the AutoTrading button in the toolbar).
3. Show the gold symbol in Market Watch. The name is often `XAUUSD`, and on RoboForex it is often `XAUUSDc`, `XAUUSD.c` or `XAUUSDm`. `--symbol XAUUSD` uses the exact name when the terminal has it, and otherwise tries those suffixes.
4. Leave the terminal logged in. Login, password and server are read from `MT5_LOGIN`, `MT5_PASSWORD` and `MT5_SERVER` only when you want the process to attach by itself. They are never written into the repo or into the log. With the three `MT5_*` variables unset, the bot attaches to the terminal that is already logged in.

### What is sent

Orders always go through `order_send`. Demo, contest and real-money accounts
(including cent accounts) all need both the flag and the confirmation phrase:

```bash
export MT5_CONFIRM_REAL=YES_TRADE_REAL
jev-bot live --broker mt5 --allow-real --strategies consensus
jev-bot live --broker mt5 --allow-real --strategies m5 --allow-short
jev-bot live --broker mt5 --allow-real --sig-consensus --avoid-hours 13:00-17:00
```

### Lot sizing

`--lot-mode` defaults to `equity-scaled`. Stage `n` begins at
`start_equity * factor**n` and uses raw lot `base * factor**n`, floored to the
broker volume step and capped by `--max-lots`. Equity below `start_equity` (or
unreadable) stays on the base lot. The session recomputes once per UTC day and
on entry; it does **not** change mid-position. Startup logs a short stage table
and the current lot.

| stage | equity from (default) | raw lot | typical floored lot |
|---|---|---|---|
| 0 | 1000 | 0.01 | 0.01 |
| 1 | 1500 | 0.015 | 0.01 |
| 2 | 2250 | 0.0225 | 0.02 |
| 3 | 3375 | 0.03375 | 0.03 |
| 4 | 5062.5 | 0.050625 | 0.05 (cap) |

`--lot-mode fixed` uses `--lot` (default `0.01`) instead. Volume is snapped
down to the broker step. Entries wider than `--max-spread` (points, default 30)
are skipped. Filling mode follows the symbol. `--deviation` (default 15) is
slippage in points. Buys pay the ask, sells hit the bid.

`--sl-points` / `--tp-points` are the global M1 distances (defaults 200 / 400).
`0` leaves that side unset. Higher-TF strategies compute their own stops from
ATR and per-TF minimums, then clamp so they can only be tighter than the global
limits when those globals are set. Live orders send stop loss and take profit
on the position. `--max-hold-seconds` defaults to 900; `0` disables it.

`--avoid-hours` is off unless you set it. `13:00-17:00` blocks new entries from
13:00 inclusive to 17:00 exclusive in Europe/Berlin. Positions already open are
left alone: exits, stops, the hold limit and the kill file still apply.

### Strategies

Every strategy defaults **off**. Enable them with `--strategies a,b,c` (comma
list or groups `m5` / `m15` / `h1` / `h4` / `pullback`) and/or the existing
`--sig-*` flags. `h4_zone_retest` is not in the `h4` group and must be named
explicitly. `--allow-short` opens short paths on MTF modules that support them
(H1/H4 unified pullbacks included). `--h4-ema-allow-short` and
`--h1-sma50-200` flip those strategy-specific modes.

| flag / name | role |
|---|---|
| `--sig-consensus` / `consensus` | 14-Noul consensus table |
| `--sig-vwap-fade` / `vwap_fade` | session VWAP fade with a cost gate |
| `--sig-orb` / `orb` | London opening-range breakout |
| `--filter-session-dir` / `session_dir` | keep entries aligned with the gold session's first 30 minutes |
| `--sig-pa-rejection` / `pa_rejection` | M5 pin-bar rejection at a zone |
| `--sig-pa-insidebar` / `pa_insidebar` | M5 inside-bar breakout in mother direction |
| `--sig-pa-structure` / `pa_structure` | M5 structure filter for PA only (never alone) |
| `m5` / `m5_ny_orb`, `m5_asianbox`, `m5_vwap_pb` | M5 ORB, Asian box, VWAP pullback |
| `m15` / `m15_ny_orb`, `m15_asia_squeeze`, `m15_trend_pullback` | M15 setups |
| `h1` / `h1_trend_long`, `h1_pullback`, `h1_prevday_break` | H1 setups |
| `h4` / `h4_donchian`, `h4_ema_pullback` | H4 setups (`h4_zone_retest` separate) |
| `pullback` / `pullback_m1`…`pullback_h4` | unified EMA pullbacks (`--sig-pullback-m1`…`h4`) |

#### Unified pullbacks (`pullback_m1`…`pullback_h4`)

Shared logic in `jev_bot/strategies/_pullback_common.py`: higher-TF EMA trend
filter, EMA20 (EMA50 on H4) touch ±0.25 ATR with a flat 38–62% impulse
retracement, continuation close, ADX14≥20 or efficiency ratio≥0.3, SL
`max(1.2×ATR beyond extreme, min SL)`, TP 2R, and TF time stops. Jev keys are
`pb_{tf}_pb1`…`pb5` long/short (entry PB1–3 yes, PB4–5 no; exit on code
structure break or PB4 yes). M1–M15 trade both ways; H1/H4 shorts need
`--allow-short`. Legacy names `m15_trend_pullback`, `h1_pullback`, and
`h4_ema_pullback` are thin aliases of the same template.

**Honest note:** classic EMA/impulse pullbacks are often **negative after
costs** on M5–H1 for XAUUSD-style spreads (see public write-ups in the
fxabsolute / YuRa vein). The H4 EMA50 pullback variant here is **untested** in
this repo — treat it as an experiment behind a tight daily-loss cap.

Higher-TF strategies read closed candles from `MultiTFBooks`, backfilled via
`copy_rates_from_pos` when the broker exposes it (`--backfill-bars` defaults
to M1:500, M5:500, M15:400, H1:300, H4:300). If backfill is unavailable the books stay
empty and those strategies hold.

There is **no proven edge after costs** for VWAP fade, London ORB, session-dir,
the price-action rules, or the M5–H4 experiment modules on XAUUSD. Compare
against a random-entry control before scaling size. **H1/H4 minimum stops are
large** (defaults 1000 / 2000 points); they are no longer blocked by a daily
budget, but may still fail the cost gate or the equity plausibility check
(loss at SL for one new order would exceed current account equity). Override
floors with `--min-sl-m5` / `--min-sl-m15` / `--min-sl-h1` / `--min-sl-h4` only
if you understand the cost.

### Limits

| control | default | effect |
|---|---|---|
| `--lot-mode` | `equity-scaled` | or `fixed` with `--lot` |
| `--lot-base` / `--lot-start-equity` / `--lot-factor` | `0.01` / `1000` / `1.5` | equity stage table |
| `--max-lots` | `0.05` | hard cap on order and position size |
| `--max-positions` | `1` | no new entry at the cap; closes are still sent |
| `--max-spread` | `30` | skip a new entry when the spread is wider, in points |
| `--sl-points` | `200` | global M1 stop; `0` leaves it unset |
| `--tp-points` | `400` | global take-profit; `0` leaves it unset |
| `--deviation` | `15` | slippage allowance in points |
| `--max-hold-seconds` | `900` | close after this many seconds; `0` disables |
| `--avoid-hours` | off | `HH:MM-HH:MM` Europe/Berlin; blocks new entries only |
| `--hours gold` | on | XAUUSD session on America/New_York; `--hours always` disables |
| `--allow-real` + `MT5_CONFIRM_REAL` | required | demo and real; no silent send |
| `jev.KILL` | `--kill-file` | closes the bot's positions and stops |
| a dead terminal, or ~30 s without a tick while open | | close, then stop |
| Jev API errors | | hold that bar; process stays up |

Closes use the bot's magic number (`--magic`, default `20261027`) on the
configured symbol. Other positions on the account are left alone.

### Depth, and the signals that go quiet

Each second is one row: last bid, last ask, and tick volume signed by the MT5
tick flags. When `market_book_get` returns both a bid and an ask, those levels
fill the book. Otherwise the second is a level-1 fallback. Queue imbalance,
depth imbalance and micro-price are degraded on that fallback. Aggressor tape,
VPIN, Kyle's λ, spread, realised volatility, the forming candle and fair value
still use bid, ask and signed tick volume.

MT5 tick times are the broker server clock, not UTC. The session measures that
offset, shifts bars to UTC, and asks for history in the broker clock. The gold
session stays on real UTC.

Gold spreads and commissions are wide relative to the move this bot is built
to catch. They can consume the edge entirely.

---

## Layout

```
jev_bot/
  typesafe.py    Jev client: Noul / Choice / Score, answers, retries, cost, cache
  questions.py   consensus Nouls and truth table
  state.py       numbers → semantic state
  features.py    OFI, micro-price, imbalances, VPIN, Kyle's λ, candle shape
  quant.py       walk-forward fair-value model
  live.py        always-send live session on a broker feed (multi-magic)
  mt5_feed.py    one-second bars, tick-flag aggressor, level-1 fallback
  mt5_broker.py  MetaTrader 5 terminal: quotes, depth, rates, order_send, equity
  risk.py        confirm gate, equity lot scaling, spread/cost limits, session hours
  bars.py        multi-timeframe OHLC builders and backfill defaults
  indicators.py  ATR, EMA, RSI, ADX, Donchian, VWAP, session clocks
  strategies/    optional M1 / PA / M5–H4 / pullback strategies, magics table
  cli.py         jev-bot live | ask | models
docs/MATH.md     formulas and papers
```

---

## Disclaimer

Research and education code. `jev-bot live` always sends orders. Demo and
real-money accounts both need `--allow-real` and `MT5_CONFIRM_REAL`. Gold
spreads and fees can erase any edge. Nothing in this repository is financial
advice. Jev and TypeSafe are products of TypeSafe AI; this project is not
affiliated with them or with RoboForex.
