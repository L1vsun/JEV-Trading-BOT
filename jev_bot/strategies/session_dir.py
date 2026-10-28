"""Session-direction filter: pure code, no TypeSafe call.

Flag ``--filter-session-dir`` (default off). Keeps only entries that match
the sign of the return over the first 30 minutes of the current gold session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..risk import require_new_york
from .common import MarketContext, Signal
from .minutes import MinuteBar

NAME = "session_dir"
OPEN_MINUTES = 30


def _session_open_local(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny = require_new_york()
    local = now.astimezone(ny)
    open_at = local.replace(hour=17, minute=5, second=0, microsecond=0)
    if local < open_at:
        open_at = open_at - timedelta(days=1)
    while open_at.weekday() == 5:
        open_at = open_at - timedelta(days=1)
    return open_at


def opening_return(minutes: list[MinuteBar], now: datetime) -> dict | None:
    """Sign of the first-30-minute return of the current gold session."""
    if not minutes:
        return None
    open_local = _session_open_local(now)
    open_utc = int(open_local.astimezone(timezone.utc).timestamp())
    end_utc = open_utc + OPEN_MINUTES * 60
    bars = [m for m in minutes if open_utc <= m.t < end_utc]
    if len(bars) < 5:
        return None
    start = bars[0].open
    end = bars[-1].close
    if start <= 0:
        return None
    ret = (end - start) / start
    direction = 1 if ret > 0 else -1 if ret < 0 else 0
    return {
        "return": ret,
        "direction": direction,
        "open": start,
        "close_30m": end,
        "session_open": open_utc,
    }


def compute(ctx: MarketContext) -> dict | None:
    return opening_return(ctx.minutes, ctx.now)


def allows(ctx: MarketContext, action: str) -> tuple[bool, dict]:
    """True when ``action`` matches the opening-drive direction."""
    feat = compute(ctx)
    if feat is None:
        return False, {"session_dir": "unknown"}
    if feat["direction"] == 0:
        return False, feat
    if action == "buy":
        return feat["direction"] > 0, feat
    if action == "sell":
        return feat["direction"] < 0, feat
    return True, feat


def filter_signal(ctx: MarketContext, signal: Signal) -> Signal:
    if signal.action not in ("buy", "sell"):
        return signal
    ok, feat = allows(ctx, signal.action)
    features = dict(signal.features)
    features["session_dir"] = {
        k: (round(v, 6) if isinstance(v, float) else v) for k, v in feat.items()
    } if isinstance(feat, dict) else feat
    if ok:
        return Signal(
            action=signal.action, size=signal.size,
            reason=signal.reason + f" | {NAME}: aligned",
            strategy=signal.strategy, features=features,
            sl_points=signal.sl_points, tp_points=signal.tp_points,
            max_hold_seconds=signal.max_hold_seconds,
            composite=signal.composite, answers=signal.answers,
        )
    return Signal(
        action="hold",
        reason=f"{signal.strategy}: blocked by {NAME}",
        strategy=signal.strategy,
        features=features,
        answers=signal.answers,
        composite=signal.composite,
    )
