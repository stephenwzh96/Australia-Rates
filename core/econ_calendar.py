"""Australian data releases between now and a meeting -- dates only.

DELIBERATELY MUCH SMALLER THAN THE US VERSION IT REPLACES, and worth saying why
rather than leaving it looking unfinished. The original carries release dates,
series mappings, actual values and consensus for the whole US calendar, because
it feeds a Labour tab, an Inflation tab and a Release prep tab. None of those
are in this build. What survives is the one job the rest of the app still needs
from a calendar: telling `core.volatility` which days are loud, so the Monte
Carlo does not price a CPI Wednesday like a quiet Tuesday.

So this module generates DATES and nothing else. No series keys, no actuals, no
`attach_actuals`. When the Inflation and Labour tabs arrive with their own
figures, that is the point to give events values again.

EVERY DATE COMES FROM A RULE OR A PUBLISHED SCHEDULE, NEVER A GUESS
--------------------------------------------------------------------
The same standard as `core.rba_calendar`'s `verified` flag:

  1. Genuinely fixed, institution-stated RULES, recomputed live for any year:
       * RBA decision        2:30pm on the second day of a Board meeting
       * RBA minutes         two weeks after the decision, per the RBA's own
                             stated release policy
       * Statement on        with the Feb/May/Aug/Nov decisions
         Monetary Policy

  2. The ABS publishes its release calendar in advance, and its headline
     macro releases follow stable rules that this module recomputes rather
     than hardcoding a year of dates that would rot:
       * Labour Force        Thursday of the third week of the month
       * CPI (quarterly)     last Wednesday of the month after quarter end
       * Monthly CPI         about four weeks after the reference month
         indicator
       * Wage Price Index    quarterly, mid-month after quarter end
       * National Accounts   quarterly GDP, early in the third month

     These are RULES matching the ABS's long-standing practice, not scraped
     dates, and they are marked `source="rule"` so the UI can say so. An ABS
     date that genuinely matters should be confirmed against abs.gov.au before
     it is traded on -- which is what the meeting's own editable calendar in
     the Structure section is for.

AUSTRALIAN CPI IS QUARTERLY, AND THAT CHANGES THE SHAPE OF THE PROBLEM
-----------------------------------------------------------------------
The US version can assume a monthly CPI, so between any two FOMC meetings
there is reliably one. Australia's headline CPI prints four times a year
against eight meetings, so half of all RBA decisions have no new quarterly
inflation read since the last one. The monthly CPI indicator partly fills the
gap but is a narrower collection. The practical consequence for this app is
that a quarterly-CPI meeting is a genuinely bigger event than a non-CPI one,
and the volatility multipliers should be allowed to reflect that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .rba_calendar import SMP_MONTHS, Meeting

LOOKBACK_DAYS = 120

_WEDNESDAY, _THURSDAY = 2, 3

# Release names, so the volatility multipliers and the UI agree on spelling.
DECISION = "RBA decision"
MINUTES = "RBA minutes"
SMP = "Statement on Monetary Policy"
CPI_Q = "CPI (quarterly)"
CPI_M = "Monthly CPI indicator"
LABOUR = "Labour Force"
WPI = "Wage Price Index"
GDP = "National Accounts (GDP)"

# The RBA publishes minutes a fortnight after the decision.
MINUTES_LAG_DAYS = 14


@dataclass(frozen=True)
class Event:
    when: date
    label: str
    weight: int              # 1 = top tier, 2 = second tier
    source: str              # "rule" | "manual"
    note: str = ""
    release: str = ""        # groups same-day lines into ONE shock

    @property
    def is_manual(self) -> bool:
        return self.source == "manual"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _months_between(start: date, end: date):
    m = date(start.year, start.month, 1)
    while m <= end:
        yield m
        m = date(m.year + (m.month // 12), m.month % 12 + 1, 1)


@dataclass(frozen=True)
class ExactDates:
    """Published release days for one release over the span its source covers
    (the ABS release calendar ahead, the ABS release pages behind). Inside
    `[lo, hi]` these REPLACE the rule's dates -- not month by month, because a
    release can move across a month end (the November 2026 CPI is due on
    6 January 2027)."""
    release: str
    dates: tuple[date, ...]
    lo: date
    hi: date


#: How an exact-dated release is shown: the rule's label and weight.
_EXACT_LABEL = {
    LABOUR: ("Labour Force (employment, unemployment rate)", 1),
    CPI_Q: ("CPI, quarterly (headline and trimmed mean)", 1),
    CPI_M: ("Monthly CPI indicator", 2),
    WPI: ("Wage Price Index", 2),
    GDP: ("National Accounts (GDP)", 2),
}


def _apply_exact(events: list[Event], exact: "tuple[ExactDates, ...] | list[ExactDates]",
                 start: date, end: date) -> list[Event]:
    for ex in exact:
        label, weight = _EXACT_LABEL.get(ex.release, (ex.release, 2))
        events = [e for e in events if not (e.release == ex.release and ex.lo <= e.when <= ex.hi)]
        events += [Event(d, label, weight, "abs", "ABS release calendar (exact).",
                         release=ex.release)
                   for d in ex.dates if ex.lo <= d <= ex.hi and start <= d <= end]
    return events


def recurring_events(start: date, end: date,
                     meetings: list[Meeting] | None = None,
                     exact: "tuple[ExactDates, ...] | list[ExactDates]" = ()) -> list[Event]:
    """Every Australian event in [start, end]: exact ABS release days where
    `exact` covers them, the rule otherwise."""
    out: list[Event] = []

    for m in _months_between(start, end):
        y, mo = m.year, m.month

        # ABS Labour Force -- Thursday of the third week.
        labour = _nth_weekday(y, mo, _THURSDAY, 3)
        out.append(Event(labour, "Labour Force (employment, unemployment rate)",
                         1, "rule", "ABS, Thursday of the third week.",
                         release=LABOUR))

        # Quarterly CPI: last Wednesday of the month AFTER quarter end.
        if mo in (1, 4, 7, 10):
            cpi = _last_weekday(y, mo, _WEDNESDAY)
            quarter = {1: "Dec", 4: "Mar", 7: "Jun", 10: "Sep"}[mo]
            out.append(Event(cpi, "CPI, quarterly (headline and trimmed mean)",
                             1, "rule",
                             f"ABS, {quarter} quarter. The trimmed mean is the "
                             "number the RBA targets.", release=CPI_Q))
        else:
            # Monthly indicator in the other months -- narrower, second tier.
            cpi_m = _last_weekday(y, mo, _WEDNESDAY)
            out.append(Event(cpi_m, "Monthly CPI indicator", 2, "rule",
                             "ABS. Narrower collection than the quarterly CPI.",
                             release=CPI_M))

        # Quarterly Wage Price Index and National Accounts.
        if mo in (2, 5, 8, 11):
            out.append(Event(_nth_weekday(y, mo, _WEDNESDAY, 3),
                             "Wage Price Index", 2, "rule",
                             "ABS, quarterly.", release=WPI))
        if mo in (3, 6, 9, 12):
            out.append(Event(_nth_weekday(y, mo, _WEDNESDAY, 1),
                             "National Accounts (GDP)", 2, "rule",
                             "ABS, quarterly.", release=GDP))

    for mt in (meetings or []):
        if start <= mt.end <= end:
            label = "RBA decision" + (" + Statement on Monetary Policy"
                                      if mt.has_smp else "")
            out.append(Event(mt.end, label, 1, "rule",
                             "2:30pm Sydney. The new target applies the next day."
                             + (" Full forecast round." if mt.has_smp else ""),
                             release=SMP if mt.has_smp else DECISION))
        minutes = mt.end + timedelta(days=MINUTES_LAG_DAYS)
        if start <= minutes <= end:
            out.append(Event(minutes, "RBA minutes", 2, "rule",
                             "A fortnight after the decision.", release=MINUTES))

    out = _apply_exact(out, exact, start, end)
    out.sort(key=lambda e: (e.when, -e.weight))
    return [e for e in out if start <= e.when <= end]


def events_from_state(rows) -> list[Event]:
    """The meeting's own hand-entered calendar. Anything this module cannot
    derive from a rule belongs here, with a date the user has verified."""
    out: list[Event] = []
    for r in rows or []:
        try:
            when = date.fromisoformat(r["when"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append(Event(when, r.get("label", "(unnamed)"),
                         int(r.get("weight", 2) or 2), "manual",
                         r.get("note", ""), r.get("release", "")))
    return out


def merged_calendar(start: date, end: date, manual_rows,
                    meetings: list[Meeting] | None = None,
                    exact: "tuple[ExactDates, ...] | list[ExactDates]" = ()) -> list[Event]:
    """Rule-derived and hand-entered events in one list.

    A manual entry on the same day as a rule-derived one WINS -- if the user
    has typed a date they verified, it supersedes this module's rule for that
    release. `exact` plays the US version's `release_dates`: published release
    days (the ABS calendar) that replace the rules inside the span they cover.
    """
    manual = events_from_state(manual_rows)
    claimed = {(e.when, e.release) for e in manual if e.release}
    rules = [e for e in recurring_events(start, end, meetings, exact)
             if (e.when, e.release) not in claimed]
    return sorted(manual + rules, key=lambda e: (e.when, -e.weight))

