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

This repository is **live-only** on MetaTrader 5. There is no `--shadow`, no
dry-run / paper / demo / backtest / simulate CLI path, and no test suite in
this tree (tests come later under a Ludwig order). Orders are always sent when
`live` runs.

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
strategy router                 each enabled strategy votes for its own magic
  jev_bot/strategies/           each strategy owns its Nouls + truth table
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

`jev-bot live` always sends orders. There is no `--dry-run`, no `--shadow`,
and no `--send`. You must pass `--allow-real` and set
`MT5_CONFIRM_REAL=YES_TRADE_REAL` for **both demo and real** accounts. Enable
at least one strategy (`--strategies` or a `--sig-*` flag); consensus defaults
**off**.

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

`--lot-mode` defaults to `equity-scaled` (flags `--lot-base` 0.01,
`--lot-start-equity` 1000, `--lot-factor` 1.5). Equity is taken from
`account_info().equity` as MT5 reports it (no cent-account reinterpretation).

Stage `n` begins at `start_equity * factor**n` with raw lot
`base * factor**n`, then **always floored** to the symbol `volume_step`
(never below `volume_min`), then capped by `--max-lots`. With the usual
0.01 step there are **no intermediate sizes** such as 0.015: stages 0 and 1
both deliver 0.01; stage 2 delivers 0.02; stage 3 → 0.03; stage 4 → 0.05
(then the max-lots cap). Equity below `start_equity`, or unreadable equity,
stays on 0.01 (never a larger guess). The session recomputes once per UTC
day and when flat before an entry; it does **not** change mid-position.
Startup logs the stage table (through ~20 000 equity) and the current lot.

| stage | equity from | raw lot | lot after 0.01 floor / max-lots 0.05 |
|---|---|---|---|
| 0 | &lt;1000 and from 1000 | 0.01 | 0.01 |
| 1 | 1500 | 0.015 | 0.01 |
| 2 | 2250 | 0.0225 | 0.02 |
| 3 | 3375 | 0.03375 | 0.03 |
| 4 | 5062.5 | 0.050625 | 0.05 |
| 5+ | 7593.75… | … | 0.05 (capped) |

`--lot-mode fixed` uses `--lot` (default `0.01`). In `equity-scaled` mode
`--lot` is **ignored**. Volume is snapped down to the broker step. Entries
wider than `--max-spread` (points, default 30) are skipped. Filling mode
follows the symbol. `--deviation` (default 15) is slippage in points. Buys
pay the ask, sells hit the bid.

`--sl-points` / `--tp-points` are the global **M1 / 1-s** distances (defaults
200 / 400). `0` leaves that side unset. They do **not** cap M5–H4 stops.
Higher-TF strategies set
`SL = max(k_sl × ATR14(TF), min-SL(TF), stops_level + spread)` with defaults
`k_sl` M5/M15 1.5, H1 2.0 (trend-long 2.5), H4 2.5 (2.0–3.0 band) and min-SL
M1 200 / M5 300 / M15 500 / H1 1000 / H4 2000 points (override with
`--min-sl-*`). TP is usually `2 × SL` (H4 Donchian has no fixed TP; trailing).
Large H1/H4 stops are **not** blocked by a daily-loss / Tagesbudget cap
(there is none). They are gated only by the **cost share** and by a single-order
**equity plausibility** check (loss at SL for that new order must not exceed
current account equity) — for example a 0.02 lot on a small account can still
be held. There is no sum-of-open-risk vs remaining day budget. Live orders
send those SL/TP prices on the position. `--max-hold-seconds` defaults to 900;
`0` disables it.

`--avoid-hours` is off unless you set it. `13:00-17:00` blocks new entries from
13:00 inclusive to 17:00 exclusive in Europe/Berlin. Positions already open are
left alone: exits, stops, the hold limit and the kill file still apply.

### Strategies

Every strategy defaults **off**. Enable with `--strategies a,b,c` (comma list
or groups `m5` / `m15` / `h1` / `h4` / `pullback`) and/or `--sig-*` flags.
`h4_zone_retest` is not in the `h4` group — name it explicitly.
`--allow-short` opens short paths on MTF modules that support them (including
H1/H4 unified pullbacks). `--h4-ema-allow-short` and `--h1-sma50-200` flip
those strategy-specific modes.

#### Multi-magic positions

Each entry strategy has a fixed MT5 magic: `--magic` base (default
`20261027`; consensus keeps offset `0`) plus the offset in
`jev_bot/strategies/magics.py`. Override with `--magic-offset-json`. Live uses
`decide_all`: every strategy that votes buy/sell/exit acts on **its own**
magic. There is no "first signal wins" and no cross-strategy long/short →
hold. Conflicting sides are allowed and logged; `--no-hedge` (default off)
blocks opening against an existing bot side. Caps: **1 position per
strategy/magic**, and `--max-positions` (default 5) across all bot magics.
`--max-lots` caps the **sum** of open bot lots plus the new order. Exits,
SL/TP and hold timers are per magic; the kill file flattens every bot magic.
Foreign magics on the account are never touched. Order comments look like
`jev:<strategy>`; logs include `magic=` and `strategy=` on each order/exit.

MTF setups evaluate **closed** signal-TF candles only. Live asks Jev when a
ready strategy contributes questions; pass `--cache FILE` to record answers
and reuse identical request bodies.

| flag / name | role |
|---|---|
| `--sig-consensus` / `consensus` | 14-Noul consensus table (exit Nouls only when that magic is open) |
| `--sig-vwap-fade` / `vwap_fade` | session VWAP fade; **two** Jev Nouls per side must both be yes |
| `--sig-orb` / `orb` | London ORB; **two** Jev Nouls per side must both be yes |
| `--filter-session-dir` / `session_dir` | keep entries aligned with the gold session's first 30 minutes |
| `--sig-pa-rejection` / `pa_rejection` | M5 pin-bar rejection at a zone |
| `--sig-pa-insidebar` / `pa_insidebar` | M5 inside-bar breakout in mother direction |
| `--sig-pa-structure` / `pa_structure` | M5 structure filter for PA only (never alone) |
| `m5_ny_orb` | M5 NY ORB (09:30–10:00 NY); one trade/session; time stop 21:00 UTC |
| `m5_asianbox` | M5 Asian-box breakout (box 00–07 UTC, trade 07–10 UTC) |
| `m5_vwap_pb` | M5 session-VWAP pullback continuation (12:30–16:30 UTC) |
| `m15_ny_orb` | M15 NY ORB (first four M15 after 09:30 NY) |
| `m15_asia_squeeze` | M15 Asia-range squeeze breakout (18:00–03:00 NY) |
| `m15_trend_pullback` | legacy alias of unified M15 EMA pullback |
| `h1_trend_long` | H1 EMA9/20 (or SMA50/200) cross **long only**; gold bull bias |
| `h1_pullback` | legacy alias of unified H1 EMA pullback |
| `h1_prevday_break` | H1 first break of prior NY calendar day high/low |
| `h4_donchian` | H4 Donchian-20 breakout; long-biased when EMA50>EMA200 |
| `h4_ema_pullback` | legacy alias of unified H4 EMA pullback |
| `h4_zone_retest` | H4 fractal-zone first retest (experiment; not in `h4` group) |
| `pullback_m1`…`pullback_h4` | unified EMA pullbacks (`--sig-pullback-m1`…`h4` or group `pullback`) |
| groups `m5` / `m15` / `h1` / `h4` | enable the three named modules per TF (`h4` omits zone retest) |

#### Mehr-Zeitrahmen / M5–H4

Public Beleglage (not this repo's live log): the
[fxabsolute XAUUSD study](https://fxabsolute.com/research/xauusd-strategy-backtest)
(20 setups, 39,947 trades, XAUUSD 2021–2026) reports median cost per R of
**M5 15.5%**, **M15 10.6%**, **H1 5.5%**, with Pearson correlation to
expectancy about **−0.861**. After multiple-testing correction, **no strategy
was significant**. Candle backtests (e.g. fxbacktest.app) systematically
**overestimate** edge versus tick/cost-aware paths.

All M5–H4 modules here are **experiments, default off**, and **not proven
profitable after costs** on XAUUSD. Gold's multi-year bull market biases
trend-following longs (`h1_trend_long`); in those studies buy-and-hold beat
active rules. Compare any enablement against a random-entry control before
sizing up.

Intentionally **omitted** (negatively evidenced or unproven for this cost
regime): EMA 9/21 cross on M5, RSI(2) pullback, Larry Williams breakout,
ICT/SMC (FVG, order block, CHoCH, BOS, Silver Bullet), range mean-reversion
with RSI, Engulfing, break-and-retest.

Higher-TF books come from `MultiTFBooks`, backfilled with
`copy_rates_from_pos` when the broker exposes it. `--backfill-bars` defaults
to M1:500, M5:500, M15:400, H1:300, H4:300 (one int for every TF, or
`TF:count` pairs). Empty books → those strategies hold.

| TF | min SL (pts) | default `k_sl` |
|---|---:|---:|
| M1 | 200 | 1.2 (pullback / 1-s use global 200) |
| M5 | 300 | 1.5 |
| M15 | 500 | 1.5 |
| H1 | 1000 | 2.0 (`h1_trend_long` 2.5) |
| H4 | 2000 | 2.5 |

Override floors with `--min-sl-m5` / `--min-sl-m15` / `--min-sl-h1` /
`--min-sl-h4` only if you understand the cost. The global `--sl-points 200`
cap is **M1-only**. H1/H4 entries may still fail the **cost gate** or the
single-order **equity plausibility** check (loss-at-SL > equity → hold).
`--max-daily-loss` and any day-budget risk sum were removed.

`--hold-over-weekend` keeps bot positions across the gold-session close /
weekend instead of flattening. `--hold-override NAME:SECONDS` lets a named
strategy's hold exceed `--max-hold-seconds` (MTF modules already use session
ends or `max_bars × TF` as explicit overrides when sizing the trade).

#### Unified pullbacks (`pullback_m1`…`pullback_h4`)

Shared logic in `jev_bot/strategies/_pullback_common.py`: higher-TF EMA trend
filter, EMA20 (EMA50 on H4) touch ±0.25 ATR with a flat 38–62% impulse
retracement, continuation close, ADX14≥20 or efficiency ratio≥0.3, SL
`max(1.2×ATR beyond extreme, min SL)`, TP 2R, and TF time stops. Jev keys are
`pb_{tf}_pb1`…`pb5` long/short (entry PB1–3 yes, PB4–5 no; exit on code
structure break or PB4 yes). M1–M15 trade both ways; H1/H4 shorts need
`--allow-short`. Legacy names `m15_trend_pullback`, `h1_pullback`, and
`h4_ema_pullback` are thin aliases of the same template.

Classic EMA/impulse pullbacks are often **negative after costs** on M5–H1 for
XAUUSD-style spreads (same fxabsolute / cost-per-R picture). The H4 EMA50
variant is **untested** here — treat it as an experiment gated by cost share
and equity plausibility only (no daily-loss / Tagesbudget stop).

There is also **no proven edge after costs** for VWAP fade, London ORB,
session-dir, or the price-action rules on XAUUSD.

#### Decision paths (no silent offline Jev)

There is no `jev_bot/surrogate.py` and no `--model surrogate` / offline
fallback. Missing `TYPESAFE_API_KEY` or an unreachable Jev API at live start
aborts before any order. Mid-session Jev failures hold that bar; SL/TP, hold
time, and the kill file still run. Entries always need Jev answers for the
enabled strategy; pure code only applies gates (spread, cost, equity
plausibility, session filters) and exits (broker SL/TP, max hold, kill,
weekend flatten).

Live uses `decide_all` (one Decision per strategy / magic; conflicts logged).
The legacy single-Decision `decide` helper only picks one vote by
`STRATEGY_PRIORITY` and is not used for live fills. VWAP fade and London ORB
each require **two** independent yes Nouls after the code precondition; a
single yes does not fire. Consensus exit questions are not asked when that
magic is flat.

### Limits

| control | default | effect |
|---|---|---|
| `--lot-mode` | `equity-scaled` | or `fixed` with `--lot` |
| `--lot-base` / `--lot-start-equity` / `--lot-factor` | `0.01` / `1000` / `1.5` | equity stage table |
| `--max-lots` | `0.05` | hard cap on **sum** of open bot lots + new order |
| `--max-positions` | `5` | across all bot magics (also max 1 per strategy/magic) |
| `--no-hedge` | off | block opening a side opposite any open bot position |
| *(no `--max-daily-loss`)* | — | removed; only single-order equity plausibility remains |
| `--max-spread` | `30` | skip a new entry when the spread is wider, in points |
| `--sl-points` | `200` | global M1 stop; `0` leaves it unset |
| `--tp-points` | `400` | global take-profit; `0` leaves it unset |
| `--min-sl-m5` / `m15` / `h1` / `h4` | 300 / 500 / 1000 / 2000 | MTF stop floors in points |
| `--deviation` | `15` | slippage allowance in points |
| `--max-hold-seconds` | `900` | close after this many seconds; `0` disables |
| `--hold-override NAME:SECONDS` | off | per-strategy hold above the global cap |
| `--hold-over-weekend` | off | keep positions across session close / weekend |
| `--backfill-bars` | M1:500,… | higher-TF history depth (int or `TF:count` pairs) |
| `--allow-short` | off | short paths on MTF modules that support them |
| `--avoid-hours` | off | `HH:MM-HH:MM` Europe/Berlin; blocks new entries only |
| `--hours gold` | on | XAUUSD session on America/New_York; `--hours always` disables |
| `--allow-real` + `MT5_CONFIRM_REAL` | required | demo and real; no silent send |
| `jev.KILL` | `--kill-file` | closes the bot's positions and stops |
| a dead terminal, or ~30 s without a tick while open | | close, then stop |
| Jev API errors | | hold that bar; process stays up |

Closes use the bot's magic family (`--magic` base, default `20261027`, plus
per-strategy offsets in `jev_bot/strategies/magics.py`) on the configured
symbol. Other positions on the account are left alone. Override offsets with
`--magic-offset-json '{"vwap_fade":1}'` when needed.

| strategy | offset | strategy | offset |
|---|---:|---|---:|
| `consensus` | 0 | `m15_ny_orb` | 20 |
| `vwap_fade` | 1 | `m15_asia_squeeze` | 21 |
| `orb_london` | 2 | `m15_trend_pullback` | 22 |
| `session_dir` (filter) | 3 | `h1_trend_long` | 30 |
| `pa_rejection` | 4 | `h1_pullback` | 31 |
| `pa_insidebar` | 5 | `h1_prevday_break` | 32 |
| `pa_structure` (filter) | 6 | `h4_donchian` | 40 |
| `m5_ny_orb` | 10 | `h4_ema_pullback` | 41 |
| `m5_asianbox` | 11 | `h4_zone_retest` | 42 |
| `m5_vwap_pb` | 12 | `pullback_m1` | 50 |
| | | `pullback_m5` | 51 |
| | | `pullback_m15` | 52 |
| | | `pullback_h1` | 53 |
| | | `pullback_h4` | 54 |

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
