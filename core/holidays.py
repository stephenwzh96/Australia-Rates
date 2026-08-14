"""Sydney business days -- rule-based, no data source.

BBSW is published by ASX on each Sydney business day, and ASX 24 runs its
interest-rate session on the same calendar, so "how many trading days until the
meeting" and "which days does a rate fix on" both resolve here rather than the
app assuming "weekdays".

Every date is COMPUTED from a stated rule, never a hardcoded table, so it is
correct for any year without maintenance -- the same standard the rest of this
app holds itself to (see `core.rba_calendar`'s docstring).

WHY THIS MATTERS LESS HERE THAN IT DID FOR SOFR
-----------------------------------------------
The US original needed an exact business-day schedule because SR3 settles on
SOFR COMPOUNDED over the real fixing calendar, so a missing holiday moved the
capture share. Neither Australian contract works that way:

  * IB settles on the simple average of the cash rate over every CALENDAR day
    of the month, with weekends and public holidays carrying the previous
    business day's fix (ASX contract spec). A simple average over calendar days
    is exactly `days_at_new_rate / days_in_month` no matter which of those days
    were holidays -- the holiday calendar cancels out of the arithmetic
    entirely. See `core.contracts.ib_capture_share`.
  * IR settles on a single 3-month BBSW fix on its last trading day. One day,
    no accrual schedule.

So holidays now only reach two things: the Monte Carlo's trading-day count, and
which days BBSW prints. A missing holiday costs one day out of roughly thirty
in the simulation horizon -- visible but not structural, where in the US version
it would have biased a settlement calculation.

NSW, SPECIFICALLY
-----------------
Sydney is the settlement centre, so this is the NSW list, and NSW has two
quirks worth naming:

  * ANZAC DAY IS NEVER SUBSTITUTED. Unlike New Year's Day or Australia Day, 25
    April falling on a weekend does NOT create a Monday holiday in NSW. Applying
    the generic weekend rule to it would invent a closure.
  * EASTER SATURDAY AND SUNDAY are NSW public holidays but always land on a
    weekend, so they never change a business-day answer. Omitted rather than
    listed, to keep the set to days that can actually matter.

Deliberately NOT modelled: the first-Monday-in-August NSW Bank Holiday. It
closes bank branches but is not a general public holiday, and the financial
markets -- ASX 24 and the BBSW fixing among them -- run through it. Including it
would remove a day that genuinely trades. Stated here rather than left as a
silent gap.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

_MONDAY, _SATURDAY, _SUNDAY = 0, 5, 6


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _observed(d: date) -> date:
    """Weekend-substitution rule for a fixed-date NSW public holiday.

    Both a Saturday and a Sunday date move FORWARD to the Monday, which is the
    NSW rule and differs from the US federal one (where a Saturday holiday
    moves back to the Friday). Applied only to the holidays that actually
    substitute -- notably not Anzac Day.
    """
    if d.weekday() in (_SATURDAY, _SUNDAY):
        return d + timedelta(days=7 - d.weekday())
    return d


def easter(year: int) -> date:
    """Gregorian Easter Sunday -- Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def good_friday(year: int) -> date:
    return easter(year) - timedelta(days=2)


def easter_monday(year: int) -> date:
    return easter(year) + timedelta(days=1)


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    """Every day the Sydney money market is closed in `year`.

    Christmas and Boxing Day substitute independently, which is what produces
    the 27th/28th December pair when the 25th falls on a weekend: 25 Dec on a
    Saturday moves to Monday 27th, and 26 Dec on a Sunday moves to Monday 27th
    too -- so Boxing Day is pushed on to the 28th rather than collapsing into
    one holiday. Handled by substituting Boxing Day onto the first free weekday
    at or after its own observed date.
    """
    christmas = _observed(date(year, 12, 25))
    boxing = _observed(date(year, 12, 26))
    while boxing <= christmas:
        boxing += timedelta(days=1)
    while boxing.weekday() >= _SATURDAY:
        boxing += timedelta(days=1)

    return frozenset({
        _observed(date(year, 1, 1)),            # New Year's Day
        _observed(date(year, 1, 26)),           # Australia Day
        good_friday(year),
        easter_monday(year),
        date(year, 4, 25),                      # Anzac Day -- never substituted in NSW
        _nth_weekday(year, 6, _MONDAY, 2),      # King's Birthday
        _nth_weekday(year, 10, _MONDAY, 1),     # Labour Day (NSW)
        christmas,
        boxing,
    })


def is_business_day(d: date) -> bool:
    """A Sydney business day: a weekday that is not a NSW public holiday. This
    is the set of days BBSW is published for."""
    return d.weekday() < _SATURDAY and d not in holidays(d.year)


def business_days(start: date, end: date) -> list[date]:
    """Business days in [start, end) -- half-open, matching how an interest
    accrual period is quoted (accrues ON the start date, NOT on the end date,
    which is the next period's start)."""
    out, d = [], start
    while d < end:
        if is_business_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def previous_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def last_business_day_of_month(year: int, month: int) -> date:
    """The IB contract's last trading day -- 4:30pm on the final business day
    of the expiry month, per the ASX contract specification.

    Worth having as a named function because the ASX price API's `dateExpiry`
    field is NOT this date: it reports a fixed two calendar days earlier on
    every contract, which is a vendor artifact rather than a contract term.
    `data.asx` derives the contract month from the SYMBOL for exactly that
    reason, and this is what the month actually ends on.
    """
    nxt = date(year + month // 12, month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def nth_business_day(year: int, month: int, n: int) -> date:
    """The n-th business day of a month -- what an "N-th business day" release
    convention means (ABS uses one for several series)."""
    d = date(year, month, 1)
    count = 0
    while True:
        if is_business_day(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
