"""When every observation first became public -- the no-look-ahead guarantee.

The forecast for month m is made on the eve of its Labour Force release, R_m,
and may use an observation only if it was released on a calendar day STRICTLY
before R_m.

R_m itself is exact from the ABS release pages (every release from December
2019: all Thursdays, the 11th to the 25th of the following month). Before
that a rule dates it on the SECOND Thursday of the following month -- the
earliest the release has come -- so the cutoff can only be too early, which
costs an input a month and never leaks one. Every input rule errs the other
way, LATE -- an earlier Labour Force print used as an input included (the
28th of the following month) -- and so does the test of whether a print is
out by the as-of date: a date too late can only understate what was known.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Callable

from nextprint import inputs as I

_THURSDAY = 3


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    k = d.year * 12 + d.month - 1 + n
    return date(k // 12, k % 12 + 1, 1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def nth_weekday(month: date, weekday: int, n: int) -> date:
    d = date(month.year, month.month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def lf_rule(m: date) -> date:
    """Second Thursday of the following month: the earliest a Labour Force
    release has come, so a cutoff built on it is never late."""
    return nth_weekday(add_months(m, 1), _THURSDAY, 2)


def lf_release(m: date, exact: dict[date, date]) -> date:
    """The day the forecast for m is made: exact, else the EARLY rule, so the
    cutoff can only cost an input, never admit one."""
    return exact.get(month_start(m)) or lf_rule(m)


def lf_rule_late(m: date) -> date:
    """The 28th of the following month: later than any release has come
    (the 11th to the 25th). Used wherever an early date would leak -- an
    earlier print read as an input, or a print counted as out by an as-of."""
    nxt = add_months(m, 1)
    return date(nxt.year, nxt.month, 28)


def lf_published(m: date, exact: dict[date, date]) -> date:
    """When month m's Labour Force data was certainly public."""
    return exact.get(month_start(m)) or lf_rule_late(m)


def nab_release(m: date) -> date:
    """The NAB monthly survey is published in the first half of the following
    month (the second Tuesday, lately); dated the 21st to err late."""
    nxt = add_months(m, 1)
    return date(nxt.year, nxt.month, 21)


def westpac_release(m: date) -> date:
    """Surveyed in the first week of the month and published around its
    second Wednesday; dated the month's last day to err late."""
    return month_end(m)


def vacancies_release(m: date) -> date:
    """Job vacancies (reference: the middle month of the quarter, dated its
    last day by the RBA) come out about five weeks later; dated the last day
    of the second month after to err late."""
    return month_end(add_months(m, 2))


#: How each timing class is dated, in words for the tab's timing table.
RULE_TEXT = {
    I.LABOUR_FORCE: "earlier Labour Force release (exact from Dec 2019, else the 28th)",
    I.NAB: "21st of the next month (published ~2nd Tuesday)",
    I.WESTPAC_MI: "end of the survey month (published ~2nd Wednesday)",
    I.VACANCIES: "end of the 2nd month after (published ~5 weeks after)",
}


def rule_for(timing: str, exact_lf: dict[date, date]) -> Callable[[date], date]:
    return {
        I.LABOUR_FORCE: lambda d: lf_published(d, exact_lf),
        I.NAB: nab_release,
        I.WESTPAC_MI: westpac_release,
        I.VACANCIES: vacancies_release,
    }[timing]


def release_dates(obs_dates, timing: str, exact_lf: dict[date, date]) -> list[date]:
    rule = rule_for(timing, exact_lf)
    return [rule(d) for d in obs_dates]


def known(release: date | None, cutoff: date) -> bool:
    """Public strictly before `cutoff`."""
    return release is not None and release < cutoff
