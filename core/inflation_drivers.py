"""What feeds inflation from outside the price index: expectations and wages.

The US tab draws household expectations (NY Fed point and density, Michigan
5-10 year) beside the Employment Cost Index. Australia's twins are richer on
expectations and as good on wages:

* RBA table G3 -- the Melbourne Institute's consumer 1-year expectation, the
  market economists' 2-year expectation and the 10-year bond break-even (the
  long-run anchor the RBA watches, where the Fed reads Michigan 5-10 year);
  quarterly. Business and union expectations are kept in the readings table.
* the Wage Price Index (RBA H4) -- quarter-on-quarter, total, private and
  public. Like the ECI it holds the mix of jobs fixed.
"""

from __future__ import annotations

from datetime import date

from core.consumer import Line
from data.common import Observation

EXPECTATION_CODES = {
    "consumer_1y": ("GCONEXP", "Consumer, 1-year (Melbourne Institute)"),
    "market_2y": ("GMAREXPYY", "Market economists, 2-year"),
    "breakeven_10y": ("GBONYLD", "10-year bond break-even"),
    "business_3m": ("GBUSEXP", "Business, 3-months (NAB)"),
    "union_2y": ("GUNIEXPYY", "Union officials, 2-year"),
    "market_1y": ("GMAREXPY", "Market economists, 1-year"),
}

WPI_CODES = {"public": ("GWPIPUBQP", "Public"), "private": ("GWPIPRIQP", "Private"),
             "total": ("GWPIQP", "Total")}

EXPECTATIONS_START = date(1995, 1, 1)


def _obs(rows) -> list[Observation]:
    return [Observation(date(d.year, d.month, 1), v) for d, v in rows or []]


def expectation_lines(g3: dict) -> list[Line]:
    """The three drawn lines, back to front: consumer (noisy, highest) first so
    the two anchored measures draw over it."""
    return [Line(k, EXPECTATION_CODES[k][1], _obs(g3.get(EXPECTATION_CODES[k][0])))
            for k in ("consumer_1y", "market_2y", "breakeven_10y")]


def expectation_readings(g3: dict) -> list[dict]:
    """The latest print of every G3 series, for the table under the chart."""
    out = []
    for key, (code, label) in EXPECTATION_CODES.items():
        rows = g3.get(code) or []
        if rows:
            d, v = rows[-1]
            out.append({"key": key, "label": label, "date": d, "value": v})
    return out


def wpi_lines(h4: dict) -> list[Line]:
    """Quarter-on-quarter, as published; public first so total draws on top."""
    return [Line(k, WPI_CODES[k][1], _obs(h4.get(WPI_CODES[k][0])))
            for k in ("public", "private", "total")]
