"""Australian economic growth -- a monthly activity tracker against the accounts.

The US chart sets the New York Fed's Weekly Economic Index (already scaled to
four-quarter GDP growth) and its 13-week average against real GDP and GDI.
Australia has no weekly index. Its standard monthly read on output is labour
INPUT: total hours worked, published with every Labour Force survey, because
GDP is hours times productivity and productivity moves slowly. So the tracker
here is hours worked, year-ended, and its 3-month average (one quarter, which
is what makes it comparable to the quarterly steps), against real GDP and real
GDI year-ended from RBA table H1. The gap between the two is the same read: how
far the monthly tracker has run ahead of the last quarter measured -- and the
negative productivity the RBA has been forecasting shows up as hours running
above GDP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from data.common import Observation

HOURS = "HOURS"
GDP = "GDP"
GDI = "GDI"
HOURS_SMOOTH = "HOURS_SMOOTH"

#: Three months is one quarter -- what makes the average comparable to the
#: quarterly steps rather than merely calmer.
SMOOTH_MONTHS = 3

HISTORY_START = date(2005, 1, 1)


@dataclass(frozen=True)
class GrowthSeries:
    """One growth measure, already in year-over-year percent."""

    code: str
    label: str
    cadence: str                 # "monthly" or "quarterly"
    observations: list[Observation]

    @property
    def ok(self) -> bool:
        return bool(self.observations)

    @property
    def latest(self) -> Observation | None:
        return self.observations[-1] if self.observations else None


def yoy(obs: list[Observation], months: int = 12) -> list[Observation]:
    """Year-over-year percent change, paired by calendar month."""
    by = {o.date.year * 12 + o.date.month: o.value for o in obs}
    out = []
    for o in obs:
        prior = by.get(o.date.year * 12 + o.date.month - months)
        if prior:
            out.append(Observation(o.date, (o.value / prior - 1.0) * 100.0))
    return out


def rolling_mean(obs: list[Observation], window: int) -> list[Observation]:
    """Trailing mean over `window` observations; only full windows."""
    if window < 1 or len(obs) < window:
        return []
    return [Observation(obs[i].date, sum(o.value for o in obs[i - window + 1:i + 1]) / window)
            for i in range(window - 1, len(obs))]


def quarter_start(d: date) -> date:
    """RBA tables date a quarter on its last day; the chart steps from its first."""
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)


def build(hours: list[Observation], h1: dict[str, list[Observation]]) -> list[GrowthSeries]:
    """The four plotted series, back to front: the noisy monthly line first so
    the quarterly steps and the smoothed average draw over it."""
    hours_yoy = yoy(hours)

    def q(code: str) -> list[Observation]:
        return [Observation(quarter_start(o.date), o.value) for o in h1.get(code, [])]

    return [
        GrowthSeries(HOURS, "Hours worked", "monthly", hours_yoy),
        GrowthSeries(GDI, "Real GDI", "quarterly", q("GGDPCVRGDIY")),
        GrowthSeries(GDP, "Real GDP", "quarterly", q("GGDPCVGDPY")),
        GrowthSeries(HOURS_SMOOTH, f"Hours {SMOOTH_MONTHS}-Month Avg", "monthly",
                     rolling_mean(hours_yoy, SMOOTH_MONTHS)),
    ]


def latest_readings(series_list: list[GrowthSeries]) -> list[dict]:
    return [{"indicator": s.label, "cadence": s.cadence, "date": s.latest.date,
             "value": s.latest.value} for s in series_list if s.latest]


def gap(series_list: list[GrowthSeries]) -> float | None:
    """The smoothed tracker less the latest GDP print, percentage points."""
    by = {s.code: s for s in series_list}
    smooth, gdp = by.get(HOURS_SMOOTH), by.get(GDP)
    if not (smooth and smooth.latest and gdp and gdp.latest):
        return None
    return smooth.latest.value - gdp.latest.value
