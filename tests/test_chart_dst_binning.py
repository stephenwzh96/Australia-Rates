"""A chart that bins dates by month or quarter must hand Vega noon, not midnight.

Streamlit passes a naive date to Vega shifted by the browser's UTC offset at
that instant. On a Sydney DST-start day the instant is already in summer time,
so 1 Oct 2023 00:00 arrived as 23:00 on 30 Sep: Employment by component
stacked two quarters into 2023 Q3 and left Q4 empty, and the monthly and
quarterly bars on the Labour, Inflation and Macro tabs did the same. Noon is
eleven hours from either day boundary, so an hour's error cannot move a bin.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from core.macro import MomentumPoint
from ui import charts
from ui.theme import DARK

DST_DAY = date(2023, 10, 1)          # Sydney summer time began at 02:00
QUARTERS = [date(2023, 4, 1), date(2023, 7, 1), DST_DAY, date(2024, 1, 1)]


def _stamps(chart, field: str) -> list[str]:
    spec = chart.to_dict()
    return [row[field] for rows in spec.get("datasets", {}).values()
            for row in rows if row.get(field) is not None]


def _all_noon(stamps: list) -> None:
    assert stamps, "the chart carried no dates to check"
    times = [pd.Timestamp(s) for s in stamps]
    assert all((t.hour, t.minute) == (12, 0) for t in times), stamps


def test_employment_by_component_bins_at_noon():
    series = [(d, 10.0 + i) for i, d in enumerate(QUARTERS)]
    comp = {"market": series, "health_ed": series, "public_admin": series, "total": series}
    _all_noon(_stamps(charts.employment_components_chart(comp, DARK), "date"))


def test_net_employment_gains_bins_at_noon():
    months = [date(2023, 8, 1), date(2023, 9, 1), DST_DAY, date(2023, 11, 1)]
    data = {"EMP_CHANGE_3MA": [(d, 20.0) for d in months]}
    _all_noon(_stamps(charts.au_employment_change_chart(data, DARK), "date"))


def test_cpi_composition_bins_at_noon():
    buckets = {"Goods": [(d, 0.2) for d in QUARTERS], "Services": [(d, 0.3) for d in QUARTERS]}
    line = [(d, 0.5) for d in QUARTERS]
    _all_noon(_stamps(charts.cpi_composition(buckets, line, ["Goods", "Services"], DARK), "when"))


def test_macro_momentum_bins_at_noon():
    hist = [MomentumPoint(month=d, by_group={"Labour Market": 0.1}, total=0.1,
                          contributions={"Labour Market": 0.1}) for d in QUARTERS]
    _all_noon(_stamps(charts.macro_momentum(hist, DARK), "month"))
