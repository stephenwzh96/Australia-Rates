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

    for obs in columns.values():
        obs.sort(key=lambda o: o.date)
    return Workbook(period, {k: v for k, v in columns.items() if v}, ids)


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


def labour_force_rates() -> Workbook:
    """X29 -- unemployment, underemployment and youth unemployment rates."""
    return workbook(X29)


def duration_counts() -> Workbook:
    """14a -- unemployed counts by duration of job search. Detailed release."""
    return workbook(DURATION, detailed=True)
