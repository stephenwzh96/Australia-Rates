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

from core import clock, econ_calendar
from core import employment as empmod
from core import inflation as infl
from core import macro as macromod
from data import abs as absmod
from data import abs_sdmx
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


# ------------------------------------------------------------------ refresh

def refresh_all_sources() -> None:
    """Both layers, or nothing actually refetches: the Streamlit memo in front,
    the disk cache and the in-process table memo behind it."""
    for fn in (fetch_ib, fetch_ir, fetch_rba_table, rba_spot, bbsw_history,
               cash_rate_history, cash_target_history, kill_series_history,
               policy_changes, abs_series, rba_table, gross_flows, cpi_series,
               cpi_classification, flow_series, fetch_macro_bundle):
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
