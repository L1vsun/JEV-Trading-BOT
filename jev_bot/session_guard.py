"""Trading-session entry guards for the gold (XAUUSD) New York clock.

Uses ``America/New_York`` via :func:`jev_bot.risk.require_new_york` so the
daily pause (16:55–17:05) and the Friday weekly close follow US daylight
saving the same way :func:`jev_bot.risk.gold_session_open` does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .risk import gold_session_open, require_new_york

# Minutes around the daily gold pause where new entries are blocked.
PAUSE_BUFFER_MINUTES = 15
# Friday: no new entries in the last N hours before weekly close (16:55 NY).
FRIDAY_ENTRY_CUTOFF_HOURS = 2
# Friday: flatten bot positions this many minutes before weekly close when
# ``hold_over_weekend`` is False.
FRIDAY_FLATTEN_MINUTES = 30
# Feed hole that forces a full closed signal candle before the next entry.
GAP_ENTRY_SECONDS = 3600

_SUNDAY = 6
_FRIDAY = 4
_SATURDAY = 5

# Gold session minutes-of-day on America/New_York.
_OPEN_AT = 17 * 60 + 5   # 17:05
_SHUT_AT = 16 * 60 + 55  # 16:55


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _ny(now: datetime) -> datetime:
    return _as_utc(now).astimezone(require_new_york())


def _minute_of_day(local: datetime) -> int:
    return local.hour * 60 + local.minute


def is_sunday_stub(bar_t: int, tf_seconds: int) -> bool:
    """True when a signal bar is the Sunday-open stub (or truncated by it).

    Rejects bars whose open falls on Sunday New York time, and bars whose
    ``[open, open+tf)`` span covers the Sunday 17:05 gold reopen (duration
    truncated by the weekend gap).
    """
    if tf_seconds <= 0:
        return False
    ny = require_new_york()
    start = datetime.fromtimestamp(int(bar_t), tz=timezone.utc).astimezone(ny)
    if start.weekday() == _SUNDAY:
        return True
    # Truncated: Sunday 17:05 falls strictly inside this bar's span.
    end_utc = int(bar_t) + int(tf_seconds)
    for day_offset in range(-7, 8):
        d = start.date() + timedelta(days=day_offset)
        if d.weekday() != _SUNDAY:
            continue
        probe = datetime(d.year, d.month, d.day, 17, 5, 0, tzinfo=ny)
        open_utc = int(probe.astimezone(timezone.utc).timestamp())
        if int(bar_t) < open_utc < end_utc:
            return True
    return False


def allows_entry(
    now: datetime,
    *,
    hold_over_weekend: bool = False,
    last_gap_at: float | int | None = None,
) -> tuple[bool, str]:
    """Session clocks for a new entry. Does not close existing positions.

    ``hold_over_weekend`` does not reopen the Friday two-hour entry cutoff;
    it only affects weekend flatten via :func:`should_flatten_weekend`.

    When ``last_gap_at`` is not ``None``, the caller has an unresolved post-gap
    wait (no full closed signal candle yet) and entry is blocked.
    """
    del hold_over_weekend  # entry cutoff is independent of weekend hold
    if last_gap_at is not None:
        return False, "post-gap waiting for closed signal candle"
    if not gold_session_open(now):
        return False, "session closed"

    local = _ny(now)
    wd = local.weekday()
    minute = _minute_of_day(local)
    pre_pause = _SHUT_AT - PAUSE_BUFFER_MINUTES          # 16:40
    post_reopen = _OPEN_AT + PAUSE_BUFFER_MINUTES        # 17:20
    friday_cutoff = _SHUT_AT - FRIDAY_ENTRY_CUTOFF_HOURS * 60  # 14:55

    if wd == _SATURDAY:
        return False, "weekend"
    if wd == _FRIDAY and minute >= friday_cutoff:
        return False, "friday entry cutoff"

    if wd == _SUNDAY:
        if minute < _OPEN_AT:
            return False, "session closed"
        if minute < post_reopen:
            return False, "post-reopen buffer"
        return True, "ok"

    # Mon–Thu daily pause ± buffer; Friday before the 2h cutoff still uses the
    # morning session but has no afternoon reopen.
    if wd in (0, 1, 2, 3):
        if pre_pause <= minute < _SHUT_AT:
            return False, "pre-pause buffer"
        if _SHUT_AT <= minute < _OPEN_AT:
            return False, "daily pause"
        if _OPEN_AT <= minute < post_reopen:
            return False, "post-reopen buffer"
    elif wd == _FRIDAY:
        if pre_pause <= minute < friday_cutoff:
            # Between 16:40 and 14:55 never happens; kept for clarity.
            return False, "pre-pause buffer"

    return True, "ok"


def should_flatten_weekend(now: datetime, hold_over_weekend: bool = False) -> bool:
    """True when bot positions should be closed before the Friday weekly close.

    With ``hold_over_weekend=True`` this never fires. Otherwise flatten starts
    ``FRIDAY_FLATTEN_MINUTES`` (30) before Friday 16:55 New York.
    """
    if hold_over_weekend:
        return False
    local = _ny(now)
    if local.weekday() != _FRIDAY:
        return False
    minute = _minute_of_day(local)
    return minute >= (_SHUT_AT - FRIDAY_FLATTEN_MINUTES)


@dataclass
class EntryWindow:
    """Post-gap recovery: one full closed signal candle per TF before entry.

    ``last_gap_at`` / ``last_gap_until`` mark when a feed hole longer than
    :data:`GAP_ENTRY_SECONDS` was observed. ``first_closed_after_gap`` records
    the first non-stub closed bar open per timeframe after that hole.
    """

    last_gap_at: float | None = None
    last_gap_until: float | None = None
    first_closed_after_gap: dict[str, int] = field(default_factory=dict)

    def note_feed_gap(self, gap_seconds: float, now_unix: float) -> None:
        if float(gap_seconds) <= GAP_ENTRY_SECONDS:
            return
        self.last_gap_at = float(now_unix)
        self.last_gap_until = float(now_unix)
        self.first_closed_after_gap.clear()

    def note_closed_bar(self, tf: str, bar_t: int, tf_seconds: int) -> None:
        if self.last_gap_until is None:
            return
        if tf in self.first_closed_after_gap:
            return
        if is_sunday_stub(int(bar_t), int(tf_seconds)):
            return
        # Full candle after the gap: open at or after the gap end.
        if int(bar_t) >= int(self.last_gap_until):
            self.first_closed_after_gap[tf] = int(bar_t)

    def gap_ready(self, tf: str) -> bool:
        if self.last_gap_until is None:
            return True
        return tf in self.first_closed_after_gap

    def gap_block_token(self, tf: str) -> float | None:
        """Value for ``allows_entry(..., last_gap_at=...)`` — ``None`` when ready."""
        if self.gap_ready(tf):
            return None
        return self.last_gap_at
