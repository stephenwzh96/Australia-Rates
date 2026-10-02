"""Every external data source the tabs read, behind one Streamlit cache each.

The US version keeps its fetches here so a tab never talks to a network module
directly and the Data tab has one place to report on (and clear) everything.
Same rule here: tabs call these, nothing in `ui/tabs/` imports `data.*` for a
fetch. Every function degrades to empty on failure -- a dead source must never
block the Verdict.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

from core import clock, contracts, econ_calendar, release_prep, release_research
from core import employment as empmod
from core import inflation as infl
from core import macro as macromod
from data import abs as absmod
from data import abs_sdmx
from data import agent as claude_agent
from data import wb_agent
from data import asx, rba
from data import cache as datacache
from data.common import Observation

ROOT = Path(__file__).resolve().parent.parent

# The composition buckets and the cyclical split are analytical judgements, not
# published series, so they live beside the roster and the calendar as data a
# user can edit rather than as constants in the code.
CLASSIFICATION_PATH = ROOT / "meetings" / "_cpi_classification.json"

# Series with no public feed, held as a manual series (the NAB capacity
# utilisation print, digitised off the bench; others join the same file).
# Reference data, not a per-meeting judgement: one paste serves every meeting.
MANUAL_SERIES_PATH = ROOT / "meetings" / "_manual_series.json"
NAB_CAPACITY = "NAB capacity utilisation"

# The navy line on the capacity-vs-unemployment chart is the trailing 3-month
# mean of the raw prints (NAB's own convention; it pins the Jun 2020 trough at
# 74.1-74.2 exactly), derived in-app so it extends as each print lands.
CAPACITY_MA_WINDOW = 3


# ------------------------------------------------------------------ research agent

#: The research panels (release consensus, speech roundup, street calls, trade
#: recs) run on either agent CLI, exactly as the US version offers them.
RESEARCH_AGENTS = {"claude": claude_agent, "workbuddy": wb_agent}
RESEARCH_AGENT_LABELS = {"claude": "Claude SDK", "workbuddy": "WorkBuddy SDK"}
RESEARCH_AGENT_MODELS = {"claude": "opus", "workbuddy": "hy4-preview"}
RESEARCH_AGENT_MODEL_CHOICES = {
    "claude": ["opus", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5"],
    "workbuddy": ["hy4-preview", "balanced-model", "custom-local:deepseek-v4-flash",
                  "custom-local:deepseek-v4-pro", "hy3", "kimi-k3"],
}
DEFAULT_RESEARCH_BACKEND = "workbuddy"
RELEASE_RESEARCH_TIMEOUT = 480


# ------------------------------------------------------------------ market

@st.cache_resource(show_spinner=False)
def fetch_clock() -> dict[str, datetime]:
    """Wall-clock time of the last real fetch, process-wide like the cache."""
    return {}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_ib():
    fetch_clock()["ib"] = clock.now()
    return asx.fetch_strip_cached("ib")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_ir():
    fetch_clock()["ir"] = clock.now()
    return asx.fetch_strip_cached("ir")


def refresh_curve_quotes() -> None:
    fetch_ib.clear()
    fetch_ir.clear()
    for k in [k for k in st.session_state if str(k).startswith("curve_spot_")]:
        st.session_state.pop(k, None)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_rba_table():
    return rba.table_cached("f1")


@st.cache_data(ttl=3600, show_spinner=False)
def rba_spot() -> dict[str, float | None]:
    """The handful of live RBA readings the rest of the app anchors on."""
    t = fetch_rba_table()

    def last(code):
        obs = t.get(code, [])
        return obs[-1].value if obs else None

    cash, bbsw = last(rba.CASH_TARGET_CODE), last(rba.BBSW_CODE)
    return {
        "cash": cash,
        "bbsw": bbsw,
        "aonia": last(rba.CASH_TRADED_CODE),
        "spot_basis_bp": None if (cash is None or bbsw is None) else (bbsw - cash) * 100.0,
        "gap_bp": rba.cash_rate_gap_bp(),
    }


@st.cache_data(ttl=3600, show_spinner=False)
def policy_changes() -> set:
    """Real dated cash rate moves, kept out of the volatility quiet bucket."""
    return rba.policy_change_dates()


@st.cache_data(ttl=3600, show_spinner=False)
def bbsw_history() -> list[tuple[date, float]]:
    """Daily 3-month BBSW, the volatility anchor.

    The ASX price feed has no history, so contract-price volatility -- what the
    US version calibrates on -- does not exist here. BBSW is a rate and a bill
    future is `100 - yield`, so a bp of this series is a bp of contract price.
    It spans about 1.5 meetings, so it reads as the short end's daily noise
    rather than one meeting's; type an override when that matters."""
    return [(o.date, o.value) for o in fetch_rba_table().get(rba.BBSW_CODE, [])]


@st.cache_data(ttl=3600, show_spinner=False)
def cash_rate_history() -> list[tuple[date, float]]:
    """Daily AONIA -- what an IB contract settles on, averaged over its month."""
    return [(o.date, o.value) for o in fetch_rba_table().get(rba.CASH_TRADED_CODE, [])]


@st.cache_data(ttl=3600, show_spinner=False)
def cash_target_history() -> list[tuple[date, float]]:
    """Daily cash rate target -- the policy rate a decision sets."""
    return [(o.date, o.value) for o in fetch_rba_table().get(rba.CASH_TARGET_CODE, [])]


def kill_series_values() -> tuple[dict, dict]:
    """Live values for the auto-wired strip-health checks, plus their units."""
    t = fetch_rba_table()
    spot = rba_spot()
    values: dict[str, float | None] = {}
    units = {"BBSW_CASH_SPREAD": "bp", "CASH_GAP_ABS": "bp", "BBSW_14D_MOVE": "bp"}
    values["BBSW_CASH_SPREAD"] = spot["spot_basis_bp"]
    values["CASH_GAP_ABS"] = None if spot["gap_bp"] is None else abs(spot["gap_bp"])
    obs = t.get(rba.BBSW_CODE, [])
    if len(obs) > 1:
        cutoff = obs[-1].date - timedelta(days=14)
        window = [o for o in obs if o.date >= cutoff]
        values["BBSW_14D_MOVE"] = (abs(window[-1].value - window[0].value) * 100.0
                                   if len(window) > 1 else None)
    else:
        values["BBSW_14D_MOVE"] = None
    return values, units


@st.cache_data(ttl=3600, show_spinner=False)
def kill_series_history() -> dict[str, list]:
    """Recent history for the strip-health checks, for the Data-tab sparklines."""
    t = fetch_rba_table()
    bb = {o.date: o.value for o in t.get(rba.BBSW_CODE, [])}
    tgt = {o.date: o.value for o in t.get(rba.CASH_TARGET_CODE, [])}
    trd = {o.date: o.value for o in t.get(rba.CASH_TRADED_CODE, [])}
    days = sorted(set(bb) & set(tgt))[-180:]
    out: dict[str, list] = {
        "BBSW_CASH_SPREAD": [Observation(d, (bb[d] - tgt[d]) * 100.0) for d in days],
        "CASH_GAP_ABS": [Observation(d, abs(trd[d] - tgt[d]) * 100.0)
                         for d in days if d in trd],
    }
    moves = []
    for i, d in enumerate(days):
        prior = [x for x in days[:i + 1] if (d - x).days <= 14]
        if prior:
            moves.append(Observation(d, abs(bb[d] - bb[prior[0]]) * 100.0))
    out["BBSW_14D_MOVE"] = moves
    return out


# ------------------------------------------------------------------ labour

@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS labour workbooks…")
def abs_series() -> dict[str, list]:
    """Every labour series the full-employment panel scores, in one call."""
    wb = absmod.labour_force_rates()
    dur = absmod.duration_counts()
    h5 = rba.table_cached("h5")

    def col(w, header, stype="Seasonally Adjusted"):
        return [(o.date, o.value) for o in w.find(header, stype)]

    def h(code):
        return [(o.date, o.value) for o in h5.get(code, [])]

    une = col(wb, absmod.UNEMPLOYMENT)
    und = col(wb, absmod.UNDEREMPLOYMENT)
    buckets = [col(dur, b, "Original") for b in absmod.MEDIUM_TERM_BUCKETS]
    return {
        "unemployment": une,
        "underemployment": und,
        "underutilisation": empmod.derive_underutilisation(une, und),
        "medium_term": empmod.derive_medium_term(buckets, h("GLFSLFSA")),
        "youth": col(wb, absmod.YOUTH_UNEMPLOYMENT),
        # Vacancies are quarterly and unemployment monthly, so the current
        # reading pairs the latest of each rather than the latest shared month.
        "vacancies_to_unemployment": empmod.derive_ratio(
            h("GLFOSVT"), h("GLFSUPSA"), pair_latest=True),
        "_periods": [absmod.latest_release(absmod.LF_LANDING) or "",
                     absmod.latest_release(absmod.LFD_LANDING) or ""],
    }


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def rba_table(name: str) -> dict[str, list[tuple[date, float]]]:
    """A whole RBA statistical table as {series id: [(date, value)]}."""
    try:
        t = rba.table_cached(name)
    except Exception:
        return {}
    return {k: [(o.date, o.value) for o in v] for k, v in t.items()}


def _lead_forward(series: list[tuple[date, float]], quarters: int = 1
                  ) -> list[tuple[date, float]]:
    """Move every observation's date forward by `quarters` quarters, values
    untouched, so a flow reading lines up with the wage print it precedes."""
    out = []
    for d, v in series:
        y = d.year + (d.month - 1 + quarters * 3) // 12
        m = (d.month - 1 + quarters * 3) % 12 + 1
        out.append((date(y, m, 1), v))
    return out


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS gross flows (100MB, once a month)…")
def gross_flows() -> dict[str, list[tuple[date, float]]]:
    """National gross flows between labour-force states, monthly, '000s."""
    try:
        return {k: [(o.date, o.value) for o in v] for k, v in absmod.gross_flows().items()}
    except Exception:
        return {}


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS gross flows (100MB, once a month)…")
def flow_series() -> dict:
    """The three flow measures the job-flows chart plots, as z-scores, quarterly."""
    pairs = lambda obs: [(o.date, o.value) for o in obs]
    flows = gross_flows()
    tenure = absmod.job_tenure()
    finding = empmod.to_quarterly(empmod.job_finding_rate(flows))
    switching = empmod.to_quarterly(empmod.job_switching_rate(
        pairs(tenure.find(absmod.TENURE_UNDER_12M, "Original")),
        pairs(tenure.find(absmod.TENURE_OVER_12M, "Original"))))
    wages = empmod.to_quarterly(pairs(rba.table_cached("h4").get("GWPIQP", [])))
    return {
        "finding": finding, "switching": switching, "wages": wages,
        "z_finding": _lead_forward(empmod.zscores(finding), empmod.FLOW_LEAD_QUARTERS),
        "z_switching": _lead_forward(empmod.zscores(switching), empmod.FLOW_LEAD_QUARTERS),
        "z_wages": empmod.zscores(wages),
        "period": absmod.latest_release(absmod.LF_LANDING) or "",
    }


def _month(d: date) -> date:
    """First of the reference month: RBA tables date a month or a quarter on its
    last day, the ABS workbooks on its first."""
    return date(d.year, d.month, 1)


def _by_month(series) -> list[tuple[date, float]]:
    return sorted((_month(d), v) for d, v in series or [])


_E = ("Employed full-time", "Employed part-time")
_U, _N = "Unemployed", "Not in the labour force (NILF)"
_MATCHED = _E + (_U, _N)


def _flow_sum(flows: dict, origins, destinations) -> dict[date, float]:
    out: dict[date, float] = {}
    for o in origins:
        for d in destinations:
            for when, v in flows.get(f"{o}>{d}", []):
                out[when] = out.get(when, 0.0) + v
    return out


def _deseasonalise(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Stable additive seasonals out of a monthly series: each calendar month's
    average gap to the centred 12-month mean is subtracted. The ABS publishes
    the gross flows unadjusted, and a percentile rank of a seasonal series would
    mostly rank the season."""
    vals = [v for _, v in series]
    if len(vals) < 36:
        return series
    gaps: dict[int, list[float]] = {}
    for i in range(6, len(vals) - 6):
        centred = (sum(vals[i - 6:i + 6]) + sum(vals[i - 5:i + 7])) / 24.0
        gaps.setdefault(series[i][0].month, []).append(vals[i] - centred)
    factor = {m: sum(g) / len(g) for m, g in gaps.items()}
    mean = sum(factor.values()) / len(factor) if factor else 0.0
    return [(d, v - (factor.get(d.month, mean) - mean)) for d, v in series]


def _smooth3(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Trailing 3-month mean, which the Labour Force's small matched sample needs
    before a flow rate is ranked month by month."""
    return [(series[i][0], sum(v for _, v in series[i - 2:i + 1]) / 3.0)
            for i in range(2, len(series))]


def flow_rates(flows: dict) -> dict[str, list[tuple[date, float]]]:
    """Matched-sample transition rates, % -- seasonally adjusted, 3-month mean.

    Each rate is a flow over the row total of its origin state among the
    MATCHED destinations (employed, unemployed, NILF): the outgoing rotation
    and the unmatched are not destinations a person moved to. The two entry
    flows are scaled by the matched labour force instead -- the pool joined --
    because the NILF pool carries an ageing trend of its own."""
    if not flows:
        return {}
    to_any = {s: _flow_sum(flows, (s,), _MATCHED) for s in _MATCHED}
    e_total = {d: to_any[_E[0]].get(d, 0.0) + to_any[_E[1]].get(d, 0.0)
               for d in to_any[_E[0]]}
    lf_now = _flow_sum(flows, _MATCHED, _E + (_U,))

    def rate(num: dict, den: dict) -> list[tuple[date, float]]:
        raw = [(d, 100.0 * num[d] / den[d]) for d in sorted(num) if den.get(d)]
        return _smooth3(_deseasonalise(raw))

    return {
        "F_UE": rate(_flow_sum(flows, (_U,), _E), to_any[_U]),
        "F_EU": rate(_flow_sum(flows, _E, (_U,)), e_total),
        "F_UN": rate(_flow_sum(flows, (_U,), (_N,)), to_any[_U]),
        "F_NU": rate(_flow_sum(flows, (_N,), (_U,)), lf_now),
        "F_NE": rate(_flow_sum(flows, (_N,), _E), lf_now),
    }


def _quarter_fill(series: list[tuple[date, float]], months: int = 2) -> list[tuple[date, float]]:
    """A quarterly reading carried across the two months after it, until the
    next one: a monthly tally reads a quarterly survey as standing for its
    quarter, not as three months of which two did not report."""
    out: list[tuple[date, float]] = []
    for i, (d, v) in enumerate(series):
        nxt = series[i + 1][0] if i + 1 < len(series) else None
        out.append((d, v))
        for k in range(1, months + 1):
            y, m = divmod(d.year * 12 + d.month - 1 + k, 12)
            fill = date(y, m + 1, 1)
            if nxt is not None and fill >= nxt:
                break
            out.append((fill, v))
    return out


#: The quarterly rows of the labour dashboard.
QUARTERLY_LABOUR = ("VAC", "VAC_LF", "WPI", "SWITCH")


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling the labour dashboard series…")
def fetch_labor_series() -> dict[str, list[tuple[date, float]]]:
    """Every series `core.labor` ranks, keyed as its indicators name them, each
    dated on the first of its reference month. Missing sources leave their keys
    out; the dashboard ranks whatever loaded."""
    h5, h3, h4 = rba_table("h5"), rba_table("h3"), rba_table("h4")
    out: dict[str, list[tuple[date, float]]] = {
        "UR": _by_month(h5.get("GLFSURSA")),
        "PR": _by_month(h5.get("GLFSPRSA")),
        "EPOP": _by_month(h5.get("GLFSEPTPOP")),
        "EMP": _by_month(h5.get("GLFSEPTSA")),
        "FT": _by_month(h5.get("GLFSEPFSA")),
        "HOURS": _by_month(h5.get("GLFMHW")),
        "UNEMP": _by_month(h5.get("GLFSUPSA")),
        "LF": _by_month(h5.get("GLFSLFSA")),
        "VAC": _by_month(h5.get("GLFOSVT")),
        "VAC_LF": _by_month(h5.get("GLFOSVTLF")),
        "NAB_COND": _by_month(h3.get("GICNBC")),
        "SENT": _by_month(h3.get("GICWMICS")),
        "WPI": _by_month(h4.get("GWPIYP")),
        "NAB_CAP": _by_month(manual_series(NAB_CAPACITY)),
    }
    try:
        lab = abs_series()
        out["UND"] = _by_month(lab.get("underemployment"))
        out["UTIL"] = _by_month(lab.get("underutilisation"))
        out["YOUTH"] = _by_month(lab.get("youth"))
        out["MTU"] = _by_month(lab.get("medium_term"))
    except Exception:
        pass
    try:
        fl = flow_series()
        out["SWITCH"] = _by_month(fl.get("switching"))
    except Exception:
        pass
    out.update(flow_rates(gross_flows()))
    # Filled no further than the newest Labour Force month: a quarterly reading
    # stands for its quarter, never for months no survey has reached.
    cap = out["UR"][-1][0] if out.get("UR") else None
    for k in QUARTERLY_LABOUR:
        if out.get(k):
            out[k] = [(d, v) for d, v in _quarter_fill(out[k]) if cap is None or d <= cap
                      or d in {x for x, _ in out[k]}]
    return {k: v for k, v in out.items() if v}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_jobs_panel() -> dict[str, list[tuple[date, float]]]:
    """Filled jobs by ANZSIC division and in total, quarterly, seasonally
    adjusted (ABS Labour Account, item M12)."""
    raw = abs_sdmx.panel("LABOUR_ACCT_Q,1.1", "M12.AUS..20.Q", ("LABOURACCT_IND",),
                         start="2000")
    return {k: [(o.date, o.value) for o in v] for k, v in raw.items()
            if len(k) == 1 or k == "TOTAL"}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_wpi_panel() -> dict[str, list[tuple[date, float]]]:
    """Wage Price Index (total hourly rates excluding bonuses), year-ended %, by
    sector and industry, keyed "SECTOR|INDUSTRY" (7 all, 1 private, 2 public)."""
    raw = abs_sdmx.panel("WPI,1.2.0", "3.THRPEB...10.AUS.Q", ("SECTOR", "INDUSTRY"),
                         start="2012")
    return {k: [(o.date, o.value) for o in v] for k, v in raw.items()}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def au_recessions() -> list[tuple[date, float]]:
    """1.0 for each month inside a technical recession -- two or more
    consecutive quarters of falling real GDP (RBA H1) -- else 0.0, monthly.

    Australia has no NBER. The two-quarter rule is the convention the ABS
    commentary and the press use, and computing it from the published GDP
    keeps the shading reproducible rather than a list typed by hand: it marks
    1982-83, 1990-91 and 2020."""
    gdp = sorted((_month(d), v) for d, v in rba_table("h1").get("GGDPCVGDP", []))
    if len(gdp) < 3:
        return []
    falling = [(d, v < p) for (_, p), (d, v) in zip(gdp, gdp[1:])]
    marked: set[int] = set()
    run: list[int] = []
    for i, (_, down) in enumerate(falling + [(None, False)]):
        if down:
            run.append(i)
            continue
        if len(run) >= 2:
            marked.update(run)
        run = []
    out: list[tuple[date, float]] = []
    for i, (d, _) in enumerate(falling):
        for k in range(3):                     # the quarter's three months
            y, m = divmod(d.year * 12 + d.month - 1 - 2 + k, 12)
            out.append((date(y, m + 1, 1), 1.0 if i in marked else 0.0))
    return out


def flow_transition_rates(flows: dict) -> dict[str, list[tuple[date, float]]]:
    """Monthly transition rates by origin (fractions), matched sample, stable
    seasonals removed -- the inputs `core.labor_flows` multiplies by stocks."""
    if not flows:
        return {}
    to_any = {s: _flow_sum(flows, (s,), _MATCHED) for s in _MATCHED}
    e_total = {d: to_any[_E[0]].get(d, 0.0) + to_any[_E[1]].get(d, 0.0)
               for d in to_any[_E[0]]}

    def rate(num: dict, den: dict) -> list[tuple[date, float]]:
        return _deseasonalise([(d, num[d] / den[d]) for d in sorted(num) if den.get(d)])

    return {"UE": rate(_flow_sum(flows, (_U,), _E), to_any[_U]),
            "EU": rate(_flow_sum(flows, _E, (_U,)), e_total),
            "UN": rate(_flow_sum(flows, (_U,), (_N,)), to_any[_U]),
            "NU": rate(_flow_sum(flows, (_N,), (_U,)), to_any[_N])}


def decomposition_stocks() -> dict[str, list[tuple[date, float]]]:
    """Seasonally adjusted labour force, unemployed, employed and not in the
    labour force ('000, RBA H5), the last derived from the participation rate."""
    h5 = rba_table("h5")
    lf, pr = dict(_by_month(h5.get("GLFSLFSA"))), dict(_by_month(h5.get("GLFSPRSA")))
    return {"L": sorted(lf.items()), "U": _by_month(h5.get("GLFSUPSA")),
            "E": _by_month(h5.get("GLFSEPTSA")),
            "N": [(d, lf[d] * (100.0 / pr[d] - 1.0)) for d in sorted(lf) if pr.get(d)]}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_labour_grid() -> dict[str, list[tuple[date, float]]]:
    """The six labour-market charts' series: unemployment, participation and
    emp-pop (RBA H5); the jobs-workers gap; job loss and job switching; the
    monthly employment change and its 3-month mean; and the recession bands."""
    h5 = rba_table("h5")
    emp, lf, vac = (dict(_by_month(h5.get(c))) for c in ("GLFSEPTSA", "GLFSLFSA", "GLFOSVT"))
    gap = [(d, 100.0 * (emp[d] + vac[d] - lf[d]) / lf[d]) for d in sorted(vac)
           if d in emp and lf.get(d)]
    emp_s = sorted(emp.items())
    change = [(d, v - p) for (_, p), (d, v) in zip(emp_s, emp_s[1:])]
    ma3 = [(change[i][0], sum(v for _, v in change[i - 2:i + 1]) / 3.0)
           for i in range(2, len(change))]
    rates = flow_rates(gross_flows())
    out = {"UR": _by_month(h5.get("GLFSURSA")), "PR": _by_month(h5.get("GLFSPRSA")),
           "EPOP": _by_month(h5.get("GLFSEPTPOP")), "JWGAP": gap,
           "JOB_LOSS": rates.get("F_EU", []), "EMP_CHANGE": change, "EMP_CHANGE_3MA": ma3,
           "RECESSION": au_recessions()}
    try:
        out["SWITCHING"] = _by_month(flow_series().get("switching"))
    except Exception:
        out["SWITCHING"] = []
    return out


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling the Household Spending Indicator…")
def fetch_hsi() -> dict[str, list]:
    """ABS Household Spending Indicator, A$m, current price, seasonally
    adjusted, national, monthly -- the total and every category, as
    `data.common.Observation`s keyed by HSI category code."""
    raw = abs_sdmx.panel("HSI_M,1.6.0", "7..CUR.20.AUS.M", ("CATEGORY",), start="2018")
    return raw


def _observations(rows) -> list[Observation]:
    return [Observation(d, v) for d, v in rows or []]


def fetch_growth_inputs() -> tuple[list[Observation], dict[str, list[Observation]]]:
    """Monthly hours worked (RBA H5, first of the month) and RBA table H1."""
    hours = _observations(_by_month(rba_table("h5").get("GLFMHW")))
    h1 = {k: _observations(v) for k, v in rba_table("h1").items()}
    return hours, h1


def fetch_household_income() -> dict[str, list[Observation]]:
    """RBA table H2 -- real consumption and disposable income, saving ratio --
    with each quarter dated on its first month, as the growth chart steps."""
    from core.growth import quarter_start
    return {k: [Observation(quarter_start(d), v) for d, v in rows]
            for k, rows in rba_table("h2").items()}


def manual_series(name: str) -> list[tuple[date, float]]:
    """A manual series off disk, for the sources with no public feed."""
    try:
        with open(MANUAL_SERIES_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for pair in raw.get(name) or []:
        try:
            out.append((date.fromisoformat(pair[0]), float(pair[1])))
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(out)


def trailing_mean(series: list[tuple[date, float]], window: int) -> list[tuple[date, float]]:
    """Trailing (causal) moving average over `window` consecutive observations;
    the first `window - 1` points take a partial window."""
    if not series or window < 1:
        return []
    vals = [v for _, v in series]
    out = []
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        out.append((series[i][0], sum(vals[lo:i + 1]) / (i - lo + 1)))
    return out


def data_asof(data: dict) -> date:
    """Newest observation anywhere in a series panel."""
    dates = [s[-1][0] for k, s in data.items() if not k.startswith("_") and s]
    return max(dates) if dates else clock.today()


def capped(series: list[tuple[date, float]], as_of: date) -> list[tuple[date, float]]:
    """A series cut at the as-of: no reference period after it is shown."""
    return [(d, v) for d, v in series if d <= as_of]


# ------------------------------------------------------------------ inflation

@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS CPI workbooks…")
def cpi_series() -> dict:
    """Every CPI series the Inflation tab draws, resolved in one call.

    The hierarchy is resolved here too: the list of expenditure classes that the
    breadth count, the composition stack and the cycle split all count over, so
    all three measure the same basket."""
    idx, order = absmod.cpi_class_indexes()
    contrib, _ = absmod.cpi_contributions()
    sa = absmod.cpi_class_indexes_sa()

    pairs = lambda obs: [(o.date, o.value) for o in obs]
    panel = {k: pairs(v) for k, v in contrib.items()}
    tree = infl.build_tree(order, panel)

    at = infl.latest_common({k: v for k, v in panel.items()}) if panel else None
    flat = {k: dict(v)[at] for k, v in panel.items() if at and at in dict(v)}
    leaves = [n for n in tree.leaves if n in sa]
    classes = {n: pairs(sa[n]) for n in leaves}
    weights = infl.index_point_contributions(classes, flat, at) if at else {}

    qtr = absmod.cpi_quarterly_analytical()
    qtr_yr = absmod.cpi_quarterly_analytical(
        "Percentage Change from Corresponding Quarter of Previous Year")
    mth = absmod.cpi_monthly_analytical()

    return {
        "order": order,
        "children": tree.children,
        "leaves": leaves,
        "contribution": flat,
        "as_at": at,
        "residual": infl.reconciles(tree, flat) if flat else None,
        "classes": classes,
        "weights": weights,
        "n_classes_total": len(sa),
        "trimmed_q": pairs(qtr.get(absmod.TRIMMED_MEAN, [])),
        "trimmed_yr": pairs(qtr_yr.get(absmod.TRIMMED_MEAN, [])),
        "headline_q": pairs(qtr.get(absmod.ALL_GROUPS_SA, [])),
        "monthly_trimmed_yr": pairs(mth.get(absmod.TRIMMED_MEAN, [])),
        "monthly_ex_volatiles_yr": pairs(mth.get(absmod.EX_VOLATILES, [])),
        "period": absmod.latest_release(absmod.CPI_LANDING) or "",
    }


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling the monthly CPI…")
def fetch_cpi_monthly() -> dict[str, list[tuple[date, float]]]:
    """The complete monthly CPI (ABS data API `CPI` v2.0.0), keyed
    "measure|index|tsest": index levels, % changes and contributions for every
    item in the tree, original and (where the ABS adjusts) seasonally adjusted."""
    raw = abs_sdmx.panel("CPI,2.0.0", "1+2+3+6+7...50.M", ("MEASURE", "INDEX", "TSEST"),
                         start="2024-01")
    return {k: [(o.date, o.value) for o in v] for k, v in raw.items()}


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cpi_tree():
    """The CPI expenditure tree, from the ABS's own codelist."""
    from core import cpi_monthly
    return cpi_monthly.tree(abs_sdmx.codelist("CPI", "2.0.0", "CL_CPI_INDEX"))


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cpi_weights() -> dict[str, float]:
    """Each CPI item's basket weight (%), the latest annual reweight."""
    raw = abs_sdmx.panel("CPI_WEIGHTS,1.0.0", "1..50.Q", ("INDEX",), start="2020")
    return {k: v[-1].value for k, v in raw.items() if v}


def cpi_monthly_vintage() -> date | None:
    """The newest month the monthly CPI prints, as a vintage stamp."""
    panel = fetch_cpi_monthly()
    rows = panel.get("1|10001|10") or []
    return rows[-1][0] if rows else None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cpi_classification() -> tuple[list, list]:
    """The two editable analytical splits, off disk."""
    return infl.load_classification(str(CLASSIFICATION_PATH))


# ------------------------------------------------------------------ macro

@st.cache_data(ttl=6 * 3600, show_spinner="Pulling macro indicators…")
def fetch_macro_bundle() -> dict[str, list[tuple[date, float]]]:
    """Raw history for every Macro-tab indicator that has a wired feed, keyed by
    the `core.macro` indicator key. Three keyless sources -- the ABS SDMX data
    API, RBA tables and the labour/CPI fetches above. Never raises."""
    out: dict[str, list[tuple[date, float]]] = {}

    def put(key, rows):
        rows = [(d, v) for d, v in (rows or []) if v is not None]
        if rows:
            out[key] = sorted(rows)

    try:
        lab = abs_series()
        put("unemployment", lab.get("unemployment"))
        put("underemployment", lab.get("underemployment"))
    except Exception:
        pass
    try:
        put("cpi_trimmed_mean", cpi_series().get("trimmed_q"))
    except Exception:
        pass
    put("job_vacancies", rba_table("h5").get("GLFOSVT"))
    put("wpi", rba_table("h4").get("GWPIQP"))
    # RBA D1's DGFAC* credit-growth series froze at the June 2019 EFS
    # changeover; Private Credit stays on the slide reading until a current
    # credit series is wired.
    for k in abs_sdmx.wired_keys():
        try:
            put(k, abs_sdmx.series_pairs(k))
        except Exception:
            continue

    # A pasted history for any indicator wins outright, keyed "macro:<key>".
    try:
        with open(MANUAL_SERIES_PATH, encoding="utf-8") as fh:
            raw_manual = json.load(fh)
    except (OSError, ValueError):
        raw_manual = {}
    for ind in macromod.INDICATORS:
        pasted = []
        for pair in raw_manual.get(f"macro:{ind.key}") or []:
            try:
                pasted.append((date.fromisoformat(pair[0]), float(pair[1])))
            except (ValueError, TypeError, IndexError):
                continue
        if pasted:
            out[ind.key] = sorted(pasted)
    return out


# ------------------------------------------------------------------ calendar

def next_release(release: str, today: date, horizon_days: int = 200) -> date | None:
    """The next rule-dated print of a release on or after today."""
    return next((e.when for e in econ_calendar.recurring_events(
        today, today + timedelta(days=horizon_days)) if e.release == release), None)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=64)
def fetch_release_research(release: str, when: date, series: tuple[str, ...],
                           backend: str, model: str,
                           timeout: int = RELEASE_RESEARCH_TIMEOUT, retry: int = 0):
    """One consensus search, cached on the exact question (billable, slow)."""
    specs = [release_prep.SPECS[k] for k in series if k in release_prep.SPECS]
    if not specs:
        return release_research.Research(
            release, when, error="Nothing in this report has a spec to price.")
    raw, err = RESEARCH_AGENTS[backend].research(
        release_research.prompt(release, when, specs),
        release_research.SYSTEM, model=model or None, timeout=timeout)
    if err:
        return release_research.Research(release, when, error=err)
    return release_research.parse(raw, release, when, specs)


def playbook_market_level(ctx) -> float | None:
    """The strip's priced level for this meeting in bp, or None when it is not a
    clean read: the step the Curve tab shows, measured from the same spot
    (a typed override included), or the cumulative move through the meeting
    when the sized contract carries several."""
    try:
        sp, M = ctx.path, ctx.M
        if sp is None or not sp.ok:
            return None
        step = sp.step_for(M.meeting.key)
        if step is None or not step.reliable:
            return None
        return float(step.cum_bp if M.multi_move else step.step_bp)
    except Exception:                                           # noqa: BLE001
        return None


def playbook_anchor_price(ctx, market_level_bp: float | None = None) -> float | None:
    """The following-month IB price consistent with the priced level in play:
    the contract wholly at the new rate, shifted by the gap between the strip's
    own level and the one the card is repriced from."""
    try:
        end = ctx.M.meeting.end
        far = contracts.add_month(date(end.year, end.month, 1))
        price = next((float(q.price) for q in ctx.market.ib_quotes
                      if q.ok and q.month == far), None)
        if price is None:
            return None
        strip = playbook_market_level(ctx)
        if market_level_bp is not None and strip is not None:
            return price + (strip - market_level_bp) / 100.0
        return price
    except Exception:                                           # noqa: BLE001
        return None


# ------------------------------------------------------------------ refresh

def refresh_all_sources() -> None:
    """Both layers, or nothing actually refetches: the Streamlit memo in front,
    the disk cache and the in-process table memo behind it."""
    for fn in (fetch_ib, fetch_ir, fetch_rba_table, rba_spot, bbsw_history,
               cash_rate_history, cash_target_history, kill_series_history,
               policy_changes, abs_series, rba_table, gross_flows, cpi_series,
               cpi_classification, flow_series, fetch_macro_bundle):
        fn.clear()
    for fn in (fetch_release_research,):
        fn.clear()
    for extra in _EXTRA_CLEARS:
        extra()
    rba._table_memo.cache_clear()
    absmod.latest_release.cache_clear()
    datacache.clear()


#: Cached fetches registered by later modules (growth, SMP, labour panels...),
#: so one refresh clears them too without this module importing each of them.
_EXTRA_CLEARS: list = []


def register_refresh(fn) -> None:
    if fn not in _EXTRA_CLEARS:
        _EXTRA_CLEARS.append(fn)
