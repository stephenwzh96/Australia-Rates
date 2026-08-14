"""RBA statistical tables adapter.

Everything here degrades to an empty series rather than raising. This is a
pre-meeting tool: a failed HTTP call must never block the Verdict, and the
whole dashboard has to stay usable offline with values typed by hand.

WHY THIS IS SIMPLER THAN THE FRED ADAPTER IT REPLACES
------------------------------------------------------
Two differences, both in this app's favour:

  * NO API KEY. The RBA publishes its statistical tables as plain CSV at stable
    URLs. The US original needs a FRED key, degrades to "no-key" without one,
    and carries a whole `.env` path to manage it. None of that exists here --
    the Data tab has nothing to apologise for and no setup step.

  * ONE REQUEST, MANY SERIES. FRED bills per series, so the US adapter fires a
    request per code and caches per code. An RBA table arrives whole: table F1
    is a single ~300KB CSV carrying seventeen daily series including everything
    this app needs. So the fetch and the cache are keyed on the TABLE, and
    pulling a second series off a table already in hand costs nothing.

WHAT THE CASH RATE IS, AND WHY IB CAN BE READ SO CLEANLY
----------------------------------------------------------
`FIRMMCRID` is the Interbank Overnight Cash Rate -- the actual traded overnight
rate, and the rate IB futures settle against. `FIRMMCRTD` is the RBA's target.
Under the RBA's ample-reserves framework the two sit almost on top of each
other, which is a real advantage over the US setup: EFFR wanders inside its
corridor with reserve scarcity and IORB technicals, so a fed funds contract
carries drift that has nothing to do with policy. AONIA barely does. A gap
between target and traded rate is therefore genuine news here rather than
routine noise, and `cash_rate_gap_bp` surfaces it.

CSV SHAPE
---------
Eight header rows (Title, Description, Frequency, Type, Units, Source,
Publication date, Series ID), then one row per date in DD/MM/YYYY, one column
per series. Blank cells are ordinary -- a series that started in 1969 sits
beside one that started in 2013 -- so a missing value is skipped, never
treated as zero.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests

from . import cache
from .common import Observation

BASE = "https://www.rba.gov.au/statistics/tables/csv/{table}-data.csv"
TIMEOUT = 20

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}

# The row label that carries the machine-readable codes, and the one that
# carries the publication date the whole table was stamped with.
_ID_ROW = "Series ID"
_PUBLISHED_ROW = "Publication date"


@dataclass(frozen=True)
class Series:
    code: str
    label: str
    unit: str
    table: str
    # Last date the RBA actually published this series, when it has stopped.
    # Carried rather than dropped so the Data tab can say "discontinued 2022"
    # instead of showing an empty panel and leaving the user to wonder whether
    # the fetch broke.
    discontinued: date | None = None

    @property
    def live(self) -> bool:
        return self.discontinued is None


# Everything this app reads, all from table F1 (daily money market). Codes and
# coverage confirmed against the live CSV on 2026-08-14 -- note the trailing
# "D", which marks the daily variant; the same series without it lives in F1.1
# as a monthly average and is NOT interchangeable.
#
# THREE OF THESE ARE DEAD, and finding that out changed the design. The RBA
# stopped publishing its OIS series after 1 December 2022 and its Treasury Note
# yields after 2013. The plan for this app had the BBSW/OIS basis coming
# straight off F1 as two columns of one CSV; that is simply not available for
# any current date. `core.bbsw` therefore derives the OIS leg from the IB strip
# instead -- which is the better construction anyway, and the one the US
# original uses: IB prices the average cash rate over a month, so an IB strip
# IS an OIS curve, quoted live and tradeable rather than surveyed. They are
# kept in the catalogue because the pre-2023 history is still worth plotting
# for context on where the basis has been.
CATALOGUE: tuple[Series, ...] = (
    Series("FIRMMCRTD", "Cash rate target", "%", "f1"),
    Series("FIRMMCRID", "Interbank overnight cash rate (AONIA)", "%", "f1"),
    Series("FIRMMBAB30D", "1-month BBSW", "%", "f1"),
    Series("FIRMMBAB90D", "3-month BBSW", "%", "f1"),
    Series("FIRMMBAB180D", "6-month BBSW", "%", "f1"),
    Series("FIRMMCRIV", "Cash market turnover", "$m", "f1"),
    Series("FIRMMOIS1D", "1-month OIS", "%", "f1", date(2022, 12, 1)),
    Series("FIRMMOIS3D", "3-month OIS", "%", "f1", date(2022, 12, 1)),
    Series("FIRMMOIS6D", "6-month OIS", "%", "f1", date(2022, 12, 1)),
    Series("FIRMMTN1D", "1-month Treasury Note", "%", "f1", date(2013, 5, 17)),
    Series("FIRMMTN3D", "3-month Treasury Note", "%", "f1", date(2013, 5, 17)),
)

BY_CODE = {s.code: s for s in CATALOGUE}
LIVE_CODES = tuple(s.code for s in CATALOGUE if s.live)

BBSW_CODE = "FIRMMBAB90D"
OIS_CODE = "FIRMMOIS3D"          # historical only -- see the note above
CASH_TARGET_CODE = "FIRMMCRTD"
CASH_TRADED_CODE = "FIRMMCRID"


@dataclass(frozen=True)
class SeriesData:
    code: str
    label: str
    unit: str
    observations: list[Observation]
    error: str | None = None

    @property
    def latest(self) -> Observation | None:
        return self.observations[-1] if self.observations else None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.observations)

    def at_or_before(self, when: date) -> Observation | None:
        candidates = [o for o in self.observations if o.date <= when]
        return candidates[-1] if candidates else None

    def since(self, start: date) -> list[Observation]:
        return [o for o in self.observations if o.date >= start]


def _empty(code: str, error: str) -> SeriesData:
    s = BY_CODE.get(code)
    return SeriesData(code, s.label if s else code, s.unit if s else "", [], error)


# The RBA is not consistent across tables: F1 (daily) writes "04-Jan-2011",
# F1.1 (monthly) writes "30/06/1969". Both are day-first, which is the part
# that matters -- reading either as month-first would silently relabel every
# observation before the 13th of a month. Tried in order; anything matching
# neither is dropped rather than guessed at.
_DATE_FORMATS = ("%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")


def _parse_date(raw: str) -> date | None:
    """Parse an RBA date cell, or None if it is not one.

    Returning None is how a header or footer row gets skipped, so this is
    called on every row and must stay cheap and silent.
    """
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_table(text: str) -> dict[str, list[Observation]]:
    """Split one RBA CSV into `{series id: observations}`. Never raises.

    Column positions are taken from the `Series ID` header row rather than
    assumed, so a table gaining or reordering a column does not silently
    shift every series by one.
    """
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except (csv.Error, ValueError):
        return {}

    ids: list[str] = []
    body_start = 0
    for i, row in enumerate(rows):
        if row and row[0].strip() == _ID_ROW:
            ids = [c.strip() for c in row]
            body_start = i + 1
            break
    if not ids:
        return {}

    out: dict[str, list[Observation]] = {code: [] for code in ids[1:] if code}
    for row in rows[body_start:]:
        if not row or not row[0].strip():
            continue
        when = _parse_date(row[0])
        if when is None:
            continue
        for col in range(1, min(len(row), len(ids))):
            code = ids[col]
            raw = row[col].strip()
            if not code or not raw:
                continue
            try:
                out[code].append(Observation(when, float(raw)))
            except (ValueError, KeyError):
                continue

    for obs in out.values():
        obs.sort(key=lambda o: o.date)
    return out


def published_at(text: str) -> date | None:
    """The table's own publication stamp, so the Data tab can show a vintage
    rather than implying the numbers are from today."""
    for row in csv.reader(io.StringIO(text)):
        if row and row[0].strip() == _PUBLISHED_ROW:
            for cell in row[1:]:
                cell = cell.strip()
                if not cell:
                    continue
                for fmt in ("%d-%b-%Y", "%d/%m/%Y"):
                    try:
                        return datetime.strptime(cell, fmt).date()
                    except ValueError:
                        continue
            return None
    return None


def fetch_table(table: str) -> str | None:
    """Raw CSV text for one RBA table, or None. Never raises."""
    try:
        r = requests.get(BASE.format(table=table), headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None


def table_cached(table: str, recheck_after: int = 1) -> dict[str, list[Observation]]:
    """One table's series, behind the on-disk cache.

    Cached at the TABLE level: a single fetch already carries every series the
    app reads, so caching per series would re-download the same 300KB once per
    code. Rechecked daily -- nothing in F1 publishes more often, and the first
    visit of the day is the "did anything print?" check.
    """
    key = f"rba-table-{table}"
    marker = cache.load(f"{key}-stamp")
    today = date.today()
    fresh = marker is not None and marker.fetched_at == today

    if not fresh:
        text = fetch_table(table)
        if text:
            parsed = parse_table(text)
            # `parsed` is keyed off the header row, so a table whose BODY failed
            # to parse still comes back as a full dict of EMPTY lists -- truthy,
            # and enough to poison the cache for a day if it were written. Only
            # a table with actual observations counts as a successful fetch.
            if any(obs for obs in parsed.values()):
                cache.write_json(key, [
                    {"code": c, "obs": [[o.date.isoformat(), o.value] for o in obs]}
                    for c, obs in parsed.items()
                ])
                cache.save(f"{key}-stamp", [Observation(today, 1.0)], today)
                return parsed

    out: dict[str, list[Observation]] = {}
    for row in cache.read_json(key):
        code = row.get("code")
        if not code:
            continue
        obs: list[Observation] = []
        for pair in row.get("obs", []):
            try:
                obs.append(Observation(date.fromisoformat(pair[0]), float(pair[1])))
            except (ValueError, TypeError, IndexError):
                continue
        out[code] = obs
    return out


def fetch(code: str, years: float = 3.0) -> SeriesData:
    """One catalogued series. Never raises."""
    spec = BY_CODE.get(code)
    if spec is None:
        return _empty(code, f"unknown series {code}")
    series = table_cached(spec.table).get(code)
    if not series:
        return _empty(code, "unavailable")
    cutoff = date.today() - timedelta(days=int(365.25 * years))
    obs = [o for o in series if o.date >= cutoff]
    if not obs:
        return _empty(code, "no observations")
    return SeriesData(code, spec.label, spec.unit, obs)


def fetch_many(codes: list[str], years: float = 3.0) -> dict[str, SeriesData]:
    """Several series off however few tables they live on."""
    return {c: fetch(c, years) for c in codes}


# --------------------------------------------------------------------------
# Derived readings
# --------------------------------------------------------------------------

def historical_bbsw_ois_basis() -> list[Observation]:
    """3-month BBSW minus 3-month OIS in bp -- HISTORY ONLY, ends Dec 2022.

    Not the live basis, and must not be presented as one. The RBA retired its
    OIS series on 1 December 2022, so this stops there permanently no matter
    how recently the table was fetched. Its job is context: it says where this
    spread has historically sat, which is what makes a live reading from
    `core.bbsw` -- derived off the IB strip rather than a published OIS quote
    -- interpretable as wide or narrow.

    Both legs come from the same table on the same dates, so there is no
    alignment guesswork; dates present in only one leg are dropped rather than
    forward-filled.
    """
    table = table_cached("f1")
    bbsw = {o.date: o.value for o in table.get(BBSW_CODE, [])}
    ois = {o.date: o.value for o in table.get(OIS_CODE, [])}
    return [Observation(d, (bbsw[d] - ois[d]) * 100.0)
            for d in sorted(set(bbsw) & set(ois))]


def cash_rate_gap_bp() -> float | None:
    """Traded AONIA minus the target, in bp, on the latest common date.

    Normally within a basis point. A persistent gap is a funding-market signal
    and a reason to distrust an IB-implied path, because the contract settles
    on the traded rate, not the target.
    """
    table = table_cached("f1")
    target = {o.date: o.value for o in table.get(CASH_TARGET_CODE, [])}
    traded = {o.date: o.value for o in table.get(CASH_TRADED_CODE, [])}
    common = sorted(set(target) & set(traded))
    if not common:
        return None
    d = common[-1]
    return (traded[d] - target[d]) * 100.0


def current_cash_rate() -> float | None:
    """The prevailing target, for anchoring the strip when the front IB
    contract cannot supply a clean spot (see `core.strip.implied_spot`)."""
    obs = table_cached("f1").get(CASH_TARGET_CODE, [])
    return obs[-1].value if obs else None


def realised_daily_vol_bp(code: str = BBSW_CODE, days: int = 90,
                          exclude: set[date] | None = None) -> float | None:
    """Standard deviation of daily changes in a rate series, in bp.

    This is what stands in for the US version's contract-price volatility. The
    ASX feed has no history at all, so there is nothing to calibrate a
    simulation against -- but a 90-day bank bill future is quoted as
    `100 - yield`, so a one basis point move in the underlying rate IS a one
    basis point move in the contract price. Realised vol of the rate is
    therefore the right scale for the contract, not a proxy for it.

    `exclude` drops known decision dates, whose jump the Monte Carlo simulates
    separately from the pre-announcement drift being estimated here.
    """
    obs = table_cached("f1").get(code, [])
    if len(obs) < 3:
        return None
    cutoff = date.today() - timedelta(days=days)
    window = [o for o in obs if o.date >= cutoff]
    skip = exclude or set()
    diffs = [(b.value - a.value) * 100.0
             for a, b in zip(window, window[1:]) if b.date not in skip]
    if len(diffs) < 2:
        return None
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
    return var ** 0.5
