"""Australian labour-market slack dashboard -- every indicator as a percentile
of its own history, oriented so the axis reads More Slack -> Less Slack.

The FOMC version's engine, unchanged: percentile ranks against a common
history, the slack heatmap, the diffusion index and its exact split into
channels. What is Australian is the indicator set. There is no JOLTS, no
weekly claims and no regional-Fed employment survey here, so the same seven
channels are filled from what Australia publishes:

* the Labour Force survey (rates, participation, employment, hours),
* its gross flows (LMS1) -- job finding, job loss, entry and exit, as matched-
  sample transition rates with stable seasonals removed, because the ABS
  publishes the flows unadjusted,
* Labour Force Detailed (medium-term unemployment, job tenure),
* RBA H5 vacancies, H3 NAB business conditions and Westpac-MI sentiment, H4
  wages, and NAB capacity utilisation where it has been pasted.

Every key below is a series assembled by `ui.sources.fetch_labor_series`,
dated on the first of its reference month (a quarterly series on the month its
survey refers to), so one transform and one month axis serve them all.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import clock
from . import publication as pub

#: The percentile pool. 2000 rather than the US 1995: the gross flows start in
#: Sep 2007 and NAB capacity in 2000, so an earlier start would rank most rows
#: against a pool the flow rows never see.
BASE_START = date(2000, 1, 1)

YEAR_AGO_MONTHS = 12

CHANGE_MONTHS = 12
CHANGE_WINDOWS = (1, 3, 6, 12)

MIN_RANK_OBS = 12

FLAT_BAND_PCT = 2.0

# Days from an observation's reference month (first of the month) to the day
# it prints. Each release family has its own lag, so the lag names the release.
LAG_LABOUR_FORCE = 48       # month M -> Thursday of the third week of M+1
LAG_LF_DETAILED = 56        # LFD and the gross flows, a week after the headline
LAG_VACANCIES = 66          # mid-quarter survey month -> early in the month after next
LAG_NAB = 40                # NAB monthly business survey, second week of M+1
LAG_SENTIMENT = 9           # Westpac-MI, second Tuesday of the month itself
LAG_WPI = 77                # quarter's last month -> mid second month after

RELEASE_NAMES = {LAG_LABOUR_FORCE: "Labour Force", LAG_LF_DETAILED: "LF Detailed",
                 LAG_VACANCIES: "Job Vacancies", LAG_NAB: "NAB survey",
                 LAG_SENTIMENT: "Westpac-MI", LAG_WPI: "WPI"}


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    series: tuple[str, ...]      # >1 = averaged into a composite
    direction: int               # +1 high is tighter, -1 high is more slack
    transform: str               # level | diff3 | pct3 | share
    denominator: str | None = None      # share only
    approximate: bool = False    # a proxy for the US row, not the same measure
    note: str = ""
    lag_days: int = LAG_LABOUR_FORCE    # reference month -> print day
    source: str = "ABS Labour Force"

    @property
    def codes(self) -> tuple[str, ...]:
        return self.series + ((self.denominator,) if self.denominator else ())

    @property
    def release(self) -> str:
        """The release this indicator prints in, e.g. "Labour Force"."""
        return RELEASE_NAMES[self.lag_days]


INDICATORS: tuple[Indicator, ...] = (
    Indicator("ur", "Unemployment Rate", ("UR",), -1, "level", source="RBA H5"),
    Indicator("und", "Underemployment Rate", ("UND",), -1, "level"),
    Indicator("util", "Underutilisation Rate", ("UTIL",), -1, "level",
              note="Unemployment plus underemployment, the ABS's U-6 equivalent."),
    Indicator("youth", "Youth Unemp (15-24)", ("YOUTH",), -1, "level",
              note="The margin that turns first: young workers are hired last and "
                   "let go first."),
    Indicator("mtu", "Medium-Term Unemp", ("MTU",), -1, "level",
              lag_days=LAG_LF_DETAILED, source="ABS LF Detailed",
              note="Unemployed one to twelve months, % of the labour force -- the "
                   "duration the RBA reads, since the long-term pool moves on a "
                   "multi-year horizon. Stops at Mar 2026 with the survey changes."),
    Indicator("f_nu", "NILF to Unemp Flow", ("F_NU",), -1, "level", approximate=True,
              lag_days=LAG_LF_DETAILED, source="ABS gross flows (LMS1)",
              note="Entrants landing in unemployment rather than a job, as a share "
                   "of the labour force joined. Matched-sample rate, stable "
                   "seasonals removed."),
    Indicator("f_un", "Unemp to NILF Flow", ("F_UN",), -1, "level", approximate=True,
              lag_days=LAG_LF_DETAILED, source="ABS gross flows (LMS1)",
              note="Share of last month's unemployed who stopped looking -- "
                   "discouragement, not placement."),
    Indicator("f_eu", "Emp to Unemp Flow", ("F_EU",), -1, "level",
              lag_days=LAG_LF_DETAILED, source="ABS gross flows (LMS1)",
              note="Share of last month's employed now unemployed: Australia's "
                   "nearest to a layoffs rate, since it has no JOLTS and no claims."),
    Indicator("f_ue", "Unemp to Emp Flow", ("F_UE",), +1, "level",
              lag_days=LAG_LF_DETAILED, source="ABS gross flows (LMS1)",
              note="The job-finding rate: share of last month's unemployed in work "
                   "this month."),
    Indicator("f_ne", "NILF to Emp Flow", ("F_NE",), +1, "level", approximate=True,
              lag_days=LAG_LF_DETAILED, source="ABS gross flows (LMS1)",
              note="Hires straight from outside the labour force, as a share of the "
                   "labour force -- the pool joined, not the ageing NILF pool."),
    Indicator("pr", "Participation Change", ("PR",), +1, "diff3", source="RBA H5"),
    Indicator("epop", "Emp-Pop Change", ("EPOP",), +1, "diff3", source="RBA H5"),
    Indicator("emp", "Employment Change", ("EMP",), +1, "pct3", source="RBA H5"),
    Indicator("hours", "Hours Worked Change", ("HOURS",), +1, "pct3", source="RBA H5",
              note="Total hours, the labour input GDP is built from."),
    Indicator("ft", "Full-Time Share", ("FT",), +1, "share", denominator="EMP",
              source="RBA H5",
              note="Full-time employment as a share of all employment: firms convert "
                   "part-time to full-time when they are short of labour."),
    Indicator("vac", "Vacancies, % of LF", ("VAC_LF",), +1, "level",
              lag_days=LAG_VACANCIES, source="RBA H5 (ABS Job Vacancies)",
              note="Quarterly survey; the 2008-09 suspension is a gap, not a zero."),
    Indicator("vu", "Vacancies per Unemployed", ("VAC",), +1, "share",
              denominator="UNEMP", lag_days=LAG_VACANCIES,
              source="RBA H5 (ABS Job Vacancies)",
              note="The Beveridge-curve tightness ratio, in vacancies per 100 "
                   "unemployed."),
    Indicator("nab_cap", "NAB Capacity Utilisation", ("NAB_CAP",), +1, "level",
              lag_days=LAG_NAB, source="NAB (pasted)", approximate=True,
              note="Commercial series, pasted into meetings/_manual_series.json. Left "
                   "out of every count when it is not loaded."),
    Indicator("nab_cond", "NAB Business Conditions", ("NAB_COND",), +1, "level",
              lag_days=LAG_NAB, source="RBA H3", approximate=True,
              note="Stands in for the regional Fed employment surveys, which have no "
                   "Australian equivalent on a free feed."),
    Indicator("switch", "Job-Switching Rate", ("SWITCH",), +1, "level", approximate=True,
              lag_days=LAG_LF_DETAILED, source="ABS LF Detailed",
              note="Employed under a year with their current employer -- the "
                   "nearest Australian series to a quits rate. Quarterly."),
    Indicator("wpi", "Wage Price Index, y/y", ("WPI",), +1, "level", lag_days=LAG_WPI,
              source="RBA H4", approximate=True,
              note="Wages still moving is the bargaining-power read the US takes from "
                   "the zero-wage-growth share."),
    Indicator("sent", "Consumer Sentiment", ("SENT",), +1, "level", approximate=True,
              lag_days=LAG_SENTIMENT, source="RBA H3 (Westpac-MI)",
              note="Household confidence, where the US reads job leavers. The "
                   "labour-specific Westpac-MI unemployment-expectations index is "
                   "commercial."),
)

BY_KEY = {i.key: i for i in INDICATORS}


def required_codes() -> list[str]:
    """Every series key the dashboard needs, de-duplicated."""
    seen: list[str] = []
    for ind in INDICATORS:
        for c in ind.codes:
            if c not in seen:
                seen.append(c)
    return seen


@dataclass(frozen=True)
class Reading:
    indicator: Indicator
    current_pct: float | None       # 0-100, already slack-oriented
    year_ago_pct: float | None
    current_value: float | None     # transformed units, for the tooltip
    as_of: date | None
    n_obs: int

    @property
    def ok(self) -> bool:
        return self.current_pct is not None

    @property
    def travel(self) -> str:
        """cooling | tightening | flat -- movement along the slack axis."""
        if self.current_pct is None or self.year_ago_pct is None:
            return "flat"
        d = self.current_pct - self.year_ago_pct
        if abs(d) < FLAT_BAND_PCT:
            return "flat"
        return "tightening" if d > 0 else "cooling"


def _months_before(d: date, months: int) -> date:
    y, m = divmod((d.year * 12 + d.month - 1) - months, 12)
    return date(y, m + 1, 1)


def _at_or_before(rows: list[tuple[date, float]], when: date) -> tuple[date, float] | None:
    hits = [r for r in rows if r[0] <= when]
    return hits[-1] if hits else None


def percentile_rank(pool: list[float], value: float) -> float:
    """Share of `pool` at or below `value`, 0-100 -- the weak (`<=`) reading."""
    if not pool:
        return 50.0
    return 100.0 * sum(1 for x in pool if x <= value) / len(pool)


def published(rows: list[tuple[date, float]], ind: Indicator,
              as_of: date | None) -> list[tuple[date, float]]:
    """Only the observations that had actually PRINTED by `as_of`."""
    if as_of is None:
        return rows
    return [(d, v) for d, v in rows
            if d + timedelta(days=ind.lag_days) <= as_of]


#: Past releases a projected next date is built from, when the source files no
#: calendar ahead (the Atlanta Fed wage tracker).
CADENCE_RELEASES = 7


@dataclass(frozen=True)
class ReleaseTiming:
    latest: date | None         # the day the row's shown observation was first published
    next: date | None           # the day its next observation is (or was) first published
    next_estimated: bool = False    # no calendar ahead: projected from past cadence


def _projected(releases: list[date], after: date) -> date | None:
    """The next date on a release cadence, strictly after `after`: the last
    release stepped forward by the median recent gap."""
    recent = sorted(set(releases))[-CADENCE_RELEASES:]
    if len(recent) < 3:
        return None
    step = timedelta(days=statistics.median((b - a).days for a, b in zip(recent, recent[1:])))
    out = recent[-1] + step
    while out <= after:
        out += step
    return out


def release_timing(ind: Indicator, shown: date | None,
                   first_release: dict[str, dict[date, date]],
                   calendars: dict[str, list[date]],
                   as_of: date) -> ReleaseTiming:
    """When the row's shown observation printed, and when its next one does.

    `first_release` maps each series' observation dates to the day each was
    first published; `calendars` holds each series' release dates, scheduled
    ones included. A composite or a share moves only when its LAST input has
    the next observation, so both dates are the latest across its inputs --
    for each input, the day that observation printed if it has, else that
    input's next scheduled release.
    """
    if shown is None:
        return ReleaseTiming(None, None)
    latest_each, next_each, estimated = [], [], False
    for code in ind.codes:
        fr = first_release.get(code) or {}
        latest_each.append(fr.get(shown))
        later = [d for d in fr if d > shown]
        if later:
            next_each.append(fr[min(later)])
            continue
        floor = max([as_of - timedelta(days=1)] + ([fr[shown]] if shown in fr else []))
        upcoming = [d for d in calendars.get(code, []) if d > floor]
        if upcoming:
            next_each.append(min(upcoming))
            continue
        projected = _projected(list(fr.values()), floor)
        next_each.append(projected)
        estimated = estimated or projected is not None

    latest = None if None in latest_each else max(latest_each)
    nxt = None if None in next_each else max(next_each)
    return ReleaseTiming(latest, nxt, estimated and nxt is not None)


#: A first-release date further than this after its reference date is not a
#: release, so the observation falls back to its fixed lag. Australia has no
#: ALFRED-style archive of first-print dates: `first_release` is normally empty
#: here and every row dates from its release family's fixed lag.
MAX_RELEASE_LAG = timedelta(days=150)

CODE_LAG = {c: ind.lag_days for ind in INDICATORS for c in ind.codes}

#: Each release lag names a family in `core.publication`, whose rule (exact
#: Labour Force dates where known, otherwise a late rule) dates the print.
LAG_FAMILY = {LAG_LABOUR_FORCE: pub.LABOUR_FORCE, LAG_LF_DETAILED: pub.LF_DETAILED,
              LAG_VACANCIES: pub.VACANCIES, LAG_NAB: pub.NAB,
              LAG_SENTIMENT: pub.WESTPAC_MI, LAG_WPI: pub.WPI}
CODE_FAMILY = {c: LAG_FAMILY[lag] for c, lag in CODE_LAG.items()}


def as_published(data: dict[str, list[tuple[date, float]]], as_of: date | None,
                 first_release: dict[str, dict[date, date]],
                 exact_lf: dict[date, date] | None = None
                 ) -> dict[str, list[tuple[date, float]]]:
    """Every series cut to what had printed by `as_of`.

    An observation counts from the day it was first published where that is
    known (`first_release`), else from its release family's rule in
    `core.publication` -- exact Labour Force days from `exact_lf`, a late rule
    otherwise. The result is already cut: pass it on with `as_of=None`.
    """
    if as_of is None:
        return data
    out = {}
    for code, rows in data.items():
        fr = first_release.get(code) or {}
        family = CODE_FAMILY.get(code, pub.LABOUR_FORCE)
        kept = []
        for d, v in rows:
            rel = fr.get(d)
            printed = (rel if rel is not None and rel - d <= MAX_RELEASE_LAG
                       else pub.public_on(family, d, exact_lf))
            if printed <= as_of:
                kept.append((d, v))
        out[code] = kept
    return out


def month_after(m: date) -> date:
    return _months_before(m, -1)


def indicator_month(ind: Indicator, data: dict[str, list[tuple[date, float]]],
                    change_months: int = CHANGE_MONTHS) -> date | None:
    """The latest whole month one indicator has, first of the month. A weekly
    series has a month once the last week ending in it is in."""
    rows = transform(ind, data, change_months)
    if not rows:
        return None
    last = rows[-1][0]
    if last.day == 1:
        return last
    first = date(last.year, last.month, 1)
    return first if last + timedelta(days=7) >= month_after(first) else _months_before(first, 1)


def complete_month(data: dict[str, list[tuple[date, float]]],
                   change_months: int = CHANGE_MONTHS) -> date | None:
    """The latest month every indicator has printed, first of the month.

    The views that read the indicators together -- the overall indicator, the
    diffusion index and its channels -- stop here, so a month never shows
    with some indicators on it and others still a month behind. An indicator
    with no data at all is left out rather than blanking the rest.
    """
    months = [m for m in (indicator_month(ind, data, change_months) for ind in INDICATORS)
              if m is not None]
    if not months:
        return None
    # A row whose source has stopped -- the Labour Force Detailed rows froze at
    # the April 2026 survey changes -- would otherwise hold every other row back
    # to its last month. More than STALL_MONTHS behind the newest, it is left out
    # of the gate and shown with its last reading, flagged as stalled.
    newest = max(months)
    live = [m for m in months if _months_between(m, newest) <= STALL_MONTHS]
    return min(live)


#: How far a row may trail the newest month before it stops gating the rest.
STALL_MONTHS = 3


def _months_between(a: date, b: date) -> int:
    return (b.year * 12 + b.month) - (a.year * 12 + a.month)


def stalled(data: dict[str, list[tuple[date, float]]],
            change_months: int = CHANGE_MONTHS) -> list[Indicator]:
    """Indicators whose latest month trails the newest by more than STALL_MONTHS."""
    months = {ind.key: indicator_month(ind, data, change_months) for ind in INDICATORS}
    have = [m for m in months.values() if m is not None]
    if not have:
        return []
    newest = max(have)
    return [BY_KEY[k] for k, m in months.items()
            if m is not None and _months_between(m, newest) > STALL_MONTHS]


def through_month(data: dict[str, list[tuple[date, float]]],
                  month: date | None) -> dict[str, list[tuple[date, float]]]:
    """Every series cut to observations dated inside or before `month`."""
    if month is None:
        return data
    end = month_after(month)
    return {c: [(d, v) for d, v in rows if d < end] for c, rows in data.items()}


def transform(ind: Indicator, data: dict[str, list[tuple[date, float]]],
              change_months: int = CHANGE_MONTHS,
              as_of: date | None = None) -> list[tuple[date, float]]:
    """Apply the indicator's transform, returning `(date, value)` sorted."""
    frames = [f for f in (published(sorted(data.get(c) or []), ind, as_of)
                          for c in ind.series) if f]
    if not frames:
        return []

    if len(frames) == 1:
        base = frames[0]
    else:
        common = set(d for d, _ in frames[0])
        for f in frames[1:]:
            common &= {d for d, _ in f}
        if not common:
            return []
        lookup = [dict(f) for f in frames]
        base = [(d, statistics.fmean([lk[d] for lk in lookup])) for d in sorted(common)]

    if ind.transform == "level":
        return base

    if ind.transform == "share":
        denom = dict(published(sorted(data.get(ind.denominator) or []), ind, as_of))
        return [(d, 100.0 * v / denom[d]) for d, v in base
                if denom.get(d) not in (None, 0)]

    if ind.transform in ("diff3", "pct3"):
        lookup = dict(base)
        out: list[tuple[date, float]] = []
        for d, v in base:
            prior = lookup.get(_months_before(d, change_months))
            if prior is None:
                continue
            if ind.transform == "diff3":
                out.append((d, v - prior))
            elif prior:
                out.append((d, 100.0 * (v - prior) / abs(prior)))
        return out

    return base


def assess(ind: Indicator, data: dict[str, list[tuple[date, float]]],
           base_start: date = BASE_START,
           change_months: int = CHANGE_MONTHS,
           as_of: date | None = None) -> Reading:
    """One indicator's current and year-ago slack percentile."""
    rows = [r for r in transform(ind, data, change_months, as_of)
            if r[0] >= base_start]
    if len(rows) < MIN_RANK_OBS:
        return Reading(ind, None, None, None, None, len(rows))

    pool = [v for _, v in rows]
    latest_date, latest_value = rows[-1]
    prior = _at_or_before(rows, _months_before(latest_date, YEAR_AGO_MONTHS))

    def oriented(value: float) -> float:
        pct = percentile_rank(pool, value)
        return pct if ind.direction > 0 else 100.0 - pct

    return Reading(
        indicator=ind,
        current_pct=oriented(latest_value),
        year_ago_pct=oriented(prior[1]) if prior else None,
        current_value=latest_value,
        as_of=latest_date,
        n_obs=len(rows),
    )


def dashboard(data: dict[str, list[tuple[date, float]]],
              base_start: date = BASE_START,
              change_months: int = CHANGE_MONTHS,
              as_of: date | None = None) -> list[Reading]:
    """Every indicator, in the source chart's order."""
    return [assess(ind, data, base_start, change_months, as_of)
            for ind in INDICATORS]


def summarise(readings: list[Reading]) -> dict[str, float | int]:
    """Headline counts -- how broad the move is, not just where the level sits."""
    live = [r for r in readings if r.ok]
    return {
        "n": len(live),
        "median_pct": statistics.median([r.current_pct for r in live]) if live else 0.0,
        "tightening": sum(1 for r in live if r.travel == "tightening"),
        "cooling": sum(1 for r in live if r.travel == "cooling"),
        "flat": sum(1 for r in live if r.travel == "flat"),
    }


HEATMAP_MONTHS = 12

HEATMAP_WINDOWS = (12, 18, 24)


@dataclass(frozen=True)
class HeatCell:
    month: date                 # reference month, keyed to the first
    pct: float | None           # slack-oriented percentile, None = nothing printed
    value: float | None         # transformed units, for the tooltip


@dataclass(frozen=True)
class HeatRow:
    indicator: Indicator
    cells: tuple[HeatCell, ...]
    n_obs: int                  # months in the ranking pool, not in the row

    @property
    def ok(self) -> bool:
        return any(c.pct is not None for c in self.cells)

    @property
    def latest(self) -> HeatCell | None:
        """Rightmost populated cell -- the dashboard's dot for this row."""
        for cell in reversed(self.cells):
            if cell.pct is not None:
                return cell
        return None


@dataclass(frozen=True)
class Heatmap:
    months: tuple[date, ...]
    rows: tuple[HeatRow, ...]

    @property
    def ok(self) -> bool:
        return any(r.ok for r in self.rows)

    def row(self, key: str) -> HeatRow | None:
        return next((r for r in self.rows if r.indicator.key == key), None)


def _heat_row(ind: Indicator, rows: list[tuple[date, float]],
              months: tuple[date, ...]) -> HeatRow:
    """One indicator's percentile for each month on the axis."""
    by_month = _monthly(rows)
    if len(by_month) < MIN_RANK_OBS:
        return HeatRow(ind, tuple(HeatCell(m, None, None) for m in months),
                       len(by_month))
    pool = list(by_month.values())

    def oriented(value: float) -> float:
        pct = percentile_rank(pool, value)
        return pct if ind.direction > 0 else 100.0 - pct

    cells = tuple(
        HeatCell(m, oriented(by_month[m]) if m in by_month else None,
                 by_month.get(m))
        for m in months)
    return HeatRow(ind, cells, len(by_month))


def heatmap(data: dict[str, list[tuple[date, float]]],
            base_start: date = BASE_START,
            change_months: int = CHANGE_MONTHS,
            as_of: date | None = None,
            months: int = HEATMAP_MONTHS) -> Heatmap:
    """Every indicator's slack percentile across the last `months` months."""
    prepared = [
        (ind, [r for r in transform(ind, data, change_months, as_of)
               if r[0] >= base_start])
        for ind in INDICATORS
    ]
    latest = max((date(rows[-1][0].year, rows[-1][0].month, 1)
                  for _, rows in prepared if rows), default=None)
    if latest is None:
        anchor = as_of or clock.today()
        latest = date(anchor.year, anchor.month, 1)
    axis = tuple(_months_before(latest, months - 1 - k) for k in range(months))
    return Heatmap(months=axis,
                   rows=tuple(_heat_row(ind, rows, axis) for ind, rows in prepared))


DIFFUSION_SMOOTH = 3

MIN_DIFFUSION_N = 12


@dataclass(frozen=True)
class DiffusionPoint:
    when: date
    mom: float | None       # share tightening vs last month, smoothed, 0-100
    yoy: float | None       # share tightening vs a year ago, smoothed, 0-100
    n: int                  # indicators reporting


def _monthly(rows: list[tuple[date, float]]) -> dict[date, float]:
    """One value per month, keyed to the first of the month."""
    buckets: dict[date, list[float]] = {}
    for d, v in rows:
        buckets.setdefault(date(d.year, d.month, 1), []).append(v)
    return {k: statistics.fmean(vs) for k, vs in buckets.items()}


def _votes(series: dict[str, dict[date, float]],
           when: date, months_back: int) -> dict[str, float]:
    """Each reporting indicator's vote over `months_back`: 1 improved, a half
    unchanged, 0 deteriorated -- "improved" in that series' own direction."""
    prior_month = _months_before(when, months_back)
    out: dict[str, float] = {}
    for key, rows in series.items():
        now, prior = rows.get(when), rows.get(prior_month)
        if now is None or prior is None:
            continue
        move = (now - prior) * BY_KEY[key].direction
        out[key] = 1.0 if move > 0 else 0.5 if move == 0 else 0.0
    return out


def _share(votes: dict[str, float]) -> float | None:
    return 100.0 * sum(votes.values()) / len(votes) if votes else None


def _share_tightening(series: dict[str, dict[date, float]],
                      when: date, months_back: int) -> tuple[float | None, int]:
    """Share of indicators that improved over `months_back`, 0-100."""
    votes = _votes(series, when, months_back)
    return _share(votes), len(votes)


def _indicator_series(data: dict[str, list[tuple[date, float]]], change_months: int,
                      as_of: date | None) -> dict[str, dict[date, float]]:
    """Every indicator that has data, transformed and one value per month."""
    series = {ind.key: _monthly(transform(ind, data, change_months, as_of))
              for ind in INDICATORS}
    return {k: v for k, v in series.items() if v}


def _monthly_votes(data: dict[str, list[tuple[date, float]]], change_months: int,
                   as_of: date | None) -> list[tuple[date, dict[str, float], dict[str, float]]]:
    """(month, month-over-month votes, year-over-year votes) for every month
    enough indicators report. The headline and its channel decomposition both
    read this one tally, so the two cannot drift apart."""
    return _tally(_indicator_series(data, change_months, as_of))


def _tally(series: dict[str, dict[date, float]]
           ) -> list[tuple[date, dict[str, float], dict[str, float]]]:
    if not series:
        return []
    out = []
    for m in sorted(set().union(*(set(v) for v in series.values()))):
        mom, yoy = _votes(series, m, 1), _votes(series, m, YEAR_AGO_MONTHS)
        if max(len(mom), len(yoy)) < MIN_DIFFUSION_N:
            continue
        out.append((m, mom, yoy))
    return out


def diffusion(data: dict[str, list[tuple[date, float]]],
              change_months: int = CHANGE_MONTHS,
              smooth: int = DIFFUSION_SMOOTH,
              as_of: date | None = None) -> list[DiffusionPoint]:
    """Share of indicators tightening, month-over-month and year-over-year."""
    raw = [(m, _share(mom), _share(yoy), max(len(mom), len(yoy)))
           for m, mom, yoy in _monthly_votes(data, change_months, as_of)]

    out: list[DiffusionPoint] = []
    for i, (m, _, _, n) in enumerate(raw):
        window = raw[max(0, i - smooth + 1):i + 1]
        if len(window) < smooth:
            continue

        def avg(idx: int) -> float | None:
            vals = [w[idx] for w in window if w[idx] is not None]
            return statistics.fmean(vals) if len(vals) == smooth else None

        out.append(DiffusionPoint(m, avg(1), avg(2), n))
    return out


# ------------------------------------------------------------ channels

@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    members: tuple[str, ...]     # Indicator keys; the channels partition INDICATORS
    reads: str                   # what the channel measures, and why a desk watches it


#: The labour market by the margin each indicator measures. The order is the
#: stacking order, nearest the 50 line first: the flow margins that call a
#: turn, then firms' stated demand, realised employment, bargaining power,
#: labour supply, and last the slack stock, which confirms a turn after the fact.
#:
#: Chosen on economics and checked on the data (Sep 2026): over 2001-2026 ex
#: Covid, each indicator's 12-month change should move with its own channel
#: more than with the rest. This grouping lifts the inside-less-across
#: correlation gap from +0.17 to +0.26 over the first six-channel draft, and
#: the indicators that fit their own channel best from 13 of 29 to 18. EPOP
#: moved to employment (it tracks payrolls at 0.72, not participation); the
#: NILF-unemployment flows moved to slack (they scale with the unemployed pool,
#: 0.67). Two calls overrule the statistics on purpose: Firing is kept though
#: its noisy weekly and CPS inputs hold its gap to +0.07, because it is the
#: recession channel; and realised hiring is split from stated demand though
#: the two co-move at 0.65, because their divergence is the point.
CHANNELS: tuple[Channel, ...] = (
    Channel("firing", "Firing", ("f_eu",),
            "Separations into unemployment. Australia has no claims and no JOLTS, "
            "so this channel is the one flow -- thin, and read with that in mind."),
    Channel("hiring", "Hiring", ("f_ue", "f_ne"),
            "Hiring that happened: the gross-flow job-finding rates. Weak here beside "
            "benign firing is the low-hire, low-fire market."),
    Channel("demand", "Stated demand", ("vac", "vu", "nab_cap", "nab_cond"),
            "What firms say they want: vacancies, the V/U ratio and the NAB survey. "
            "Read against Hiring -- demand that does not turn into hires points to a "
            "supply constraint or to intentions that will not last."),
    Channel("employment", "Employment & hours", ("emp", "hours", "epop", "ft"),
            "Realised employment and hours, and the full-time share firms lift when "
            "labour is short."),
    Channel("confidence", "Worker confidence", ("switch", "wpi", "sent"),
            "Workers moving by choice and wages still rising: bargaining power, the "
            "channel that carries into wage growth and so the inflation side."),
    Channel("participation", "Participation", ("pr",),
            "Labour supply. Australia's participation rate sits near its record, so "
            "a fall here can be supply as much as demand."),
    Channel("slack", "Slack", ("ur", "und", "util", "youth", "mtu", "f_nu", "f_un"),
            "The stock of under-used labour and the churn at the edge of the "
            "unemployed pool. It lags the flows, so it confirms a turn rather than "
            "calling it."),
)

CHANNEL_OF = {k: c.key for c in CHANNELS for k in c.members}

DIFFUSION_BASES = ("yoy", "mom")


@dataclass(frozen=True)
class Component:
    """One indicator's part in its channel's bar."""
    contribution: float                 # pp of (share - 50); a channel's components sum to its bar
    votes: tuple[float | None, ...]     # each month of the window, oldest first; None = not reporting
    now: float | None                   # the latest month's value, in the indicator's own units
    prior: float | None                 # what it is compared with: a month or a year earlier


@dataclass(frozen=True)
class ChannelPoint:
    when: date
    share: float                        # the headline, exactly as diffusion() has it
    contribution: dict[str, float]      # pp of (share - 50); they sum to share - 50
    breadth: dict[str, float | None]    # each channel's own diffusion, 0-100
    reporting: dict[str, int]           # indicators reporting, last month of the window
    n: int                              # all indicators reporting, same month
    components: dict[str, Component] = field(default_factory=dict)   # by indicator key
    months: tuple[date, ...] = ()       # the months averaged, oldest first


def channel_contributions(data: dict[str, list[tuple[date, float]]],
                          basis: str = "yoy",
                          change_months: int = CHANGE_MONTHS,
                          smooth: int = DIFFUSION_SMOOTH,
                          as_of: date | None = None) -> list[ChannelPoint]:
    """The headline diffusion index, split exactly into its channels.

    Each indicator's vote v is 1, a half or 0, so over the n that report

        share - 50 = sum over channels c of (100 / n) * sum over i in c of (v_i - 1/2)

    -- an identity, not a regression, and a tie contributes nothing. The
    smoothing is a plain average, which is linear, so it passes through the
    sum: the smoothed contributions still add to the smoothed headline.

    Month by month a channel's contribution is also (n_c / n) * (breadth_c - 50):
    its weight, set by how many indicators it holds, times how lopsided it is
    inside. `breadth` is the second factor alone, so a big channel moving a
    little can be told from a small one moving hard.
    """
    idx = {"mom": 1, "yoy": 2}[basis]
    back = {"mom": 1, "yoy": YEAR_AGO_MONTHS}[basis]
    series = _indicator_series(data, change_months, as_of)
    raw: list[tuple | None] = []
    for row in _tally(series):
        votes = row[idx]
        if not votes:
            raw.append(None)
            continue
        n = len(votes)
        by = {c.key: [votes[k] for k in c.members if k in votes] for c in CHANNELS}
        raw.append((row[0], _share(votes), n,
                    {k: 100.0 * sum(v - 0.5 for v in vs) / n for k, vs in by.items()},
                    {k: 100.0 * sum(vs) / len(vs) if vs else None for k, vs in by.items()},
                    {k: len(vs) for k, vs in by.items()},
                    {k: (v, 100.0 * (v - 0.5) / n) for k, v in votes.items()}))

    out: list[ChannelPoint] = []
    for i in range(smooth - 1, len(raw)):
        window = raw[i - smooth + 1:i + 1]
        if any(w is None for w in window):
            continue
        when, _, n, _, _, reporting, _ = window[-1]

        def breadth(key: str) -> float | None:
            vals = [w[4][key] for w in window if w[4][key] is not None]
            return statistics.fmean(vals) if len(vals) == smooth else None

        def component(key: str) -> Component:
            # A month the indicator did not report adds nothing, which keeps
            # the components summing exactly to the channel's bar.
            rows = series.get(key, {})
            return Component(
                contribution=statistics.fmean(w[6].get(key, (None, 0.0))[1] for w in window),
                votes=tuple(w[6].get(key, (None, 0.0))[0] for w in window),
                now=rows.get(when), prior=rows.get(_months_before(when, back)))

        out.append(ChannelPoint(
            when=when,
            share=statistics.fmean(w[1] for w in window),
            contribution={c.key: statistics.fmean(w[3][c.key] for w in window)
                          for c in CHANNELS},
            breadth={c.key: breadth(c.key) for c in CHANNELS},
            reporting=reporting, n=n,
            components={i.key: component(i.key) for i in INDICATORS},
            months=tuple(w[0] for w in window)))
    return out
