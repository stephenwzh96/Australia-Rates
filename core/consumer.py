"""The Australian consumer -- household spending and income, as the US tab
reads retail sales and personal income.

The US Growth tab builds three views from the monthly prints: the retail
control group at one, three and twelve months; personal income, income
excluding transfers and spending; and the latest month's retail categories as
bubbles. Australia's twins:

* the ABS Household Spending Indicator (HSI) replaced Retail Trade in July
  2025 -- monthly, current price, seasonally adjusted, fourteen categories that
  sum to the total, and it covers services, which retail never did. Its total
  stands in for the control group (both are the consumption slice the GDP
  trackers read), and its categories are the bubbles;
* household income is in the quarterly national accounts only (RBA H2): real
  household disposable income and consumption, year-ended, and the saving
  ratio -- quarterly, where the US prints monthly.

The arithmetic -- `Line`, `change`, `combine`, the snapshot -- is the US
module's, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from data.common import Observation

#: The HSI's twelve top-level categories (CL_HSI_CATEGORY, parent TOT), which
#: sum to the total -> short labels for the bubbles.
CATEGORIES: dict[str, str] = {
    "10": "Food", "15": "Alcohol & tobacco", "20": "Clothing", "25": "Housing & utilities",
    "30": "Furnishings", "35": "Health", "40": "Transport", "45": "Communications",
    "50": "Recreation", "55": "Education", "60": "Hotels & cafes", "65": "Misc goods & svcs",
}
#: The analytical splits the ABS publishes alongside: goods / services and
#: discretionary / non-discretionary.
AGGREGATES: dict[str, str] = {"1": "Goods", "2": "Services", "3": "Discretionary",
                              "4": "Non-discretionary"}
TOTAL = "TOT"


def _by_date(obs: list[Observation]) -> dict[date, float]:
    return {o.date: o.value for o in obs}


def combine(plus: list[Observation], minus: list[list[Observation]]) -> list[Observation]:
    """`plus` less every series in `minus`, on the months all of them print."""
    legs = [_by_date(m) for m in minus]
    return [Observation(o.date, o.value - sum(leg[o.date] for leg in legs))
            for o in plus if all(o.date in leg for leg in legs)]


def change(levels: list[Observation], lag: int, annualise: bool = False,
           months_per_step: int = 1) -> list[Observation]:
    """Percent change over `lag` periods, optionally compounded to an annual
    rate, paired by calendar month rather than by position."""
    span = lag * months_per_step
    power = 12 / span if annualise else 1.0
    by_month = {o.date.year * 12 + o.date.month: o.value for o in levels}
    out: list[Observation] = []
    for o in levels:
        prior = by_month.get(o.date.year * 12 + o.date.month - span)
        if prior is not None and prior > 0 and o.value > 0:
            out.append(Observation(o.date, ((o.value / prior) ** power - 1) * 100))
    return out


@dataclass(frozen=True)
class Line:
    key: str
    label: str
    observations: list[Observation]

    @property
    def ok(self) -> bool:
        return bool(self.observations)

    @property
    def latest(self) -> Observation | None:
        return self.observations[-1] if self.observations else None


def spending_lines(total: list[Observation]) -> list[Line]:
    """Household spending at 1 and 3 months annualised, and 12 months -- the
    control-group panel's three lines."""
    return [Line("1m", "1 Month (AR)", change(total, 1, annualise=True)),
            Line("3m", "3 Month (AR)", change(total, 3, annualise=True)),
            Line("12m", "12 Month", change(total, 12))]


def household_lines(h2: dict[str, list[Observation]]) -> list[Line]:
    """Year-ended real consumption and real disposable income (quarterly, RBA
    H2) -- the US panel's spending and income, at the cadence Australia prints."""
    return [Line("spending", "Household Consumption", h2.get("GGDPECCVPSHY", [])),
            Line("income", "Real Disposable Income", h2.get("GGDPICHRDIY", []))]


@dataclass(frozen=True)
class Point:
    """One category in the latest month: its two changes and its size."""

    label: str
    mom: float
    yoy: float
    sales: float
    share: float


@dataclass(frozen=True)
class Snapshot:
    month: date
    overall: Point
    categories: list[Point]


def _months_back(d: date, n: int) -> date:
    m = d.year * 12 + d.month - 1 - n
    return date(m // 12, m % 12 + 1, 1)


def _point(label: str, obs: dict[date, float], month: date, prior: date,
           year_ago: date, total: float) -> Point | None:
    if not all(d in obs and obs[d] > 0 for d in (month, prior, year_ago)):
        return None
    cur = obs[month]
    return Point(label, (cur / obs[prior] - 1) * 100, (cur / obs[year_ago] - 1) * 100,
                 cur, cur / total * 100 if total else 0.0)


def snapshot(hsi: dict[str, list[Observation]]) -> Snapshot | None:
    """The newest month the HSI total prints, every category placed on it."""
    total = _by_date(hsi.get(TOTAL, []))
    if not total:
        return None
    month = max(total)
    prior, year_ago = _months_back(month, 1), _months_back(month, 12)
    overall = _point("Overall", total, month, prior, year_ago, total[month])
    if overall is None:
        return None
    cats = [pt for code, label in CATEGORIES.items()
            if (pt := _point(label, _by_date(hsi.get(code, [])), month, prior, year_ago,
                             total[month])) is not None]
    return Snapshot(month, overall, cats)
