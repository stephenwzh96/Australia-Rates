"""ABS SDMX (data API) adapter -- the structured route the labour workbooks
never had.

`data.abs` reads Excel workbooks because the ABS SDMX endpoint does not carry
the live Labour Force series. It DOES carry the national-accounts, prices and
business-indicator families, as versioned dataflows returning plain CSV with no
key -- which is exactly what the Macro tab's z-score table needs for the dozen
indicators that are neither labour nor CPI.

One request per series, keyed on the fully-specified SDMX key (every dimension
pinned), cached on disk through `data.cache` like every other adapter here.
Never raises: a bad key, a renamed dataflow or an outage returns `[]` and the
Macro tab falls back to the seeded slide reading for that row.

TIME_PERIOD comes in two shapes -- `2026-Q2` for quarterly series and `2026-07`
for monthly -- and both are stamped onto the FIRST day of the period, matching
the ABS month-start convention the rest of the app already uses.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

import requests

from . import cache
from .common import Observation

BASE = "https://data.api.abs.gov.au/rest/data/ABS,{flow}/{key}/"
TIMEOUT = 60
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}

# Macro-tab indicator key -> (dataflow with version, fully-pinned SDMX key).
# Dimension order per dataflow is documented inline; every value is pinned so
# the response is a single time series. Keys verified against the live API in
# September 2026; a wrong one simply degrades to the seed.
SERIES: dict[str, tuple[str, str]] = {
    # ANA_EXP  MEASURE.DATA_ITEM.SECTOR.TSEST.REGION.FREQ
    "gdp":                   ("ANA_EXP,1.0.0", "PCT_VCH.GPM.SSS.20.AUS.Q"),
    "household_consumption": ("ANA_EXP,1.0.0", "PCT_VCH.FCE.PHS.20.AUS.Q"),
    # QBIS  MEASURE.PRICE_ADJUSTMENT.INDUSTRY.SCOPE.TSEST.REGION.FREQ
    #   M8 = gross operating profits, q/q % change (current price)
    #   M4 = inventories, q/q % change (chain volume)
    "company_profit":        ("QBIS,1.0.0", "M8.CUR.TOT.TOT.20.AUS.Q"),
    "inventories":           ("QBIS,1.0.0", "M4.CVM.TOT.TOT.20.AUS.Q"),
    # CWD  MEASURE.PRICE_ADJUSTMENT.SECTOR_OWN.CONSTRUCTION_TYPE.TSEST.REGION.FREQ
    #   M1 value of work done, CVM, all sectors (9), total construction -- a level
    "construction_work_done": ("CWD,1.0.0", "M1.CVM.9.TOT.20.AUS.Q"),
    # CAPEX  MEASURE.PRICE_ADJUSTMENT.ASSET.INDUSTRY.TSEST.STATE.FREQ
    #   M1 actual expenditure, CVM, total asset, total industry -- a level
    "capex":                 ("CAPEX,1.0.0", "M1.CVM.TOT.TOT.20.AUS.Q"),
    # PPI_FD  MEASURE.INDEX.SOURCE.DESTINATION.FREQ
    #   2 = % change from previous period, Total All Industries, ex exports
    "ppi":                   ("PPI_FD,1.1.0", "2.TOT.TOT.TOTXE.Q"),
}


def _parse_period(token: str) -> date | None:
    token = token.strip()
    if "-Q" in token:
        y, q = token.split("-Q")
        try:
            return date(int(y), (int(q) - 1) * 3 + 1, 1)
        except ValueError:
            return None
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(token, fmt).date().replace(day=1)
        except ValueError:
            continue
    if token.isdigit() and len(token) == 4:
        return date(int(token), 1, 1)
    return None


def _parse_csv(text: str) -> list[Observation]:
    """ABS `format=csvfile`: a header row, then one row per observation with
    `TIME_PERIOD` and `OBS_VALUE` columns. Rows out of order; sorted here."""
    rows = list(csv.DictReader(io.StringIO(text)))
    out: dict[date, float] = {}
    for r in rows:
        d = _parse_period(r.get("TIME_PERIOD", ""))
        raw = (r.get("OBS_VALUE") or "").strip()
        if d is None or raw == "":
            continue
        try:
            out[d] = float(raw)
        except ValueError:
            continue
    return [Observation(d, v) for d, v in sorted(out.items())]


def _fetch(flow: str, key: str, since: date | None) -> list[Observation]:
    params = {"format": "csvfile"}
    params["startPeriod"] = str((since.year - 1) if since else 1998)
    try:
        r = requests.get(BASE.format(flow=flow, key=key), params=params,
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return _parse_csv(r.text)
    except requests.RequestException:
        return []


def series(indicator_key: str) -> list[Observation]:
    """The wired ABS SDMX series for a Macro-tab indicator, or `[]`.

    Cached on disk and rechecked at most daily -- these are monthly/quarterly
    statistics, so a per-render round trip would only ever confirm nothing has
    changed.
    """
    spec = SERIES.get(indicator_key)
    if spec is None:
        return []
    flow, key = spec
    return cache.get_series(
        f"abs-sdmx-{indicator_key}",
        lambda since: _fetch(flow, key, since),
        recheck_after=1,
    )


def series_pairs(indicator_key: str) -> list[tuple[date, float]]:
    return [(o.date, o.value) for o in series(indicator_key)]


def wired_keys() -> list[str]:
    return list(SERIES)
