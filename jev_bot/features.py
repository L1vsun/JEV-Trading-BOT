"""Microstructure features. Every function is causal: the value at ``t`` uses
only data at or before ``t``.

References are in docs/MATH.md.
"""
from __future__ import annotations

import math

import numpy as np

from .book import Market


# ---------------------------------------------------------------- helpers
def rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    c = np.cumsum(np.concatenate([[0.0], x]))
    out = c[1:] - c[np.maximum(0, np.arange(1, len(x) + 1) - w)]
    return out


def rolling_mean(x: np.ndarray, w: int) -> np.ndarray:
    n = np.minimum(np.arange(1, len(x) + 1), w)
    return rolling_sum(x, w) / n


def rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    m = rolling_mean(x, w)
    m2 = rolling_mean(x * x, w)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def ewm(x: np.ndarray, halflife: float) -> np.ndarray:
    a = 1 - 0.5 ** (1 / halflife)
    out = np.empty_like(x, dtype=float)
    acc = x[0]
    for i, xi in enumerate(x):
        acc = acc + a * (xi - acc)
        out[i] = acc
    return out


# ---------------------------------------------------------------- the book
def weighted_mid(m: Market) -> np.ndarray:
    """Imbalance-weighted mid (first-order micro-price).
    Leans toward the side with the *smaller* queue, which is where the price
    is more likely to move next."""
    qb, qa = m.bid_sz[:, 0], m.ask_sz[:, 0]
    return (m.bid * qa + m.ask * qb) / (qa + qb)


def queue_imbalance(m: Market) -> np.ndarray:
    """(Q_bid - Q_ask) / (Q_bid + Q_ask) at the best level, in [-1, 1]."""
    qb, qa = m.bid_sz[:, 0], m.ask_sz[:, 0]
    return (qb - qa) / (qb + qa)


def depth_imbalance(m: Market, levels: int | None = None, decay: float = 0.6) -> np.ndarray:
    """Depth-weighted imbalance across the first ``levels`` of the book."""
    L = levels or m.bid_sz.shape[1]
    w = decay ** np.arange(L)
    b = (m.bid_sz[:, :L] * w).sum(1)
    a = (m.ask_sz[:, :L] * w).sum(1)
    return (b - a) / (b + a)


def ofi(m: Market) -> np.ndarray:
    """Order-flow imbalance of Cont, Kukanov & Stoikov (2014), per interval.

    e_t = 1{Pb_t >= Pb_{t-1}} qb_t - 1{Pb_t <= Pb_{t-1}} qb_{t-1}
        - 1{Pa_t <= Pa_{t-1}} qa_t + 1{Pa_t >= Pa_{t-1}} qa_{t-1}
    """
    pb, pa = m.bid, m.ask
    qb, qa = m.bid_sz[:, 0], m.ask_sz[:, 0]
    e = np.zeros(len(m))
    e[1:] = ((pb[1:] >= pb[:-1]) * qb[1:] - (pb[1:] <= pb[:-1]) * qb[:-1]
             - (pa[1:] <= pa[:-1]) * qa[1:] + (pa[1:] >= pa[:-1]) * qa[:-1])
    return e


# ---------------------------------------------------------------- trades
def trade_imbalance(m: Market, w: int) -> np.ndarray:
    b, s = rolling_sum(m.buy_vol, w), rolling_sum(m.sell_vol, w)
    return (b - s) / np.maximum(b + s, 1e-9)


def bulk_volume_classification(price: np.ndarray, volume: np.ndarray, w: int = 300) -> tuple[np.ndarray, np.ndarray]:
    """Split unsigned volume into buy/sell with the bulk volume classification
    of Easley, Lopez de Prado & O'Hara (2012): buy share = Phi(dP / sigma)."""
    dp = np.diff(price, prepend=price[0])
    sig = np.maximum(rolling_std(dp, w), 1e-12)
    z = dp / sig
    share = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
    return volume * share, volume * (1 - share)


def vpin(m: Market, bucket: float | None = None, n_buckets: int = 50, calib: int = 300) -> np.ndarray:
    """Volume-synchronised probability of informed trading (Easley, Lopez de
    Prado & O'Hara 2012). Volume is poured into equal-volume buckets; VPIN is
    the mean |buy - sell| / bucket over the last ``n_buckets`` completed
    buckets, carried forward in clock time. In [0, 1].

    The bucket size (about one minute of volume) is calibrated on the first
    ``calib`` seconds only, and VPIN is 0 until then, so no value depends on
    later data."""
    vol = m.buy_vol + m.sell_vol
    calib = min(calib, len(m))
    bucket = bucket or max(1.0, vol[:calib].sum() / max(1, calib) * 60)
    out = np.full(len(m), np.nan)
    imb: list[float] = []
    fill = bb = ss = 0.0
    cur = np.nan
    for i in range(calib, len(m)):
        b, s = m.buy_vol[i], m.sell_vol[i]
        tot = b + s
        while tot > 0:
            take = min(tot, bucket - fill)
            fb = b / tot if tot else 0.5
            bb += take * fb
            ss += take * (1 - fb)
            fill += take
            b -= take * fb
            s -= take * (1 - fb)
            tot -= take
            if fill >= bucket - 1e-9:
                imb.append(abs(bb - ss) / bucket)
                if len(imb) > n_buckets:
                    imb.pop(0)
                cur = float(np.mean(imb))
                fill = bb = ss = 0.0
        out[i] = cur
    return np.nan_to_num(out, nan=0.0)


def kyle_lambda(m: Market, w: int = 300) -> np.ndarray:
    """Rolling OLS slope of mid changes on net signed volume (price impact)."""
    dm = np.diff(m.mid, prepend=m.mid[0])
    q = m.buy_vol - m.sell_vol
    mq = rolling_mean(q, w)
    md = rolling_mean(dm, w)
    cov = rolling_mean(q * dm, w) - mq * md
    var = rolling_mean(q * q, w) - mq * mq
    return cov / np.maximum(var, 1e-12)


def realized_vol(m: Market, w: int = 300) -> np.ndarray:
    """Rolling std of one-interval mid changes (price units)."""
    return rolling_std(np.diff(m.mid, prepend=m.mid[0]), w)


# ---------------------------------------------------------------- the candle
def candle_shape(m: Market, bar: int = 60) -> dict[str, np.ndarray]:
    """Shape of the *forming* candle at every second.

    For each t inside a bar we describe the partial path from the bar open:
    elapsed fraction, return so far, range, where the price sits in the range,
    wick asymmetry, up-tick share and the Levy area of the (time, price) path,
    which is positive when the move happened late and negative when it came
    early. These are what the candle "looks like" before it closes.
    """
    mid = m.mid
    n = len(m)
    keys = ("elapsed", "ret", "range", "pos", "wick", "upticks", "levy")
    out = {k: np.zeros(n) for k in keys}
    sig = np.maximum(realized_vol(m, 600) * math.sqrt(bar), 1e-9)
    for start in range(0, n, bar):
        end = min(n, start + bar)
        o = mid[start]
        hi = lo = o
        area = 0.0
        ups = moves = 0
        prev_x, prev_y = 0.0, 0.0
        for i in range(start, end):
            p = mid[i]
            hi, lo = max(hi, p), min(lo, p)
            if i > start:
                d = p - mid[i - 1]
                if d:
                    moves += 1
                    ups += d > 0
            x = (i - start + 1) / bar
            y = (p - o) / sig[i]
            area += 0.5 * (prev_x * y - prev_y * x)
            prev_x, prev_y = x, y
            r = hi - lo
            out["elapsed"][i] = x
            out["ret"][i] = (p - o) / sig[i]
            out["range"][i] = r / sig[i]
            out["pos"][i] = (p - lo) / r * 2 - 1 if r > 0 else 0.0
            out["wick"][i] = ((hi - max(o, p)) - (min(o, p) - lo)) / sig[i]
            out["upticks"][i] = (ups / moves * 2 - 1) if moves else 0.0
            out["levy"][i] = area
    return out


# ---------------------------------------------------------------- assembly
OFI_WINDOWS = (15, 60, 180)
TRADE_WINDOWS = (30, 120)


def feature_matrix(m: Market) -> tuple[np.ndarray, list[str], dict[str, np.ndarray]]:
    """All Jev features as a matrix (n, k), their names, and a dict of raw series."""
    mid = m.mid
    rv = np.maximum(realized_vol(m, 300), m.tick * 0.05)
    depth = np.maximum(rolling_mean(m.bid_sz[:, 0] + m.ask_sz[:, 0], 300), 1.0)
    e = ofi(m)
    raw: dict[str, np.ndarray] = {
        "mid": mid, "spread": m.ask - m.bid, "rv": rv,
        "wmid_dev": (weighted_mid(m) - mid) / rv,
        "qimb": queue_imbalance(m), "dimb": depth_imbalance(m),
        "ofi": e, "vpin": vpin(m), "lambda": kyle_lambda(m),
    }
    cols, names = [], []
    for w in OFI_WINDOWS:
        x = rolling_sum(e, w) / (depth * math.sqrt(w))
        raw[f"ofi_{w}"] = x
        cols.append(x); names.append(f"ofi_{w}")
    for w in TRADE_WINDOWS:
        x = trade_imbalance(m, w)
        raw[f"timb_{w}"] = x
        cols.append(x); names.append(f"timb_{w}")
    cols += [raw["wmid_dev"], raw["qimb"], raw["dimb"]]
    names += ["wmid_dev", "qimb", "dimb"]
    # toxicity-signed: VPIN only matters with a direction
    tox = raw["vpin"] * np.sign(raw["timb_120"])
    cols.append(tox); names.append("vpin_signed")
    # momentum of the mid over 1 and 5 minutes, in vol units
    for w in (60, 300):
        r = (mid - np.concatenate([np.full(w, mid[0]), mid[:-w]])) / (rv * math.sqrt(w))
        cols.append(r); names.append(f"mom_{w}")
    shape = candle_shape(m)
    for k in ("ret", "pos", "levy", "upticks"):
        cols.append(shape[k] * shape["elapsed"]); names.append(f"candle_{k}")
    raw.update({f"candle_{k}": v for k, v in shape.items()})
    X = np.column_stack(cols)
    return np.nan_to_num(X), names, raw
