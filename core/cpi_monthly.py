"""The complete monthly CPI -- trend, contributions and the item breakdown.

Since November 2025 the ABS publishes a complete monthly CPI (the ABS data API's
`CPI` v2.0.0 dataflow, history from April 2024): the full group -> sub-group ->
class tree, seasonally adjusted underlying measures, and each item's
contribution to the monthly and the annual change. That is what lets the
Inflation tab carry the US tab's panels as exact twins rather than proxies:

* the sequential trend -- monthly m/m on the trimmed mean, weighted median,
  headline and ex-volatiles, beside the quarterly series back to 1982 (the US
  pairs a CPI panel with a PCE panel; here the pair is monthly and quarterly);
* contribution stacks by expenditure group -- the ABS's own contribution
  measures, where the US reads the FRBSF workbooks;
* the largest and smallest contributors to the month;
* the item breakdown monitor, with the ABS basket weights.

Pure: `ui.sources` fetches the panel; everything here is arithmetic on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

# Measures (CL_CPI_MEASURES) and adjustments (CL_TSEST).
INDEX, PCT_PERIOD, PCT_YEAR, CONTRIB_PERIOD, CONTRIB_YEAR = "1", "2", "3", "6", "7"
ORIGINAL, SA = "10", "20"

ALL_GROUPS = "10001"
TRIMMED_MEAN, WEIGHTED_MEDIAN, HEADLINE_SA, EX_VOLATILES = "999902", "999903", "999901", "999904"

#: Months the breakdown shows, newest first -- the US monitor's six.
MONTHS_SHOWN = 6

Series = list[tuple[date, float]]


def key(measure: str, index: str, tsest: str) -> str:
    """The panel key `ui.sources.fetch_cpi_monthly` stores each series under."""
    return f"{measure}|{index}|{tsest}"


@dataclass(frozen=True)
class TrendSeries:
    code: str
    label: str
    panel: str                 # "Monthly" | "Quarterly"
    observations: Series       # % change on the period
    headline: bool = False     # drawn by default; the rest start in the legend

    @property
    def ok(self) -> bool:
        return bool(self.observations)


#: Per-period pace that compounds to the 2.5% midpoint of the target band.
TARGET_ANNUAL = 2.5


def target_pace(periods_per_year: int) -> float:
    return ((1 + TARGET_ANNUAL / 100.0) ** (1 / periods_per_year) - 1) * 100.0


def _pct(levels: Series) -> Series:
    """Period-on-period % change between adjacent calendar months only."""
    out = []
    for (d0, v0), (d1, v1) in zip(levels, levels[1:]):
        if (d1.year * 12 + d1.month) - (d0.year * 12 + d0.month) == 1 and v0:
            out.append((d1, (v1 / v0 - 1.0) * 100.0))
    return out


def monthly_trend(panel: dict[str, Series]) -> list[TrendSeries]:
    """The monthly panel: seasonally adjusted m/m where the ABS publishes it,
    else computed from the SA index, else the original change, labelled so."""
    def mm(index: str) -> tuple[Series, str]:
        got = panel.get(key(PCT_PERIOD, index, SA))
        if got:
            return got, ""
        lv = panel.get(key(INDEX, index, SA))
        if lv:
            return _pct(lv), ""
        return panel.get(key(PCT_PERIOD, index, ORIGINAL), []), " (original)"

    rows = []
    for code, label, headline in ((TRIMMED_MEAN, "Trimmed mean", True),
                                  (HEADLINE_SA, "Headline CPI", True),
                                  (WEIGHTED_MEDIAN, "Weighted median", False),
                                  (EX_VOLATILES, "Ex volatiles & holiday travel", False)):
        obs, tag = mm(code)
        rows.append(TrendSeries(code, label + tag, "Monthly", obs, headline))
    return rows


def quarterly_trend(g1: dict[str, Series]) -> list[TrendSeries]:
    """The quarterly panel off RBA table G1, back to the 1980s."""
    def m(rows: Series) -> Series:
        return [(date(d.year, d.month, 1), v) for d, v in rows or []]

    return [TrendSeries("GCPIOCPMTMQP", "Trimmed mean", "Quarterly",
                        m(g1.get("GCPIOCPMTMQP")), True),
            TrendSeries("GCPIAGSAQP", "Headline CPI", "Quarterly", m(g1.get("GCPIAGSAQP")), True),
            TrendSeries("GCPIOCPMWMQP", "Weighted median", "Quarterly",
                        m(g1.get("GCPIOCPMWMQP")), False),
            TrendSeries("GCPIXVIQP", "Ex volatile items", "Quarterly",
                        m(g1.get("GCPIXVIQP")), False)]


def latest_readings(series: list[TrendSeries]) -> list[dict]:
    rows = []
    for s in series:
        if not s.observations:
            continue
        d, v = s.observations[-1]
        per_year = 12 if s.panel == "Monthly" else 4
        rows.append({"panel": s.panel, "indicator": s.label, "date": d, "change": v,
                     "annualised": ((1 + v / 100.0) ** per_year - 1) * 100.0})
    return rows


# ------------------------------------------------------------------ the tree

@dataclass(frozen=True)
class Node:
    code: str
    label: str
    parent: str
    level: int                 # 0 all groups, 1 group, 2 sub-group, 3 class


def tree(codelist: list[tuple[str, str, str]]) -> list[Node]:
    """The expenditure tree under All groups, in the ABS's own order. The
    analytical series (ex-, goods/services, trimmed mean) sit under other
    roots and are left out."""
    parent = {c: p for c, _, p in codelist}

    def depth(c: str) -> int | None:
        d, seen = 0, set()
        while c and c != ALL_GROUPS:
            if c in seen:
                return None
            seen.add(c)
            c = parent.get(c, "")
            d += 1
        return d if c == ALL_GROUPS else None

    out = []
    for c, name, p in codelist:
        d = depth(c)
        if d is not None:
            out.append(Node(c, name, p, d))
    return out


# ------------------------------------------------------------------ contributions

@dataclass(frozen=True)
class ContributionFigure:
    """One contribution chart: signed components that stack to the total.
    The same shape `ui.contributions` draws for the US FRBSF figures."""

    key: str
    title: str
    basis: str                       # "YoY" | "MoM"
    dates: list[date]
    total: list[Optional[float]]
    components: dict[str, list[Optional[float]]]
    seasonal: str = "original"

    @property
    def empty(self) -> bool:
        return not self.dates or not self.components

    def between(self, start: date | None = None, end: date | None = None) -> "ContributionFigure":
        keep = [i for i, d in enumerate(self.dates)
                if (start is None or d >= start) and (end is None or d <= end)]
        if len(keep) == len(self.dates):
            return self
        return replace(self, dates=[self.dates[i] for i in keep],
                       total=[self.total[i] for i in keep] if self.total else [],
                       components={k: [v[i] for i in keep] for k, v in self.components.items()})


def _figure(panel, nodes: list[Node], measure: str, total_measure: str, fkey: str,
            title: str, basis: str) -> ContributionFigure:
    comps = {n.label: dict(panel.get(key(measure, n.code, ORIGINAL), [])) for n in nodes}
    comps = {k: v for k, v in comps.items() if v}
    total = dict(panel.get(key(total_measure, ALL_GROUPS, ORIGINAL), []))
    dates = sorted(set().union(*[set(v) for v in comps.values()]) & set(total)) if comps else []
    return ContributionFigure(fkey, title, basis, dates, [total.get(d) for d in dates],
                              {k: [v.get(d) for d in dates] for k, v in comps.items()})


def group_contributions(panel: dict[str, Series], nodes: list[Node]
                        ) -> dict[str, ContributionFigure]:
    """Annual and monthly contributions by expenditure group, with the headline."""
    groups = [n for n in nodes if n.level == 1]
    return {
        "yoy": _figure(panel, groups, CONTRIB_YEAR, PCT_YEAR, "groups_yoy",
                       "Contribution to annual CPI inflation · by group", "YoY"),
        "mom": _figure(panel, groups, CONTRIB_PERIOD, PCT_PERIOD, "groups_mom",
                       "Contribution to monthly CPI inflation · by group", "MoM"),
    }


def class_contributions(panel: dict[str, Series], nodes: list[Node]) -> ContributionFigure:
    """Every class's contribution to the monthly change -- the granular view."""
    leaves = [n for n in nodes if n.level == 3]
    fig = _figure(panel, leaves, CONTRIB_PERIOD, PCT_PERIOD, "classes_mom",
                  "Largest contributors to monthly CPI", "MoM")
    groups = {n.code: n.label for n in nodes if n.level == 1}
    parent = {n.code: n.parent for n in nodes}
    by_label = {n.label: n.code for n in leaves}

    def group_of(code: str) -> str:
        c = code
        while c and c not in groups:
            c = parent.get(c, "")
        return groups.get(c, "")

    # "Group: class", the shape the US granular chart's labels and colours read.
    return replace(fig, components={f"{group_of(by_label[k])}: {k}": v
                                    for k, v in fig.components.items()})


# ------------------------------------------------------------------ breakdown

@dataclass(frozen=True)
class Row:
    item: str
    label: str
    level: int
    weight: float
    basis: str                 # "SA" | "original"


@dataclass(frozen=True)
class BreakdownRow:
    row: Row
    values: list[float | None]

    @property
    def label(self) -> str:
        return self.row.label


@dataclass(frozen=True)
class Breakdown:
    months: list[date]
    rows: list[BreakdownRow] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.months or not any(v is not None for r in self.rows for v in r.values)

    @property
    def latest(self) -> date | None:
        return self.months[0] if self.months else None


def breakdown(panel: dict[str, Series], nodes: list[Node], weights: dict[str, float],
              months: int = MONTHS_SHOWN, max_level: int = 3) -> Breakdown:
    """The item monitor: each node's m/m change for the last `months` months,
    seasonally adjusted where the ABS adjusts that item, else original."""
    def change(code: str) -> tuple[dict[date, float], str]:
        # All groups has no SA index under its own code: the ABS publishes the
        # adjusted headline as the analytical series 999901.
        lv = panel.get(key(INDEX, HEADLINE_SA if code == ALL_GROUPS else code, SA))
        if lv:
            return dict(_pct(lv)), "SA"
        return dict(panel.get(key(PCT_PERIOD, code, ORIGINAL), [])), "original"

    head, _ = change(ALL_GROUPS)
    if not head:
        return Breakdown([])
    cols = sorted(head)[-months:][::-1]
    rows = []
    for n in nodes:
        if n.level > max_level:
            continue
        ch, basis = change(n.code)
        w = 100.0 if n.code == ALL_GROUPS else weights.get(n.code, 0.0)
        rows.append(BreakdownRow(Row(n.code, n.label, n.level, w, basis),
                                 [ch.get(d) for d in cols]))
    return Breakdown(cols, rows)
