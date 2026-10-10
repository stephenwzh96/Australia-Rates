"""When each observation first became public -- the as-of rule for every tab.

The sidebar's as-of date is a global "today": every tab must show only what
had been PUBLISHED by then, not every observation whose reference date falls
before it. A June-quarter GDP figure is dated April or June but printed in
September; cutting on the reference date would show it to an as-of in July.

Every source the app reads belongs to one release FAMILY below. A family's
rule maps an observation's reference period to the day it was certainly
public, and every rule errs LATE: a date too late can only hide a print for a
few days, a date too early would leak the future into a replay. The Labour
Force is dated exactly wherever the ABS release page still exists (every
release from December 2019, passed in as `exact_lf`).

Rules key on the reference PERIOD (the month or quarter containing the
observation date), never on the date as stamped: the RBA dates a quarter at
its last day, the ABS API at its first, and both must land on the same rule.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence, TypeVar

# ------------------------------------------------------------------ families

MARKET = "market"                  # daily rates and settlements: public the next day
LABOUR_FORCE = "labour_force"      # ABS Labour Force, monthly
LF_DETAILED = "lf_detailed"        # LF Detailed and the gross flows, a week later
CPI_MONTHLY = "cpi_monthly"        # ABS monthly CPI
CPI_QUARTERLY = "cpi_quarterly"    # ABS quarterly CPI
WPI = "wpi"                        # ABS Wage Price Index
NATIONAL_ACCOUNTS = "national_accounts"   # GDP, GDI, consumption, H1/H2, labour account, QBIS
CAPEX = "capex"                    # ABS private new capital expenditure
CONSTRUCTION = "construction"      # ABS construction work done
PPI = "ppi"                        # ABS producer prices
VACANCIES = "vacancies"            # ABS job vacancies (quarterly, mid-quarter reference)
NAB = "nab"                        # NAB monthly business survey (via RBA H3, or pasted)
WESTPAC_MI = "westpac_mi"          # Westpac-MI consumer sentiment (via RBA H3)
EXPECTATIONS = "expectations"      # RBA G3, quarterly averages of the surveys
CREDIT = "credit"                  # RBA D1 financial aggregates
APPROVALS = "approvals"            # ABS building approvals
LENDING = "lending"                # ABS lending indicators (quarterly)
HSI = "hsi"                        # ABS monthly household spending indicator
LABOUR_ACCOUNT = "labour_account"  # ABS Labour Account (filled jobs), quarterly
PRIVATE_MONTHLY = "private_monthly"  # pasted commercial monthlies (ANZ, CoreLogic)
SEED = "seed"                      # a seeded reading dated at its vintage

QUARTERLY = {CPI_QUARTERLY, WPI, NATIONAL_ACCOUNTS, CAPEX, CONSTRUCTION, PPI, EXPECTATIONS,
             LENDING, LABOUR_ACCOUNT}

#: Each family's name as a reader knows it.
LABEL = {
    MARKET: "ASX and RBA daily rates", LABOUR_FORCE: "Labour Force",
    LF_DETAILED: "LF Detailed, gross flows", CPI_MONTHLY: "CPI, monthly",
    CPI_QUARTERLY: "CPI, quarterly", WPI: "Wage Price Index",
    NATIONAL_ACCOUNTS: "National accounts (GDP, H1, H2)", CAPEX: "Private capex",
    CONSTRUCTION: "Construction work done", PPI: "Producer prices",
    VACANCIES: "Job vacancies", NAB: "NAB business survey", WESTPAC_MI: "Westpac-MI sentiment",
    EXPECTATIONS: "Inflation expectations (G3)", CREDIT: "Credit aggregates (D1)",
    APPROVALS: "Building approvals", LENDING: "Lending indicators",
    HSI: "Household spending indicator", LABOUR_ACCOUNT: "Labour Account (filled jobs)",
    PRIVATE_MONTHLY: "Commercial monthlies (pasted)", SEED: "Seeded readings",
}

#: How each family is dated, in words, for the Data tab.
RULE_TEXT = {
    MARKET: "the next day",
    LABOUR_FORCE: "exact from Dec 2019 (ABS release pages); else the 28th of the next month",
    LF_DETAILED: "a week after the Labour Force (exact + 7 days); else the 7th of month +2",
    CPI_MONTHLY: "end of the next month (published the last Wednesday)",
    CPI_QUARTERLY: "end of the month after the quarter (published the last Wednesday)",
    WPI: "end of the 2nd month after the quarter (published mid-month)",
    NATIONAL_ACCOUNTS: "15th of the 3rd month after the quarter (published the first week)",
    CAPEX: "end of the 2nd month after the quarter (published the last Thursday)",
    CONSTRUCTION: "end of the 2nd month after the quarter (published the 4th week)",
    PPI: "5th of the 2nd month after the quarter (published at month end)",
    VACANCIES: "end of the 2nd month after the reference month (published ~5 weeks on)",
    NAB: "21st of the next month (published ~2nd Tuesday)",
    WESTPAC_MI: "end of the survey month (published ~2nd Wednesday)",
    EXPECTATIONS: "end of the month after the quarter (G3 is quarterly)",
    CREDIT: "end of the next month (published the last business day)",
    APPROVALS: "10th of month +2 (published ~30 days after the month)",
    LENDING: "20th of the 2nd month after the quarter (published ~6 weeks on)",
    HSI: "15th of month +2 (published ~5 weeks after the month)",
    LABOUR_ACCOUNT: "end of the 3rd month after the quarter (published after the accounts)",
    PRIVATE_MONTHLY: "end of the next month (each publishes within it)",
    SEED: "its own date",
}


def _add_months(d: date, n: int) -> date:
    k = d.year * 12 + d.month - 1 + n
    return date(k // 12, k % 12 + 1, 1)


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _quarter_end_month(d: date) -> date:
    """First of the last month of the quarter containing d."""
    return date(d.year, (d.month - 1) // 3 * 3 + 3, 1)


def _day(month: date, day: int) -> date:
    return date(month.year, month.month, min(day, calendar.monthrange(month.year, month.month)[1]))


def public_on(family: str, d: date, exact_lf: Mapping[date, date] | None = None) -> date:
    """The day an observation dated `d` in `family` was certainly public."""
    m = date(d.year, d.month, 1)
    q = _quarter_end_month(d)
    exact = exact_lf or {}
    if family == MARKET:
        return d + timedelta(days=1)
    if family == LABOUR_FORCE:
        return exact.get(m) or _day(_add_months(m, 1), 28)
    if family == LF_DETAILED:
        hit = exact.get(m)
        return hit + timedelta(days=7) if hit else _day(_add_months(m, 2), 7)
    if family == CPI_MONTHLY:
        return _month_end(_add_months(m, 1))
    if family == CPI_QUARTERLY:
        return _month_end(_add_months(q, 1))
    if family == WPI:
        return _month_end(_add_months(q, 2))
    if family == NATIONAL_ACCOUNTS:
        return _day(_add_months(q, 3), 15)
    if family in (CAPEX, CONSTRUCTION):
        return _month_end(_add_months(q, 2))
    if family == PPI:
        return _day(_add_months(q, 2), 5)
    if family == VACANCIES:
        return _month_end(_add_months(m, 2))
    if family == NAB:
        return _day(_add_months(m, 1), 21)
    if family == WESTPAC_MI:
        return _month_end(m)
    if family == EXPECTATIONS:
        return _month_end(_add_months(q, 1))
    if family == CREDIT:
        return _month_end(_add_months(m, 1))
    if family == APPROVALS:
        return _day(_add_months(m, 2), 10)
    if family == LENDING:
        return _day(_add_months(q, 2), 20)
    if family == HSI:
        return _day(_add_months(m, 2), 15)
    if family == LABOUR_ACCOUNT:
        return _month_end(_add_months(q, 3))
    if family == PRIVATE_MONTHLY:
        return _month_end(_add_months(m, 1))
    if family == SEED:
        return d
    raise KeyError(f"unknown release family {family!r}")


T = TypeVar("T")


def _date_of(row) -> date:
    if isinstance(row, tuple):
        return row[0]
    return row.date


def published(rows: Iterable[T], family: str, as_of: date | None,
              exact_lf: Mapping[date, date] | None = None) -> list[T]:
    """`rows` -- `(date, value)` pairs or `Observation`s -- cut to what had
    printed by `as_of` (inclusive). `as_of=None` keeps everything."""
    rows = list(rows)
    if as_of is None:
        return rows
    return [r for r in rows if public_on(family, _date_of(r), exact_lf) <= as_of]


def begun(rows: Iterable[T], as_of: date | None) -> list[T]:
    """`rows` whose reference MONTH had begun by `as_of` -- the cut for a copy
    read today, where the copy itself proves a print is out. The RBA stamps a
    month at its last day, so a print made inside its own month (Westpac-MI,
    out on the 7th, row dated the 31st) is dated after the day it was read;
    cutting on the stamp hid it."""
    rows = list(rows)
    if as_of is None:
        return rows
    return [r for r in rows if _date_of(r).replace(day=1) <= as_of]


def published_map(data: Mapping[str, Sequence[T]], families: Mapping[str, str] | str,
                  as_of: date | None, exact_lf: Mapping[date, date] | None = None,
                  default: str | None = None) -> dict[str, list[T]]:
    """`published` over a dict of series: one family for all, or per key
    (`default` for keys the mapping does not name; a key with neither is an
    error -- an unclassified series must not be shown uncut)."""
    out = {}
    for k, rows in data.items():
        fam = families if isinstance(families, str) else families.get(k, default)
        if fam is None:
            raise KeyError(f"series {k!r} has no release family")
        out[k] = published(rows, fam, as_of, exact_lf)
    return out
