# Jev Trading Bot

An order-flow trading bot whose decisions are made by **Jev**, TypeSafe's
*System One* model: a model that doesn't write text, but returns typed,
calibrated answers (a choice, a score, a yes/no probability) in ~100 ms.

The split follows Jev's own documentation. **Code does the math, and Jev
does the judgment.** A quant engine reads the limit order book second by
second and measures what the market is doing (order-flow imbalance,
micro-price, VPIN, a fair-value model, the shape of the forming candle). The
bot turns those numbers into a plain-language market state, asks Jev 12 atomic
questions about it in one parallel call, and only trades when the answers are
confident, agree with each other, and agree with the flow.

![replay](docs/replay.png)

*The replay dashboard: price against the fair value, the book, every typed
answer from every Jev call, the decision gates and the exact state Jev was
sent. `jev-bot demo` writes it as one self-contained HTML file.*

---

## What it does

```
limit order book (1 s snapshots) + signed trade volume
        │
        ▼
quant engine                    order-flow imbalance (Cont, Kukanov & Stoikov 2014)
  jev_bot/features.py           micro-price, queue and depth imbalance
  jev_bot/quant.py              VPIN (Easley, López de Prado & O'Hara 2012), Kyle's λ
                                fair value: online RLS on all of the above, walk-forward
                                shape of the forming candle (incl. Lévy area of the path)
        │
        ▼
state builder                   numbers → named buckets Jev reads well
  jev_bot/state.py              "heavy buying", "bid queue much thicker than the ask",
                                "price is below where the order flow says it should be"
        │
        ▼
Jev  POST /v1/systemone         12 typed questions, one call, evaluated in parallel
  jev_bot/questions.py          regime · flow_control · informed_flow · price_lagging_flow
  jev_bot/typesafe.py           book_pressure · candle_close · liquidity_stress · setup_quality
                                action ×3 (self-consistency) · exit_now (speculative)
        │
        ▼
decision                        composite score (weights in code)
  jev_bot/decide.py             confidence-gated sizing: full / half / none
                                all three action answers must agree
        │
        ▼
execution + grading             fill 1 s later at the far side of the book, plus fees
  jev_bot/engine.py             vs 1,000 random traders with the same trade count and hold times
```

### Every Jev feature, and where it's used

| Jev / TypeSafe feature | Where |
|---|---|
| **Choice** (options + probabilities + confidence) | `regime`, `flow_control`, `book_pressure`, `candle_close`, three `action` questions |
| **Score** (probability-weighted level + confidence) | `liquidity_stress` (3 levels), `setup_quality` (5 levels) |
| **Noul** (yes/no probability, with `true`/`false` criteria) | `informed_flow`, `price_lagging_flow`, `exit_now` |
| **Parallel questions in one call** | all 12 per decision tick; latency barely moves with question count |
| **Speculative fan-out** | `exit_now` is asked even when flat and only read when a position is open |
| **Structured instructions** (object + backtick field references) | `price_lagging_flow`, `action_alt`, every question points at a named state field |
| **Structured JSON state** | `state.py` builds nested JSON with only the fields the questions need |
| **Confidence-gated routing** | `decide.py`: < 0.35 no trade, 0.35–0.65 half size, ≥ 0.65 full size |
| **Composite scoring** | `decide.composite_score`: weighted sum of atomic answers, weights in code |
| **Self-consistency** | the action is asked three ways; all three must agree |
| **Model aliases / pinning** | `--model jev-latest` or pin `--model jev-1.13.0`; the answering version is logged |
| **`GET /v1/models`** | `jev-bot models` |
| **Usage and cost** | input tokens × $0.042/Mtok tracked per run, p50 latency reported |
| **Retries on 429 / 529** | exponential backoff honouring `retry-after` |
| **Replayable runs** | `--cache run.jsonl` records every call; `--replay-cache` replays with no key |

And the Jev 1.13 [jaggedness notes](https://docs.typesafe.ai/model-jaggedness/jev-1.13) are
designed in: no arithmetic or counting is asked of the model, numbers arrive
as named buckets, each question is literal and single-hop, and the state
carries only what the questions need.

---

## Quick start

```bash
git clone <this repo> && cd jev-trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

jev-bot demo                     # simulate a 6 h session, trade it, write runs/demo/
open runs/demo/replay.html       # the animated replay
```

Without a key the bot uses an **offline surrogate** (`jev_bot/surrogate.py`),
a small rule model that answers the same questions with the same typed
shapes so everything runs, tests and renders. It is *not* Jev.

With Jev:

```bash
export TYPESAFE_API_KEY=...      # https://console.typesafe.ai/keys
jev-bot models                   # which models your key can use
jev-bot ask examples/state.json  # one call, all 12 typed answers
jev-bot --model jev-latest --cache runs/jev.jsonl demo
jev-bot --replay-cache runs/jev.jsonl --model jev-latest demo   # replay it, no key needed
jev-bot --model jev-latest paper --minutes 30 -v                # real-time paper loop
```

A 6-hour session at one call every 10 s is 1,980 calls. The request (state
plus 12 questions) is about 5.2k characters, roughly 1,300 input tokens, so a
session is ~2.6 M tokens, **about $0.11** at the published $0.042 per million.
Output is free. The exact count is in the report (`API usage`).

Your own data: `jev-bot backtest --csv your.csv`. The CSV needs
`t, bid, ask, bid_sz0, ask_sz0` and either `buy_vol, sell_vol` (aggressor-signed)
or `volume` (split with bulk volume classification). Extra depth columns
`bid_sz1.. / ask_sz1..` are used when present. `jev-bot simulate out.csv`
writes an example.

---

## Results on the simulator (surrogate model)

`jev-bot demo`, 6 h at 1 s resolution, a decision every 10 s, 100 units per
full position, fees on both sides:

| seed | trades | net PnL | hit rate | beats random traders | flags → 2σ move | median flag lead |
|---|---|---|---|---|---|---|
| 1 | 55 | $484 | 53% | 100% | 9 / 25 | 206 s |
| 2 | 74 | $206 | 41% | 100% | 5 / 24 | 115 s |
| 3 | 59 | $461 | 49% | 100% | 12 / 30 | 260 s |
| 4 | 28 | $97 | 57% | 100% | 3 / 9 | 75 s |
| 5 | 46 | $346 | 57% | 100% | 7 / 18 | 164 s |

Reading the forming candle 20–50 s into the bar, the surrogate's
`candle_close` was right 84–91% of the times it committed (about 70% of
ticks), against 76–84% for "the candle keeps its current colour".

**Read these numbers for what they are.** The simulator (`jev_bot/sim.py`)
is an agent model *built to contain* the effect the bot hunts: informed
traders know a hidden fair value, and the market maker absorbs their flow with
a lag. The results show that the pipeline finds a real order-flow edge when
one exists, fills honestly and doesn't look ahead (there are tests for that).
They say nothing about any real market, and they were produced by the
surrogate, not by Jev. Run it against Jev and your own data before believing
anything.

---

## Layout

```
jev_bot/
  typesafe.py    Jev client: Noul / Choice / Score, answers, retries, cost, cache, /v1/models
  questions.py   the 12 questions
  state.py       numbers → semantic, structured state
  surrogate.py   offline stand-in with the same typed interface
  decide.py      composite score, confidence gates, self-consistency
  features.py    OFI, micro-price, imbalances, VPIN, Kyle's λ, candle shape
  quant.py       walk-forward fair-value model (RLS) and candle model (online logistic)
  signals.py     divergence flags
  engine.py      the session loop, fills, grading
  sim.py         synthetic order book with a hidden fair value
  replay.py      builds the animated HTML dashboard (assets/dashboard.html)
  cli.py         jev-bot demo | backtest | paper | ask | models | simulate
tests/           20 tests: math, causality, client against a fake API, gates, end to end
docs/MATH.md     the formulas and the papers they come from
```

```bash
python -m pytest          # or: python -m unittest
```

---

## Disclaimer

Research and education code. Paper trading only; there is no exchange
connectivity and none is planned here. Nothing in this repository is financial
advice. Jev and TypeSafe are products of TypeSafe AI; this project is not
affiliated with them.
