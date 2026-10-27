# The math

Everything below is computed in code, causally (a value at second *t* uses
data up to *t* only). Jev never does arithmetic; it gets the results as named
buckets.

## Order book

**Imbalance-weighted mid** (first-order micro-price). With best bid/ask `Pb, Pa`
and queues `Qb, Qa`:

    wmid = (Pb·Qa + Pa·Qb) / (Qa + Qb)

It leans toward the thinner side, which is where the next tick is more likely.
Stoikov, *The micro-price: a high-frequency estimator of future prices*, Quantitative Finance 18(12), 2018.

**Queue imbalance** `(Qb − Qa)/(Qb + Qa)` and **depth imbalance** over five
levels with geometric weights 0.6ᵏ. Gould & Bonart, *Queue imbalance as a
one-tick-ahead price predictor in a limit order book*, 2016.

**Order-flow imbalance** per interval:

    e_t = 1{Pb_t ≥ Pb_{t−1}}·Qb_t − 1{Pb_t ≤ Pb_{t−1}}·Qb_{t−1}
        − 1{Pa_t ≤ Pa_{t−1}}·Qa_t + 1{Pa_t ≥ Pa_{t−1}}·Qa_{t−1}

summed over 15, 60 and 180 s and scaled by average depth × √window.
Cont, Kukanov & Stoikov, *The price impact of order book events*, Journal of Financial Econometrics 12(1), 2014.

## Trades

**Trade imbalance** `(B − S)/(B + S)` of aggressor volume over 30 and 120 s.

**VPIN**: volume is poured into equal-volume buckets (≈ one minute of volume,
sized on the first 5 minutes only); VPIN is the mean `|B − S| / V` over the last
50 buckets. When only unsigned volume is available it is split by **bulk
volume classification**, buy share = Φ(ΔP / σ_ΔP). Easley, López de Prado &
O'Hara, *Flow toxicity and liquidity in a high-frequency world*, Review of
Financial Studies 25(5), 2012.

**Kyle's λ**: rolling OLS slope of mid changes on net signed volume. Kyle,
*Continuous auctions and insider trading*, Econometrica 53(6), 1985.

## The forming candle

For every second inside a 1-minute bar, the partial path from the open is
described by: return so far and range (both in units of σ·√60), position
inside the range, wick asymmetry, up-tick share, and the **Lévy area** of the
normalised path (x = elapsed fraction, y = return):

    A = ½ Σ (x_{i−1}·y_i − y_{i−1}·x_i)

Signed by the direction of the move, A > 0 means the move is arriving late and
still building, and A < 0 means it came early and stalled. This is the level-2
term of the path signature; see Lyons, *Rough paths, signatures and the
modelling of functions on streams*, 2014.

## Fair value: "what the market should be doing"

`quant.FairValueModel` predicts the mid's move over the next 180 s, in units of
realised volatility, from all features above (standardised with running
Welford means). The model is recursive least squares with forgetting factor
λ = 0.99995 and a ridge prior. A label for second *s* is used for training
only at *s* + 180, when it has been observed, so the prediction at *t* never
contains information from after *t* (`tests/test_bot.py::Quant`).

    fair_t = mid_t + ŷ_t · σ_t · √180

`fair_value_gap` in the state is ŷ bucketed at 0.25 / 0.6 / 1.1.

## Decision

    composite = 0.35·(P(buy) − P(sell))_avg over 3 action questions
              + 0.20·(P(buyers) − P(sellers))
              + 0.15·(P(book up) − P(book down))
              + 0.10·(P(candle up) − P(candle down))
              + 0.10·(P(trending up) − P(trending down))
              + 0.10·P(price lagging flow)·sign

A trade needs all gates: the three action answers agree, every action
confidence ≥ 0.35, `price_lagging_flow` ≥ 0.55, `liquidity_stress` ≤ 1.4,
`setup_quality` ≥ 1.6, and |composite| ≥ 0.30 on the same side. The size is
full at confidence ≥ 0.65 and half otherwise. A position is closed when
`exit_now` ≥ 0.65 or after 15 minutes.

Confidence, as TypeSafe documents it for k options, is `(k·p_max − 1)/(k − 1)`.

## Live MT5 book

`jev-bot live` builds the same one-second `Market` the formulas above already
expect. Bid and ask are the last quote in each second. Trade volume is the
tick volume, signed by the MT5 tick flags (buy/sell), then by the last price
against the quote, then by the tick rule. When `market_book_get` returns both
sides, those sizes fill `bid_sz` / `ask_sz`. With no depth the second is a
one-level book of equal placeholder size, so queue imbalance, depth imbalance
and the micro-price go to zero and the order-flow imbalance keeps only the
sign of quote changes. Trade imbalance, VPIN, Kyle's λ, spread, volatility,
the candle and the fair-value regression still run; the regression's book
inputs are flat for as long as that placeholder is in the window.

## Grading

- Fills one second after the decision at the far side of the book, fee 0.0005 per unit per side.
- **Random traders**: 1,000 traders who make the same number of trades with the
  same sizes and holding times, at random entry times and random sides.
  The report gives the share of them the bot beats.
- **Flags**: the fair-value prediction above 1σ on one side for 10 s. The flag
  counts as followed if a ≥ 2σ(10 min) move in that direction happens within
  10 minutes. The lead is the time from the flag until half of that move is done.
