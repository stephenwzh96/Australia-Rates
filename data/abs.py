"""ABS Labour Force adapter.

The ABS publishes its labour statistics as Excel time-series workbooks, not
through an API. Its SDMX endpoint (data.api.abs.gov.au) exists and serves 712
dataflows, but they are almost all Census tables -- the live Labour Force
series are not there. Checked, rather than assumed.

So this reads the workbooks. They share one rigid layout, which is what makes
that tractable: a header block whose rows are labelled `Series Type`,
`Series ID`, `Unit` and so on, then one row per month with a date in column A.
`series()` finds a column by its header text plus series type and returns the
whole history, so a workbook gaining or reordering columns costs nothing.

RELEASES MOVE, AND THE TWO RELEASES ARE NOT IN STEP
-----------------------------------------------------
File URLs embed the reference period -- `.../jun-2026/62020X29.xlsx` -- so a
hardcoded path silently rots every month. `latest_release()` reads the actual
month off the landing page instead.

More important, and the reason this matters beyond housekeeping: **Labour Force
and Labour Force Detailed are on different months.** As at August 2026 the
headline release is June while Detailed has not moved past March, because of
the survey changes the ABS made in April 2026. Every duration-based number --
which here means the medium-term unemployment rate -- is therefore three months
staler than the rest of the panel, and the UI has to say so. The RBA hits the
same wall: its own labour-tightness graph greys out the medium-term
unemployment rate for exactly this reason.

WHAT THIS MODULE DOES NOT DO
-----------------------------
Three of the nine full-employment indicators have no free source at all --
firms reporting labour constraints and employment intentions (NAB Business
Survey) and job ads as a share of the labour force (ANZ-Indeed). They are
commercial. `core.employment` carries them as manual inputs rather than
pretending a public series exists.
"""

from __future__ import annotations

import datetime as _dt
import io
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import requests

from . import cache
from .common import Observation

LF_LANDING = ("https://www.abs.gov.au/statistics/labour/employment-and-unemployment/"
              "labour-force-australia/latest-release")
LFD_LANDING = ("https://www.abs.gov.au/statistics/labour/employment-and-unemployment/"
               "labour-force-australia-detailed/latest-release")

LF_BASE = ("https://www.abs.gov.au/statistics/labour/employment-and-unemployment/"
           "labour-force-australia/{period}/{fname}")
LFD_BASE = ("https://www.abs.gov.au/statistics/labour/employment-and-unemployment/"
            "labour-force-australia-detailed/{period}/{fname}")

TIMEOUT = 120

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}

# The two workbooks this app reads, and why each one.
#
# X29 is the old Table 22, renamed in the April 2026 restructure (the ABS ships
# a concordance file for the transition). It is the single most useful workbook
# here: unemployment, underemployment and youth unemployment rates all sit in
# it, seasonally adjusted, monthly back to February 1978.
#
# 14a is the duration-of-job-search table, and the only route to a medium-term
# unemployment rate. Original terms only -- the ABS does not seasonally adjust
# the fine duration buckets.
X29 = "62020X29.xlsx"          # Underutilised persons by Age and Sex
DURATION = "6291014a.xlsx"     # Unemployed persons by duration of job search

_HEADER_ROWS = 10              # rows before the first dated observation


@dataclass(frozen=True)
class Workbook:
    """A parsed ABS workbook: one dict per (sheet, column) keyed by header."""
    period: str
    columns: dict[tuple[str, str], list[Observation]]
    series_ids: dict[tuple[str, str], str]

    @property
    def ok(self) -> bool:
        return bool(self.columns)

    def find(self, header: str, series_type: str = "Seasonally Adjusted"
             ) -> list[Observation]:
        """Exact header match, filtered by series type. [] when absent."""
        return self.columns.get((header.strip(), series_type), [])

    def series_id(self, header: str, series_type: str = "Seasonally Adjusted") -> str | None:
        return self.series_ids.get((header.strip(), series_type))

    @property
    def latest(self) -> date | None:
        ends = [obs[-1].date for obs in self.columns.values() if obs]
        return max(ends) if ends else None


def _month_slug(text: str) -> str | None:
    """`.../jun-2026/62020001.xlsx` -> `jun-2026`."""
    m = re.search(r'/((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)-\d{4})/', text, re.I)
    return m.group(1).lower() if m else None


@lru_cache(maxsize=4)
def latest_release(landing: str) -> str | None:
    """The reference-period slug the current release actually lives under.

    Read off the landing page rather than derived from today's date: the two
    releases run on different lags, and the headline one is itself published
    three weeks after the month it describes.
    """
    try:
        r = requests.get(landing, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException:
        return None
    return _month_slug(r.text)


def _fetch_workbook(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    except requests.RequestException:
        return None


def parse_workbook(blob: bytes, period: str = "") -> Workbook:
    """Parse an ABS time-series workbook. Never raises.

    Only `Data*` sheets are read. Column identity comes from the header text
    paired with the `Series Type` row, because the same measure appears three
    times in most ABS tables -- Trend, Seasonally Adjusted and Original -- and
    picking the wrong one silently changes the number.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception:
        return Workbook(period, {}, {})

    columns: dict[tuple[str, str], list[Observation]] = {}
    ids: dict[tuple[str, str], str] = {}

    for name in wb.sheetnames:
        if not name.startswith("Data"):
            continue
        try:
            ws = wb[name]
            head = list(ws.iter_rows(min_row=1, max_row=_HEADER_ROWS, values_only=True))
        except Exception:
            continue
        if not head:
            continue

        titles = head[0]
        types = next((r for r in head if r and r[0] == "Series Type"), None)
        sids = next((r for r in head if r and r[0] == "Series ID"), None)
        if types is None:
            continue

        wanted: dict[int, tuple[str, str]] = {}
        for col in range(1, len(titles)):
            title = str(titles[col]).strip() if titles[col] else ""
            stype = (str(types[col]).strip()
                     if col < len(types) and types[col] else "")
            if not title:
                continue
            key = (title, stype)
            wanted[col] = key
            columns.setdefault(key, [])
            if sids is not None and col < len(sids) and sids[col]:
                ids[key] = str(sids[col]).strip()

        try:
            for row in ws.iter_rows(min_row=_HEADER_ROWS + 1, values_only=True):
                when = row[0]
                if not isinstance(when, _dt.datetime):
                    continue
                d = when.date()
                for col, key in wanted.items():
                    if col < len(row) and isinstance(row[col], (int, float)):
                        columns[key].append(Observation(d, float(row[col])))
        except Exception:
            pass

    # Deduplicate by date. The CPI workbooks publish the SAME series twice when
    # a sub-group has exactly one expenditure class -- Tobacco, Rents, Household
    # textiles and 21 others each appear in two columns with identical values
    # and different series IDs. Concatenating them gave every observation twice,
    # which a quarterly change reads as a zero-length period.
    out: dict[tuple[str, str], list[Observation]] = {}
    for key, obs in columns.items():
        if not obs:
            continue
        merged = {o.date: o.value for o in obs}
        out[key] = [Observation(d, merged[d]) for d in sorted(merged)]
    return Workbook(period, out, ids)


def _cache_key(fname: str, period: str) -> str:
    return f"abs-{fname.replace('.xlsx', '')}-{period}"


def workbook(fname: str, detailed: bool = False) -> Workbook:
    """One ABS workbook, behind the on-disk cache. Never raises.

    Cached under the RELEASE PERIOD, so a new month is a cache miss and an old
    month is never refetched. That matters more here than elsewhere in this
    app: these files run to several megabytes and the duration workbook is a
    separate download again.
    """
    landing = LFD_LANDING if detailed else LF_LANDING
    period = latest_release(landing)
    if period is None:
        # Offline: serve whatever period was cached last, newest first.
        for row in sorted(cache.read_json(f"abs-index-{fname}"), reverse=True,
                          key=lambda r: r.get("period", "")):
            cached = _from_cache(_cache_key(fname, row.get("period", "")), row.get("period", ""))
            if cached.ok:
                return cached
        return Workbook("", {}, {})

    key = _cache_key(fname, period)
    cached = _from_cache(key, period)
    if cached.ok:
        return cached

    base = LFD_BASE if detailed else LF_BASE
    blob = _fetch_workbook(base.format(period=period, fname=fname))
    if blob is None:
        return Workbook(period, {}, {})
    parsed = parse_workbook(blob, period)
    if parsed.ok:
        _to_cache(key, parsed)
        cache.write_json(f"abs-index-{fname}", [{"period": period}])
    return parsed


def _to_cache(key: str, wb: Workbook) -> None:
    cache.write_json(key, [
        {"h": h, "t": t, "id": wb.series_ids.get((h, t)),
         "o": [[o.date.isoformat(), o.value] for o in obs]}
        for (h, t), obs in wb.columns.items()
    ])


def _from_cache(key: str, period: str) -> Workbook:
    cols: dict[tuple[str, str], list[Observation]] = {}
    ids: dict[tuple[str, str], str] = {}
    for row in cache.read_json(key):
        k = (row.get("h", ""), row.get("t", ""))
        obs = []
        for pair in row.get("o", []):
            try:
                obs.append(Observation(date.fromisoformat(pair[0]), float(pair[1])))
            except (ValueError, TypeError, IndexError):
                continue
        if obs:
            cols[k] = obs
            if row.get("id"):
                ids[k] = row["id"]
    return Workbook(period, cols, ids)


# --------------------------------------------------------------------------
# The specific series this app reads
# --------------------------------------------------------------------------

# Header text is matched EXACTLY, so these are quoted verbatim from the
# workbook including the ABS's own doubled spaces around semicolons.
UNEMPLOYMENT = "Unemployment rate ;  Persons ;"
UNDEREMPLOYMENT = "Underemployment rate (proportion of labour force) ;  Persons ;"
YOUTH_UNEMPLOYMENT = "Unemployment rate ;  Persons ;  15-24 years ;"

# Duration buckets, `Unemployed total` counts in thousands, Original terms.
DURATION_BUCKETS = (
    "> Under 4 weeks (under 1 month) ;  Unemployed total ;  Persons ;",
    "> 4 weeks and under 13 weeks (1-3 months) ;  Unemployed total ;  Persons ;",
    "> 13 weeks and under 26 weeks (3-6 months) ;  Unemployed total ;  Persons ;",
    "> 26 weeks and under 52 weeks (6-12 months) ;  Unemployed total ;  Persons ;",
    "52 weeks and over (Long-term unemployed) ;  Unemployed total ;  Persons ;",
)

# The medium-term rate counts everyone searching between one month and one
# year: past frictional churn, short of long-term detachment. Verified against
# a published reading -- 389.2k over a 15,400.4k labour force is 2.5273%, which
# reproduces the figure to four decimals.
MEDIUM_TERM_BUCKETS = DURATION_BUCKETS[1:4]


# LMS1 is the gross-flows cube: every respondent's labour force status this
# month against their status last month, monthly from July 2007. It is the only
# public route to a JOB-FINDING RATE -- what share of the unemployed found work
# -- which moves months before the unemployment rate does, because a rate can
# sit still while the flows underneath it collapse.
#
# It costs 100MB and a million rows, so it is aggregated to national totals
# during the parse and only the aggregate is cached: 228 months by 16 status
# pairs, a few thousand numbers instead of a million.
GROSS_FLOWS = "LMS1.xlsx"

# Table 17 carries how long each employed person has been with their current
# employer. Under twelve months, as a share of everyone employed, is the
# standard public proxy for a JOB-SWITCHING RATE -- Australia has no quarterly
# quits series, and this is what stands in for one.
TENURE = "6291017.xlsx"

EMPLOYED_STATUSES = ("Employed full-time", "Employed part-time")
UNEMPLOYED_STATUS = "Unemployed"

TENURE_UNDER_12M = ("With current employer or business for fewer than 12 months ;"
                    "  Employed total ;  Persons ;")
TENURE_OVER_12M = ("With current employer or business for 12 months or more ;"
                   "  Employed total ;  Persons ;")


def labour_force_rates() -> Workbook:
    """X29 -- unemployment, underemployment and youth unemployment rates."""
    return workbook(X29)


def job_tenure() -> Workbook:
    """Table 17 -- employed persons by months with their current employer."""
    return workbook(TENURE, detailed=True)


def _parse_gross_flows(blob: bytes) -> dict[str, list[Observation]]:
    """Aggregate LMS1's `Data 1` sheet to national flows by status pair.

    The sheet is a flat pivot export -- one row per month, sex, age, state and
    status pair -- so a national flow is a sum over every demographic cell, not
    a row that can be looked up. Keys come back as `"previous>current"`.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
        ws = wb["Data 1"]
    except Exception:
        return {}

    acc: dict[tuple[str, date], float] = {}
    try:
        for row in ws.iter_rows(min_row=5, values_only=True):
            when = row[0]
            if not isinstance(when, _dt.datetime) or not isinstance(row[6], (int, float)):
                continue
            key = (f"{row[5]}>{row[4]}", when.date())
            acc[key] = acc.get(key, 0.0) + float(row[6])
    except Exception:
        pass

    out: dict[str, list[Observation]] = {}
    for (pair, when), value in acc.items():
        out.setdefault(pair, []).append(Observation(when, value))
    for obs in out.values():
        obs.sort(key=lambda o: o.date)
    return out


def gross_flows() -> dict[str, list[Observation]]:
    """`{"previous>current": monthly national flow in thousands}`.

    Behind the same release-keyed disk cache as everything else here, which
    matters more for this one than for any other source: the download is 100MB
    and the parse is half a minute, and both happen once a month.
    """
    period = latest_release(LF_LANDING)

    def from_cache(key: str) -> dict[str, list[Observation]]:
        rows: dict[str, list[Observation]] = {}
        for row in cache.read_json(key):
            obs = []
            for pair in row.get("o", []):
                try:
                    obs.append(Observation(date.fromisoformat(pair[0]), float(pair[1])))
                except (ValueError, TypeError, IndexError):
                    continue
            if obs:
                rows[row.get("h", "")] = obs
        return rows

    if period is None:
        # Offline: the same index-cache fallback `workbook()` uses. Building
        # the key from `period or ""` here would read "abs-LMS1-", a key
        # nothing ever writes to (every successful write below is keyed on a
        # real period string) -- silently returning empty instead of the last
        # good cache, and with it the Labour tab's job-finding rate going
        # blank on any network outage even though good data sits on disk.
        for row in sorted(cache.read_json(f"abs-index-{GROSS_FLOWS}"), reverse=True,
                          key=lambda r: r.get("period", "")):
            cached = from_cache(_cache_key(GROSS_FLOWS, row.get("period", "")))
            if cached:
                return cached
        return {}

    key = _cache_key(GROSS_FLOWS, period)
    cached = from_cache(key)
    if cached:
        return cached

    blob = _fetch_workbook(LF_BASE.format(period=period, fname=GROSS_FLOWS))
    if blob is None:
        return {}
    parsed = _parse_gross_flows(blob)
    if parsed:
        cache.write_json(key, [
            {"h": pair, "o": [[o.date.isoformat(), o.value] for o in obs]}
            for pair, obs in parsed.items()])
        cache.write_json(f"abs-index-{GROSS_FLOWS}", [{"period": period}])
    return parsed


def duration_counts() -> Workbook:
    """14a -- unemployed counts by duration of job search. Detailed release."""
    return workbook(DURATION, detailed=True)


# --------------------------------------------------------------------------
# Consumer Price Index
# --------------------------------------------------------------------------

CPI_LANDING = ("https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/"
               "consumer-price-index-australia/latest-release")
CPI_BASE = ("https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/"
            "consumer-price-index-australia/{period}/{fname}")

# The CPI became a MONTHLY index from the October 2025 reference month, and
# that split every class-level series in two. Tables 3 and 13 are the monthly
# successors and start in 2017 and December 2024 respectively -- too short to
# say anything about how the current episode compares with the pre-pandemic
# decade. The quarterly history survives in two files, and both are needed.
#
# TABLE 18 is the quarterly class table in ORIGINAL terms. It carries index
# numbers back to 1948 and, for the three most recent quarters only, a
# `Contribution to Total CPI` at every level of the hierarchy. Three quarters
# is thin for a time series but it is not being used as one: contributions are
# additive down the tree, which is what lets `core.inflation` recover the
# expenditure classes from the data instead of a hardcoded list of group names
# that would rot at the next ABS restructure. The All groups contribution
# equals the All groups index exactly, so a contribution is an index point and
# a class weight falls straight out of it.
CPI_CLASSES_Q = "6401018.xlsx"

# APPENDIX 1a is the quarterly series the monthly restructure would otherwise
# have ended: 87 SEASONALLY ADJUSTED expenditure-class indexes -- exactly the
# leaves the hierarchy parse finds -- plus trimmed mean and weighted median as
# index, quarterly change and year-ended change, back to 1982. Everything on
# this tab that is quarterly and seasonally adjusted comes from here.
CPI_QUARTERLY_SA = "64010Appendix1a.xlsx"

# TABLE 6 is the monthly analytical series -- trimmed mean, weighted median and
# the ex-volatiles measures. Short history (April 2024 onwards), because the
# complete monthly CPI is new and the older Monthly CPI Indicator was retired
# after September 2025.
CPI_ANALYTICAL_M = "640106.xlsx"

# The measure the published chart calls "ex volatiles, travel & electricity".
# The ABS stops one term short -- there is no published series that also strips
# electricity -- so this is the closest real series and it is labelled by its
# own name on screen rather than by the one it is standing in for.
EX_VOLATILES = "All groups CPI excluding 'volatile items' and holiday travel"
TRIMMED_MEAN = "Trimmed Mean"
WEIGHTED_MEDIAN = "Weighted Median"
ALL_GROUPS = "All groups CPI"
ALL_GROUPS_SA = "All groups CPI, seasonally adjusted"

# Appendix 1a keeps the analytical measures on the same tab as the expenditure
# classes, so the class list has to exclude them by name or the breadth count
# ends up measuring the trimmed mean against itself.
_ANALYTICAL = (TRIMMED_MEAN, WEIGHTED_MEDIAN, ALL_GROUPS_SA, ALL_GROUPS)


def cpi_workbook(fname: str) -> Workbook:
    """One CPI workbook, behind the same release-keyed disk cache."""
    period = latest_release(CPI_LANDING)
    if period is None:
        for row in sorted(cache.read_json(f"abs-index-{fname}"), reverse=True,
                          key=lambda r: r.get("period", "")):
            cached = _from_cache(_cache_key(fname, row.get("period", "")),
                                 row.get("period", ""))
            if cached.ok:
                return cached
        return Workbook("", {}, {})

    key = _cache_key(fname, period)
    cached = _from_cache(key, period)
    if cached.ok:
        return cached

    blob = _fetch_workbook(CPI_BASE.format(period=period, fname=fname))
    if blob is None:
        return Workbook(period, {}, {})
    parsed = parse_workbook(blob, period)
    if parsed.ok:
        _to_cache(key, parsed)
        cache.write_json(f"abs-index-{fname}", [{"period": period}])
    return parsed


def _by_name(wb: Workbook, prefix: str, city: str = "Australia"
             ) -> tuple[dict[str, list[Observation]], list[str]]:
    """Series under one measure prefix, keyed on the bare series name.

    Also returns the names in WORKBOOK ORDER, which is not decoration: the ABS
    lists the CPI hierarchy depth-first, and that ordering is the only thing
    marking which series is a parent of which. Sorting it away, or handing back
    a dict and trusting insertion order to survive a round trip, would destroy
    the one signal `core.inflation.build_tree` runs on.
    """
    out: dict[str, list[Observation]] = {}
    order: list[str] = []
    for (header, _stype), obs in wb.columns.items():
        if not header.startswith(prefix):
            continue
        parts = [p.strip() for p in header.split(";")]
        if len(parts) < 3 or parts[2] != city or not parts[1] or not obs:
            continue
        if parts[1] not in out:
            order.append(parts[1])
        out[parts[1]] = obs
    return out, order


def cpi_class_indexes() -> tuple[dict[str, list[Observation]], list[str]]:
    """Quarterly index numbers at every hierarchy level, original terms.

    Keyed on the bare series name rather than the ABS's full header, because
    every downstream consumer -- the breadth counters, the cyclical split, the
    classification file a user edits by hand -- wants to say "Rents", not
    "Index Numbers ;  Rents ;  Australia ;".
    """
    return _by_name(cpi_workbook(CPI_CLASSES_Q), "Index Numbers")


def cpi_contributions() -> tuple[dict[str, list[Observation]], list[str]]:
    """Index-point contributions at every hierarchy level.

    Three quarters deep and additive down the tree. The order that comes back
    with it is what `core.inflation.build_tree` parses.
    """
    return _by_name(cpi_workbook(CPI_CLASSES_Q), "Contribution to Total CPI")


def cpi_class_indexes_sa() -> dict[str, list[Observation]]:
    """Quarterly SEASONALLY ADJUSTED indexes, expenditure classes only.

    87 series, which is the leaf count the hierarchy parse arrives at from the
    contributions independently -- two different files agreeing on the shape of
    the basket.
    """
    named, _ = _by_name(cpi_workbook(CPI_QUARTERLY_SA), "Index Numbers")
    return {k: v for k, v in named.items() if k not in _ANALYTICAL}


def cpi_quarterly_analytical(measure: str = "Percentage Change from Previous Period"
                             ) -> dict[str, list[Observation]]:
    """Quarterly trimmed mean, weighted median and headline, back to 1982.

    `measure` selects the transform the ABS has already published -- index,
    `Percentage Change from Previous Period`, or `Percentage Change from
    Corresponding Quarter of Previous Year`. Taken as published rather than
    derived from the index, because a trimmed mean is re-trimmed each quarter
    and its year-ended change is not the compounding of its quarterly ones.
    """
    return _by_name(cpi_workbook(CPI_QUARTERLY_SA), measure)[0]


def cpi_monthly_analytical(measure: str = "Percentage Change from Corresponding "
                                          "Month of Previous Year"
                           ) -> dict[str, list[Observation]]:
    """Monthly trimmed mean and ex-volatiles measures. April 2024 onwards."""
    return _by_name(cpi_workbook(CPI_ANALYTICAL_M), measure)[0]
