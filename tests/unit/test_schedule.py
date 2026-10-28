"""Recurring tickets' schedules (tasking/schedule.py): the local wall-clock time of a zone, across
daylight saving, for daily, weekly, monthly and cron schedules."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tasking.schedule import ScheduleError, cron, every, zone

NY, LONDON, TOKYO, SYDNEY = (ZoneInfo(z) for z in ("America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney"))


def due(s, start: datetime, end: datetime) -> list[str]:
    """The due times between two instants, in UTC, as 'YYYY-MM-DD HH:MM'."""
    return [f"{datetime.fromtimestamp(ts, UTC):%Y-%m-%d %H:%M}" for ts, _ in s.between(start.timestamp(), end.timestamp())]


def local(s, start: datetime, end: datetime) -> list[str]:
    return [f"{datetime.fromtimestamp(ts, s.tz):%Y-%m-%d %H:%M %Z}" for ts, _ in s.between(start.timestamp(), end.timestamp())]


def test_daily_keeps_the_wall_clock_time_across_both_changes_of_the_clocks():
    s = every("day", "07:30", None, LONDON)
    spring = local(s, datetime(2026, 3, 28, tzinfo=LONDON), datetime(2026, 3, 31, tzinfo=LONDON))
    assert spring == ["2026-03-28 07:30 GMT", "2026-03-29 07:30 BST", "2026-03-30 07:30 BST"]
    assert due(s, datetime(2026, 3, 28, tzinfo=LONDON), datetime(2026, 3, 31, tzinfo=LONDON)) == \
        ["2026-03-28 07:30", "2026-03-29 06:30", "2026-03-30 06:30"]          # an hour earlier in UTC after the change
    autumn = local(s, datetime(2026, 10, 24, tzinfo=LONDON), datetime(2026, 10, 27, tzinfo=LONDON))
    assert autumn == ["2026-10-24 07:30 BST", "2026-10-25 07:30 GMT", "2026-10-26 07:30 GMT"]


def test_a_time_the_clocks_skip_is_due_when_they_reach_it_and_a_repeated_time_is_due_once():
    gap = every("day", "02:30", None, NY)                  # 2026-03-08: 02:00 EST jumps to 03:00 EDT
    assert local(gap, datetime(2026, 3, 7, tzinfo=NY), datetime(2026, 3, 10, tzinfo=NY)) == \
        ["2026-03-07 02:30 EST", "2026-03-08 03:30 EDT", "2026-03-09 02:30 EDT"]
    twice = every("day", "01:30", None, NY)                # 2026-11-01: 01:30 happens twice; due the first time
    assert due(twice, datetime(2026, 10, 31, tzinfo=NY), datetime(2026, 11, 3, tzinfo=NY)) == \
        ["2026-10-31 05:30", "2026-11-01 05:30", "2026-11-02 06:30"]


def test_the_same_schedule_in_other_zones_is_due_at_their_own_wall_clock():
    start, end = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 3, tzinfo=UTC)
    assert due(every("day", "09:00", None, TOKYO), start, end) == ["2026-10-02 00:00", "2026-10-03 00:00"]   # (start, end]
    assert due(every("day", "09:00", None, SYDNEY), start, end) == ["2026-10-01 23:00", "2026-10-02 23:00"]   # AEST
    # Sydney springs forward on 2026-10-04 (southern summer): 09:00 AEDT is 22:00 UTC the day before
    assert due(every("day", "09:00", None, SYDNEY), datetime(2026, 10, 4, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC)) == \
        ["2026-10-04 22:00", "2026-10-05 22:00"]


def test_weekly_on_weekdays_and_daily_kept_to_weekdays():
    s = every("week", "09:00", ["mon", "Thursday"], NY)
    assert local(s, datetime(2026, 10, 26, tzinfo=NY), datetime(2026, 11, 6, tzinfo=NY)) == \
        ["2026-10-26 09:00 EDT", "2026-10-29 09:00 EDT", "2026-11-02 09:00 EST", "2026-11-05 09:00 EST"]
    assert s.text == "weekly on mon, thu at 09:00 America/New_York"
    brief = every("day", "07:00", ["mon", "tue", "wed", "thu", "fri"], LONDON)
    assert [x[:10] for x in local(brief, datetime(2026, 10, 30, tzinfo=LONDON), datetime(2026, 11, 3, 12, tzinfo=LONDON))] == \
        ["2026-10-30", "2026-11-02", "2026-11-03"]                               # no Saturday or Sunday


def test_monthly_on_a_day_past_a_short_months_end_falls_on_its_last_day_and_last_is_the_last():
    s = every("month", "10:00", 31, TOKYO)
    assert [x[:10] for x in local(s, datetime(2026, 1, 1, tzinfo=TOKYO), datetime(2026, 5, 1, tzinfo=TOKYO))] == \
        ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
    last = every("month", "18:00", "last", NY)
    assert [x[:10] for x in local(last, datetime(2028, 1, 15, tzinfo=NY), datetime(2028, 3, 15, tzinfo=NY))] == ["2028-01-31", "2028-02-29"]
    first = every("monthly", "09:00", [1, 15], LONDON)                     # across London's autumn change
    assert local(first, datetime(2026, 10, 1, tzinfo=LONDON), datetime(2026, 11, 2, tzinfo=LONDON)) == \
        ["2026-10-01 09:00 BST", "2026-10-15 09:00 BST", "2026-11-01 09:00 GMT"]


def test_cron_fields_names_steps_and_either_day_field():
    s = cron("30 7 * * mon-fri", LONDON)
    assert [x[:10] for x in local(s, datetime(2026, 10, 30, tzinfo=LONDON), datetime(2026, 11, 3, tzinfo=LONDON))] == ["2026-10-30", "2026-11-02"]
    quarter = cron("0 */6 1 jan,apr,jul,oct *", UTC)
    assert due(quarter, datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 4, 2, tzinfo=UTC)) == \
        ["2026-04-01 06:00", "2026-04-01 12:00", "2026-04-01 18:00"]          # 00:00 is the start, not after it
    either = cron("0 9 13 * fri", UTC)                                         # the 13th, or any Friday, as cron reads it
    got = [x[:10] for x in due(either, datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC))]
    assert got == ["2026-11-06", "2026-11-13"]
    assert cron("0 0 * * 0", UTC).days == cron("0 0 * * 7", UTC).days == frozenset({6})      # 0 and 7 are Sunday


def test_the_next_due_time_and_a_29_february_four_years_on():
    s = every("day", "07:30", None, NY)
    ts, when = s.next_after(datetime(2026, 10, 27, 8, tzinfo=NY).timestamp())
    assert ts == when.timestamp()
    assert f"{when:%Y-%m-%d %H:%M}" == "2026-10-28 07:30"
    _ts, when = cron("0 12 29 2 *", UTC).next_after(datetime(2026, 3, 1, tzinfo=UTC).timestamp())
    assert f"{when:%Y-%m-%d}" == "2028-02-29"


@pytest.mark.parametrize(("make", "says"), [
    (lambda: zone("Mars/Olympus"), "not a known IANA zone"),
    (lambda: zone(None), "timezone is required"),
    (lambda: every("fortnight", "09:00", None, UTC), "write \"day\", \"week\" or \"month\""),
    (lambda: every("day", "9am", None, UTC), "HH:MM"),
    (lambda: every("day", "24:00", None, UTC), "HH:MM"),
    (lambda: every("week", "09:00", None, UTC), "needs on: the weekday"),
    (lambda: every("week", "09:00", "funday", UTC), "not a weekday"),
    (lambda: every("month", "09:00", None, UTC), "needs on: the day of the month"),
    (lambda: every("month", "09:00", 32, UTC), "1-31, or \"last\""),
    (lambda: cron("0 7 * *", UTC), "five fields"),
    (lambda: cron("61 7 * * *", UTC), "outside 0-59"),
    (lambda: cron("0 7 * * blursday", UTC), "not a number or a name"),
    (lambda: cron("*/0 7 * * *", UTC), "above 0"),
])
def test_a_schedule_that_cannot_be_read_says_what_to_write(make, says):
    with pytest.raises(ScheduleError, match=None) as e:
        make()
    assert says in str(e.value)
