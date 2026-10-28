"""When a recurring ticket is due (tasking/recurring.py): a schedule on the wall clock of a time
zone, daily, weekly, monthly or cron-like, and the due times it gives between two instants.

A due time is a local wall-clock time in the schedule's zone (07:30 in Europe/London), so it
follows daylight saving: 07:30 stays 07:30 local when the clocks change. Two edges, both as cron
treats them: a time the clocks skip (02:30 on the night they spring forward) is due at the
moment the clocks reach it, an hour later on the wall (03:30); a time that happens twice (01:30 on
the night they fall back) is due once, the first time. Pure: no store, no bus, no clock of its
own; the caller says what "now" is."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")          # Python's weekday(): Monday is 0
FULL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
EVERY = {"day": "day", "daily": "day", "week": "week", "weekly": "week", "month": "month", "monthly": "month"}
SCAN_DAYS = 366 * 5               # how far ahead the next due time is looked for (a 29 February needs four years)
_AT = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class ScheduleError(ValueError):
    """A schedule that cannot be read; the message says what to write instead."""


def zone(name) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ScheduleError("timezone is required: an IANA name such as \"Europe/London\", \"America/New_York\" or \"UTC\"")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(f"timezone {name!r} is not a known IANA zone (e.g. \"Europe/London\", \"UTC\")") from e


def weekday(word) -> int:
    """0 (Monday) to 6 (Sunday) for "mon" or "monday", any case."""
    w = str(word).strip().lower()
    for i, short in enumerate(WEEKDAYS):
        if w in (short, FULL_DAYS[i]):
            return i
    raise ScheduleError(f"{word!r} is not a weekday: write mon, tue, wed, thu, fri, sat or sun")


def at_time(text) -> time:
    m = _AT.fullmatch(str(text).strip()) if isinstance(text, str) else None
    if not m:
        raise ScheduleError(f"at = {text!r}: write a 24-hour time, HH:MM (\"07:30\", \"17:00\")")
    return time(int(m.group(1)), int(m.group(2)))


@dataclass(frozen=True)
class Schedule:
    """A schedule: the local times of day it is due (``times``), and on which days: every day
    whose date passes ``on_day``. ``text`` says it in words for the pages."""
    tz: ZoneInfo
    times: tuple[time, ...]
    days: frozenset[int] | None = None        # weekdays (0 = Monday) it is due on; None: any
    monthdays: frozenset[int] | None = None   # days of the month; None: any
    months: frozenset[int] | None = None      # 1-12; None: any
    either: bool = False                      # cron: a day matches by weekday OR day of month (both restricted)
    text: str = ""

    def on_day(self, d: date) -> bool:
        if self.months is not None and d.month not in self.months:
            return False
        by_week = self.days is None or d.weekday() in self.days
        by_month = self.monthdays is None or d.day in self.monthdays
        return (by_week or by_month) if self.either else (by_week and by_month)

    def _on(self, d: date) -> Iterator[tuple[float, datetime]]:
        """The due times on the local date ``d``, as (epoch seconds, local wall time) ascending; a
        time the clocks skip falls at the moment they reach it (fold 0, cron's way)."""
        if not self.on_day(d):
            return
        seen: set[float] = set()
        for t in self.times:
            local = datetime.combine(d, t, tzinfo=self.tz)
            ts = local.timestamp()
            if ts not in seen:
                seen.add(ts)
                yield ts, local

    def between(self, after: float, until: float, limit: int = 100_000) -> Iterator[tuple[float, datetime]]:
        """The due times in (``after``, ``until``], ascending, at most ``limit`` of them."""
        d = datetime.fromtimestamp(after, self.tz).date() - timedelta(days=1)   # a day early: a skipped time may land after midnight
        last = datetime.fromtimestamp(until, self.tz).date() + timedelta(days=1)
        n = 0
        while d <= last and n < limit:
            for ts, local in self._on(d):
                if after < ts <= until:
                    n += 1
                    yield ts, local
            d += timedelta(days=1)

    def next_after(self, after: float) -> tuple[float, datetime] | None:
        """The first due time after ``after``, or None when none falls in the next five years."""
        return next(self.between(after, after + SCAN_DAYS * 86400.0, limit=1), None)


def _ints(spec, lo: int, hi: int, names: tuple[str, ...] = (), base: int = 0, what: str = "") -> frozenset[int] | None:
    """A cron field: ``*``, ``n``, ``a-b``, ``*/s``, ``a-b/s`` and lists of them; None for ``*``."""
    if spec == "*":
        return None
    out: set[int] = set()

    def num(x: str) -> int:
        x = x.lower()
        if x in names:
            return names.index(x) + base
        if not x.isdigit():
            raise ScheduleError(f"cron {what}: {x!r} is not a number" + (f" or a name ({', '.join(names)})" if names else ""))
        return int(x)

    for part in spec.split(","):
        rng, _, step = part.partition("/")
        if step and (not step.isdigit() or int(step) == 0):
            raise ScheduleError(f"cron {what}: step {step!r} must be a whole number above 0")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (num(x) for x in rng.split("-", 1))
        else:
            a = b = num(rng)
            if step:
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise ScheduleError(f"cron {what}: {part!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, int(step or 1)))
    return frozenset(out)


def cron(expr, tz: ZoneInfo) -> Schedule:
    """Five fields, as crontab(5): minute hour day-of-month month day-of-week. Names for months
    (jan) and weekdays (mon; 0 and 7 are Sunday). With both day fields restricted a day matches
    either, as cron does."""
    fields = str(expr).split() if isinstance(expr, str) else []
    if len(fields) != 5:
        raise ScheduleError(f"cron = {expr!r}: write five fields, minute hour day-of-month month day-of-week (\"30 7 * * mon-fri\")")
    mi, hr, dom, mon, dow = fields
    minutes = _ints(mi, 0, 59, what="minute")
    hours = _ints(hr, 0, 23, what="hour")
    monthdays = _ints(dom, 1, 31, what="day of month")
    months = _ints(mon, 1, 12, MONTHS, base=1, what="month")
    sunday_first = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
    raw_days = _ints(dow, 0, 7, sunday_first, what="day of week")
    days = None if raw_days is None else frozenset((d - 1) % 7 for d in raw_days)   # cron's 0/7 Sunday to Python's 6
    times = tuple(time(h, m) for h in sorted(hours if hours is not None else range(24))
                  for m in sorted(minutes if minutes is not None else range(60)))
    return Schedule(tz=tz, times=times, days=days, monthdays=monthdays, months=months,
                    either=days is not None and monthdays is not None, text=f"cron {' '.join(fields)}")


def every(unit, at, on, tz: ZoneInfo) -> Schedule:
    """``every = "day" | "week" | "month"`` at ``at`` (HH:MM); ``on`` the weekdays (a week; or
    a day, to keep to some weekdays) or the days of the month (1-31, or "last"; a day past the
    month's end falls on its last day)."""
    kind = EVERY.get(str(unit).strip().lower()) if isinstance(unit, str) else None
    if kind is None:
        raise ScheduleError(f"every = {unit!r}: write \"day\", \"week\" or \"month\" (or use cron = \"...\")")
    t = at_time(at)
    ons = [] if on is None else list(on) if isinstance(on, list) else [on]
    when = f"{t:%H:%M} {getattr(tz, 'key', tz)}"
    if kind == "month":
        if not ons:
            raise ScheduleError("every = \"month\" needs on: the day of the month (1-31, or \"last\")")
        days: set[int] = set()
        for x in ons:
            if isinstance(x, str) and x.strip().lower() == "last":
                days.add(32)
            elif isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 31:
                days.add(x)
            else:
                raise ScheduleError(f"on = {x!r}: a day of the month is 1-31, or \"last\"")
        return _monthly(t, days, tz, f"monthly on {', '.join(str(x) for x in ons)} at {when}")
    days_of_week = frozenset(weekday(x) for x in ons) if ons else None
    if kind == "week" and days_of_week is None:
        raise ScheduleError("every = \"week\" needs on: the weekday (\"mon\", or a list [\"mon\", \"thu\"])")
    words = ", ".join(WEEKDAYS[d] for d in sorted(days_of_week)) if days_of_week else ""
    text = (f"weekly on {words} at {when}" if kind == "week" else f"daily at {when}" + (f" on {words}" if words else ""))
    return Schedule(tz=tz, times=(t,), days=days_of_week, text=text)


@dataclass(frozen=True)
class _Monthly(Schedule):
    """``every = "month"``: 32 in ``monthdays`` is "last"; a day past a shorter month's end
    (29-31) falls on its last day, so a monthly ticket is never lost to February."""
    def on_day(self, d: date) -> bool:
        last = ((d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        days = self.monthdays or frozenset()
        return d.day in days or (d.day == last and (32 in days or any(x > last for x in days if x != 32)))


def _monthly(t: time, days: set[int], tz: ZoneInfo, text: str) -> Schedule:
    return _Monthly(tz=tz, times=(t,), monthdays=frozenset(days), text=text)


__all__ = ["EVERY", "Schedule", "ScheduleError", "cron", "every", "zone"]
