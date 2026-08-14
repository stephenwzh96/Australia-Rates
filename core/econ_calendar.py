"""US data releases between now and a meeting -- rule-based, not read off a page.

Most headline releases (CPI, core PCE, retail sales, GDP) do not fall on a fixed
weekday or a fixed day-of-month; BLS/BEA/Census publish the exact date via an
annual schedule instead. Inventing a specific day for one of those would be
exactly the kind of guessed fact this app's calendar handling otherwise refuses
to present (see `core.rba_calendar`'s `verified` flag) -- so this module only
ever generates a date from one of two sources, both auditable back to an
institution's own statement, never a guess:

  1. A genuinely fixed, institution-stated RULE (recomputed live, any year):
       * Employment Situation (NFP)   first Friday of the month
                                       (same report, same day: unemployment
                                       rate and average hourly earnings too)
       * ISM Manufacturing PMI        first business day of the month
       * ISM Services PMI             third business day of the month
       * Initial jobless claims       every Thursday (Wed in Thanksgiving week)
       * FOMC minutes                 exactly 3 weeks after a meeting's last day
                                       (the Fed's own stated release policy)

  2. A published one-off annual SCHEDULE, hardcoded from the source, for the one
     calendar year it actually covers (see `_CPI_SCHEDULE_2026` below):
       * Consumer Price Index (CPI)   headline and core, same report/day
       * Personal Income and Outlays  the PCE price index, headline and core

     Source: OMB's "Schedule of Release Dates for Principal Federal Economic
     Indicators" for CY2026 (statspolicy.gov/assets/fcsm/files/docs/
     OMB_pfei_schedule_release_dates_cy2026.pdf), which is itself the document
     BLS and BEA file their release calendars into. Fetched 2026-08-01. A date
     from here is exactly as reliable as this module's rule-based dates -- it's
     from the agency's own advance publication, not scraped or interpolated --
     but unlike a rule it does NOT extend to a year the table doesn't cover, so
     a schedule for 2027+ needs adding here explicitly rather than assumed.

Where a release bundles multiple headline numbers on the same day (NFP+
unemployment rate+wages; CPI headline+core; PCE headline+core), each becomes
its own Event sharing that date rather than one row hiding the rest -- core
CPI/PCE in particular is what the Fed itself watches most closely, and folding
it into a single "CPI" line would bury the number that matters most.

Everything else -- PPI, retail sales, GDP, Michigan sentiment, Case-Shiller,
Consumer Confidence, ECI -- still has to come from the meeting's own
"Pre-decision calendar" in the Structure tab, entered with a real date the user
has verified. `merged_calendar` combines all of the above rather than this
module trying to guess the rest.

PAST EVENTS AND ACTUAL VALUES
------------------------------
For the events that map onto a real data series (NFP -> PAYEMS, unemployment
rate -> UNRATE, average hourly earnings -> CES0500000003, initial claims ->
ICSA, headline/core CPI -> CPIAUCSL/CPILFESL, headline/core PCE -> PCEPI/
PCEPILFE, all from FRED; ISM Manufacturing/Services PMI from a PR Newswire
scrape -- FRED dropped ISM's data in 2016 over licensing and ISM's own report
now sits behind a subscriber login, so there is no API for it, only its own
public press release, see `data.ism`), `Event.series_key` names which series
supplies it and `attach_actuals` fills it in.
This module stays decoupled from `data/` entirely -- it takes plain `(date,
value)` history regardless of where it came from, so `core/` never imports
`data/`, matching how `core.model.build` already receives FRED values as plain
dicts rather than `data.fred` objects. The value attached is the LATEST
revision as of today, not the as-first-reported print; payroll data in
particular gets revised, and the UI note says so rather than implying a
point-in-time capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .rba_calendar import Meeting

_MONDAY, _TUESDAY, _WEDNESDAY, _THURSDAY, _FRIDAY, _SATURDAY, _SUNDAY = range(7)
_THANKSGIVING_MONTH = 11

FOMC_MINUTES_LAG_DAYS = 21

# Report names -- the unit a volatility multiplier is estimated per.
EMPLOYMENT_SITUATION = "Employment Situation"
CPI_RELEASE = "Consumer Price Index"
PCE_RELEASE = "Personal Income and Outlays"

# "The past quarter" -- how far back the calendar looks for realised context.
LOOKBACK_DAYS = 91

# series_key -> how to render its attached actual. PAYEMS is already in
# thousands (a month-over-month DIFFERENCE), so a bad month prints negative;
# ICSA is a raw weekly claim COUNT, so it needs the /1000 scale and carries no
# sign since it is a level, not a change. CPIAUCSL/CPILFESL/PCEPI/PCEPILFE/
# CES0500000003 all come out of the FRED catalogue already transformed to
# y/y %. UNRATE is a level, not a change, so it carries no sign. The ISM keys
# are the headline PMI level itself (already a %, no transform needed).
_ACTUAL_FORMATTERS = {
    "PAYEMS": lambda v: f"{v:+.0f}k",
    "UNRATE": lambda v: f"{v:.1f}%",
    "CES0500000003": lambda v: f"{v:+.1f}% y/y",
    "ICSA": lambda v: f"{v / 1000.0:.0f}k",
    "CPIAUCSL": lambda v: f"{v:+.1f}% y/y",
    "CPILFESL": lambda v: f"{v:+.1f}% y/y",
    "PCEPI": lambda v: f"{v:+.1f}% y/y",
    "PCEPILFE": lambda v: f"{v:+.1f}% y/y",
    "ISM_MFG_PMI": lambda v: f"{v:.1f}",
    "ISM_SVC_PMI": lambda v: f"{v:.1f}",
}

# Published one-off annual schedules -- see the module docstring's source note.
# (release_date, reference_month_label). "Data are for previous month" per the
# source, so e.g. the Feb 2026 CPI print reports January 2026.
_CPI_SCHEDULE_2026: tuple[tuple[date, str], ...] = (
    (date(2026, 1, 13), "Dec 2025"), (date(2026, 2, 11), "Jan 2026"),
    (date(2026, 3, 11), "Feb 2026"), (date(2026, 4, 10), "Mar 2026"),
    (date(2026, 5, 12), "Apr 2026"), (date(2026, 6, 10), "May 2026"),
    (date(2026, 7, 14), "Jun 2026"), (date(2026, 8, 12), "Jul 2026"),
    (date(2026, 9, 11), "Aug 2026"), (date(2026, 10, 14), "Sep 2026"),
    (date(2026, 11, 10), "Oct 2026"), (date(2026, 12, 10), "Nov 2026"),
)

_PCE_SCHEDULE_2026: tuple[tuple[date, str], ...] = (
    (date(2026, 1, 29), "Dec 2025"), (date(2026, 2, 26), "Jan 2026"),
    (date(2026, 3, 27), "Feb 2026"), (date(2026, 4, 30), "Mar 2026"),
    (date(2026, 5, 28), "Apr 2026"), (date(2026, 6, 25), "May 2026"),
    (date(2026, 7, 30), "Jun 2026"), (date(2026, 8, 26), "Jul 2026"),
    (date(2026, 9, 30), "Aug 2026"), (date(2026, 10, 29), "Sep 2026"),
    (date(2026, 11, 25), "Oct 2026"), (date(2026, 12, 23), "Nov 2026"),
)


@dataclass(frozen=True)
class Event:
    when: date
    label: str
    tier: int                    # 1 = market-moving, 2 = notable, 3 = context
    source: str                  # "rule" | "manual"
    note: str = ""
    series_key: str | None = None     # which series, if any, supplies `actual`
    actual: str | None = None         # filled in by attach_actuals; None until then
    # Which REPORT this line came out of. Several series print in one report --
    # payrolls, the unemployment rate and average hourly earnings are all the
    # Employment Situation, published in one 8:30 release. They are separate
    # rows because you read them separately, but they are ONE shock: anything
    # aggregating risk per day has to count the report once, not once per
    # series, or a three-line report looks three times as volatile as it is.
    release: str | None = None

    @property
    def key(self) -> tuple[date, str]:
        return (self.when, self.label)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th occurrence (1-indexed) of `weekday` in `year`-`month`."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _is_weekend(d: date) -> bool:
    return d.weekday() >= _SATURDAY  # Saturday=5, Sunday=6 -- Friday is a business day


def _nth_business_day(year: int, month: int, n: int) -> date:
    """The n-th weekday-only day of the month. Weekends only -- federal
    holidays (e.g. New Year's Day pushing the 1st business day) are not
    accounted for, which is noted in the UI rather than silently assumed away."""
    d = date(year, month, 1)
    count = 0
    while True:
        if not _is_weekend(d):
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def nfp_date(year: int, month: int) -> date:
    """Employment Situation: first Friday of the month."""
    return _nth_weekday(year, month, _FRIDAY, 1)


def ism_manufacturing_date(year: int, month: int) -> date:
    return _nth_business_day(year, month, 1)


def ism_services_date(year: int, month: int) -> date:
    return _nth_business_day(year, month, 3)


def jobless_claims_dates(start: date, end: date) -> list[date]:
    """Every Thursday in [start, end], shifted to Wednesday in the week
    containing the fourth Thursday of November (Thanksgiving)."""
    out: list[date] = []
    d = start + timedelta(days=(_THURSDAY - start.weekday()) % 7)
    while d <= end:
        if d.month == _THANKSGIVING_MONTH:
            fourth_thursday = _nth_weekday(d.year, _THANKSGIVING_MONTH, _THURSDAY, 4)
            if d == fourth_thursday:
                out.append(d - timedelta(days=1))
                d += timedelta(days=7)
                continue
        out.append(d)
        d += timedelta(days=7)
    return out


def fomc_minutes_date(meeting_end: date) -> date:
    """The Fed's own stated policy: minutes three weeks after the decision."""
    return meeting_end + timedelta(days=FOMC_MINUTES_LAG_DAYS)


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


def recurring_events(start: date, end: date,
                     meetings: list[Meeting] | None = None,
                     release_dates: dict[str, list[date]] | None = None) -> list[Event]:
    """Rule-, schedule- and feed-generated events in [start, end], nearest first.

    `meetings` surfaces FOMC minutes for EVERY meeting (past or future) whose
    minutes date falls in the window -- with a quarter of lookback that can be
    more than one meeting, and each is genuinely relevant context. The 3-week
    lag is the Fed's own stated policy, not a guess.

    `release_dates` supplies real publication dates per release name (see
    `data.fred.fetch_release_dates`). When present it REPLACES the hardcoded
    schedule for that release, which is what lets CPI and PCE resolve for any
    year rather than only the one tabulated below. Absent -- no API key, no
    network -- the tables still answer for 2026, so the calendar degrades to
    what it did before rather than emptying out.
    """
    if start > end:
        return []
    out: list[Event] = []
    feed = release_dates or {}

    def scheduled(name: str, table: tuple[tuple[date, str], ...]) -> list[tuple[date, str]]:
        """Feed dates if we have them, else the published table. Reference
        labels only exist on the table, so a feed date carries none -- the
        date is the fact that matters, the "June data" annotation is a
        convenience."""
        if feed.get(name):
            return [(d, "") for d in feed[name] if start <= d <= end]
        return [(d, ref) for d, ref in table if start <= d <= end]

    def note(ref: str, tail: str) -> str:
        return f"{ref} data. {tail}" if ref else tail

    for y, m in _months_between(start, end):
        nfp = nfp_date(y, m)
        if start <= nfp <= end:
            out.append(Event(nfp, "Employment Situation (NFP)", 1, "rule",
                             "First Friday of the month.", series_key="PAYEMS",
                             release=EMPLOYMENT_SITUATION))
            out.append(Event(nfp, "Unemployment Rate", 1, "rule",
                             "Same report as NFP, first Friday of the month.",
                             series_key="UNRATE", release=EMPLOYMENT_SITUATION))
            out.append(Event(nfp, "Average Hourly Earnings", 2, "rule",
                             "Same report as NFP, first Friday of the month.",
                             series_key="CES0500000003", release=EMPLOYMENT_SITUATION))
        ism_m = ism_manufacturing_date(y, m)
        if start <= ism_m <= end:
            out.append(Event(ism_m, "ISM Manufacturing PMI", 2, "rule",
                             "First business day of the month.",
                             series_key="ISM_MFG_PMI", release="ISM Manufacturing"))
        ism_s = ism_services_date(y, m)
        if start <= ism_s <= end:
            out.append(Event(ism_s, "ISM Services PMI", 2, "rule",
                             "Third business day of the month.",
                             series_key="ISM_SVC_PMI", release="ISM Services"))

    for d in jobless_claims_dates(start, end):
        out.append(Event(d, "Initial jobless claims", 3, "rule",
                         "Weekly, 8:30am ET.", series_key="ICSA",
                         release="Jobless claims"))

    src = "BLS/BEA published release date." if feed else "OMB's official 2026 release schedule."
    for d, ref in scheduled("CPI", _CPI_SCHEDULE_2026):
        out.append(Event(d, "Consumer Price Index (CPI)", 1, "rule",
                         note(ref, "Headline. " + src), series_key="CPIAUCSL",
                         release=CPI_RELEASE))
        out.append(Event(d, "Core CPI (ex food & energy)", 1, "rule",
                         note(ref, src), series_key="CPILFESL", release=CPI_RELEASE))
    for d, ref in scheduled("PCE", _PCE_SCHEDULE_2026):
        out.append(Event(d, "Core PCE Price Index", 1, "rule",
                         note(ref, "The Fed's preferred inflation gauge. " + src),
                         series_key="PCEPILFE", release=PCE_RELEASE))
        out.append(Event(d, "PCE Price Index (headline)", 2, "rule",
                         note(ref, src), series_key="PCEPI", release=PCE_RELEASE))

    for m in meetings or []:
        minutes = fomc_minutes_date(m.end)
        if start <= minutes <= end:
            out.append(Event(minutes, f"FOMC minutes -- {m.label} meeting", 1,
                             "rule", "Three weeks after the decision.",
                             release="FOMC minutes"))

    out.sort(key=lambda e: e.key)
    return out


def manual_events(rows: list[dict], start: date, end: date) -> list[Event]:
    """The meeting's own Structure-tab calendar, filtered to the window.

    These carry real dates the user typed in (or the framework doc verified for
    July), so they can safely stand next to the rule-based events above.
    """
    out: list[Event] = []
    for r in rows:
        raw = r.get("date")
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw))
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        try:
            tier = int(r.get("tier") or 3)
        except (TypeError, ValueError):
            tier = 3
        out.append(Event(d, str(r.get("release") or ""), tier, "manual"))
    out.sort(key=lambda e: e.key)
    return out


def merged_calendar(start: date, end: date, manual_rows: list[dict],
                    meetings: list[Meeting] | None = None,
                    release_dates: dict[str, list[date]] | None = None) -> list[Event]:
    """Rule-based and manual events, de-duplicated and sorted.

    A manual row wins over a rule-based one landing on the same day with the
    same label (case-insensitive) -- if the user has typed in a confirmed date
    for something the rule also generated, the confirmed one is the one to show.
    """
    manual = manual_events(manual_rows, start, end)
    manual_keys = {(e.when, e.label.strip().lower()) for e in manual}
    rules = [e for e in recurring_events(start, end, meetings, release_dates)
            if (e.when, e.label.strip().lower()) not in manual_keys]
    out = manual + rules
    out.sort(key=lambda e: e.key)
    return out


def attach_actuals(events: list[Event],
                   history: dict[str, list[tuple[date, float]]],
                   as_of: date | None = None) -> list[Event]:
    """Fill in `actual` for events tagged with a `series_key`, from plain
    `(date, value)` history -- see the module docstring for why this stays
    decoupled from `data/` entirely. Events with no series, or no history
    supplied for their series, pass through unchanged (their `actual` stays
    None, which the UI renders as a blank rather than a fabricated value).

    `as_of` is the app's own simulated "today", not the real clock -- a live
    feed runs on the real calendar, so without this cutoff a release the app
    still treats as upcoming could pick up a value that's already out in the
    real world, making an unreleased row look like it already printed. Events
    at or after `as_of` are always left blank; pass None to disable the
    cutoff (e.g. in tests that don't model a simulated present).
    """
    out: list[Event] = []
    for e in events:
        if e.series_key is None or (as_of is not None and e.when >= as_of):
            out.append(e)
            continue
        series = history.get(e.series_key)
        if not series:
            out.append(e)
            continue
        candidates = [(d, v) for d, v in series if d <= e.when]
        if not candidates:
            out.append(e)
            continue
        _, value = max(candidates, key=lambda t: t[0])
        fmt = _ACTUAL_FORMATTERS.get(e.series_key, lambda v: f"{v:g}")
        out.append(Event(e.when, e.label, e.tier, e.source, e.note,
                         e.series_key, fmt(value)))
    return out
