"""Employment by component and wage growth by industry -- the Australian twins
of the US "payrolls by component" chart and the sector wage-growth heatmap.

The US splits monthly nonfarm payrolls into private ex health & education,
education & health, and government. Australia has no monthly establishment
survey; the ABS Labour Account counts FILLED JOBS by industry each quarter,
and the same three-way split is the one the RBA itself argues about -- how
much of employment growth is the non-market sector:

* Market sector          every industry except O, P and Q
* Health & education     Q (health care and social assistance) + P (education)
* Public administration  O (public administration and safety)

They sum to total filled jobs exactly. The wage rows are the Wage Price Index
by industry, year-ended: a fixed-composition index, which is the better wage
measure (the US heatmap has to use average hourly earnings, which a shift in
the mix of jobs can move on its own).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

HEALTH, EDUCATION, PUBLIC_ADMIN = "Q", "P", "O"
TOTAL = "TOTAL"

#: (key, label), bottom of the stack first.
COMPONENTS: tuple[tuple[str, str], ...] = (
    ("market", "Market sector"),
    ("health_ed", "Health care & education"),
    ("public_admin", "Public administration"),
)

INDUSTRIES: dict[str, str] = {
    "A": "Agriculture, forestry & fishing", "B": "Mining", "C": "Manufacturing",
    "D": "Electricity, gas, water & waste", "E": "Construction", "F": "Wholesale trade",
    "G": "Retail trade", "H": "Accommodation & food", "I": "Transport, postal & warehousing",
    "J": "Information media & telecoms", "K": "Financial & insurance",
    "L": "Rental, hiring & real estate", "M": "Professional, scientific & technical",
    "N": "Administrative & support", "O": "Public administration & safety",
    "P": "Education & training", "Q": "Health care & social assistance",
    "R": "Arts & recreation", "S": "Other services",
}

#: Quarters shown in the wage heatmap, newest first -- two years, the span the
#: US table's twelve months cover at a quarterly cadence.
HEATMAP_QUARTERS = 8

Series = list[tuple[date, float]]


@dataclass(frozen=True)
class WageRow:
    key: str              # "SECTOR|INDUSTRY" in the WPI panel
    label: str
    level: int            # 0 = all, 1 = sector, 2 = industry
    jobs_key: str | None  # the Labour Account industry its share comes from


WAGE_ROWS: tuple[WageRow, ...] = (
    (WageRow("7|TOT", "All industries", 0, TOTAL),
     WageRow("1|TOT", "Private sector", 1, None),
     WageRow("2|TOT", "Public sector", 1, None))
    + tuple(WageRow(f"7|{code}", label, 2, code) for code, label in INDUSTRIES.items())
)


def _changes(series: Series) -> Series:
    return [(d, v - p) for (_, p), (d, v) in zip(series, series[1:])]


def components(jobs: dict[str, Series]) -> dict[str, Series]:
    """Quarterly change in filled jobs ('000) by component, plus the total and
    the market sector as lines. Built from levels, so the parts sum exactly."""
    total = dict(jobs.get(TOTAL, []))
    if not total:
        return {}
    parts = {k: dict(jobs.get(k, [])) for k in (HEALTH, EDUCATION, PUBLIC_ADMIN)}
    dates = sorted(d for d in total if all(d in p for p in parts.values()))
    he = [(d, parts[HEALTH][d] + parts[EDUCATION][d]) for d in dates]
    pa = [(d, parts[PUBLIC_ADMIN][d]) for d in dates]
    market = [(d, total[d] - parts[HEALTH][d] - parts[EDUCATION][d] - parts[PUBLIC_ADMIN][d])
              for d in dates]
    tot = [(d, total[d]) for d in dates]
    # The Labour Account publishes jobs in thousands already.
    return {"market": _changes(market), "health_ed": _changes(he),
            "public_admin": _changes(pa), "total": _changes(tot)}


def shares(jobs: dict[str, Series]) -> dict[str, float]:
    """Each industry's share of filled jobs, latest quarter, %."""
    total = jobs.get(TOTAL) or []
    if not total:
        return {}
    when, tot = total[-1]
    out = {TOTAL: 100.0}
    for code in INDUSTRIES:
        level = dict(jobs.get(code, [])).get(when)
        if level is not None and tot:
            out[code] = 100.0 * level / tot
    return out
