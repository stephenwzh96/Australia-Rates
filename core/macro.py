"""Australian macro-momentum dashboard -- every indicator as a z-score of its
own 2000-2020 short-run momentum, grouped into five blocks, with a Total.

WHAT THIS REPRODUCES
--------------------
The Westpac CIO "Australian Macroeconomic Indicators" table (the sibling of the
US version in `core.macro` of the FOMC app). Each series is put through the
short-run momentum transform named in the source -- ``QoQ``, ``3m/3m m.a.``,
``Annualised (3m/3m m.a.)``, ``MoM (3m m.a.)``, ``3m m.a.`` -- then scored as a
z against a fixed January-2000 to December-2020 mean and standard deviation.
"Bad news" series (unemployment, underemployment) are sign-flipped so that a
POSITIVE z always means a stronger economy. Group scores are the mean of their
members; the Total is the mean of every scored member (NOT the mean of the five
group means -- the groups hold 4/4/4/4/7 indicators, and the source slide's
Total, -0.006, is the equal-weighted mean across all 22 printed z-scores; the
mean of the five group means would be -0.014).

WHY A SEED, AND WHAT LIVE DATA DOES
-----------------------------------
Australia has no FRED. Of the 23 series the slide uses, two are commercial
with no free feed (ANZ-Indeed job ads, ANZ-Roy Morgan consumer confidence).
Three more that look commercial are republished by the RBA -- Westpac-MI
sentiment and NAB business conditions in table H3, the Melbourne Institute
inflation expectations in G3 -- and are read from there; two (CoreLogic home
values, the HSI in volume terms) have no free feed in the slide's form. So the baseline mean and standard deviation for every indicator are
SEEDED from the published slide -- ``seed_mean`` is the printed "Historical Mean
(2000-2020)" and ``seed_sd`` is backed out from the printed z-score as
``|current - mean| / |z|``. That makes the table reproduce the slide exactly
out of the box.

Where a keyless public source exists (``data.abs_sdmx``, ``data.rba`` tables,
the labour and CPI fetches the rest of the app already runs) the latest reading
is recomputed from live data and re-scored against the SEED mean/sd -- the same
move `core.employment.manual_row` makes. A live series also carries history, so
it can feed the Momentum Signal chart; a seed-only indicator sits the chart out
until it is wired or a history is pasted for it.

EVERY indicator carries its real momentum transform, so a pasted raw series
flows through the same arithmetic as a wired one. ``transform`` is only ever
empty for a row we have chosen not to recompute.

`core/` never imports `data/` or `streamlit`: every function here takes plain
`{key: [(date, value)]}` and returns plain readings, testable without a network.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date

# The stated z-score base window, inclusive. Same window as `core.employment`.
BASELINE_START = date(2000, 1, 1)
BASELINE_END = date(2020, 12, 31)

# Slide order, top to bottom.
GROUPS = ("Labour Market", "Inflation", "Housing", "Consumer",
          "Cyclical Economic Indicators")

# The transform vocabulary. `transform_label` is what prints in the table; the
# `transform` tag drives the live/pasted recompute in `transform()`.
#   pass_q    the series is already the q/q % or ppt the slide wants
#   qoq_lvl   quarterly level -> % change on the prior quarter
#   chg_3m3m  3-month moving average, minus its value 3 months earlier (ppt)
#   ann_3m3m  3m m.a., annualised % change vs 3 months earlier
#   ma3       plain 3-month moving average of the level
#   mom3      month-on-month % change, then a 3-month moving average


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    group: str
    transform_label: str        # what prints in the "Transform" column
    seed_mean: float            # slide's "Historical Mean (2000-2020)"
    seed_sd: float              # backed out of the slide's z-score
    seed_current: float         # slide's "Current"
    seed_asof: date             # the slide vintage
    transform: str = ""         # a tag above; "" = never recompute
    bad_news: bool = False      # flip the z so +z = stronger economy
    auto: bool = False          # a wired feed exists (else seed / paste only)
    commercial: bool = False    # no free feed exists at all
    # Score the live series against ITS OWN 2000-2020 mean/sd rather than the
    # slide's: for a row whose public series is on a different scale or level
    # from the one the slide used (see the row's source_label).
    own_baseline: bool = False
    source_label: str = ""      # shown in the Data tab / tooltip
    note: str = ""


# The slide vintage: the August 2026 RBA SoMP round the picture is dated to.
_ASOF = date(2026, 8, 1)


def _sd(current: float, mean: float, z: float) -> float:
    """Back the window standard deviation out of the slide's printed z."""
    return abs(current - mean) / abs(z)


# Order matches the source slide, top to bottom. seed_sd is computed inline
# from the slide's own three printed numbers so the arithmetic is on show.
INDICATORS: tuple[Indicator, ...] = (
    # -- Labour Market -------------------------------------------------------
    Indicator("unemployment", "Unemployment Rate", "Labour Market",
              "3m/3m m.a.", -0.0193, _sd(0.21, -0.0193, -1.200), 0.21, _ASOF,
              transform="chg_3m3m", bad_news=True, auto=True,
              source_label="ABS Labour Force, unemployment rate SA (3m/3m m.a. change, ppt)"),
    Indicator("underemployment", "Underemployment Rate", "Labour Market",
              "3m/3m m.a.", 0.0252, _sd(0.313, 0.0252, -1.273), 0.313, _ASOF,
              transform="chg_3m3m", bad_news=True, auto=True,
              source_label="ABS Labour Force, underemployment rate SA (3m/3m m.a. change, ppt)"),
    Indicator("job_vacancies", "Job Vacancies QoQ", "Labour Market",
              "QoQ", 1.09, _sd(-2.11, 1.09, -0.578), -2.11, _ASOF,
              transform="qoq_lvl", auto=True,
              source_label="RBA table H5, job vacancies SA (q/q % change)"),
    Indicator("anz_job_ads", "ANZ Job Ads Annualised", "Labour Market",
              "Ann. (3m/3m m.a.)", 6.54, _sd(3.23, 6.54, -0.141), 3.23, _ASOF,
              transform="ann_3m3m", commercial=True,
              source_label="ANZ-Indeed Australian Job Advertisements (commercial -- paste to update)"),
    # -- Inflation ---------------------------------------------------------
    Indicator("cpi_trimmed_mean", "CPI Trimmed Mean QoQ", "Inflation",
              "QoQ", 0.633, _sd(0.813, 0.633, 0.860), 0.813, _ASOF,
              transform="pass_q", auto=True,
              source_label="ABS CPI, trimmed mean SA (q/q % change)"),
    Indicator("mi_inflation_expectations", "MI Inflation Expectations", "Inflation",
              "3m m.a.", 4.43, _sd(5.26, 4.43, 0.947), 5.26, _ASOF,
              transform="ma3", auto=True,
              source_label="Melbourne Institute consumer inflation expectations, as "
                           "RBA table G3 republishes it (GCONEXP) -- a quarterly average of "
                           "the monthly survey, which IS its 3m m.a. at the quarter's end"),
    Indicator("wpi", "WPI QoQ", "Inflation",
              "QoQ", 0.785, _sd(0.754, 0.785, -0.146), 0.754, _ASOF,
              transform="pass_q", auto=True,
              source_label="RBA table H4 / ABS WPI, total hourly rates SA (q/q % change)"),
    Indicator("ppi", "PPI QoQ", "Inflation",
              "QoQ", 0.558, _sd(1.3, 0.558, 1.315), 1.3, _ASOF,
              transform="pass_q", auto=True,
              source_label="ABS Producer Price Indexes, final demand ex exports (q/q % change)"),
    # -- Housing ---------------------------------------------------------
    Indicator("building_approvals", "Building Approvals MoM", "Housing",
              "MoM (3m m.a.)", 10.4, _sd(43.7, 10.4, 0.844), 43.7, _ASOF,
              transform="mom3", auto=True, own_baseline=True,
              source_label="ABS Building Approvals table 6, total dwelling units SA (m/m %, "
                           "3m m.a.). The slide's 43.7 / mean 10.4 is not reproducible from "
                           "the published series (its 2000-2020 m/m 3m m.a. averages 0.4, "
                           "sd 3.9), so the row is scored on that own baseline"),
    Indicator("private_building_approvals", "Private Building Approvals MoM",
              "Housing", "MoM (3m m.a.)", 4.15, _sd(12.1, 4.15, 0.245), 12.1, _ASOF,
              transform="mom3", auto=True, own_baseline=True,
              source_label="ABS Building Approvals table 6, private sector houses SA (m/m %, "
                           "3m m.a.), scored on its own 2000-2020 baseline as the total is"),
    Indicator("house_prices", "House Prices Annualised", "Housing",
              "Ann. (3m/3m m.a.)", 5.09, _sd(-3.26, 5.09, -1.261), -3.26, _ASOF,
              transform="ann_3m3m",
              source_label="CoreLogic dwelling values, capital cities -- the slide "
                           "measure. No free monthly feed (ABS RPPI is quarterly), "
                           "so this stays on the seed -- paste to update"),
    Indicator("home_loans", "Home Loans QoQ", "Housing",
              "QoQ", 1.14, _sd(-5.22, 1.14, -1.118), -5.22, _ASOF,
              transform="qoq_lvl", auto=True,
              source_label="ABS Lending Indicators table 1, households' new housing loan "
                           "commitments excl. refinancing, value SA (q/q % change)"),
    # -- Consumer ---------------------------------------------------------
    Indicator("household_consumption", "Household Consumption QoQ", "Consumer",
              "QoQ", 0.765, _sd(0.509, 0.765, -0.472), 0.509, _ASOF,
              transform="pass_q", auto=True,
              source_label="ABS National Accounts, household final consumption CVM SA (q/q % change)"),
    Indicator("household_spending", "Household Spending Ind. Annualised",
              "Consumer", "Ann. (3m/3m m.a.)", 3.18, _sd(4.07, 3.18, 0.231), 4.07, _ASOF,
              transform="ann_3m3m",
              source_label="ABS Monthly Household Spending Indicator, total CVM SA "
                           "(annualised 3m/3m m.a.) -- the monthly SDMX series is "
                           "current-price only and annualises well above the CVM "
                           "slide reading, so this row stays on the seed -- paste CVM to update"),
    Indicator("westpac_mi_sentiment", "Westpac-MI Consumer Sentiment", "Consumer",
              "3m/3m m.a.", 0.0524, _sd(-5.19, 0.0524, -0.945), -5.19, _ASOF,
              transform="chg_3m3m", auto=True,
              source_label="Westpac-Melbourne Institute Consumer Sentiment Index, as RBA "
                           "table H3 republishes it (GICWMICS, 3m/3m m.a. change)"),
    Indicator("anz_rm_confidence", "ANZ-Roy Morgan Consumer Conf.", "Consumer",
              "3m/3m m.a.", 0.0616, _sd(10.2, 0.0616, 2.205), 10.2, _ASOF,
              transform="chg_3m3m", commercial=True,
              source_label="ANZ-Roy Morgan Consumer Confidence (commercial -- paste to update)"),
    # -- Cyclical Economic Indicators -----------------------------------
    Indicator("capex", "CapEx QoQ", "Cyclical Economic Indicators",
              "QoQ", 1.23, _sd(6.5, 1.23, 1.196), 6.5, _ASOF,
              transform="qoq_lvl", auto=True,
              source_label="ABS Private New Capital Expenditure, total CVM SA (q/q % change)"),
    Indicator("construction_work_done", "Construction Work Done QoQ",
              "Cyclical Economic Indicators",
              "QoQ", 0.87, _sd(3.42, 0.87, 0.517), 3.42, _ASOF,
              transform="qoq_lvl", auto=True,
              source_label="ABS Construction Work Done, total CVM SA (q/q % change)"),
    Indicator("company_profit", "Company Profit QoQ", "Cyclical Economic Indicators",
              "QoQ", 2.31, _sd(-1.34, 2.31, -0.580), -1.34, _ASOF,
              transform="pass_q", auto=True,
              source_label="ABS Business Indicators, company gross operating profits SA (q/q % change)"),
    Indicator("inventories", "Inventories QoQ", "Cyclical Economic Indicators",
              "QoQ", 0.387, _sd(0.499, 0.387, 0.121), 0.499, _ASOF,
              transform="pass_q", auto=True,
              source_label="ABS Business Indicators, inventories CVM SA (q/q % change)"),
    Indicator("gdp", "GDP QoQ", "Cyclical Economic Indicators",
              "QoQ", 0.7, _sd(0.274, 0.7, -0.984), 0.274, _ASOF,
              transform="pass_q", auto=True,
              source_label="ABS National Accounts, GDP CVM SA (q/q % change)"),
    Indicator("private_credit", "Private Credit MoM", "Cyclical Economic Indicators",
              "MoM (3m m.a.)", 0.573, _sd(0.736, 0.573, 0.438), 0.736, _ASOF,
              transform="ma3", auto=True,
              source_label="RBA table D1, total credit incl. select financial businesses, "
                           "m/m % growth (DGFACNWM, 3m m.a.) -- the EFS-era successor to "
                           "DGFACM, which froze at the June 2019 changeover"),
    Indicator("nab_business_conditions", "NAB Business Conditions",
              "Cyclical Economic Indicators",
              "3m m.a.", 6.18, _sd(3.22, 6.18, -0.367), 3.22, _ASOF,
              transform="ma3", auto=True, own_baseline=True,
              source_label="NAB Monthly Business Survey business conditions, as RBA table "
                           "H3 republishes it (GICNBC, 3m m.a.). H3's series sits about four "
                           "points below the slide's, so it is scored on its own 2000-2020 "
                           "baseline -- which puts its z within 0.1 of the slide's"),
)

#: Release family of each row's live feed (or of the series a paste replaces).
RELEASE = {
    "unemployment": "labour_force", "underemployment": "labour_force",
    "job_vacancies": "vacancies", "anz_job_ads": "private_monthly",
    "cpi_trimmed_mean": "cpi_quarterly", "mi_inflation_expectations": "expectations",
    "wpi": "wpi", "ppi": "ppi",
    "building_approvals": "approvals", "private_building_approvals": "approvals",
    "house_prices": "private_monthly", "home_loans": "lending",
    "household_consumption": "national_accounts", "household_spending": "hsi",
    "westpac_mi_sentiment": "westpac_mi", "anz_rm_confidence": "private_monthly",
    "capex": "capex", "construction_work_done": "construction",
    "company_profit": "national_accounts", "inventories": "national_accounts",
    "gdp": "national_accounts", "private_credit": "credit",
    "nab_business_conditions": "nab",
}

BY_KEY = {i.key: i for i in INDICATORS}


def auto_keys() -> list[str]:
    """Indicators with a wired feed the app fills in."""
    return [i.key for i in INDICATORS if i.auto]


# --------------------------------------------------------------------------
# Series maths -- the live / pasted path
# --------------------------------------------------------------------------

def _clean(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Sorted `(date, float)`, dropping anything that will not parse.

    The wired feeds already hand floats, but the paste path (`macro:<key>` in
    `meetings/_manual_series.json`) can carry a stray blank or a text cell, and
    a transform that raises would take the whole tab down with it.
    """
    out: list[tuple[date, float]] = []
    for d, v in rows:
        if v is None or not isinstance(d, date):
            continue
        try:
            out.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _monthly(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """One value per calendar month, keyed to the first, mean within the month."""
    buckets: dict[date, list[float]] = {}
    for d, v in rows:
        buckets.setdefault(date(d.year, d.month, 1), []).append(v)
    return [(k, statistics.fmean(vs)) for k, vs in sorted(buckets.items())]


def _quarterly(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """One value per calendar quarter, keyed to the first month, mean within."""
    buckets: dict[date, list[float]] = {}
    for d, v in rows:
        q0 = date(d.year, (d.month - 1) // 3 * 3 + 1, 1)
        buckets.setdefault(q0, []).append(v)
    return [(k, statistics.fmean(vs)) for k, vs in sorted(buckets.items())]


def _is_quarterly(rows: list[tuple[date, float]]) -> bool:
    """True when observations sit roughly a quarter apart."""
    gaps = sorted((b[0] - a[0]).days for a, b in zip(rows, rows[1:]))
    return bool(gaps) and gaps[len(gaps) // 2] > 60


def _ma3(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """3-point average over CONSECUTIVE calendar months only: a gap (the
    Oct-2025 CPI hole, the 2019 credit changeover) ends the window rather than
    letting it reach across. Keyed on months, never on row position."""
    out: list[tuple[date, float]] = []
    for i in range(2, len(rows)):
        a, c = rows[i - 2][0], rows[i][0]
        if (c.year - a.year) * 12 + c.month - a.month != 2:
            continue
        out.append((rows[i][0], statistics.fmean(v for _, v in rows[i - 2:i + 1])))
    return out


def _month_gap(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def _months_before(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return date(y, m + 1, 1)


def transform(tag: str, raw: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Apply a momentum transform to a raw series. Returns `(date, value)`.

    Anything it cannot compute (too little history, a zero denominator) comes
    back as ``[]`` and the caller falls back to the seed reading.
    """
    rows = _clean(raw)
    if not rows or not tag:
        return []

    if tag == "pass_q":
        return _quarterly(rows)

    if tag == "qoq_lvl":
        # Adjacent quarters only. The ABS suspended the Job Vacancies survey
        # from Aug 2008 to Aug 2009; differencing by row printed the 18-month
        # change across the hole (-19.3%) as one quarter's, dated Q4 2009.
        q = _quarterly(rows)
        return [(b[0], (b[1] / a[1] - 1.0) * 100.0)
                for a, b in zip(q, q[1:])
                if a[1] and _month_gap(a[0], b[0]) == 3]

    if tag == "ma3":
        # A quarterly series of monthly-survey averages is already the 3m m.a.
        # at each quarter's end; smoothing it again would average 9 months.
        return _quarterly(rows) if _is_quarterly(rows) else _ma3(_monthly(rows))

    m = _monthly(rows)
    ma = _ma3(m)
    if tag == "chg_3m3m":
        by = dict(ma)
        return [(d, v - by[_months_before(d, 3)])
                for d, v in ma if _months_before(d, 3) in by]
    if tag == "ann_3m3m":
        by = dict(ma)
        out = []
        for d, v in ma:
            back = by.get(_months_before(d, 3))
            if back and back > 0 and v > 0:
                out.append((d, ((v / back) ** 4 - 1.0) * 100.0))
        return out
    if tag == "mom3":
        mom = [(b[0], (b[1] / a[1] - 1.0) * 100.0)
               for a, b in zip(m, m[1:]) if a[1] and _month_gap(a[0], b[0]) == 1]
        return _ma3(mom)
    return []


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Reading:
    indicator: Indicator
    last: float | None          # latest transformed value, natural units
    hist_mean: float | None     # 2000-2020 mean (the seed)
    hist_sd: float | None
    z: float | None             # sign-oriented: +z = stronger economy
    z_prev: tuple[float | None, float | None] = (None, None)  # 3m, 6m back
    as_of: date | None = None
    live: bool = False
    freq: str = "M"             # "Q" when the reading is a quarter's

    @property
    def ok(self) -> bool:
        return self.z is not None


def _z_of(value: float | None, mean: float, sd: float, bad_news: bool) -> float | None:
    if value is None or sd <= 0:
        return None
    flip = -1.0 if bad_news else 1.0
    return flip * (value - mean) / sd


def assess(ind: Indicator,
           live: list[tuple[date, float]] | None = None) -> Reading:
    """One indicator. Uses the live/pasted series when it transforms cleanly,
    else the slide's seeded current reading. Either way the z is against the
    seed mean/sd, so a live row and a seed row sit on the same axis -- except
    an `own_baseline` row, scored on its live series' own 2000-2020 window
    when that window is at least three-quarters covered."""
    mean, sd = ind.seed_mean, ind.seed_sd

    pts = transform(ind.transform, live) if live else []
    if pts and ind.own_baseline:
        mean, sd = own_baseline(pts) or (mean, sd)
    if pts:
        d_last, v_last = pts[-1]
        by = dict(pts)
        p3 = by.get(_months_before(d_last, 3))
        p6 = by.get(_months_before(d_last, 6))
        return Reading(
            ind, v_last, mean, sd, _z_of(v_last, mean, sd, ind.bad_news),
            (_z_of(p3, mean, sd, ind.bad_news), _z_of(p6, mean, sd, ind.bad_news)),
            d_last, live=True, freq="Q" if _is_quarterly(pts) else "M")

    return Reading(ind, ind.seed_current, mean, sd,
                   _z_of(ind.seed_current, mean, sd, ind.bad_news),
                   (None, None), ind.seed_asof, live=False)


def own_baseline(pts: list[tuple[date, float]]) -> tuple[float, float] | None:
    """(mean, sd) of a transformed series over the 2000-2020 window, or None
    when less than three-quarters of the window is covered."""
    window = [v for d, v in pts if BASELINE_START <= d <= BASELINE_END]
    expected = 84 if _is_quarterly(pts) else 252
    if len(window) < 0.75 * expected or len(window) < 2:
        return None
    return statistics.fmean(window), statistics.stdev(window)


def dashboard(live_by_key: dict[str, list[tuple[date, float]]] | None = None
              ) -> list[Reading]:
    """Every indicator, in slide order."""
    live_by_key = live_by_key or {}
    return [assess(ind, live_by_key.get(ind.key)) for ind in INDICATORS]


@dataclass(frozen=True)
class GroupScore:
    group: str
    z: float | None
    n: int                      # members contributing


def groups(readings: list[Reading]) -> list[GroupScore]:
    """Group score = mean of its scored members."""
    out: list[GroupScore] = []
    for g in GROUPS:
        zs = [r.z for r in readings if r.indicator.group == g and r.z is not None]
        out.append(GroupScore(g, statistics.fmean(zs) if zs else None, len(zs)))
    return out


def total(readings: list[Reading]) -> float | None:
    """Mean of every scored member -- equal weight per indicator, matching the
    slide (its Total, -0.006, is the mean of all 22 printed z-scores, not the
    mean of the five group means, which would be -0.014)."""
    zs = [r.z for r in readings if r.z is not None]
    return statistics.fmean(zs) if zs else None


# --------------------------------------------------------------------------
# History -- the Momentum Signal chart
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MomentumPoint:
    month: date
    by_group: dict[str, float]          # group -> mean member z that quarter
    total: float
    contributions: dict[str, float]     # group -> its share of the Total


def momentum_history(hist_by_key: dict[str, list[tuple[date, float]]],
                     start: date) -> list[MomentumPoint]:
    """Group z-scores at each quarter-end from `start` (plus the latest print),
    for the stacked bars.

    Built only from indicators with a wired (or pasted) history; a seed-only
    indicator sits the chart out. Each indicator's transform is computed once
    over its whole history and the value used at point *t* is its most recent
    transform point at or before *t*, so a quarterly series holds its level
    between prints. Scored against the same mean/sd as the table.

    The grid is quarter-aligned, but a final point is pinned to the freshest
    transform date across the wired set so the last bar reflects the same
    vintage as the table (the monthly labour rows would otherwise show a
    quarter stale). Each group contributes its members' mean z weighted by its
    share of the reporting count, so the bars stack up to the Total line.
    """
    prepared: list[tuple[Indicator, list[tuple[date, float]], float, float]] = []
    for ind in INDICATORS:
        pts = transform(ind.transform, hist_by_key.get(ind.key) or [])
        if pts:
            mean, sd = ((own_baseline(pts) if ind.own_baseline else None)
                        or (ind.seed_mean, ind.seed_sd))
            prepared.append((ind, pts, mean, sd))
    if not prepared:
        return []

    last_month = max(pts[-1][0] for _, pts, _, _ in prepared)
    months: list[date] = []
    m = date(start.year, (start.month - 1) // 3 * 3 + 1, 1)
    while m <= last_month:
        months.append(m)
        m = _months_before(m, -3)
    if months and months[-1] < last_month:
        months.append(last_month)

    out: list[MomentumPoint] = []
    for mth in months:
        per_group: dict[str, list[float]] = {g: [] for g in GROUPS}
        for ind, pts, mean, sd in prepared:
            val = None
            for d, v in pts:
                if d <= mth:
                    val = v
                else:
                    break
            z = _z_of(val, mean, sd, ind.bad_news)
            if z is not None:
                per_group[ind.group].append(z)
        n_members = sum(len(v) for v in per_group.values())
        gvals = {g: statistics.fmean(v) for g, v in per_group.items() if v}
        if len(gvals) < 3 or not n_members:
            continue
        tot = statistics.fmean(z for zs in per_group.values() for z in zs)
        contrib = {g: statistics.fmean(per_group[g]) * len(per_group[g]) / n_members
                   for g in gvals}
        out.append(MomentumPoint(mth, gvals, tot, contrib))
    return out
