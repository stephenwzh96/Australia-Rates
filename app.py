"""RBA event-pricing dashboard.

An Australian rerun of the event-pricing framework: is the market's implied
probability of an RBA move wrong enough to trade? Left column carries the
working; the right rail carries the Verdict, always visible whichever section
you are on.

Every derived number carries a `?` control holding the formula, a plain-English
gloss, and the substitution using the CURRENT inputs. That substitution is the
audit trail: it is what makes a points-vs-probability slip visible on screen
instead of three steps downstream.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from core import bbsw as bbswmod
from core import employment as empmod
from core import contracts, econ_calendar, model, montecarlo, pricing, rba_calendar
from core import inflation as infl
from core import sizing as sizingmod
from core import strip as stripmod
from data import abs as absmod
from data import asx, rba
from data import cache as datacache
from data.common import Observation
from export import report
from state import store
from ui import charts, components as C
from ui.theme import active as active_palette, pnl_colour

st.set_page_config(page_title="RBA event pricing", page_icon="◷", layout="wide")
C.inject_css()

P = active_palette()
TODAY = date.today()

# The composition buckets and the cyclical split are analytical judgements, not
# published series, so they live beside the roster and the calendar as data a
# user can edit rather than as constants in the code.
CLASSIFICATION_PATH = ROOT / "meetings" / "_cpi_classification.json"

# Series with no public feed, held here as a manual series (the NAB capacity
# utilisation print, digitised off the bench; others could join the same file).
# Beside the classification rather than inside a meeting's saved state: this is
# reference data, not a per-meeting judgement, and one paste should serve every
# meeting rather than being cloned forward with each one.
MANUAL_SERIES_PATH = ROOT / "meetings" / "_manual_series.json"
NAB_CAPACITY = "NAB capacity utilisation"

# The navy line on the bench's capacity-vs-unemployment chart (bench.jpg) is a
# moving average of the grey raw-print line. Digitising the bench and fitting
# the COVID trough pins it down: the navy low is 74.1-74.2 in Jun 2020, exactly
# the trailing 3-month mean of the official prints (Mar 75.1, Apr 72.0, May
# rebound) — a 2-month mean would trough lower (73.3), anything longer lags.
# NAB's own convention is a 3-month trailing average, and a trailing (causal)
# average extends automatically as each new monthly print lands, so it is
# derived in-app, not stored.
CAPACITY_MA_WINDOW = 3


# ------------------------------------------------------------------ data

@st.cache_data(ttl=300, show_spinner=False)
def fetch_ib():
    return asx.fetch_strip_cached("ib")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_ir():
    return asx.fetch_strip_cached("ir")


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
    """Real dated cash rate moves, to keep them out of the volatility quiet
    bucket -- see `data.rba.policy_change_dates`."""
    return rba.policy_change_dates()


@st.cache_data(ttl=3600, show_spinner=False)
def bbsw_history() -> list[tuple[date, float]]:
    """Daily 3-month BBSW, the volatility anchor.

    The ASX price feed has no history at all, so contract-price volatility --
    what the US version calibrates on -- simply does not exist here. BBSW is
    quoted as a rate and a bank bill future as `100 - yield`, so a basis point
    of this series is a basis point of contract price: the right UNITS, and no
    capture rescale is applied to it for that reason.

    It is still a proxy, and the honest caveat is tenor rather than credit. An
    earlier version of this note claimed the BBSW/cash spread showed credit
    noise swamping the signal; that was an artifact of differencing against a
    step function, and excluding the handful of days the RBA actually moved the
    two series measure the same 2.1bp/day. What BBSW genuinely does is span
    about 1.5 meetings, so it reads as the short end's daily noise rather than
    this one meeting's. Type an override when that distinction matters.
    """
    return [(o.date, o.value) for o in fetch_rba_table().get(rba.BBSW_CODE, [])]


@st.cache_data(ttl=3600, show_spinner=False)
def kill_series_history() -> dict[str, list]:
    """Recent history for each auto-wired kill criterion, for its sparkline.

    Derived series, not raw ones: two of the three criteria are spreads or
    rolling moves that exist nowhere in table F1 as a column, so they are
    computed here on the same definitions `kill_series_values` uses. Keeping
    both in this module is what stops the chart and the trigger disagreeing.
    """
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


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS labour workbooks…")
def abs_series() -> dict[str, list]:
    """Every labour series the full-employment panel scores, in one call.

    Cached hard: the ABS ships these as multi-megabyte Excel workbooks, and
    `data.abs` keys its disk cache on the release period so a month that has
    already been downloaded is never fetched again.
    """
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


def _lead_forward(series: list[tuple[date, float]], quarters: int = 1
                 ) -> list[tuple[date, float]]:
    """Move every observation's date forward by `quarters` quarters; values
    untouched. Plots the flow measures one quarter AHEAD of their true month so
    each flow reading lines up with the wage print it precedes (flows lead pay
    by a quarter). Unlike empmod.lead this shifts the TIME axis, not the values,
    so each point keeps its own z-score against history."""
    out = []
    for d, v in series:
        y = d.year + (d.month - 1 + quarters * 3) // 12
        m = (d.month - 1 + quarters * 3) % 12 + 1
        out.append((date(y, m, 1), v))
    return out


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS gross flows (100MB, once a month)…")
def flow_series() -> dict:
    """The three flow measures the second Labour chart plots, as z-scores.

    Quarterly throughout: job-switching and wages are only published quarterly,
    and averaging the monthly job-finding rate into quarters beats sampling one
    month of a survey and discarding the other two.
    """
    pairs = lambda obs: [(o.date, o.value) for o in obs]
    flows = {k: pairs(v) for k, v in absmod.gross_flows().items()}
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
    rows = raw.get(name) or []
    out = []
    for pair in rows:
        try:
            out.append((date.fromisoformat(pair[0]), float(pair[1])))
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(out)


def trailing_mean(series: list[tuple[date, float]],
                  window: int) -> list[tuple[date, float]]:
    """Trailing (causal) moving average over `window` consecutive monthly obs.

    The first `window - 1` points get a partial window so the line starts at
    the first observation rather than leaving a gap. Used for the capacity MA
    line, which must extend live as the monthly release arrives — so it is
    derived here from the stored grey series rather than saved as its own series.
    """
    if not series or window < 1:
        return []
    vals = [v for _, v in series]
    out = []
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        out.append((series[i][0], sum(vals[lo:i + 1]) / (i - lo + 1)))
    return out


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling ABS CPI workbooks…")
def cpi_series() -> dict:
    """Every CPI series the Inflation tab draws, resolved in one call.

    All the ingestion lives here rather than in the section that renders, for
    the same reason the labour workbooks do: three multi-megabyte Excel files
    behind one `@st.cache_data`, so a rerun costs nothing and the Data tab has
    a single place to report what was fetched and a single thing to clear.

    The hierarchy is resolved here too. It is not a chart -- it is the list of
    87 expenditure classes that four of the five charts count over, and
    deriving it once means the breadth count, the composition stack and the
    cycle split are all measuring the same basket.
    """
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


def kill_series_values() -> tuple[dict, dict]:
    """Live values for the auto-wired kill criteria, plus their units."""
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
        values["BBSW_14D_MOVE"] = (
            abs(window[-1].value - window[0].value) * 100.0 if len(window) > 1 else None)
    else:
        values["BBSW_14D_MOVE"] = None
    return values, units


# ------------------------------------------------------------------ state

def calendar_meetings() -> list[rba_calendar.Meeting]:
    saved = store.load_calendar()
    if saved:
        return [rba_calendar.meeting_from_dict(m) for m in saved]
    return list(rba_calendar.DEFAULT_MEETINGS)


MEETINGS = calendar_meetings()


# The history-window selector, shared by the Labour and Inflation tabs so the
# two navigate identically. Same options and same default as the FOMC version:
# one control, one vocabulary, wherever a chart shows recent history.
HISTORY_WINDOWS = ("1Y", "2Y", "3Y", "5Y", "10Y", "All")

# The Labour panel compares each indicator against an earlier reading. Offered
# as fixed lookbacks rather than a bare date box for the same reason the window
# above is: a named span is a decision a reader can repeat, and the published
# version of this panel compares against the prior year end.
COMPARE_WINDOWS = ("3M", "6M", "1Y", "2Y", "Prior year end")

# Release families, so the economic calendar groups its rows by what kind of
# news they are rather than listing them flat. Keyed on `Event.label`, which is
# what the calendar table prints; the manual rows a user types on the Data tab
# are free text with no fixed vocabulary and so get no family -- the "confirmed"
# tag in the Source column already marks them out. Same four-colour language as
# the FOMC version, with the Fed's slot given over to the RBA's own calendar.
CALENDAR_FAMILY = {
    "Labour Force (employment, unemployment rate)": "labour",
    "Wage Price Index": "labour",
    "CPI, quarterly (headline and trimmed mean)": "inflation",
    "Monthly CPI indicator": "inflation",
    "National Accounts (GDP)": "activity",
}
CALENDAR_FAMILY_TINT = {
    "labour": "rgba(59,130,246,0.11)",
    "inflation": "rgba(217,119,6,0.12)",
    "activity": "rgba(13,148,136,0.11)",
    "rba": "rgba(139,92,246,0.12)",
}
CALENDAR_FAMILY_TINT_SOLID = {
    "labour": "#3b82f6", "inflation": "#d97706",
    "activity": "#0d9488", "rba": "#8b5cf6",
}
CALENDAR_FAMILY_NAME = {
    "labour": "Labour market", "inflation": "Inflation",
    "activity": "Activity", "rba": "RBA calendar",
}

# RELEASE (the report) -> family, so the Monte Carlo path markers reuse the four
# colours the calendar table already established rather than inventing a second
# colour language for the same events. Keyed on `Event.release`, which is what
# the volatility multipliers and the chart both group by.
RELEASE_FAMILY = {
    econ_calendar.LABOUR: "labour",
    econ_calendar.WPI: "labour",
    econ_calendar.CPI_Q: "inflation",
    econ_calendar.CPI_M: "inflation",
    econ_calendar.GDP: "activity",
    econ_calendar.DECISION: "rba",
    econ_calendar.SMP: "rba",
    econ_calendar.MINUTES: "rba",
}
RELEASE_FAMILY_COLOUR = {name: CALENDAR_FAMILY_TINT_SOLID[fam]
                         for name, fam in RELEASE_FAMILY.items()}


def _calendar_family(label: str) -> str | None:
    """The family a calendar row belongs to, or None for a manual entry.

    RBA decision rows carry the Statement suffix when the meeting has one, so
    they are matched by prefix rather than looked up whole.
    """
    fam = CALENDAR_FAMILY.get(label)
    if fam:
        return fam
    if label.startswith("RBA decision") or label.startswith("RBA minutes"):
        return "rba"
    return None


def _history_window_start(choice: str) -> date | None:
    """Map a history-window selector choice to a cutoff date (None = all)."""
    years = {"1Y": 1, "2Y": 2, "3Y": 3, "5Y": 5, "10Y": 10}
    if choice == "All" or choice not in years:
        return None
    today = date.today()
    try:
        return today.replace(year=today.year - years[choice])
    except ValueError:
        # Feb 29 on a non-leap target year -> clamp to Feb 28.
        return date(today.year - years[choice], 2, 28)


def _compare_date(choice: str, anchor: date) -> date:
    """Map a comparison-window choice to the date the second dot reads at."""
    months = {"3M": 3, "6M": 6, "1Y": 12, "2Y": 24}
    if choice not in months:
        return date(anchor.year - 1, 12, 31)          # prior year end
    back = months[choice]
    year, month = anchor.year, anchor.month - back
    while month <= 0:
        year, month = year - 1, month + 12
    day = min(anchor.day, [31, 29 if year % 4 == 0 and (year % 100 or not year % 400)
                           else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _num(x):
    """Text-input to float, or None. The manual labour inputs are text rather
    than number widgets so an empty field stays genuinely empty instead of
    collapsing to 0.0 and scoring as a real reading."""
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def data_asof(data: dict) -> date:
    """Newest observation anywhere in the ABS panel -- the date a typed
    reading is assumed to be current as of."""
    dates = [s[-1][0] for k, s in data.items() if not k.startswith("_") and s]
    return max(dates) if dates else TODAY


def mark_dirty() -> None:
    st.session_state["dirty"] = True


# ------------------------------------------------------------------ sidebar

with st.sidebar:
    st.markdown("#### Meeting")

    saved_keys = store.list_saved()
    options = sorted({m.key for m in MEETINGS} | set(saved_keys))
    default = next((k for k in options if k >= TODAY.isoformat()), options[-1])
    if "key" not in st.session_state:
        wanted = st.query_params.get("meeting")
        st.session_state["key"] = (
            wanted if wanted in options else (default if default in options else options[0]))

    def _fmt(k: str) -> str:
        m = next((x for x in MEETINGS if x.key == k), None)
        return (m.label if m else k) + ("" if k in saved_keys else "  (new)")

    key = st.selectbox("Meeting", options, index=options.index(st.session_state["key"]),
                       format_func=_fmt, label_visibility="collapsed")
    if key != st.session_state.get("key"):
        st.session_state["key"] = key
        st.session_state.pop("state", None)
        st.session_state["dirty"] = False
    if st.query_params.get("meeting") != key:
        st.query_params["meeting"] = key

    if "state" not in st.session_state:
        st.session_state["state"] = store.load_or_seed(
            key, [rba_calendar.meeting_to_dict(m) for m in MEETINGS], TODAY)

    S = st.session_state["state"]
    trade = S.setdefault("trade", {})
    prob = S.setdefault("probability", {})
    sizing_state = S.setdefault("sizing", {})
    pth = S.setdefault("path", {})
    if S.pop("_recovered", False):
        st.warning("The saved file for this meeting was unreadable and has been "
                   "replaced with a fresh one. Nothing was overwritten on disk "
                   "until you press Save.")

    st.divider()
    st.markdown("#### The trade")

    trade["direction"] = st.radio(
        "Direction", ["hike", "cut"],
        index=0 if trade.get("direction", "hike") == "hike" else 1,
        horizontal=True, on_change=mark_dirty)
    trade["side"] = st.radio(
        "Side", ["fade", "back"],
        index=0 if trade.get("side", "fade") == "fade" else 1,
        horizontal=True, on_change=mark_dirty,
        help="Fade = sell the event. Back = buy it.")

    pts = st.number_input(
        "Points priced (bp)", value=float(trade.get("points") or 0.0),
        step=0.5, format="%.2f", min_value=0.0, on_change=mark_dirty,
        help="What the market charges for the event. NOT the probability.")
    trade["points"] = pts if pts else None
    # min_value matters: implied probability is points/size, so a zero or
    # negative size has no meaning and used to raise straight out of
    # `model.build` into a red traceback.
    trade["size"] = st.number_input(
        "Size of the move (bp)", value=float(trade.get("size") or 25.0),
        step=5.0, format="%.0f", min_value=1.0, on_change=mark_dirty)

    st.divider()
    st.markdown("#### Your probability")
    mode = st.radio(
        "Mode", ["decomposition", "manual"],
        index=0 if prob.get("mode", "decomposition") == "decomposition" else 1,
        horizontal=True, on_change=mark_dirty, label_visibility="collapsed")
    prob["mode"] = mode
    prob["p_bloc"] = st.slider(
        "P(bloc votes for the move)", 0.0, 1.0,
        float(prob.get("p_bloc") or 0.25), 0.05, on_change=mark_dirty)
    prob["p_centre"] = st.slider(
        "P(centre joins | bloc moves)", 0.0, 1.0,
        float(prob.get("p_centre") or 0.40), 0.05, on_change=mark_dirty)
    if mode == "manual":
        prob["override_q"] = st.slider(
            "q, entered directly", 0.0, 1.0,
            float(prob.get("override_q") or 0.10), 0.01, on_change=mark_dirty)

    st.divider()
    st.markdown("#### Sizing conventions")
    sizing_state["max_drawdown"] = st.number_input(
        "Max drawdown (A$) — the Kelly bankroll",
        value=float(sizing_state.get("max_drawdown") or sizingmod.DEFAULT_MAX_DRAWDOWN),
        step=25_000.0, format="%.0f", min_value=0.0, on_change=mark_dirty)
    sizing_state["daily_limit"] = st.number_input(
        "Daily loss limit (A$) — a veto, not a sizing input",
        value=float(sizing_state.get("daily_limit") or sizingmod.DEFAULT_DAILY_LIMIT),
        step=10_000.0, format="%.0f", min_value=0.0, on_change=mark_dirty)
    trade["kelly_fraction"] = st.select_slider(
        "Kelly fraction", options=[0.25, 1 / 3, 0.5, 1.0],
        value=float(trade.get("kelly_fraction") or 0.25),
        format_func=sizingmod.fraction_label, on_change=mark_dirty)

    universe = sizingmod.contract_universe(TODAY)
    ckeys = [c.key for c in universe] + ["custom"]
    cur = sizing_state.get("contract") or "ib"
    if cur not in ckeys:
        ckeys.insert(0, cur)
    # Legacy family keys ("ib", "ir") are what a freshly seeded meeting carries,
    # so they need labels too or the dropdown shows a bare key.
    labels = {k: c.label for k, c in sizingmod.LEGACY_CONTRACTS.items()}
    labels.update({c.key: c.label for c in universe})
    sizing_state["contract"] = st.selectbox(
        "Sizing contract", ckeys, index=ckeys.index(cur),
        format_func=lambda k: labels.get(k, k), on_change=mark_dirty)
    if sizing_state["contract"] == "custom":
        sizing_state["custom_dv01"] = st.number_input(
            "DV01 per lot (A$)", value=float(sizing_state.get("custom_dv01") or 0.0),
            step=1.0, min_value=0.0, on_change=mark_dirty)

    st.divider()
    if st.button("Save", width="stretch", type="primary"):
        S["as_of"] = TODAY.isoformat()
        store.save(key, S)
        st.session_state["dirty"] = False
        st.success("Saved.")
    if st.session_state.get("dirty"):
        st.caption("Unsaved changes.")


# ------------------------------------------------------------------ model

ib_quotes = fetch_ib()
ir_quotes = fetch_ir()
spot = rba_spot()

ib_ref = asx.latest_asof(ib_quotes)
ir_ref = asx.latest_asof(ir_quotes)
ib_stale = asx.stale_codes(ib_quotes, ib_ref)
ir_stale = asx.stale_codes(ir_quotes, ir_ref)
horizon = asx.coverage_end(ib_quotes, ib_ref)

# The IR sizing DV01 needs a live yield; take it from the contract selected.
# IR's DV01 moves with the yield level, so the model needs a live one. It is
# handed over in a THROWAWAY overlay rather than written into `S`: `S` is the
# live session dict the widgets mutate and Save persists, and a market reading
# is not a user decision -- persisting it would make a reopened meeting size
# off whatever the curve happened to be on the day it was last viewed.
sel_key = sizing_state.get("contract") or "ib"
model_state = S
if sel_key.startswith("ir:"):
    sel_month = sel_key.partition(":")[2]
    match = next((q for q in ir_quotes if q.ok and f"{q.month:%Y-%m}" == sel_month), None)
    if match is not None:
        model_state = {**S, "sizing": {**sizing_state,
                                       "contract_yield": match.implied_rate}}

kill_values, kill_units = kill_series_values()

def rebuild():
    """Recompute the whole framework from the current state.

    Streamlit runs top to bottom, so a widget rendered inside a section body
    only writes back into `S` AFTER the model would otherwise have been built
    -- which leaves the Verdict, and everything downstream of it, one
    interaction behind whatever you just edited. That is not cosmetic here: the
    Monte Carlo needs a target and a stop, and both are typed in the Path
    section, so without this the simulation could never run on the values you
    just entered.

    Cheap enough to repeat -- about 6ms of pure arithmetic, with every data
    fetch already memoised -- so it is called after each input cluster rather
    than reasoned about.
    """
    global M
    M = model.build(
        model_state, as_of=TODAY,
        rba_values=kill_values, rba_units=kill_units,
        all_meetings=MEETINGS,
        rate_history=bbsw_history(),
        rate_symbol="3-month BBSW (RBA F1)",
        policy_changes=policy_changes(),
    )
    return M


M = rebuild()

# The priced path, and the bill curve laid against it.
strip_spot = stripmod.implied_spot(ib_quotes, MEETINGS, as_of=TODAY)
anchor = strip_spot if strip_spot is not None else spot["cash"]
PATH = (stripmod.decompose(MEETINGS, ib_quotes, anchor, M.size,
                           stale_codes=ib_stale, as_of=TODAY)
        if anchor is not None else stripmod.StripPath(0.0, M.size, error="no cash rate anchor"))
BILLS = bbswmod.analyse(ir_quotes, PATH, MEETINGS, M.size, ir_stale, horizon,
                        as_of=TODAY, spot_basis_bp=spot["spot_basis_bp"])

LEFT, RIGHT = st.columns([1, 0.34], gap="large")


# ------------------------------------------------------------------ working

SECTIONS = ["Pricing", "Curve", "Sensitivity & size", "Path & kills",
            "Labour", "Inflation", "Vote count", "Data"]

with LEFT:
    st.markdown(f"### {M.label} — RBA Monetary Policy Board")
    chips: list[tuple[str, str | None]] = [
        (f"{M.days_to_meeting}d to the decision" if M.days_to_meeting >= 0
         else f"{-M.days_to_meeting}d since", None),
        ("SMP meeting" if M.meeting.has_smp else "no SMP", P.accent if M.meeting.has_smp else None),
    ]
    if M.in_blackout:
        chips.append(("blackout", P.negative))
    if not M.meeting.verified:
        chips.append(("date unverified", P.warning))
    C.chips(chips)

    # A segmented control rather than st.tabs: Streamlit keeps every tab panel
    # in the DOM, so a Vega chart on an inactive tab mounts at zero width and
    # never recovers. Rendering only the active section fixes that.
    section = st.segmented_control("Section", SECTIONS, default=SECTIONS[0],
                                   label_visibility="collapsed") or SECTIONS[0]

    # History windows reset to their default whenever the user navigates into
    # the tab; widget state otherwise persists across tab switches, so a 10Y
    # window set an hour ago would still be in force on a tab the reader has
    # just opened, with nothing on screen saying why the chart starts in 2016.
    if st.session_state.get("_prev_section") != section:
        if section == "Labour":
            st.session_state["compare_window_labour"] = "Prior year end"
            st.session_state["hist_window_labour"] = "10Y"
        if section == "Inflation":
            st.session_state["hist_window_inflation"] = "10Y"
        st.session_state["_prev_section"] = section

    # ---------------------------------------------------------- 1. Pricing
    if section == "Pricing":
        if not M.priced:
            st.info("Enter the points priced in the sidebar to begin.")
        else:
            s = M.summary
            a, b, c, d = st.columns(4)
            with a:
                C.labelled_metric(
                    "Implied probability", f"{s.implied:.1%}",
                    r"p_{\text{implied}} = \frac{\text{points priced}}{\text{size of the move}}",
                    "Points priced divided by the size of the move. Not the points themselves.",
                    f"{M.points:g} / {M.size:g} = {s.implied:.1%}", "implied")
            with b:
                C.labelled_metric(
                    "Your probability", f"{s.q:.1%}",
                    r"q = P(\text{bloc moves}) \times P(\text{centre joins})"
                    if M.q_mode == "decomposition" else r"q\ \text{(entered directly)}",
                    "The only input you control, and the only genuinely uncertain one.",
                    (f"{M.decomposition.p_bloc:.0%} x {M.decomposition.p_centre:.0%} = {s.q:.1%}"
                     if M.q_mode == "decomposition" else f"q = {s.q:.1%}"),
                    "yourq", colour=P.accent)
            with c:
                C.labelled_metric(
                    "Expected value", f"{s.ev:+.2f}bp",
                    r"EV = \text{points} - \text{size} \times q",
                    "Collapsed from (1-q)(gain) - q(loss). One question: is q below breakeven?",
                    f"{M.points:g} - {M.size:g}({s.q:.4g}) = {s.ev:+.2f}bp", "ev",
                    colour=pnl_colour(s.ev, P))
            with d:
                C.labelled_metric(
                    "Breakeven", f"{s.breakeven:.1%}",
                    r"q^* = \frac{\text{points}}{\text{size}} = p_{\text{implied}}",
                    "Equals the implied probability, always. There is no structural edge in "
                    "the payoff shape; the only edge is q differing from the market's.",
                    f"{M.points:g} / {M.size:g} = {s.breakeven:.1%}", "be")

            st.divider()
            l1, l2 = st.columns([1, 1], gap="large")

            with l1:
                C.section("Step 2 — the two terminal outcomes")
                C.table(
                    ["Outcome", "Settles at", "Your P&L"],
                    [[o.label, f"{o.settles_at:g}bp", f"{o.pnl:+g}bp"] for o in M.outcomes],
                    numeric=(1, 2),
                    colours={(i, 2): pnl_colour(o.pnl, P) for i, o in enumerate(M.outcomes)},
                )
                C.note(f"Max loss is capped at {abs(s.loss):g}bp because the RBA cannot move "
                       f"more than {M.size:g}bp at this meeting. An uncapped tail would change "
                       "the answer.")

            with l2:
                C.section("Step 3 — the arithmetic check")
                agree = pricing.ev_routes_agree(M.points, M.size, s.q, M.side)
                C.table(
                    ["Route", "Working", "Result"],
                    [
                        ["EV", f"{M.points:g} − {M.size:g}({s.q:.4g})", f"{s.ev:+.2f}bp"],
                        ["Edge", f"({s.implied:.0%} − {s.q:.0%}) × {M.size:g}",
                         f"{s.ev_via_edge:+.2f}bp"],
                    ],
                    numeric=(2,),
                )
                if agree:
                    st.markdown(
                        f'<div class="fomc-note" style="color:{P.good}">'
                        '✓ Two routes, same answer.</div>',
                        unsafe_allow_html=True)
                else:
                    st.error("The two routes disagree — check the inputs.")

            st.divider()
            m1, m2, m3 = st.columns(3)
            m1.metric(
                "Margin of safety", f"{s.margin * 100:.1f}pp",
                help="Breakeven minus your q -- how far your estimate can be wrong before "
                     "the trade stops being profitable in expectation.",
            )
            m2.metric(
                "Max gain / max loss", f"{s.gain:+g} / {s.loss:+g}",
                help="The two possible outcomes' P&L. Capped both ways because the size of "
                     "the move itself is capped.",
            )
            m3.metric(
                "Payoff ratio", f"1 : {s.rr:.1f}",
                help="How much you risk to make how much. On its own this says nothing about "
                     "whether the trade is good -- that depends on how often you actually win.",
            )

        st.divider()
        ehdr, ehlp = st.columns([1, 0.06], vertical_alignment="center")
        with ehdr:
            window_start = M.as_of - timedelta(days=econ_calendar.LOOKBACK_DAYS)
            C.section(
                f"Around {M.meeting.label}",
                f"{window_start:%d %b} → {M.meeting.end:%d %b %Y} — the past "
                f"four months' context, plus {M.days_to_meeting}d to the decision."
                if M.days_to_meeting >= 0 else
                "This meeting has already happened.")
        with ehlp:
            C.formula_help(
                r"\text{shown} = \{\, \text{Labour Force, CPI, WPI, GDP, RBA} \,\}",
                "Releases shown here come from a fixed, institution-stated rule -- "
                "Thursday of the third week (ABS Labour Force), the last Wednesday "
                "of the month (CPI: the quarterly print in Jan/Apr/Jul/Oct, the "
                "monthly indicator otherwise), the third Wednesday of the middle "
                "month of a quarter (Wage Price Index), the first Wednesday of "
                "Mar/Jun/Sep/Dec (National Accounts), the decision dates themselves "
                "and a fortnight after each for the minutes. Retail sales, building "
                "approvals and the rest do not fall on either kind of fixed date, so "
                "they are never guessed here -- add them by hand on the Data tab and "
                "they arrive marked \"confirmed\".",
                f"window: {window_start.isoformat()} to {M.meeting.end.isoformat()}",
                "econcal")

        if M.days_to_meeting < 0:
            pass
        elif not M.calendar_events:
            st.info("No rule-based release falls in this window.")
        else:
            headers = ["Date", "Out", "Release", "Tier", "Source"]

            def _rows(events, muted):
                rows, colours, marks, bg = [], {}, [], {}
                for i, e in enumerate(events):
                    out = (e.when - M.as_of).days
                    if e.when == M.meeting.end:
                        marks.append(i)
                    rows.append([
                        e.when.strftime("%a %d %b"), f"{out}d", e.label, e.weight,
                        "confirmed" if e.source == "manual" else "rule",
                    ])
                    family = _calendar_family(e.label)
                    if family:
                        bg[i] = CALENDAR_FAMILY_TINT[family]
                    if muted:
                        for col in range(len(headers)):
                            colours[(i, col)] = P.muted
                    else:
                        colours[(i, 3)] = {1: P.critical, 2: P.serious}.get(e.weight, P.muted)
                return rows, colours, marks, bg

            past = [e for e in M.calendar_events if e.when < M.as_of]
            future = [e for e in M.calendar_events if e.when >= M.as_of]

            if past:
                st.markdown('<div class="fomc-note" style="opacity:0.7;'
                            'text-transform:uppercase;letter-spacing:.04em;'
                            'font-size:0.68rem;margin-bottom:0.2rem">'
                            'Past four months — realised</div>', unsafe_allow_html=True)
                rows, colours, marks, bg = _rows(past, muted=True)
                C.table(headers, rows, numeric=(1, 3), mark_rows=marks,
                        colours=colours, row_bg=bg)
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:0.6rem;'
                    f'margin:0.5rem 0;opacity:0.55;font-size:0.7rem;">'
                    f'<div style="flex:1;height:1px;background:currentColor"></div>'
                    f'<div>AS OF · {M.as_of:%a %d %b %Y}</div>'
                    f'<div style="flex:1;height:1px;background:currentColor"></div>'
                    f'</div>', unsafe_allow_html=True)

            if future:
                if past:
                    st.markdown('<div class="fomc-note" style="opacity:0.7;'
                                'text-transform:uppercase;letter-spacing:.04em;'
                                'font-size:0.68rem;margin-bottom:0.2rem">'
                                'Before the meeting</div>', unsafe_allow_html=True)
                rows, colours, marks, bg = _rows(future, muted=False)
                C.table(headers, rows, numeric=(1, 3), mark_rows=marks,
                        colours=colours, row_bg=bg)
            elif past:
                C.note("Nothing left on the rule-based calendar before the meeting.")

            C.legend([(CALENDAR_FAMILY_NAME[f], CALENDAR_FAMILY_TINT_SOLID[f])
                      for f in ("labour", "inflation", "activity", "rba")])
            C.note("Tier 1 = market-moving, 2 = notable. \"rule\" is generated from a fixed "
                   "release convention, not read off a calendar page; public holidays that "
                   "shift an ABS release are not accounted for. \"confirmed\" marks a "
                   "manually-added calendar entry for this meeting.")

    # ------------------------------------------------------------ 2. Curve
    elif section == "Curve":
        C.section("The priced path", "Backed out of the ASX IB strip.")
        if not PATH.ok:
            st.warning(PATH.error or "No priced path available.")
        else:
            src = "the strip itself" if strip_spot is not None else "RBA table F1"
            C.vintage(f"Spot {PATH.spot:.3f}% from {src}. "
                      f"Strip settled {ib_ref or 'unknown'}. "
                      f"Reliable coverage to {horizon or 'unknown'}.")
            if strip_spot is None:
                C.note(
                    "The strip cannot recover spot right now — a decision has already taken "
                    "effect inside the front contract's month, which leaves one more unknown "
                    "than there are equations. Anchored on the published cash rate target "
                    "instead rather than inventing one.")

            upcoming = [s for s in PATH.steps if s.meeting.end >= TODAY]
            C.table(
                ["Meeting", "Contract", "Read", "Step", "Cumulative", "Implied prob", "Flag"],
                [[s.label, s.code, "clean" if s.is_clean else "inverted",
                  f"{s.step_bp:+.1f}bp", f"{s.cum_bp:+.1f}bp", f"{s.prob:.1%}",
                  "stale" if s.stale else ("ambiguous" if s.ambiguous else "")]
                 for s in upcoming],
                numeric=(3, 4, 5))
            C.note(
                "A **clean** read comes from a month sitting wholly at the new rate, so it "
                "prices it outright. An **inverted** read un-blends the meeting month, which "
                "divides by the days after the decision — reliable in the middle of a month, "
                "brutally leveraged at the end of one. The RBA's late-month meetings are "
                "exactly where that matters, which is why the source is shown per row.")
            step_here = PATH.step_for(M.meeting.key)
            if step_here is not None and step_here.reliable:
                if st.button(
                        f"Use the strip's {step_here.step_bp:+.1f}bp as points priced",
                        help="Adopts the market's own number for THIS meeting so your q "
                             "is measured against it rather than against a stale entry."):
                    trade["points"] = round(abs(step_here.step_bp), 2)
                    mark_dirty()
                    st.rerun()
            if PATH.excluded_count:
                st.caption(
                    f"{PATH.excluded_count} step(s) solved off a contract that is not trading "
                    "and are kept out of the headline numbers below.")

            st.altair_chart(charts.priced_path(PATH.steps, PATH.spot, P),
                            use_container_width=True)

            flagged = [c for c in PATH.checks if c.flagged]
            if flagged:
                C.section("Months that should reprint the carried rate, and do not")
                C.table(["Month", "Contract", "Implied", "Carried", "Difference"],
                        [[f"{c.month:%b %Y}", c.code, f"{c.implied:.3f}%",
                          f"{c.expected:.3f}%", f"{c.diff_bp:+.1f}bp"] for c in flagged])

        st.divider()
        C.section("90 day bank bills", "Where the same question stops working.")
        if not BILLS.ok:
            st.warning(BILLS.error or "No bank bill curve available.")
        else:
            C.note(
                "An IR contract settles on a **single 3-month BBSW fix**, not an average, so "
                "there is no month to un-blend and no per-meeting probability to recover from "
                "it. And BBSW is a bank credit rate, not a policy rate — an IR price is the "
                "expected cash path *plus* a credit and term spread. The basis column is that "
                "spread, measured against the IB path over the same 90 days.")
            C.table(
                ["Contract", "Fix", "Covers", "BBSW", "IB path", "Basis", "Cash-equiv", "Flag"],
                [[p.code, f"{p.fix_date:%d %b %y}", p.window_label, f"{p.implied:.3f}%",
                  "—" if p.expected is None else f"{p.expected:.3f}%",
                  "—" if p.basis_bp is None else f"{p.basis_bp:+.1f}bp",
                  "—" if p.cash_equivalent is None else f"{p.cash_equivalent:.3f}%",
                  "beyond IB" if p.beyond_horizon else ("stale" if p.stale else "")]
                 for p in BILLS.periods[:10]])
            if spot["spot_basis_bp"] is not None:
                C.note(
                    f"**Cash-equiv** strips today's observed spread of "
                    f"{spot['spot_basis_bp']:+.1f}bp (3-month BBSW {spot['bbsw']:.3f}% less the "
                    f"{spot['cash']:.3f}% target) off the bill's rate. That is an assumption — "
                    "that the credit spread holds to the fix — and it is stated rather than "
                    "hidden. It is deliberately NOT computed from the basis column, which "
                    "would be circular and would just hand back the IB path.")
            comparable = [p for p in BILLS.periods if p.comparable and not p.stale]
            if comparable:
                st.altair_chart(charts.bbsw_basis(comparable, P), use_container_width=True)
                mb = BILLS.mean_basis_bp
                hist = rba.historical_bbsw_ois_basis()
                if mb is not None and hist:
                    hist_mean = sum(o.value for o in hist) / len(hist)
                    st.caption(
                        f"Mean basis {mb:+.1f}bp across comparable contracts. The published "
                        f"BBSW/OIS spread averaged {hist_mean:.1f}bp over 2011–2022 before the "
                        "RBA retired the series, which is the only yardstick available for "
                        "whether today's reading is wide or narrow.")

    # -------------------------------------------- 3. Sensitivity & size
    elif section == "Sensitivity & size":
        if not M.priced:
            st.info("Enter the points priced in the sidebar to begin.")
        else:
            s = M.summary
            C.section("Step 4 — sensitivity to your probability estimate",
                      "This is where to spend your time: q is the only input you control.")
            sc, st_ = st.columns([1.6, 1], gap="medium")
            with sc:
                st.altair_chart(charts.ev_curve(M.points, M.size, s.q, M.side, P),
                                width="stretch", theme=None)
            with st_:
                marks = [i for i, r in enumerate(M.sensitivity)
                         if r.is_breakeven or abs(r.q - s.q) < 1e-9]
                C.table(
                    ["Your q", "EV (bp)", ""],
                    [[f"{r.q:.0%}", f"{r.ev:+.2f}",
                      "breakeven" if r.is_breakeven
                      else ("yours" if abs(r.q - s.q) < 1e-9 else "")]
                     for r in M.sensitivity],
                    numeric=(1,), mark_rows=marks,
                    colours={(i, 1): pnl_colour(r.ev, P)
                             for i, r in enumerate(M.sensitivity)},
                )
            C.note(f"{s.margin * 100:.1f} percentage points of room between your estimate "
                   f"({s.q:.0%}) and breakeven ({s.breakeven:.0%}). Read it as your margin "
                   "of safety.")

            st.divider()
            lad = M.sizing_ladder
            shdr, shlp = st.columns([1, 0.06], vertical_alignment="center")
            with shdr:
                C.section(
                    "Step 5 — how big, in DV01, lots and dollars",
                    f"Kelly sizes against the A${M.max_drawdown:,.0f} drawdown; the "
                    f"A${M.daily_limit:,.0f} daily limit only vetoes. "
                    f"{M.contract_label} at A${M.dv01:,.2f} per bp per lot. "
                    "All three are sidebar inputs.")
            with shlp:
                C.formula_help(
                    r"\text{loss}_\$ = f \times \text{drawdown} \;\Rightarrow\; "
                    r"\text{DV01}_{pos} = \frac{\text{loss}_\$}{\text{loss}_{bp}} "
                    r"\;\Rightarrow\; \text{lots} = \frac{\text{DV01}_{pos}}{\text{DV01}}",
                    "f is a share of the bankroll, so it multiplies dollars — never DV01. "
                    "The dollars risked divided by the bp you can lose gives the "
                    "position's own DV01, and that divided by the contract's DV01 gives "
                    "the lot count.",
                    (f"loss$   = {lad.selected.f:.2%} x A${M.max_drawdown:,.0f} "
                     f"= A${lad.selected.max_loss:,.0f}\n"
                     f"DV01pos = A${lad.selected.max_loss:,.0f} / {lad.loss_bp:g}bp "
                     f"= A${lad.selected.position_dv01:,.0f} per bp\n"
                     f"lots    = A${lad.selected.position_dv01:,.0f} / A${M.dv01:,.2f} "
                     f"= {lad.selected.contracts:,}")
                    if lad and lad.ok and lad.selected else "price the trade first",
                    "sizing")

            if not lad or not lad.ok:
                st.warning((getattr(lad, "error", None) or "Sizing unavailable.")
                           + "  Kelly only sizes a position that has positive expected "
                             "value — at or past breakeven there is nothing to size.")
            else:
                sel = lad.selected
                if sel and not sel.within_daily_limit:
                    st.error(
                        f"**{sel.label} Kelly breaches the daily limit.** It risks "
                        f"A${sel.max_loss:,.0f} against a A${M.daily_limit:,.0f} cap "
                        f"({sel.contracts:,} lots vs a {lad.daily_cap_contracts:,}-lot "
                        f"ceiling). Largest rung that fits: "
                        f"**{lad.largest_within_limit.label}**."
                        if lad.largest_within_limit else
                        f"**{sel.label} Kelly breaches the daily limit.**")

                g1, g2 = st.columns(2, gap="medium")
                with g1:
                    st.markdown("##### Kelly growth curve")
                    st.altair_chart(charts.kelly_growth(lad, P),
                                    width="stretch", theme=None)
                    C.note("Flat approaching the peak, a cliff past it. Under-betting is "
                           "cheap; over-betting is not.")
                with g2:
                    st.markdown("##### Capital at risk vs Kelly growth")
                    st.altair_chart(charts.capital_at_risk(lad, P),
                                    width="stretch", theme=None)
                    C.note("What the next slice of growth costs in downside. A rung the "
                           "daily limit forbids is drawn hollow.")

                st.divider()
                rows, colours, marks = [], {}, []
                for i, r in enumerate(lad.rungs):
                    if r.is_selected:
                        marks.append(i)
                    rows.append([
                        r.label, f"{r.f:.1%}", f"A${r.max_loss:,.0f}",
                        f"A${r.position_dv01:,.0f}", f"{r.contracts:,}",
                        f"A${r.expected_pnl:,.0f}", f"A${r.max_gain:,.0f}",
                        f"{r.growth * 100:.2f}%", f"{r.growth_share:.1%}",
                        f"{r.annualised:.1%}", r.status,
                        "--" if r.delta_loss is None else f"+A${r.delta_loss:,.0f}",
                        "--" if r.growth_per_10k is None
                        else f"{r.growth_per_10k * 100:.1f}pp",
                    ])
                    colours[(i, 2)] = P.negative
                    colours[(i, 5)] = P.positive
                    colours[(i, 6)] = P.positive
                    colours[(i, 10)] = P.good if r.within_daily_limit else P.critical
                C.table(
                    ["Kelly", "f", "Max loss", "DV01 risk/bp", "Lots",
                     "Expected A$", "Max gain", "g/bet", "% of full", "Annualised",
                     "Daily", "Extra risk", "Growth / A$10k"],
                    rows, numeric=(1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12),
                    mark_rows=marks, colours=colours)

                steps = [sizingmod.step_up_verdict(a, b)
                         for a, b in zip(lad.rungs, lad.rungs[1:])]
                C.note(f"▸ is the rung selected in the sidebar. The daily limit allows at "
                       f"most {lad.daily_cap_contracts:,} lots. Growth / A$10k is the "
                       f"column that decides the rung: how much growth each extra A$10,000 "
                       f"of risk actually buys. It falls at every step, which is the whole "
                       f"argument for sizing below full Kelly. "
                       + "  ".join(s_ for s_ in steps if s_))

                zero_f = lad.zero_growth_f
                if zero_f:
                    C.note(f"Full Kelly here is {lad.f_star:.0%} of the budget and growth "
                           f"hits zero at {zero_f:.0%} — past that you carry maximum risk "
                           f"for no expected growth at all. Kelly assumes q is right; the "
                           f"framework's own weak link is that q rests on a vote-count "
                           f"read, which is the case for sizing below the peak rather "
                           f"than at it.")

                st.caption(
                    f"{M.contract_label} — DV01 A${M.quoted_dv01:,.2f} per lot"
                    + (f", corrected to A${M.dv01:,.2f} per bp of the event "
                       f"({M.capture:.1%} capture)." if M.capture < 1 - 1e-9 else "."))

    # ---------------------------------------------------- 4. Path & kills
    elif section == "Path & kills":
        if not M.priced:
            st.info("Enter the points priced in the sidebar to begin.")
        else:
            C.section("Step 6 — path risk",
                      "Where you get out, and what the stop costs you to hold.")

            # The market bound: the level the priced number reaches if the
            # event fully happens. Every barrier lives between entry and here.
            bound = M.size if M.side == "fade" else 0.0
            a, b, c = st.columns(3)
            with a:
                tgt = st.number_input(
                    "Take-profit (bp priced)", value=float(pth.get("target_level") or 0.0),
                    step=0.5, format="%.2f", on_change=mark_dirty,
                    help="Where you scale out. In bp PRICED, not P&L.")
                pth["target_level"] = tgt or None
            with b:
                stp = st.number_input(
                    "Stop (bp priced)", value=float(pth.get("stop_level") or 0.0),
                    step=0.5, format="%.2f", on_change=mark_dirty,
                    help="Where the thesis is wrong enough to leave.")
                pth["stop_level"] = stp or None
            with c:
                cur = st.number_input(
                    "Current mark (bp priced)", value=float(pth.get("current_price") or 0.0),
                    step=0.5, format="%.2f", on_change=mark_dirty)
                pth["current_price"] = cur or None

            with st.expander("Partial scale-out and a hard floor"):
                d, e, f = st.columns(3)
                with d:
                    pt = st.number_input(
                        "Partial target (bp priced)",
                        value=float(pth.get("partial_target_level") or 0.0),
                        step=0.5, format="%.2f", on_change=mark_dirty,
                        help="A first scale-out before the full target. 0 = none.")
                    # 0 is "blank", not a level: a scale-out at entry is never a
                    # real barrier, and treated as one it sits on the wrong side
                    # of entry and blocks the whole simulation.
                    pth["partial_target_level"] = pt or None
                with e:
                    pth["partial_share"] = st.slider(
                        "Share taken off there", 0.0, 1.0,
                        float(pth.get("partial_share") or 0.5), 0.05,
                        on_change=mark_dirty)
                with f:
                    hs = st.number_input(
                        "Hard floor (bp priced)",
                        value=float(pth.get("hard_stop_level") or 0.0),
                        step=0.5, format="%.2f", on_change=mark_dirty,
                        help="A discipline line. Warns if misplaced, never blocks.")
                    pth["hard_stop_level"] = hs or None

            # The barriers above are what the exit map, the simulation and the
            # stop-cost decomposition are all built from, so the model has to
            # catch up before any of them is drawn.
            M = rebuild()

            st.altair_chart(charts.mtm_ladder(M.ladder, P), use_container_width=True)
            if M.exit_levels:
                C.table(["Level", "Role", "At", "MTM", "Price", "P&L"],
                        [[x.label, x.role, f"{x.level:g}bp", f"{x.mtm:+.2f}bp",
                          "—" if x.price is None else f"{x.price:.3f}",
                          "—" if x.pnl is None else f"A${x.pnl:,.0f}"]
                         for x in M.exit_levels], numeric=(2, 3, 4, 5))

            # ------------------------------------------------ volatility
            st.divider()
            C.section("Volatility", "What the priced level does on a quiet day.")
            g, h = st.columns([1, 1])
            with g:
                vo = st.number_input(
                    "Daily vol override (bp/day)",
                    value=float(pth.get("vol_override_bp") or 0.0),
                    step=0.25, format="%.2f", min_value=0.0, on_change=mark_dirty,
                    help="In EVENT terms. Overrides the estimate below.")
                pth["vol_override_bp"] = vo or None
            with h:
                pth["vol_uncertainty"] = st.checkbox(
                    "Treat the vol estimate as uncertain",
                    value=bool(pth.get("vol_uncertainty")), on_change=mark_dirty,
                    help="Draws sigma from a chi-square around the estimate rather "
                         "than trusting one number.")

            M = rebuild()

            if M.quiet_sigma_bp is not None:
                C.vintage(
                    f"Quiet-day sigma {M.quiet_sigma_bp:.2f}bp/day, from daily 3-month BBSW "
                    f"(RBA table F1). Over {montecarlo.trading_days_between(TODAY, M.meeting.end)} "
                    "trading days to the decision.")
                C.note(
                    "BBSW is a **rate**, so a basis point of it is a basis point of the event — "
                    "no contract-capture rescale is applied, unlike the US original, which "
                    "calibrates on a contract price. It spans about 1.5 meetings rather than "
                    "one, so read it as the short end's daily noise rather than this meeting's "
                    "alone.")
            if M.vol_multipliers:
                with st.expander("How each release was weighted"):
                    C.table(
                        ["Release", "Event days", "Quiet days", "Raw", "Shrunk", "95% CI"],
                        [[m.name, m.n_event, m.n_quiet, f"{m.raw:.2f}x",
                          f"{m.shrunk:.2f}x", f"{m.ci_low:.2f}–{m.ci_high:.2f}"]
                         for m in sorted(M.vol_multipliers.values(),
                                         key=lambda m: -m.shrunk)],
                        numeric=(1, 2, 3, 4, 5))
                    C.note(
                        "**Shrunk** is the number actually used: a ratio from three "
                        "observations is pulled toward 1 until the data earns it. Quarterly "
                        "CPI is the loudest release on the Australian calendar by a wide "
                        "margin, which is what the multiplier should show.")

            # ---------------------------------------------- Monte Carlo
            if M.monte_carlo is not None:
                mc = M.monte_carlo
                st.divider()
                C.section("Simulated paths", "Where the exits actually get touched.")
                st.altair_chart(
                    charts.mc_fan_chart(mc, M.points, M.size, M.side, P),
                    use_container_width=True)
                i, j, k = st.columns(3)
                i.metric("Target first", f"{mc.p_target_first:.1%}")
                j.metric("Stop first", f"{mc.p_stop_first:.1%}")
                k.metric("Rode it out", f"{mc.p_rode_out:.1%}")

                with st.expander("If I moved the stop, or the target"):
                    vol = montecarlo.VolEstimate(
                        M.quiet_sigma_bp or (pth.get("vol_override_bp") or 1.0),
                        "historical")
                    days = montecarlo.trading_days_between(TODAY, M.meeting.end)
                    lo, hi = st.columns(2)
                    with lo:
                        levels = montecarlo.suggested_sweep_levels(M.points, bound)
                        sweep = montecarlo.stop_sweep(
                            M.points, M.size, M.side, M.summary.q, mc.target_level,
                            levels, vol, days)
                        st.altair_chart(
                            charts.mc_sensitivity(sweep, mc.stop_level, "stop (bp priced)", P),
                            use_container_width=True)
                    with hi:
                        levels = montecarlo.suggested_sweep_levels(M.points, 0.0
                                                                   if M.side == "fade" else M.size)
                        sweep = montecarlo.target_sweep(
                            M.points, M.size, M.side, M.summary.q, mc.stop_level,
                            levels, vol, days)
                        st.altair_chart(
                            charts.mc_sensitivity(sweep, mc.target_level, "target (bp priced)", P),
                            use_container_width=True)
            elif not (pth.get("target_level") and pth.get("stop_level")):
                st.info("Set a take-profit and a stop above to run the simulation.")

            # -------------------------------------------- stop-path counts
            st.divider()
            C.section("What the stop costs",
                      "Out of 100 imagined runs, where did each one end?")
            C.note(
                "The stop is not free. It converts some winners into small losers, and the "
                "only way to know whether that is worth the protection is to count the paths "
                "rather than assume. Counts are per 100 runs and must sum to 100.")
            nm, mv = st.columns(2)
            with nm:
                st.caption("**If the RBA holds**")
                for key_, label in (("no_move_target_hit", "reached the target"),
                                    ("no_move_never_breached", "drifted, never stopped"),
                                    ("no_move_falsely_stopped", "stopped out anyway")):
                    pth[key_] = st.number_input(
                        label, value=float(pth.get(key_) or 0.0), step=1.0,
                        min_value=0.0, format="%.0f", key=f"sp_{key_}",
                        on_change=mark_dirty)
            with mv:
                st.caption(f"**If the RBA moves {M.size:g}bp**")
                for key_, label in (("move_target_hit", "reached the target first"),
                                    ("move_stopped_early", "stopped before the decision"),
                                    ("move_gapped_through", "gapped through on the day")):
                    pth[key_] = st.number_input(
                        label, value=float(pth.get(key_) or 0.0), step=1.0,
                        min_value=0.0, format="%.0f", key=f"sp_{key_}",
                        on_change=mark_dirty)

            M = rebuild()
            if M.stop is not None:
                s = M.stop
                C.table(
                    ["", "EV"],
                    [["Without the stop", f"{s.ev_without_stop:+.2f}bp"],
                     ["With the stop", f"{s.ev_with_stop:+.2f}bp"],
                     ["Cost of the stop", f"{s.cost_of_stop:+.2f}bp"]],
                    numeric=(1,), mark_rows=(2,))
                if s.cost_as_share_of_edge is not None:
                    st.caption(
                        f"The stop costs {abs(s.cost_as_share_of_edge):.0%} of the raw edge.")

        # ------------------------------------------------- kill criteria
        st.divider()
        C.section("Step 7 — kill criteria", "Pre-commit to what invalidates the thesis.")
        # Reserved now, filled after the editor below. The banner belongs at the
        # top where it will be read, but its content depends on toggles that are
        # rendered further down -- painting it in place would leave it one
        # interaction stale, which for a staleness warning is a bad joke.
        stale_slot = st.empty()
        C.note(
            "The first three read live off RBA table F1 and colour themselves; the rest are "
            "manual toggles. Two or more live at once and the app banners that the estimate "
            "is stale — the threshold is enforced here rather than left to memory, because "
            "the moment it matters is the moment you are least inclined to apply it.")

        raw_kills = S.setdefault("kill_criteria", [])
        for i, raw in enumerate(raw_kills):
            crit = next((k for k in M.kill.criteria if k.label == raw.get("label")), None)
            head, val, rm = st.columns([0.62, 0.28, 0.10], vertical_alignment="center")
            with head:
                raw["label"] = st.text_input(
                    "label", value=raw.get("label", ""), key=f"kill_lbl_{i}",
                    label_visibility="collapsed", on_change=mark_dirty)
            with val:
                if crit is not None and crit.is_auto:
                    tone = P.critical if crit.triggered else P.ink_secondary
                    reading = "—" if crit.current is None else f"{crit.current:.1f}{crit.unit}"
                    st.markdown(
                        f"<div style='color:{tone};font-weight:600'>{reading} "
                        f"<span style='font-weight:400;opacity:.7'>vs "
                        f"{crit.comparator} {crit.threshold:g}{crit.unit}</span></div>",
                        unsafe_allow_html=True)
                else:
                    raw["triggered"] = st.checkbox(
                        "live", value=bool(raw.get("triggered")), key=f"kill_trg_{i}",
                        on_change=mark_dirty)
            with rm:
                if st.button("✕", key=f"kill_rm_{i}", help="Remove this criterion"):
                    raw_kills.pop(i)
                    mark_dirty()
                    st.rerun()
            if raw.get("note"):
                C.note(raw["note"])
            if crit is not None and crit.is_auto and crit.series:
                hist = kill_series_history().get(crit.series)
                if hist:
                    st.altair_chart(
                        charts.sparkline(hist, crit.threshold, crit.triggered, P),
                        use_container_width=True)

        M = rebuild()
        if M.kill.is_stale:
            stale_slot.error(
                f"{M.kill.count} kill criteria live — the probability estimate is stale.")

        if st.button("+ Add kill criterion"):
            raw_kills.append({"label": "New criterion", "comparator": "manual",
                              "threshold": None, "series": None, "unit": "",
                              "triggered": False, "note": ""})
            mark_dirty()
            st.rerun()

    # ----------------------------------------------------------- 5. Labour
    elif section == "Labour":
        ehdr, ehlp = st.columns([1, 0.06], vertical_alignment="center")
        with ehdr:
            C.section("Full employment indicators",
                      "Each series scored against its own history, not against "
                      "a trend.")
        with ehlp:
            C.formula_help(
                r"z = \frac{x - \mu_{[\text{start},\,\text{end}]}}"
                r"{\sigma_{[\text{start},\,\text{end}]}}"
                r"\qquad z \mapsto -z \ \text{ for a slack measure}",
                "Every row is a z-score: how far today's reading sits from that "
                "series' own window average, in standard deviations. Right of "
                "zero is a tighter labour market than that average \u2014 which means "
                "the five slack measures have their sign flipped, or a high "
                "unemployment rate would plot on the same side as a high "
                "vacancies ratio and the panel would be unreadable.\n\n"
                "Deliberately a z-score rather than the RBA's own gap-from-trend "
                "version of the same idea. The RBA publishes neither the filters "
                "it detrends with nor how it rescales each series, so that chart "
                "can only be read off, never rebuilt. This one has no such "
                "freedom: every term comes from the data and one date range, and "
                "the range is a control on screen.",
                "see core.employment", "employment")

        data = abs_series()
        emp_state = S.setdefault("employment", {})
        C.vintage(charts.Vintage(last_modified=data_asof(data)).line)

        # Comparison window first, in the same slot the FOMC version puts its
        # change window: it is the control that changes what the chart says,
        # and the z-score window below only changes what it is measured against.
        compare = st.segmented_control(
            "Compare against", COMPARE_WINDOWS, default="Prior year end",
            key="compare_window_labour", label_visibility="collapsed",
            help="The second dot on each row, and the direction of travel every "
                 "row's rule shows. The published version of this panel compares "
                 "the latest reading against the prior year end.") or "Prior year end"
        st.caption("Compare the latest reading against")

        prior = _compare_date(compare, data_asof(data))
        emp_state["prior_date"] = prior.isoformat()

        with st.expander("Scoring window \u2014 the average each z is measured against"):
            a, b = st.columns([1, 1])
            with a:
                w_start = st.number_input(
                    "Window start year",
                    value=int(emp_state.get("window_start") or 2000),
                    min_value=1978, max_value=TODAY.year - 5, step=1,
                    on_change=mark_dirty)
                emp_state["window_start"] = int(w_start)
            with b:
                w_end = st.number_input(
                    "Window end year",
                    value=int(emp_state.get("window_end") or 2020),
                    min_value=int(w_start) + 4, max_value=TODAY.year, step=1,
                    on_change=mark_dirty)
                emp_state["window_end"] = int(w_end)
            C.note(
                "2000\u20132020 by default: long enough to average over two cycles "
                "and stopping before the pandemic, which would otherwise put its "
                "own extremes into the yardstick every reading is measured against.")

        wstart, wend = date(int(w_start), 1, 1), date(int(w_end), 12, 31)
        manual = emp_state.setdefault("manual", {})

        rows = []
        for ind in empmod.INDICATORS:
            if ind.manual:
                m = manual.setdefault(ind.key, {})
                rows.append(empmod.manual_row(
                    ind, _num(m.get("value")), _num(m.get("mean")), _num(m.get("sd")),
                    _num(m.get("prior")), data_asof(data), prior))
            else:
                rows.append(empmod.score(ind, data.get(ind.key, []), prior,
                                         wstart, wend))
        panel = empmod.Panel(rows, prior, (wstart, wend))

        prior_label = f"{prior:%b %Y}"
        _lgh, _lghlp = st.columns([1, 0.06], vertical_alignment="center")
        with _lgh:
            C.legend([(f"{prior_label} — {compare.lower()}", P.muted),
                      ("Current", P.serious)])
        with _lghlp:
            C.formula_help(
                "",
                f"Muted dot is {prior_label}, the coloured dot the latest print, "
                f"and the rule between them the travel since. Right of zero is "
                f"tighter than the {w_start}–{w_end} average.",
                "", "employment-read")
        st.altair_chart(
            charts.full_employment(rows, panel.total_z, panel.total_prior_z,
                                   prior_label, P),
            width="stretch", theme=None)

        C.table(
            ["Indicator", "Last", "As of", "z now", f"z {prior_label}", "Change", "Source"],
            [[r.indicator.label,
              "\u2014" if not r.ok else f"{r.current.value:,.{r.indicator.decimals}f}{r.indicator.unit}",
              "\u2014" if not r.ok else f"{r.current.when:%b %Y}",
              "\u2014" if not r.ok else f"{r.current.z:+.2f}",
              "\u2014" if not r.ok or r.prior is None else f"{r.prior.z:+.2f}",
              "\u2014" if r.delta_z is None else f"{r.delta_z:+.2f}",
              r.indicator.source] for r in rows],
            numeric=(1, 3, 4, 5), nowrap=True)

        if panel.total_z is not None:
            c1, c2, c3 = st.columns(3)
            c1.metric("Total (mean z)", f"{panel.total_z:+.2f}",
                      None if panel.total_delta is None else f"{panel.total_delta:+.2f}",
                      help="Unweighted mean of every scored row. Above zero is a "
                           "labour market tighter than its own window average.")
            c2.metric("Tighter than average", f"{panel.n_tighter} of {panel.n_scored}",
                      help="Rows sitting right of zero — tighter than the "
                           f"{w_start}–{w_end} average for that series.")
            c3.metric("Tightening since " + prior_label,
                      f"{panel.n_tightening} of {panel.n_scored}",
                      help="Rows that moved toward less slack over the comparison "
                           "window, whichever side of zero they sit on.")
            C.note(
                "The Total is an **unweighted** mean, and it is reported with its row "
                "count because those two facts belong together. Unweighted because these "
                "indicators overlap heavily \u2014 underutilisation is literally "
                "unemployment plus underemployment \u2014 so any weighting would be a "
                "second undocumented judgement on top of the window choice.")

        stale = panel.oldest_reading
        if stale is not None and panel.as_of and stale.current.when < panel.as_of:
            # A note, not `C.vintage`: that renders the formatted release line
            # under a title, and this is prose about one row lagging the rest.
            C.note(
                f"Panel is only as current as its stalest row: {stale.indicator.label} "
                f"stands at {stale.current.when:%b %Y} against {panel.as_of:%b %Y} for the "
                "rest. ABS Labour Force Detailed has not moved past March 2026 because of "
                "the April survey changes \u2014 the same wall the RBA hits, which is why "
                "its own tightness graph greys that indicator out.")

        # ---- Capacity and flows -------------------------------------------
        # Two charts the indicator panel above cannot show: the panel is one
        # reading per series, and these are about the path.
        st.divider()
        hist = st.segmented_control(
            "History window", HISTORY_WINDOWS, default="10Y",
            key="hist_window_labour", label_visibility="collapsed",
            help="How much recent history to plot on the two charts below. The "
                 "indicator panel above is deliberately exempt — it scores each "
                 "reading against the full scoring window by design.")
        hist_start = _history_window_start(hist)
        st.caption("Show recent history — capacity and flow charts")

        # Last true data period for each series, carried into both legends so
        # the reader sees how fresh each line is.
        _monyy = lambda d: d.strftime("%b '%y")

        lr1, lr2 = st.columns(2, gap="medium")
        with lr1:
            capacity = manual_series(NAB_CAPACITY)
            capacity_ma = (trailing_mean(capacity, CAPACITY_MA_WINDOW)
                          if capacity else None)
            unemployment = data.get("unemployment", [])
            st.altair_chart(
                charts.capacity_vs_unemployment(
                    capacity, unemployment, P, hist_start,
                    charts.Vintage(last_modified=data_asof(data)),
                    capacity_ma=capacity_ma),
                width="stretch", theme=None)
            C.legend(([(f"NAB capacity utilisation, inverted, lhs "
                        f"(to {_monyy(capacity[-1][0])})", charts.MA_BLUE)]
                      if capacity else [])
                     + [(f"Unemployment rate, rhs"
                         + (f" (to {_monyy(unemployment[-1][0])})"
                            if unemployment else ""), P.categorical[7])])
            if not capacity:
                C.note(
                    "No capacity utilisation series is loaded. NAB's is "
                    "commercial with no public feed, so only the unemployment "
                    "rate is plotted.")
            else:
                C.note(
                    "Drawn to the CIO Analytics bench: fixed axes (utilisation "
                    "70.0–87.5 inverted on the left, unemployment 3–9 on the "
                    "right, which also carries the gridlines), and the labelled "
                    f"navy line is a trailing {CAPACITY_MA_WINDOW}-month moving "
                    "average of the raw monthly prints — the thin grey line "
                    "behind it. The grey series is digitised off the bench "
                    "chart for 2000–2018 (NAB's back-history is commercial) and "
                    "holds the official prints from Jan 2019 on; the navy moving "
                    "average spans the full series.")

        with lr2:
            flows = flow_series()
            # Last true data period for each series (before the visual 1q lead).
            f_lbl = f"Job-finding (fwd 1q, to {_monyy(flows['finding'][-1][0])})"
            s_lbl = f"Job-switching (fwd 1q, to {_monyy(flows['switching'][-1][0])})"
            w_lbl = f"Quarterly WPI growth (to {_monyy(flows['wages'][-1][0])})"
            fkeys = {f_lbl: flows["z_finding"],
                     s_lbl: flows["z_switching"],
                     w_lbl: flows["z_wages"]}
            order = [k for k, v in fkeys.items() if v]
            if not order:
                st.info("No flow data — the ABS gross-flows cube did not "
                        "download. See the Data tab.")
            else:
                # Fixed colours for the flows panel: WPI dark blue,
                # job-finding grey, job-switching light blue. Theme-aware so
                # each still reads on the dark surface.
                dark = P.mode == "dark"
                flow_colours = {
                    w_lbl: "#08203f" if not dark else "#5b9bd5",
                    f_lbl: "#8a8a8a" if not dark else "#a8a8a8",
                    s_lbl: "#6baed6" if not dark else "#9ecbff",
                }
                # WPI solid; the two flow measures dotted so they read as the
                # secondary series against the dark reference line.
                flow_dashes = {
                    w_lbl: [1, 0],
                    f_lbl: [2, 3],
                    s_lbl: [2, 3],
                }
                st.altair_chart(
                    charts.labour_flows(fkeys, order, P, hist_start,
                                        charts.Vintage(last_modified=data_asof(data)),
                                        colors=flow_colours, dashes=flow_dashes),
                    width="stretch", theme=None)
                C.legend([(n, flow_colours.get(n, P.categorical[i]))
                          for i, n in enumerate(order)])
                C.note(
                    "The flow measures are plotted one quarter AHEAD of their "
                    "true month, so each flow reading lines up with the wage "
                    "print it precedes: a worker who moves in March negotiates "
                    "a pay rate that lands in the June index. Job-switching is a "
                    "proxy — Australia publishes no quits rate, so this counts "
                    "everyone under a year with their current employer, which "
                    "includes people who came from unemployment rather than "
                    "from another job.")

        with st.expander("Series with no free feed"):
            C.note(
                "Three of the nine are commercial \u2014 NAB's business survey and "
                "ANZ-Indeed \u2014 with no public series to score. They take a typed "
                "reading plus the window mean and standard deviation to score it against. "
                "A **z cannot be typed directly on purpose**: nothing on screen would show "
                "what it had been measured against, and it would not move when the reading "
                "did.")
            for ind in empmod.INDICATORS:
                if not ind.manual:
                    continue
                m = manual.setdefault(ind.key, {})
                st.markdown(f"**{ind.label}** \u2014 {ind.source}")
                q1, q2, q3, q4 = st.columns(4)
                for col, key, lbl in ((q1, "value", "Latest"),
                                      (q2, "prior", prior_label),
                                      (q3, "mean", f"{w_start}\u2013{w_end} mean"),
                                      (q4, "sd", "window sd")):
                    with col:
                        raw = st.text_input(lbl, value=str(m.get(key) or ""),
                                            key=f"emp_{ind.key}_{key}",
                                            on_change=mark_dirty)
                        m[key] = raw.strip() or None

        if panel.missing:
            st.caption("Not scored: " + ", ".join(
                f"{r.indicator.label} ({r.error})" for r in panel.missing))

    # -------------------------------------------------------- 5. Inflation
    elif section == "Inflation":
        ihdr, ihlp = st.columns([1, 0.06], vertical_alignment="center")
        with ihdr:
            C.section("Inflation",
                      "Five readings of one CPI. The headline is the target; "
                      "these say whether it is a monetary problem.")
        with ihlp:
            C.formula_help(
                r"\text{annualised}_t = 100\left[\left(\frac{I_t}{I_{t-1}}"
                r"\right)^{4} - 1\right] \qquad "
                r"\text{breadth} = \frac{\sum_{i\,:\,\pi_i > k} w_i}{\sum_i w_i}",
                "A quarterly change compounded to a yearly pace, then the share "
                "of the basket above a threshold — counted two ways, once with "
                "every expenditure class equal and once with each carrying its "
                "own CPI weight. Compounded rather than multiplied by four: the "
                "breadth charts count items against a fixed threshold, so a "
                "systematic bias of the wrong sign moves items across the line "
                "and changes the count.",
                "see core.inflation", "inflation")

        cpi = cpi_series()
        buckets, cyclical = cpi_classification()
        # The vintage line answers the two questions a pre-meeting reader has:
        # which quarter am I looking at, and when does it stop being the latest
        # word. The next date comes off the same rule-derived calendar the
        # Monte Carlo prices release days from.
        _next_cpi = next((e.when for e in econ_calendar.recurring_events(
            TODAY, TODAY + timedelta(days=200))
            if e.release == econ_calendar.CPI_Q), None)
        vintage = charts.Vintage(next_release=_next_cpi,
                                 last_modified=cpi["as_at"])

        if not cpi["leaves"]:
            st.warning("No CPI data. The ABS workbooks did not download — the "
                       "Data tab has the detail and a refresh button.")
        else:
            n_leaf = len(cpi["leaves"])
            resid = cpi["residual"]
            st.caption(
                f"{n_leaf} expenditure classes, recovered from the ABS "
                f"contribution hierarchy at {cpi['as_at']:%b %Y} "
                f"(residual {resid:+.2f} index points of "
                f"{cpi['contribution'].get('All groups CPI', 0):.2f}). "
                "Weights are the latest published basket held fixed back "
                "through history."
            )

            window = st.segmented_control(
                "History window", HISTORY_WINDOWS, default="10Y",
                key="hist_window_inflation", label_visibility="collapsed",
                help="How much recent history to show. Applies to every chart "
                     "on the tab. `All` reaches back to 1990 on the breadth "
                     "charts and to the start of each series on the rest.")
            start_d = _history_window_start(window)
            st.caption("Show recent history")

            classes, weights = cpi["classes"], cpi["weights"]

            # Row 1 -- how much of the basket is hot, and what is doing it.
            r1a, r1b = st.columns(2, gap="medium")
            with r1a:
                min_live = int(0.8 * len(cpi["leaves"]))
                pts = infl.breadth(classes, weights, infl.ABOVE_BAND, 1,
                                   min_live)
                st.altair_chart(
                    charts.cpi_above_threshold(pts, P, infl.ABOVE_BAND,
                                               start_d, vintage),
                    width="stretch", theme=None)
                C.legend([("By number of items", P.categorical[0]),
                          ("By weight of price categories", P.categorical[1])])
            with r1b:
                if not buckets:
                    st.info("No bucket classification — see "
                            "`meetings/_cpi_classification.json`.")
                else:
                    grouped = infl.bucket_contributions(weights, buckets,
                                                        cpi["leaves"])
                    pp = infl.contribution_to_change(
                        grouped, infl.sub_index(weights, cpi["leaves"]))
                    order = [b.name for b in buckets]
                    if any(pp.get(infl.RESIDUAL)):
                        order.append(infl.RESIDUAL)
                    st.altair_chart(
                        charts.cpi_composition(pp, cpi["headline_q"], order, P,
                                               start_d, vintage),
                        width="stretch", theme=None)
                    # Seven buckets plus the CPI line is past what a Vega legend
                    # can hold without taking the plot's height with it, so the
                    # key is rendered here -- the same treatment the FOMC
                    # version gives its payroll-components chart.
                    C.legend([(n, P.categorical[i]) for i, n in enumerate(order)]
                             + [("CPI, % q/q", P.ink)])

            # Row 2 -- the measures the Board actually targets, and breadth
            # against the trimmed mean over the full history.
            r2a, r2b = st.columns(2, gap="medium")
            with r2a:
                q = "Quarterly trimmed mean CPI"
                m = "Monthly trimmed mean CPI"
                x = "Monthly CPI ex volatiles and holiday travel"
                st.altair_chart(
                    charts.cpi_underlying(
                        {q: cpi["trimmed_yr"], m: cpi["monthly_trimmed_yr"],
                         x: cpi["monthly_ex_volatiles_yr"]},
                        [q, m, x], P, start_d, vintage),
                    width="stretch", theme=None)
                C.legend([(q, P.categorical[0]), (m, P.categorical[1]),
                          (x, P.categorical[2]), ("2–3% target band", P.grid)])
            with r2b:
                wide = infl.breadth(classes, weights, infl.ABOVE_MIDPOINT, 1,
                                    int(0.8 * len(cpi["leaves"])))
                share = [(pt.when, pt.share_by_weight) for pt in wide]
                lo, hi = date(1993, 1, 1), date(2019, 12, 31)
                st.altair_chart(
                    charts.cpi_breadth(
                        share, cpi["trimmed_q"],
                        infl.mean_over(share, lo, hi),
                        infl.mean_over(cpi["trimmed_q"], lo, hi),
                        "1993–2019", P, start_d, vintage),
                    width="stretch", theme=None)
                C.legend([("% of basket rising > 2.5% annualised", P.categorical[1]),
                          ("Trimmed mean, % q/q", P.categorical[0]),
                          ("1993–2019 average", P.muted)])

            # Row 3 -- the classification worth arguing with, on its own row so
            # the caveat under it is readable rather than squeezed.
            r3a, r3b = st.columns(2, gap="medium")
            with r3a:
                if not cyclical:
                    st.info("No cyclical classification — see "
                            "`meetings/_cpi_classification.json`.")
                else:
                    split = infl.cycle_split(weights, cyclical, cpi["leaves"])
                    st.altair_chart(
                        charts.cpi_cycle(split, P, start_d, vintage),
                        width="stretch", theme=None)
                    C.legend([
                        (f"'Cyclical' — {split.n_cyclical} classes",
                         P.categorical[0]),
                        (f"'Non-cyclical' — {split.n_non_cyclical} classes",
                         P.categorical[1])])
            with r3b:
                st.markdown(
                    "**What is a judgement here, and what is not.**\n\n"
                    "The breadth counts and the underlying measures are "
                    "arithmetic — a threshold, a count, a weight, and the "
                    "ABS's own trim. Nothing to argue with.\n\n"
                    "The composition buckets and the cyclical split are "
                    "**not published series**. They are analytical "
                    "classifications, different houses draw them differently, "
                    "and they live in `meetings/_cpi_classification.json` as "
                    "data you can edit. Any class no bucket claims lands in a "
                    "residual rather than disappearing, so a gap shows up as "
                    "a fat *Other* instead of a quietly short bar.")
                st.caption(
                    "Quarterly series are seasonally adjusted (ABS Appendix "
                    "1a); the monthly measures start in 2024 because the "
                    "complete monthly CPI does. The breadth chart is drawn as "
                    "two stacked panels rather than the source's dual axis — "
                    "with two y-scales the crossings are an artefact of where "
                    "the axes were pinned.")

    # ------------------------------------------------------- 6. Vote count
    elif section == "Vote count":
        C.section("Vote arithmetic", "A bloc of dissenters is not a decision.")
        a = M.arithmetic
        c1, c2, c3 = st.columns(3)
        c1.metric("Board size", a.total_voters)
        c2.metric("Votes needed", a.votes_needed)
        c3.metric("Movers identified", a.mover_count)

        st.warning(
            "**The RBA never attributes a vote.** Since the 2025 reforms the Board publishes "
            "an unattributed count — \"decided by six votes to three\" — and no member is ever "
            "named. Unlike the FOMC version of this framework, the roster below can never be "
            "scored against a published tally. It encodes *your* read, and that read is doing "
            "all the work.")

        roster_rows = store.roster_for(S)
        if not any(r.get("name") for r in roster_rows[2:]):
            st.info(
                "The roster is seeded with the nine statutory seats but only the two executive "
                "names are filled, and every score is a neutral 2.5 placeholder. This app does "
                "not assert policy views for people it has not sourced. Fill the names from "
                "rba.gov.au and set the scores from each member's own speeches.")

        edited = st.data_editor(
            [{"Member": r.get("name", ""), "Role": r.get("role", ""),
              "Bloc": r.get("bloc", ""), "Hawk-dove": float(r.get("score", 2.5)),
              "Mover": bool(r.get("is_mover"))} for r in roster_rows],
            column_config={
                "Hawk-dove": st.column_config.NumberColumn(
                    min_value=0.0, max_value=5.0, step=0.1,
                    help="5 = most hawkish. Your read, from the member's own speeches."),
                "Mover": st.column_config.CheckboxColumn(
                    help="Counted in P(bloc moves), whatever their bloc."),
            },
            hide_index=True, width="stretch", key="roster_editor")
        if edited != st.session_state.get("_roster_snapshot"):
            st.session_state["_roster_snapshot"] = edited
            S["roster"] = [
                {**old, "name": row["Member"], "role": row["Role"], "bloc": row["Bloc"],
                 "score": float(row["Hawk-dove"]), "is_mover": bool(row["Mover"])}
                for old, row in zip(roster_rows, edited)]
            mark_dirty()
        C.note(
            "Editable, because the seeded file asks you to fill it and a read-only table "
            "could not honour that. Changes land in this meeting's own roster on Save, so "
            "one meeting's read never silently rewrites another's.")

        if M.priced:
            st.divider()
            C.section("EV given the bloc has already voted for the move")
            C.table(["P(centre joins)", "EV", ""],
                    [[f"{r.p_centre:.0%}", f"{r.ev:+.2f}bp",
                      "breakeven" if r.is_breakeven else ""] for r in M.conditional])
            C.note(
                "The breakeven is `points / size` again — identical to the unconditional one. "
                "That invariance is a property of the payoff, not of the scenario.")

    # -------------------------------------------------------------- 6. Data
    elif section == "Data":
        C.section("Data", "Every external source this app touches, in one place. "
                          "Nothing here can block the Verdict \u2014 every fetch degrades "
                          "to blank on failure, and any value can be typed by hand.")

        if st.button("Refresh all sources", width="stretch"):
            # Both layers, or nothing actually refetches: the Streamlit memo in
            # front, the disk cache and the in-process table memo behind it.
            for fn in (fetch_ib, fetch_ir, fetch_rba_table, rba_spot, bbsw_history,
                       kill_series_history, policy_changes, abs_series,
                       cpi_series, cpi_classification, flow_series):
                fn.clear()
            rba._table_memo.cache_clear()
            absmod.latest_release.cache_clear()
            datacache.clear()
            st.rerun()

        ib_ok = bool([q for q in ib_quotes if q.ok])
        ir_ok = bool([q for q in ir_quotes if q.ok])
        rba_ok = spot["cash"] is not None
        C.data_source_header(
            "ASX interest rate futures (Markit)", ib_ok and ir_ok,
            f"IB {len(ib_quotes)} contracts, IR {len(ir_quotes)} contracts. "
            f"Settled {ib_ref or 'unknown'}.", P)
        C.data_source_header(
            "RBA statistical table F1", rba_ok,
            f"Cash rate {spot['cash']}%, 3-month BBSW {spot['bbsw']}%, "
            f"AONIA gap {spot['gap_bp']:+.1f}bp." if rba_ok else "unavailable", P)

        _abs = abs_series()
        _lf_period, _lfd_period = (_abs.get("_periods") or ["", ""])[:2]
        _abs_ok = bool(_abs.get("unemployment"))
        C.data_source_header(
            "ABS Labour Force (Excel time series)", _abs_ok,
            f"headline release {_lf_period or 'unknown'}, detailed release "
            f"{_lfd_period or 'unknown'}." if _abs_ok else "unavailable", P)

        _flows = flow_series()
        _flows_ok = bool(_flows.get("finding"))
        C.data_source_header(
            "ABS Labour Force gross flows (LMS1)", _flows_ok,
            f"release {_flows['period'] or 'unknown'}, job-finding rate over "
            f"{len(_flows['finding'])} quarters."
            if _flows_ok else "unavailable", P)

        _cpi = cpi_series()
        _cpi_ok = bool(_cpi.get("leaves"))
        C.data_source_header(
            "ABS Consumer Price Index (Excel time series)", _cpi_ok,
            f"release {_cpi['period'] or 'unknown'}, {len(_cpi['leaves'])} "
            f"expenditure classes at {_cpi['as_at']:%b %Y}."
            if _cpi_ok else "unavailable", P)

        C.note(
            "No API key is needed for either source, which is a real difference from the US "
            "version of this tool: it needs a FRED key and degrades without one. The trade-off "
            "is that the ASX endpoint is undocumented — it is the JSON behind the exchange's "
            "own price pages, not a published API — so the last good strip is cached to disk "
            "and served if it ever disappears.")

        st.divider()
        C.section("IB strip", "30 day interbank cash rate futures.")
        C.table(["Contract", "Settlement", "Implied rate", "Chg", "Bid/ask", "Volume", "Flag"],
                [[q.code, f"{q.price:.3f}" if q.price else "—",
                  f"{q.implied_rate:.3f}%" if q.implied_rate else "—",
                  "—" if q.change_bp is None else f"{q.change_bp:+.1f}bp",
                  "—" if q.spread_bp is None else f"{q.spread_bp:.1f}bp",
                  f"{q.volume:,.0f}" if q.volume else "0",
                  "stale" if asx.is_stale(q, ib_ref) else ""] for q in ib_quotes])
        C.note(
            "The **settlement** column is ASX's official daily mark, and it is deliberately "
            "used in preference to the last traded price. On this strip the last trade can be "
            "weeks old and up to 17bp away from settlement — reading the curve off it would "
            "put most of a rate move of pure staleness into every implied probability. "
            "Contracts flagged stale are quoted but not trading; they stay in the ladder and "
            "out of the headline numbers.")

        st.divider()
        C.section("IR strip", "90 day bank accepted bill futures.")
        C.table(["Contract", "Settlement", "Implied BBSW", "Bid/ask", "Volume", "Flag"],
                [[q.code, f"{q.price:.3f}" if q.price else "—",
                  f"{q.implied_rate:.3f}%" if q.implied_rate else "—",
                  "—" if q.spread_bp is None else f"{q.spread_bp:.1f}bp",
                  f"{q.volume:,.0f}" if q.volume else "0",
                  "stale" if asx.is_stale(q, ir_ref) else ""] for q in ir_quotes])

        st.divider()
        C.section("Discontinued series", "Named so an empty panel is never a mystery.")
        C.table(["Series", "Label", "Last published"],
                [[s.code, s.label, s.discontinued.isoformat()]
                 for s in rba.CATALOGUE if not s.live])
        C.note(
            "The RBA retired its OIS series in December 2022. That is why the live BBSW basis "
            "on the Curve tab is derived from the IB strip rather than read off a published "
            "OIS quote — an IB strip *is* an OIS curve, and a tradeable one.")

        st.divider()
        C.section("ABS labour series", "What the full-employment panel is scored from.")
        C.table(["Series", "Observations", "From", "To"],
                [[empmod.BY_KEY[k].label, f"{len(v):,}",
                  f"{v[0][0]:%b %Y}", f"{v[-1][0]:%b %Y}"]
                 for k, v in _abs.items() if not k.startswith("_") and v],
                numeric=(1,))
        C.note(
            "The two ABS releases are **not on the same month**. Labour Force is at "
            f"{_lf_period or '?'}; Labour Force Detailed, which is the only route to a "
            f"duration-based medium-term unemployment rate, is at {_lfd_period or '?'} "
            "after the April 2026 survey changes. `data.abs` reads both periods off the "
            "ABS landing pages rather than deriving them from today's date, because a "
            "hardcoded path rots every month and the two would drift apart silently.")

        st.divider()
        C.section("Labour market flows",
                  "What the second Labour chart is built from.")
        _cap = manual_series(NAB_CAPACITY)
        C.table(["Series", "Observations", "From", "To"],
                [[label, f"{len(v):,}", f"{v[0][0]:%b %Y}", f"{v[-1][0]:%b %Y}"]
                 for label, v in [
                     ("Job-finding rate (quarterly)", _flows["finding"]),
                     ("Job-switching rate (quarterly)", _flows["switching"]),
                     ("WPI, % q/q (RBA H4)", _flows["wages"]),
                     (f"{NAB_CAPACITY} (digitised)", _cap)] if v]
                or [["nothing loaded", "0", "—", "—"]],
                numeric=(1,))
        C.note(
            "The gross-flows cube is a 100MB download and a million rows, so it "
            "is aggregated to national totals during the parse and only the "
            "aggregate is cached — 228 months by 16 status pairs, refreshed "
            "once a month behind the release-keyed disk cache. NAB capacity "
            "utilisation is commercial with no public feed; its series is held "
            "in meetings/_manual_series.json (digitised off the bench for "
            "2000–2018, with the official prints from Jan 2019 on) and read by "
            "the Labour tab. The dark-blue line on the Labour tab is a moving "
            "average of that capacity series, derived in-app across the full "
            "history.")

        st.divider()
        C.section("ABS CPI series",
                  "What the Inflation tab is built from, and the hierarchy it "
                  "recovers from them.")
        if not _cpi_ok:
            st.caption("The CPI workbooks did not download.")
        else:
            _root = _cpi["contribution"].get("All groups CPI")
            C.table(["Series", "Observations", "From", "To"],
                    [[label, f"{len(v):,}", f"{v[0][0]:%b %Y}", f"{v[-1][0]:%b %Y}"]
                     for label, v in [
                         ("Expenditure classes, quarterly SA",
                          next(iter(_cpi["classes"].values()), [])),
                         ("Trimmed mean, % q/q", _cpi["trimmed_q"]),
                         ("Trimmed mean, year-ended", _cpi["trimmed_yr"]),
                         ("All groups SA, % q/q", _cpi["headline_q"]),
                         ("Trimmed mean, monthly year-ended",
                          _cpi["monthly_trimmed_yr"]),
                         ("Ex volatiles and holiday travel, monthly",
                          _cpi["monthly_ex_volatiles_yr"])] if v],
                    numeric=(1,))
            st.caption(
                f"Hierarchy: {len(_cpi['order'])} series in the workbook resolve "
                f"to {len(_cpi['leaves'])} expenditure classes and "
                f"{len(_cpi['order']) - len(_cpi['leaves'])} aggregates. Leaf "
                f"contributions sum to {(_root or 0) + (_cpi['residual'] or 0):.2f} "
                f"against a published All groups CPI of {_root:.2f} — a residual "
                f"of {_cpi['residual']:+.2f} index points, which is the ABS's own "
                f"rounding.")
            with st.expander(f"The {len(_cpi['leaves'])} expenditure classes"):
                st.caption(", ".join(_cpi["leaves"]))
        C.note(
            "The classes are **not a hardcoded list**. The ABS flattens four "
            "levels of hierarchy into one ordered column block with nothing "
            "marking depth, so counting every series would count Bread once on "
            "its own and again inside Bread and cereal products. `core.inflation` "
            "recovers the tree from the contributions instead — a parent's "
            "contribution is the sum of its children's — which self-corrects "
            "when the ABS moves a class, as the April 2026 Labour Force renaming "
            "is a live reminder they do.")

        st.divider()
        C.section("Releases between now and the decision")
        events = [e for e in M.calendar_events if TODAY <= e.when <= M.meeting.end]
        if events:
            C.table(["Date", "Event", "Tier", "Source"],
                    [[f"{e.when:%a %d %b}", e.label, e.weight, e.source] for e in events])
        else:
            st.caption("Nothing scheduled between today and the decision.")


# ----------------------------------------------------------- verdict rail

with RIGHT:
    st.markdown("#### Verdict")
    if not M.priced:
        st.caption("Enter the points priced to see the verdict.")
    else:
        s = M.summary
        rows = [
            C.verdict_row("Implied", f"{s.implied:.1%}"),
            C.verdict_row("Your q", f"{s.q:.1%}", P.accent),
            C.verdict_row("Breakeven", f"{s.breakeven:.1%}"),
            C.verdict_row("Margin", f"{s.margin:+.1%}", pnl_colour(s.margin, P)),
            C.verdict_row("EV", f"{s.ev:+.2f}bp", pnl_colour(s.ev, P), emph=True),
            C.verdict_row("Risk/reward", f"{s.rr:.2f} : 1"),
        ]
        if M.sizing_ladder is not None and M.sizing_ladder.ok:
            r = M.sizing_ladder.selected
            rows.append(C.verdict_row("Size", f"{r.contracts:,.0f} lots"))
            rows.append(C.verdict_row("At risk", f"A${r.max_loss:,.0f}",
                                      None if r.within_daily_limit else P.negative))
        st.markdown("".join(rows), unsafe_allow_html=True)

        verdict = ("ON" if s.ev > 0 else "OFF")
        st.markdown(
            f"<div style='margin-top:.6rem;font-size:1.05rem;font-weight:700;"
            f"color:{pnl_colour(s.ev, P)}'>Trade is {verdict}</div>",
            unsafe_allow_html=True)
        st.caption(
            f"{'Fading' if M.side == 'fade' else 'Backing'} a {M.size:g}bp "
            f"{M.direction} at {M.points:g}bp.")

    # The market's own answer, next to yours.
    step = PATH.step_for(M.meeting.key) if PATH.ok else None
    if step is not None:
        st.divider()
        st.markdown("#### The strip says")
        st.markdown("".join([
            C.verdict_row("Priced step", f"{step.step_bp:+.1f}bp"),
            C.verdict_row("Implied prob", f"{step.prob:.1%}"),
            C.verdict_row("From", f"{step.code} ({'clean' if step.is_clean else 'inverted'})"),
        ]), unsafe_allow_html=True)
        if M.priced and abs(step.step_bp - M.points) > 1.0:
            st.caption(
                f"You have typed {M.points:g}bp; the strip prices {step.step_bp:+.1f}bp. "
                "One of the two is out of date.")
        if step.stale:
            st.caption("Solved off a contract that is not trading — treat with care.")

    if M.warnings:
        st.divider()
        for w in M.warnings:
            st.warning(w)

    st.divider()
    md = report.render(M)
    st.download_button("Export Markdown", md,
                       file_name=f"rba-{M.meeting.key}.md",
                       mime="text/markdown", width="stretch")
    if st.button("Save to runs/", width="stretch",
                 help="Keeps a timestamped copy of this report alongside the repo."):
        out = store.save_run(key, md, datetime.now().strftime("%Y%m%d-%H%M%S"))
        st.success(f"Written to {out.relative_to(ROOT)}")
