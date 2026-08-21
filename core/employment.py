"""Full-employment indicators, scored against their own history.

A reconstruction of the standard "how far is the labour market from full
employment" scorecard: nine indicators, each expressed as a z-score against a
fixed historical window, plotted at two dates so the direction of travel is
visible alongside the level.

WHY THIS IS REPRODUCIBLE WHERE THE RBA'S OWN VERSION IS NOT
------------------------------------------------------------
The RBA publishes a similar panel in its Statement on Monetary Policy, but
scores each indicator as a gap from an "estimated trend" -- and it publishes
neither the filters it detrends with, nor the smoothing parameters, nor how it
rescales each series into unemployment-rate units. Those choices set both the
dots and the width of the ranges, so that chart cannot be rebuilt from source
data; it can only be read off.

A z-score against a stated window has none of that freedom:

    z = (x - mean(window)) / stdev(window)

Every term is determined by the data and one date range. Two people with the
same series get the same number. That is the whole reason this module scores
rather than detrends.

SIGN: POSITIVE ALWAYS MEANS TIGHTER
------------------------------------
The nine indicators do not point the same way -- a high unemployment rate and a
high vacancies-to-unemployment ratio mean opposite things. Plotting raw
z-scores would put slack and tightness on the same side of zero and make the
panel unreadable, so the five slack measures carry `invert=True` and have their
sign flipped after scoring. After that, right of zero is a tighter labour
market than the window average for every row, and the average across rows means
something.

THE WINDOW IS A JUDGEMENT, AND IT IS THE ONLY ONE
--------------------------------------------------
2000-2020 is conventional for this panel: long enough to cover two full cycles,
and it stops before the pandemic distortion. It is also the single assumption
that moves every number, which is why it is a parameter with a default rather
than a constant buried in the arithmetic. Shifting the window end from 2020 to
2024 moves the unemployment z-score by roughly half a standard deviation.

No Streamlit imports, and no `data/` imports: every function here takes plain
`(date, value)` pairs, so the whole panel is callable from a test.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

Series = Sequence[tuple[date, float]]

# The scoring window. Two full cycles, ending before the pandemic.
WINDOW_START = date(2000, 1, 1)
WINDOW_END = date(2020, 12, 31)


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    source: str
    # True when a HIGHER raw reading means a LOOSER labour market, so the
    # z-score is negated to keep "right of zero = tighter" true for every row.
    invert: bool
    unit: str = ""
    decimals: int = 4
    # Set when no free series exists and the number has to be typed in.
    manual: bool = False
    note: str = ""


INDICATORS: tuple[Indicator, ...] = (
    Indicator("unemployment", "Unemployment Rate", "ABS A84423050A", True, "%",
              note="Seasonally adjusted, monthly."),
    Indicator("underemployment", "Underemployment Rate", "ABS A85255725J", True, "%",
              note="Employed people who want and are available for more hours."),
    Indicator("underutilisation", "Underutilisation Rate", "ABS (derived)", True, "%",
              note="Unemployment plus underemployment, the ABS's own definition."),
    Indicator("medium_term", "Medium-term Unemployment Rate", "ABS 6291 T14a", True, "%",
              note="Searching between one month and one year, as a share of the "
                   "labour force. Past frictional churn, short of long-term "
                   "detachment. Detailed release, so it lags the rest."),
    Indicator("youth", "Youth Unemployment", "ABS A84424185C", True, "%",
              note="15-24 year olds, seasonally adjusted."),
    Indicator("vacancies_to_unemployment", "Vacancies-to-Unemployment", "RBA H5", False, "",
              note="Job vacancies over unemployed persons, x100. Vacancies are "
                   "quarterly, so this pairs the latest of each."),
    Indicator("labour_constraint", "Firms Reporting Labour Constraint",
              "NAB Business Survey", False, "%", manual=True,
              note="Commercial series -- no free feed. Type the reading."),
    Indicator("job_ads", "Job Ads (Share of Labour Force)", "ANZ-Indeed", False, "%",
              manual=True, note="Commercial series -- no free feed. Type the reading."),
    Indicator("employment_intentions", "Employment Intentions",
              "NAB Business Survey", False, "", manual=True,
              note="Commercial series -- no free feed. Type the reading."),
)

BY_KEY = {i.key: i for i in INDICATORS}


@dataclass(frozen=True)
class Stats:
    """Mean and standard deviation over the scoring window."""
    mean: float
    sd: float
    n: int
    start: date
    end: date

    @property
    def ok(self) -> bool:
        return self.n >= 2 and self.sd > 0


def window_stats(series: Series, start: date = WINDOW_START,
                 end: date = WINDOW_END) -> Stats | None:
    """Sample mean and standard deviation over `[start, end]`.

    Sample (n-1), not population: the window is a sample of the regime, not the
    whole population of possible months. With ~250 monthly observations the
    difference is immaterial to the z-score, but the estimator should still say
    what it is.
    """
    vals = [v for d, v in series if start <= d <= end]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    inside = [d for d, _ in series if start <= d <= end]
    return Stats(mean, math.sqrt(var), len(vals), min(inside), max(inside))


def value_at(series: Series, when: date | None = None) -> tuple[date, float] | None:
    """The observation on or before `when`, or the latest when `when` is None.

    On-or-before rather than exact: these series are monthly and stamped on the
    first of the month, so asking for "December 2025" with a 31 December date
    would otherwise miss.
    """
    if not series:
        return None
    rows = sorted(series, key=lambda p: p[0])
    if when is None:
        return rows[-1]
    eligible = [p for p in rows if p[0] <= when]
    return eligible[-1] if eligible else None


@dataclass(frozen=True)
class Reading:
    when: date
    value: float
    z: float

    @property
    def tighter(self) -> bool:
        return self.z >= 0


@dataclass(frozen=True)
class Row:
    indicator: Indicator
    stats: Stats | None
    current: Reading | None
    prior: Reading | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.current is not None

    @property
    def delta_z(self) -> float | None:
        """Change in the z-score between the two dates. Positive = tightened."""
        if self.current is None or self.prior is None:
            return None
        return self.current.z - self.prior.z

    @property
    def stale_vs(self) -> date | None:
        """The current reading's date, for a panel-level staleness check."""
        return self.current.when if self.current else None


def score(indicator: Indicator, series: Series, prior_date: date,
          start: date = WINDOW_START, end: date = WINDOW_END,
          current_date: date | None = None) -> Row:
    """One indicator, scored at two dates against the same window."""
    stats = window_stats(series, start, end)
    if stats is None or not stats.ok:
        return Row(indicator, stats, None, None,
                   error="not enough history in the scoring window")

    sign = -1.0 if indicator.invert else 1.0

    def read(when: date | None) -> Reading | None:
        got = value_at(series, when)
        if got is None:
            return None
        d, v = got
        return Reading(d, v, sign * (v - stats.mean) / stats.sd)

    return Row(indicator, stats, read(current_date), read(prior_date))


def manual_row(indicator: Indicator, value: float | None, mean: float | None,
               sd: float | None, prior_value: float | None,
               when: date, prior_when: date) -> Row:
    """A row for an indicator with no free series, scored from typed inputs.

    Deliberately requires the window mean and standard deviation rather than
    accepting a z directly. A typed z is unfalsifiable -- nothing on screen
    would show what it was measured against -- whereas a mean and a standard
    deviation can be checked, and they make the row behave identically to a
    computed one when the underlying value changes.
    """
    if value is None or mean is None or sd is None or sd <= 0:
        return Row(indicator, None, None, None,
                   error="needs a value, a window mean and a window sd")
    stats = Stats(mean, sd, 0, WINDOW_START, WINDOW_END)
    sign = -1.0 if indicator.invert else 1.0
    cur = Reading(when, value, sign * (value - mean) / sd)
    pri = (Reading(prior_when, prior_value, sign * (prior_value - mean) / sd)
           if prior_value is not None else None)
    return Row(indicator, stats, cur, pri)


@dataclass(frozen=True)
class Panel:
    rows: list[Row] = field(default_factory=list)
    prior_date: date | None = None
    window: tuple[date, date] = (WINDOW_START, WINDOW_END)

    @property
    def scored(self) -> list[Row]:
        return [r for r in self.rows if r.ok]

    @property
    def total_z(self) -> float | None:
        """Unweighted mean of the scored rows.

        Unweighted because there is no defensible weighting: these indicators
        overlap heavily -- underutilisation is literally unemployment plus
        underemployment -- and any weighting scheme would be a second
        undocumented judgement on top of the window. The count is reported
        alongside so a total built from six rows is not read as one built from
        nine.
        """
        zs = [r.current.z for r in self.scored]
        return sum(zs) / len(zs) if zs else None

    @property
    def total_prior_z(self) -> float | None:
        zs = [r.prior.z for r in self.rows if r.prior is not None]
        return sum(zs) / len(zs) if zs else None

    @property
    def total_delta(self) -> float | None:
        """Mean change in z-score, over rows carrying BOTH a current and a
        prior reading.

        Deliberately NOT `total_z - total_prior_z`. Those two average over
        their own best-available population -- correct for the two dots the
        chart plots -- but a computed row's `current` and `prior` come from
        the same series and are present or absent together, whereas a
        `manual_row` takes them from two independently typed fields (see
        `app.py`'s Latest/prior text inputs). Typing a current reading and
        leaving the prior field blank puts that row in `total_z`'s average
        but not `total_prior_z`'s, so their difference reads a population
        mismatch as if it were the panel moving. Averaging `delta_z` instead
        is the same arithmetic wherever the population already matches (the
        mean of a difference equals the difference of means over one set of
        rows) and simply excludes a row from the comparison when it doesn't.
        """
        deltas = [r.delta_z for r in self.scored if r.delta_z is not None]
        return sum(deltas) / len(deltas) if deltas else None

    @property
    def n_scored(self) -> int:
        return len(self.scored)

    @property
    def n_tighter(self) -> int:
        return sum(1 for r in self.scored if r.current.z >= 0)

    @property
    def n_tightening(self) -> int:
        return sum(1 for r in self.scored
                   if r.delta_z is not None and r.delta_z > 0)

    @property
    def missing(self) -> list[Row]:
        return [r for r in self.rows if not r.ok]

    @property
    def as_of(self) -> date | None:
        dates = [r.current.when for r in self.scored]
        return max(dates) if dates else None

    @property
    def oldest_reading(self) -> Row | None:
        """The row holding the panel back.

        Worth surfacing rather than hiding behind a single as-of date: the
        medium-term unemployment rate comes from the Detailed release, which
        can sit months behind the headline one, and a panel is only as current
        as its stalest row.
        """
        got = [r for r in self.scored if r.current]
        return min(got, key=lambda r: r.current.when) if got else None


# Joining across sources needs a month key, not a date. The ABS stamps its
# monthly series on the FIRST of the month; the RBA stamps its H5 series on the
# LAST. Intersecting raw dates across the two therefore returns the empty set --
# silently, and with no error to notice. Every cross-source join below goes
# through `_by_month` for that reason.
def _by_month(series: Series) -> dict[tuple[int, int], tuple[date, float]]:
    out: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in series:
        out[(d.year, d.month)] = (d, v)
    return out


def _join(*series: Series) -> list[tuple[date, list[float]]]:
    """Month-aligned inner join. The date carried forward is the EARLIEST of
    the contributing stamps, so a derived series keeps the ABS month-start
    convention the rest of the panel uses."""
    maps = [_by_month(s) for s in series]
    if not maps:
        return []
    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    rows = []
    for key in sorted(common):
        stamps = [m[key][0] for m in maps]
        rows.append((min(stamps), [m[key][1] for m in maps]))
    return rows


def derive_underutilisation(unemployment: Series, underemployment: Series) -> list[tuple[date, float]]:
    """Underutilisation = unemployment + underemployment.

    The ABS publishes this directly, but deriving it keeps the three rows
    arithmetically consistent with each other on screen -- and it is the ABS's
    own definition, not an approximation.
    """
    return [(d, vals[0] + vals[1]) for d, vals in _join(unemployment, underemployment)]


def derive_medium_term(buckets: Sequence[Series], labour_force: Series
                       ) -> list[tuple[date, float]]:
    """Unemployed in the given duration buckets as a share of the labour force.

    Counts are in thousands and the labour force is too, so the ratio is unit
    free and the result is a percentage.
    """
    if not buckets:
        return []
    rows = _join(*buckets, labour_force)
    return [(d, sum(vals[:-1]) / vals[-1] * 100.0) for d, vals in rows if vals[-1]]


def derive_ratio(numerator: Series, denominator: Series, scale: float = 100.0,
                 pair_latest: bool = False) -> list[tuple[date, float]]:
    """A month-aligned ratio of two series, scaled.

    `pair_latest` appends one extra point built from the most recent
    observation of EACH series regardless of whether they share a month, and
    exists because vacancies-to-unemployment needs it. Vacancies are quarterly
    and unemployment monthly, so waiting for a shared month throws away up to
    two months of the unemployment leg and reports a ratio the market has
    already moved past. Pairing the latest of each is what the published
    version of this panel does, and it changes the current reading materially:
    on the August 2026 data, 48.88 on shared months against 47.97 paired.

    The appended point never lands inside the scoring window -- it is by
    construction the newest date in the series -- so the window statistics are
    untouched by it.
    """
    rows = [(d, vals[0] / vals[1] * scale)
            for d, vals in _join(numerator, denominator) if vals[1]]
    if pair_latest and numerator and denominator:
        n_d, n_v = max(numerator, key=lambda p: p[0])
        d_d, d_v = max(denominator, key=lambda p: p[0])
        if d_v:
            when = max(n_d, d_d)
            rows = [r for r in rows if r[0] < when] + [(when, n_v / d_v * scale)]
    return rows


# --------------------------------------------------------------------------
# Labour market flows
# --------------------------------------------------------------------------
# The unemployment rate is a stock, and a stock can sit still while the flows
# underneath it turn. Three series that turn first, all put on one z-score axis
# because they are in incompatible units -- a transition rate, a tenure share
# and a wage growth rate -- and the question asked of them is about direction
# rather than level.

# The flow measures are plotted led by a quarter against wages. Tightness shows
# up in the flows before it shows up in pay: a worker who moves in March
# negotiates a wage that lands in the June index. Led rather than lagged, so
# the flow line sits above the wage print it is meant to explain.
FLOW_LEAD_QUARTERS = 1


def job_finding_rate(flows: dict[str, Series],
                     employed: Sequence[str] = ("Employed full-time",
                                                "Employed part-time"),
                     unemployed: str = "Unemployed") -> list[tuple[date, float]]:
    """Share of last month's unemployed who are employed this month.

    The denominator is everyone who was unemployed a month ago, recovered by
    adding up every flow OUT of unemployment -- including the flow back into
    unemployment, which is the largest of them. Taking it from the published
    unemployment level instead would mix two different weightings: the flows
    cube reweights the matched sample, so its own row totals are the only
    denominator the numerator is consistent with.
    """
    out_of_u = {k: v for k, v in flows.items() if k.startswith(f"{unemployed}>")}
    if not out_of_u:
        return []
    found = [v for k, v in out_of_u.items()
             if k.split(">", 1)[1] in set(employed)]
    if not found:
        return []

    total: dict[date, float] = {}
    for obs in out_of_u.values():
        for d, v in obs:
            total[d] = total.get(d, 0.0) + v
    hired: dict[date, float] = {}
    for obs in found:
        for d, v in obs:
            hired[d] = hired.get(d, 0.0) + v

    return [(d, hired[d] / total[d] * 100.0)
            for d in sorted(hired) if total.get(d)]


def job_switching_rate(under_12m: Series, over_12m: Series
                       ) -> list[tuple[date, float]]:
    """Employed under a year with their current employer, as a share of all.

    A proxy, and worth naming as one: Australia publishes no quits rate, so
    this stands in for one. It counts anyone who started a job in the last year
    — which includes people who moved from unemployment or from outside the
    labour force, not only people who switched between jobs. The level is
    therefore higher than a true switching rate; the direction is the signal.
    """
    rows = _join(under_12m, over_12m)
    return [(d, vals[0] / (vals[0] + vals[1]) * 100.0)
            for d, vals in rows if (vals[0] + vals[1])]


def to_quarterly(series: Series) -> list[tuple[date, float]]:
    """Average a monthly series within each calendar quarter.

    Averaged rather than sampled at quarter end: a single month of the Labour
    Force survey carries enough sampling noise that picking one and discarding
    the other two would put that noise straight into the z-score.
    """
    buckets: dict[tuple[int, int], list[float]] = {}
    for d, v in series:
        buckets.setdefault((d.year, (d.month - 1) // 3), []).append(v)
    return [(date(y, q * 3 + 1, 1), sum(vs) / len(vs))
            for (y, q), vs in sorted(buckets.items())]


def lead(series: Series, quarters: int = FLOW_LEAD_QUARTERS
         ) -> list[tuple[date, float]]:
    """Shift a quarterly series FORWARD, so period t carries t+n's reading."""
    rows = sorted(series, key=lambda p: p[0])
    if quarters <= 0:
        return list(rows)
    return [(rows[i][0], rows[i + quarters][1])
            for i in range(len(rows) - quarters)]


def zscores(series: Series, start: date | None = None,
            end: date | None = None) -> list[tuple[date, float]]:
    """Every observation as a z-score against the window's mean and sd.

    The same arithmetic the indicator panel scores one reading with, applied
    across the whole series so it can be plotted. Window defaults to the
    series' own full history, which is what "against its long-run average"
    means on a chart with no window control of its own.
    """
    rows = sorted(series, key=lambda p: p[0])
    if len(rows) < 2:
        return []
    lo = start or rows[0][0]
    hi = end or rows[-1][0]
    stats = window_stats(rows, lo, hi)
    if stats is None or stats.sd <= 0:
        return []
    return [(d, (v - stats.mean) / stats.sd) for d, v in rows]


def parse_pasted_series(text: str) -> list[tuple[date, float]]:
    """A pasted two-column series -> observations. Never raises.

    For the commercial series with no public feed. Every terminal exports the
    same rough shape -- a date and a number per line -- so the parser is
    deliberately loose about the separator (comma, tab or whitespace), about a
    header row, and about the date format, and simply drops any line it cannot
    read rather than refusing the paste. A partly-readable paste is worth more
    than an error message.

    Dates accepted as ISO (2026-06-01), Australian (30/06/2026), or a bare
    month (Jun-2026, 2026-06). Day-first, not month-first: the source is an
    Australian terminal.
    """
    out: dict[date, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p for p in re.split(r"[,\t;]|\s{2,}|\s+", line) if p]
        if len(parts) < 2:
            continue
        when = _parse_any_date(parts[0])
        if when is None:
            continue
        try:
            value = float(parts[-1].replace("%", "").replace(",", ""))
        except ValueError:
            continue
        out[when] = value
    return sorted(out.items())


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _parse_any_date(token: str) -> date | None:
    token = token.strip().strip('"')
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return _dt.datetime.strptime(token, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"([A-Za-z]{3})[-/ ]?(\d{2,4})", token)
    if m and m.group(1).lower() in _MONTHS:
        year = int(m.group(2))
        return date(year + 2000 if year < 100 else year,
                    _MONTHS[m.group(1).lower()], 1)
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", token)
    if m:
        month = int(m.group(2))
        if 1 <= month <= 12:
            return date(int(m.group(1)), month, 1)
    return None
