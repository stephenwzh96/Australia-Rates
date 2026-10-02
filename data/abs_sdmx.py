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
from datetime import date, datetime

import requests

from . import cache
from .common import Observation
from core import clock

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
    changed -- and each check replaces the whole series (one vintage, never
    two spliced).
    """
    spec = SERIES.get(indicator_key)
    if spec is None:
        return []
    flow, key = spec
    # Whole, not windowed: every one of these is revised (the national
    # accounts, profits, capex and construction rewrite back years), and a
    # windowed merge would splice vintages. A quarterly series since 1998 is
    # a few kilobytes, so the whole history costs nothing to re-read.
    return cache.get_series(
        f"abs-sdmx-{indicator_key}",
        lambda since: _fetch(flow, key, since),
        recheck_after=1,
        whole=True,
    )


def _fetch_panel(flow: str, key: str, dims: tuple[str, ...],
                 start: str) -> dict[str, list[Observation]] | None:
    """Every series a partially pinned key matches, keyed by the values of
    `dims` joined with "|". None on any failure, so the caller keeps its cache."""
    try:
        r = requests.get(BASE.format(flow=flow, key=key),
                         params={"format": "csvfile", "startPeriod": start},
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except requests.RequestException:
        return None
    out: dict[str, dict[date, float]] = {}
    for row in rows:
        d = _parse_period(row.get("TIME_PERIOD", ""))
        raw = (row.get("OBS_VALUE") or "").strip()
        if d is None or raw == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        out.setdefault("|".join(row.get(x, "") for x in dims), {})[d] = value
    return {k: [Observation(d, v) for d, v in sorted(s.items())] for k, s in out.items()}


def panel(flow: str, key: str, dims: tuple[str, ...], start: str = "1998",
          max_age_days: int = 1) -> dict[str, list[Observation]]:
    """A whole SDMX panel -- e.g. filled jobs by industry, WPI by industry --
    keyed by the `dims` values, cached on disk as one snapshot and refetched
    whole once it is `max_age_days` old. Whole, not windowed: these are revised
    statistics, and a windowed merge would splice two vintages. A failed fetch
    serves the last good snapshot; nothing raises."""
    ckey = f"abs-sdmx-panel-{flow}-{key}-{'-'.join(dims)}"
    cached = cache.read_json(ckey)
    stamp = next((r.get("fetched") for r in cached if "fetched" in r), None)
    fresh = False
    if stamp:
        try:
            age = (clock.today() - date.fromisoformat(stamp)).days
            fresh = age < max_age_days
        except ValueError:
            fresh = False

    def unpack(rows: list[dict]) -> dict[str, list[Observation]]:
        out: dict[str, list[Observation]] = {}
        for r in rows:
            if "k" not in r:
                continue
            try:
                out.setdefault(r["k"], []).append(Observation(date.fromisoformat(r["d"]),
                                                              float(r["v"])))
            except (KeyError, ValueError, TypeError):
                continue
        return {k: sorted(v, key=lambda o: o.date) for k, v in out.items()}

    if fresh:
        return unpack(cached)
    got = _fetch_panel(flow, key, dims, start)
    if got is None:
        return unpack(cached)
    rows = [{"fetched": clock.today().isoformat()}]
    rows += [{"k": k, "d": o.date.isoformat(), "v": o.value} for k, obs in got.items()
             for o in obs]
    cache.write_json(ckey, rows)
    return got


STRUCTURE = "https://data.api.abs.gov.au/rest/dataflow/ABS/{flow}/{version}"


def codelist(flow: str, version: str, codelist_id: str,
             max_age_days: int = 30) -> list[tuple[str, str, str]]:
    """One codelist of a dataflow as (code, English name, parent code or "").

    The CPI's item tree (group -> sub-group -> class) is read from here rather
    than typed in, so an ABS reclassification lands without a code change.
    Cached on disk; a failed fetch serves the last good copy."""
    import re
    ckey = f"abs-sdmx-codelist-{flow}-{version}-{codelist_id}"
    cached = cache.read_json(ckey)
    stamp = next((r.get("fetched") for r in cached if "fetched" in r), None)
    rows = [(r["c"], r["n"], r.get("p", "")) for r in cached if "c" in r]
    if stamp and rows:
        try:
            if (clock.today() - date.fromisoformat(stamp)).days < max_age_days:
                return rows
        except ValueError:
            pass
    try:
        r = requests.get(STRUCTURE.format(flow=flow, version=version),
                         params={"references": "all"},
                         headers={**HEADERS,
                                  "Accept": "application/vnd.sdmx.structure+xml;version=2.1"},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return rows
        block = re.search(rf'<structure:Codelist[^>]*id="{re.escape(codelist_id)}".*?'
                          r"</structure:Codelist>", r.text, re.S)
        if not block:
            return rows
        got = []
        for code, body in re.findall(r'<structure:Code id="([^"]+)"[^>]*>(.*?)</structure:Code>',
                                     block.group(0), re.S):
            name = re.search(r'<common:Name xml:lang="en">([^<]+)</common:Name>', body)
            parent = re.search(r'<structure:Parent>.*?id="([^"]+)"', body, re.S)
            got.append((code, name.group(1) if name else code, parent.group(1) if parent else ""))
    except requests.RequestException:
        return rows
    if got:
        cache.write_json(ckey, [{"fetched": clock.today().isoformat()}]
                         + [{"c": c, "n": n, "p": p} for c, n, p in got])
        return got
    return rows


def series_pairs(indicator_key: str) -> list[tuple[date, float]]:
    return [(o.date, o.value) for o in series(indicator_key)]


def wired_keys() -> list[str]:
    return list(SERIES)
