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
from core import path as pathmod
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

@st.cache_resource(show_spinner=False)
def _fetch_clock() -> dict[str, datetime]:
    """Wall-clock time of the last real fetch. cache_data's own cache is
    process-wide, not per-session -- session_state would only ever show the
    time for whichever tab happened to trigger the miss, so the clock has to
    be shared too."""
    return {}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_ib():
    _fetch_clock()["ib"] = datetime.now()
    return asx.fetch_strip_cached("ib")


@st.cache_data(ttl=300, show_spinner=False)
def fetch_ir():
    _fetch_clock()["ir"] = datetime.now()
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
        # ?meeting=YYYY-MM-DD wins, so a meeting can be bookmarked or linked.
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
        # "As of" always opens on today, never on whatever a PAST session last
        # saved -- a saved meeting is a live analysis you keep reopening, not a
        # frozen snapshot, and every downstream number (blackout, days to
        # meeting, the Monte Carlo window, vol lookback) should move forward
        # with the calendar by default. Only overridden here, at the one moment
        # state is freshly loaded and no widget has rendered yet for it -- from
        # here on the "As of" widget below owns it for the rest of the session,
        # so picking an explicit date (to review a meeting as it looked on a
        # past day) sticks until the next fresh load.
        st.session_state["state"]["as_of"] = TODAY.isoformat()
        st.session_state["dirty"] = False

    S = st.session_state["state"]
    trade = S.setdefault("trade", {})
    prob = S.setdefault("probability", {})
    sizing_state = S.setdefault("sizing", {})
    pth = S.setdefault("path", {})
    if S.pop("_recovered", False):
        st.warning("The saved file for this meeting was unreadable and has been "
                   "replaced with a fresh one. Nothing was overwritten on disk "
                   "until you press Save.")
    if S.get("cloned_from"):
        st.caption(f"Started from {S['cloned_from']}. Market and path inputs cleared.")
    elif key not in saved_keys:
        st.caption("Unsaved. Roster seeded from the standing Board.")

    as_of = st.date_input("As of",
                          value=date.fromisoformat(S.get("as_of") or TODAY.isoformat()),
                          on_change=mark_dirty)
    S["as_of"] = as_of.isoformat()

    st.divider()
    st.markdown("#### Trade")
    c1, c2 = st.columns(2)
    with c1:
        trade["direction"] = st.selectbox(
            "Event", ["hike", "cut"],
            index=0 if trade.get("direction", "hike") == "hike" else 1,
            on_change=mark_dirty)
    with c2:
        sides = ["fade", "back"]
        trade["side"] = st.selectbox(
            "Side", sides, index=sides.index(trade.get("side", "fade")),
            help="Fade = sell the event. Back = buy it.", on_change=mark_dirty)
    c3, c4 = st.columns(2)
    with c3:
        # Left empty (None) rather than defaulted to 0 -- a blank meeting is
        # "not priced yet", which is a different statement from "priced at zero".
        trade["points"] = st.number_input(
            "Points priced (to enter)",
            value=float(trade["points"]) if trade.get("points") is not None else None,
            step=0.5, format="%.2f", placeholder="not priced", on_change=mark_dirty)
    with c4:
        # min_value matters: implied probability is points/size, so a zero or
        # negative size has no meaning and used to raise straight out of
        # `model.build` into a red traceback.
        trade["size"] = st.number_input(
            "Move size (bp)", value=float(trade.get("size") or 25.0),
            step=5.0, format="%.0f", min_value=1.0, on_change=mark_dirty)

    st.divider()
    st.markdown("#### Sizing")
    # Short labels and paired rows: the sidebar is ~300px, and the full
    # explanations belong in the tooltips rather than wrapping over three lines.
    z1, z2 = st.columns(2)
    with z1:
        sizing_state["max_drawdown"] = st.number_input(
            "Max DD (A$)",
            value=float(sizing_state.get("max_drawdown")
                        or sizingmod.DEFAULT_MAX_DRAWDOWN),
            min_value=0.0, step=25_000.0, format="%.0f", on_change=mark_dirty,
            help="The Kelly bankroll: the drawdown you can survive, not the daily "
                 "limit. This sets the position; the daily limit only vetoes it.",
        )
    with z2:
        sizing_state["daily_limit"] = st.number_input(
            "Daily cap (A$)",
            value=float(sizing_state.get("daily_limit")
                        or sizingmod.DEFAULT_DAILY_LIMIT),
            min_value=0.0, step=10_000.0, format="%.0f", on_change=mark_dirty,
            help="A hard cap, not a sizing input. Rungs risking more than this are "
                 "flagged as a breach rather than quietly shrunk to fit.",
        )

    # The same contracts the Curve tab quotes, nearest delivery first, so the
    # dropdown cannot offer an instrument the strip has no price for. Anchored
    # on the as-of rather than the wall clock: which contract is the front
    # month is a question about a DATE, and reviewing a past meeting should
    # offer the strip that was live then, not the one live today.
    universe = sizingmod.contract_universe(as_of) + [sizingmod.CUSTOM_CONTRACT]
    by_key = {c.key: c for c in universe}
    prior_contract = sizing_state.get("contract")
    if prior_contract not in by_key:
        # Legacy "ib"/"ir" family keys -- which is what a freshly seeded meeting
        # carries -- or a month that has rolled off the strip: keep the family
        # and roll to its nearest live contract. An unset or unrecognised key
        # defaults to the front interbank contract, never to Custom: Custom
        # means "I typed a DV01", not "nothing was saved".
        kind = (sizingmod.contract_for(prior_contract, as_of).kind
                if prior_contract else "ib")
        if kind == "custom":
            kind = "ib"
        prior_contract = next(
            (c.key for c in universe if c.kind == kind), universe[0].key)
    contract_keys = list(by_key)
    z3, z4 = st.columns([2, 1])
    with z3:
        sizing_state["contract"] = st.selectbox(
            "Contract", contract_keys, index=contract_keys.index(prior_contract),
            format_func=lambda k: by_key[k].label, on_change=mark_dirty,
            help="The strip's own contracts, nearest delivery first. Sets the DV01 "
                 "that turns dollar risk into a lot count: IB A$24.66/bp, and IR "
                 "moves with the yield because a bank bill is discount-priced.",
        )
    with z4:
        sizing_state["bets_per_year"] = st.number_input(
            "Bets/yr", value=int(sizing_state.get("bets_per_year")
                                 or sizingmod.DEFAULT_BETS_PER_YEAR),
            min_value=1, max_value=60, step=1, on_change=mark_dirty,
            help="Eight scheduled RBA meetings. Only annualises the per-bet growth.",
        )
    if sizing_state["contract"] == "custom":
        sizing_state["custom_dv01"] = st.number_input(
            "DV01 per lot (A$/bp)",
            value=float(sizing_state.get("custom_dv01") or 24.66),
            min_value=0.0, step=1.0, format="%.2f", on_change=mark_dirty)
    # IR's DV01 moves with the yield level, so the caption reads the live one
    # off the selected contract where there is one -- the same throwaway
    # overlay the model is handed below, not written back into `S`.
    _cap_yield = None
    if sizing_state["contract"].startswith("ir:"):
        _cap_month = sizing_state["contract"].partition(":")[2]
        # `fetch_ir` is memoised, so reaching for the strip here costs nothing
        # beyond the lookup the model section does again a few lines below.
        _cap_q = next((q for q in fetch_ir()
                       if q.ok and f"{q.month:%Y-%m}" == _cap_month), None)
        _cap_yield = _cap_q.implied_rate if _cap_q is not None else None
    dv01 = sizingmod.dv01_for(sizing_state["contract"],
                              sizing_state.get("custom_dv01"), _cap_yield)
    st.caption(f"**A${dv01:,.2f}** per bp per lot")

    trade["kelly_fraction"] = st.select_slider(
        "Kelly fraction", options=list(sizingmod.DEFAULT_FRACTIONS),
        value=sizingmod.snap_fraction(trade.get("kelly_fraction")),
        format_func=sizingmod.fraction_label, on_change=mark_dirty,
        help="Drives the Verdict panel. Step 5 costs every rung out in lots and "
             "dollars so this is a decision, not a default.",
    )

    st.divider()
    st.markdown("#### Your probability")
    mode = st.radio(
        "Mode", ["decomposition", "manual"],
        index=0 if prob.get("mode", "decomposition") == "decomposition" else 1,
        horizontal=True, on_change=mark_dirty,
        help="Decomposition: q comes from the two sliders below, multiplied "
             "together, and the number field is hidden. Manual: you type q "
             "directly instead and the sliders are hidden.",
    )
    prob["mode"] = mode
    # Exactly one input is ever shown: the sliders in decomposition mode, the
    # number field in manual mode. Showing both invited the two to disagree
    # silently; showing one makes it unambiguous which number is driving q.
    if mode == "decomposition":
        # Sliders work in whole percent -- a 0-1 float with a "%d%%" format
        # renders 0.25 as "0%", which is exactly the kind of unit slip this
        # app exists to avoid.
        prob["p_bloc"] = st.slider(
            "P(bloc votes for the move)", 0, 100,
            int(round((prob.get("p_bloc") or 0.25) * 100)), 1, format="%d%%",
            on_change=mark_dirty,
            help="How likely the move candidates actually vote for the move -- not "
                 "just write it into the statement's wording.",
        ) / 100
        prob["p_centre"] = st.slider(
            "P(centre joins | bloc moves)", 0, 100,
            int(round((prob.get("p_centre") or 0.40) * 100)), 1, format="%d%%",
            on_change=mark_dirty,
            help="Even if the move candidates DO vote yes, they do not have enough "
                 "votes alone to carry it -- they need enough of the rest of the "
                 "Board (the centre) to join them. This is how likely that happens, "
                 "given the candidates already voted yes.",
        ) / 100
        derived = prob["p_bloc"] * prob["p_centre"]
        st.caption(f"Decomposition: {prob['p_bloc']:.0%} × {prob['p_centre']:.0%} "
                   f"= **{derived:.1%}**")
        # Kept in sync silently rather than left to drift -- if you switch to
        # manual afterwards, the field starts from where decomposition left off
        # instead of some stale number from a previous session.
        prob["override_q"] = derived
    else:
        derived = (prob.get("p_bloc") or 0.25) * (prob.get("p_centre") or 0.40)
        prob["override_q"] = st.number_input(
            "Recorded estimate q (%)",
            value=float(round((prob.get("override_q")
                               if prob.get("override_q") is not None
                               else derived) * 100, 2)),
            min_value=0.0, max_value=100.0, step=1.0, format="%.2f",
            on_change=mark_dirty,
            help="Your overall probability the move happens -- typed directly.",
        ) / 100

    st.divider()
    dirty = st.session_state.get("dirty", False)
    if st.button("Save meeting" + (" ●" if dirty else ""),
                 type="primary" if dirty else "secondary", width="stretch"):
        store.save(key, S)
        st.session_state["dirty"] = False
        st.success(f"Saved meetings/{key}.json")

    if key in saved_keys and st.button("Reload from disk", width="stretch"):
        st.session_state["state"] = store.load(key)
        st.session_state["dirty"] = False
        st.rerun()


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
        model_state, as_of=as_of,
        rba_values=kill_values, rba_units=kill_units,
        all_meetings=MEETINGS,
        rate_history=bbsw_history(),
        rate_symbol="3-month BBSW (RBA F1)",
        policy_changes=policy_changes(),
    )
    return M


M = rebuild()

# The priced path, and the bill curve laid against it.
#
# The anchor is derived from the strip, but the Curve tab lets it be typed
# over -- so the override is read here, before the ladder is built, rather
# than inside the tab. Streamlit reruns top to bottom and a widget's value is
# already in session_state by the time the script restarts, so reading it up
# here costs no lag; building the ladder inside the tab instead would leave
# the Verdict rail and its "strip says" panel a rerun behind the typed spot.
#
# The key carries the meeting in its name rather than being reset on a change:
# a spot left over from another meeting's anchor silently skews every step on
# the ladder, and a per-meeting key makes that impossible by construction.
# Nothing here ever WRITES that key -- the widget owns it outright. Seeding a
# widget's key and omitting `value=` is the pattern the US version uses, and it
# silently broke in Streamlit 1.61: the input renders 0.0000 while the ladder
# below is built off the real derived spot. Passing the derived number as
# `value=` and letting the widget own its key works on both.
strip_spot = stripmod.implied_spot(ib_quotes, MEETINGS, as_of=as_of)
spot_derived = strip_spot if strip_spot is not None else spot["cash"]
CURVE_SPOT_KEY = f"curve_spot_{S.get('meeting')}"
anchor = st.session_state.get(CURVE_SPOT_KEY, spot_derived)
PATH = (stripmod.decompose(MEETINGS, ib_quotes, float(anchor), M.size,
                           stale_codes=ib_stale, as_of=as_of)
        if anchor is not None else stripmod.StripPath(0.0, M.size, error="no cash rate anchor"))
BILLS = bbswmod.analyse(ir_quotes, PATH, MEETINGS, M.size, ir_stale, horizon,
                        as_of=as_of, spot_basis_bp=spot["spot_basis_bp"])

# Both the header and the verdict are painted at the very END of the script,
# into placeholders reserved here. Every tab body edits state through widgets
# that have not rendered yet at this point, so anything drawn now -- the
# blackout chip, the kill count, the whole verdict -- would be one interaction
# stale. Reserving the slot and filling it last is what `rebuild()` does for
# the numbers, applied to layout.
header_slot = st.empty()

LEFT, RIGHT = st.columns([2.55, 1], gap="large")
verdict_slot = RIGHT.container()


# ------------------------------------------------------------------ working

SECTIONS = ["Pricing", "Curve", "Sensitivity & size", "Path & kills",
            "Labour", "Inflation", "Vote count", "Data"]

with LEFT:
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
        chdr, chlp = st.columns([1, 0.06], vertical_alignment="center")
        with chdr:
            C.section("Step 1a — what the strip has already priced",
                      "The whole calendar, read off futures. Every number below is derived "
                      "from prices and the meeting dates, not taken from anyone's published "
                      "probability.")
        with chlp:
            C.formula_help(
                r"r_{\text{after}} = \frac{r_{\text{month}} N - r_{\text{before}} n_1}{n_2}",
                "An IB contract settles on the month's AVERAGE cash rate, so a month "
                "containing a decision prices a blend of the old rate and the new one, "
                "weighted by days. Inverting that blend recovers the rate the market "
                "expects after the meeting; the step is the difference, and the probability "
                "is the step over one full move. Dividing by n2 is why a late-month meeting "
                "is read off the FOLLOWING contract instead -- with two days of capture, a "
                "1bp error in the starting rate becomes 15bp of imaginary priced move, and "
                "the RBA's meetings sit late in their months far more often than the Fed's.",
                "step / move size = implied probability", "strip")

        live = [q for q in ib_quotes if q.ok]

        if not live:
            if M.meeting.end < as_of:
                st.info(f"{M.meeting.label} has already happened, and its contracts have "
                        "expired and stopped quoting. The Curve tab only speaks for "
                        "meetings still ahead — select a future one to price the strip.")
            else:
                st.warning("No interbank cash rate quotes came back. Everything else in the "
                           "app still works from the numbers you have typed.")
                first = next((q for q in ib_quotes if q.error), None)
                if first:
                    C.note(f"{first.symbol}: {first.error}")
        else:
            # No magic default. When the strip cannot pin spot, saying so and
            # asking beats printing a confident ladder built on a guess.
            if anchor is None:
                st.warning(
                    "**Spot cash cannot be derived from the strip at this anchor.** "
                    "A decision has already taken effect inside the front month and "
                    "another lands before the next meeting-free contract, which leaves "
                    "the strip one unknown short of solvable. Type the prevailing cash "
                    "rate below — every step is measured from it, so a guess here is a "
                    "wrong ladder, not a rough one.")

            g1, g2, g3 = st.columns([1, 1, 1.1], vertical_alignment="center")
            with g1:
                st.number_input(
                    "Spot cash rate (%)", key=CURVE_SPOT_KEY,
                    value=float(spot_derived) if spot_derived is not None else None,
                    step=0.01, format="%.4f",
                    placeholder="type the cash rate",
                    help="Backed out of the strip: either a meeting-free contract, or the "
                         "front month inverted against one. Falls back to the RBA's "
                         "published target when the strip cannot pin it. Override it if "
                         "the cash rate has already moved month-to-date — every step below "
                         "is measured from here.")
            with g2:
                st.metric("Curve as of",
                          ib_ref.strftime("%d %b %Y") if ib_ref else "--",
                          f"{len(live)}/{len(ib_quotes)} interbank", delta_color="off")
                refreshed = _fetch_clock().get("ib")
                if refreshed:
                    mins = (datetime.now() - refreshed).total_seconds() / 60
                    when = "just now" if mins < 1 else f"{mins:.0f}m ago"
                    C.note(f"Refreshed {refreshed:%H:%M} · {when}")
            with g3:
                st.markdown('<div style="height:1.1rem"></div>', unsafe_allow_html=True)
                # on_click, not an inline pop: `curve_spot` is a widget key and
                # is locked once the input above rendered -- the callback runs
                # before the rerun, when it is still free.
                def _clear_curve_spot():
                    fetch_ib.clear()
                    fetch_ir.clear()
                    st.session_state.pop(CURVE_SPOT_KEY, None)
                st.button("Refresh quotes", width="stretch",
                          on_click=_clear_curve_spot)

            if anchor is None:
                pass          # the warning above stands in for the ladder
            elif not PATH.ok:
                st.warning(PATH.error or "The strip could not be decomposed.")
            else:
                # A spot far from the front contract means one of the two is
                # wrong, and every step inherits the error amplified.
                front_implied = live[0].implied_rate
                _spot = float(anchor)
                if front_implied is not None and abs(_spot - front_implied) > 0.50:
                    st.warning(
                        f"Spot of {_spot:.3f}% is {abs(_spot - front_implied) * 100:.0f}bp "
                        f"from what {live[0].code} implies ({front_implied:.3f}%). One of "
                        "the two is wrong, and the ladder below amplifies the gap.")

                view = st.segmented_control(
                    "Instrument", ["Interbank cash (IB)", "90 day bank bills (IR)"],
                    default="Interbank cash (IB)",
                    label_visibility="collapsed") or "Interbank cash (IB)"

                # ------------------------------------------ interbank cash
                if view == "Interbank cash (IB)":
                    k1, k2 = st.columns(2)
                    k1.metric("Terminal",
                              f"{PATH.terminal:.3f}%" if PATH.terminal else "--",
                              f"{PATH.total_bp:+.0f}bp total", delta_color="off",
                              help="Where the priced path ends inside the quoted strip.")
                    pk = PATH.peak
                    k2.metric("Peak priced", f"{pk.cum_bp:+.0f}bp" if pk else "--",
                              pk.meeting.end.strftime("%b %Y") if pk else "--",
                              delta_color="off",
                              help="Furthest the market has priced from spot, and by when.")
                    if PATH.excluded_count:
                        C.note(f"Both figures ignore {PATH.excluded_count} meeting"
                               f"{'s' if PATH.excluded_count > 1 else ''} at the back of "
                               "the strip solved off contracts that are not printing. They "
                               "stay in the ladder, marked, but a quote that has not traded "
                               "in weeks does not get to set the terminal rate.")

                    st.divider()
                    lc, rc = st.columns([1.25, 1], gap="large")
                    with lc:
                        st.markdown("### The meeting ladder")
                        rows, colours, marks = [], {}, []
                        for i, stp in enumerate(PATH.steps):
                            if stp.key == M.meeting.key:
                                marks.append(i)
                            flag = " ~" if stp.ambiguous else (" !" if stp.stale else "")
                            rows.append([
                                stp.label, f"{(stp.meeting.end - as_of).days}d",
                                stp.code + flag, f"{stp.step_bp:+.1f}",
                                f"{stp.cum_bp:+.1f}", f"{stp.prob:.0%}",
                                f"{stp.r_after:.3f}",
                            ])
                            colours[(i, 3)] = pnl_colour(stp.step_bp, P)
                            colours[(i, 4)] = pnl_colour(stp.cum_bp, P)
                        C.table(["Meeting", "Out", "Contract", "Step", "Cum",
                                 "P(move)", "Rate after"],
                                rows, numeric=(1, 3, 4, 5, 6),
                                mark_rows=marks, colours=colours)
                        C.note("▸ marks the meeting selected in the sidebar. Step is "
                               "what this meeting alone has priced; Cum is the whole path "
                               "from spot. ! = quote has not printed with the rest of the "
                               "strip; ~ = two meetings share a contract month.")
                    with rc:
                        st.markdown("### The priced path")
                        st.altair_chart(charts.priced_path(PATH.steps, _spot, P),
                                        width="stretch", theme=None)
                        if PATH.excluded_count:
                            C.note("Dashed line, hollow dots = solved off a stale or "
                                   "ambiguous contract (marked ! or ~ in the ladder) -- "
                                   "same exclusion the Terminal and Peak figures apply.")

                    st.divider()
                    mine = PATH.step_for(M.meeting.key)
                    st.markdown(f"### {M.meeting.label} — live vs the number you are "
                                "sizing on")
                    if mine is None:
                        st.info("This meeting falls outside the quoted strip.")
                    else:
                        x1, x2, x3, x4 = st.columns([1, 1, 1, 1.1],
                                                    vertical_alignment="center")
                        typed = trade.get("points")
                        x1.metric("Priced now", f"{mine.step_bp:+.2f}bp",
                                  f"{mine.prob:.1%} of a {M.size:.0f}bp move",
                                  delta_color="off")
                        x2.metric("You have typed",
                                  f"{typed:+.2f}bp" if typed is not None else "--",
                                  "sidebar → Points priced", delta_color="off")
                        with x3:
                            if typed is not None:
                                st.metric("Difference",
                                          f"{mine.step_bp - float(typed):+.2f}bp",
                                          "live minus typed", delta_color="off")
                        with x4:
                            if st.button("Use the live number", type="primary",
                                         width="stretch"):
                                trade["points"] = round(abs(mine.step_bp), 2)
                                mark_dirty()
                                st.rerun()
                        C.note(
                            f"Read off {mine.code} — "
                            + ("the following month, which sits wholly at the new rate, "
                               f"because {mine.month:%b}'s own contract captures only "
                               f"{mine.capture_share:.0%} of it."
                               if mine.is_clean else
                               f"the meeting month itself, which captures "
                               f"{mine.capture_share:.0%} ({mine.days_after} of "
                               f"{mine.days_in_month} days) at the new rate."))

                    st.divider()
                    sc1, sc2 = st.columns([1, 1], gap="large")
                    with sc1:
                        st.markdown("### The strip")
                        srows, scols = [], {}
                        for i, q in enumerate(live):
                            price_delta = q.change_one_day
                            if price_delta == 0:
                                price_delta = 0.0        # normalize -0.0 -> +0.0
                            srows.append([
                                q.code, f"{q.month:%b %Y}",
                                f"{q.price:.4f}".rstrip("0").rstrip("."),
                                f"{q.implied_rate:.3f}",
                                f"{price_delta:+.4f}".rstrip("0").rstrip(".")
                                if price_delta is not None else "--",
                                "stale" if q.code in ib_stale else "",
                            ])
                            if price_delta is not None and price_delta != 0:
                                scols[(i, 4)] = P.positive if price_delta > 0 else P.negative
                        C.table(["Code", "Month", "Price", "Implied %", "Δ price", ""],
                                srows, numeric=(2, 3, 4), colours=scols)
                        st.caption("Δ price is the contract's price move since the prior "
                                   "close — green when up, red when down, default when "
                                   "unchanged.")
                    with sc2:
                        st.markdown("### Cross-checks")
                        flagged = [c for c in PATH.checks if c.flagged]
                        if flagged:
                            C.table(["Month", "Implied", "Carried", "Diff"],
                                    [[c.month.strftime("%b %Y"), f"{c.implied:.3f}",
                                      f"{c.expected:.3f}", f"{c.diff_bp:+.1f}bp"]
                                     for c in flagged], numeric=(1, 2, 3))
                            C.note("Months with no meeting should reprint the carried "
                                   "rate. Where they do not, something outside the "
                                   "calendar is in the price — a technical adjustment to "
                                   "the remuneration rate, month-end funding, or a meeting "
                                   "date the market disagrees with.")
                        else:
                            C.note("Every meeting-free month reprints the carried rate to "
                                   f"within {stripmod.DRIFT_FLAG_BP:g}bp. The calendar and "
                                   "the strip agree.")

                # ------------------------------------------- 90 day bills
                else:
                    ir_live = [q for q in ir_quotes if q.ok]
                    if not ir_live:
                        st.warning("No bank bill quotes came back.")
                        first = next((q for q in ir_quotes if q.error), None)
                        if first:
                            C.note(f"{first.symbol}: {first.error}")
                    elif not BILLS.ok:
                        st.warning(BILLS.error or "No bank bill curve available.")
                    else:
                        cv = BILLS
                        b1, b2, b3 = st.columns(3)
                        mb = cv.mean_basis_bp
                        b1.metric("Mean basis",
                                  f"{mb:+.1f}bp" if mb is not None else "--",
                                  f"{len(cv.comparable_periods)} comparable windows",
                                  delta_color="off",
                                  help="The bill's implied BBSW less the interbank cash "
                                       "path averaged over the same 90 days. BBSW is a "
                                       "bank credit rate and the cash rate is not, so a "
                                       "positive spread is normal; a wide one is funding "
                                       "stress.")
                        fr = cv.front
                        b2.metric("Front window",
                                  f"{fr.basis_bp:+.1f}bp" if fr and fr.comparable else "--",
                                  fr.code if fr else "--", delta_color="off")
                        b3.metric("IB covers to",
                                  horizon.strftime("%b %Y") if horizon else "--",
                                  "basis stops there", delta_color="off",
                                  help="Interbank contracts go stale long before the bills "
                                       "do. Past this date there is no path to compare "
                                       "against, so the comparison is withheld rather than "
                                       "extrapolated.")

                        st.divider()
                        pc1, pc2 = st.columns([1.45, 1], gap="large")
                        with pc1:
                            st.markdown("### What each contract spans")
                            rows, colours = [], {}
                            for i, per in enumerate(cv.periods):
                                flag = " !" if per.stale else ""
                                rows.append([
                                    per.code + flag, per.window_label,
                                    f"{(per.covers_end - per.covers_start).days}",
                                    f"{len(per.meetings_covered)} ({per.covered_label})"
                                    if per.meetings_covered else "0",
                                    f"{per.implied:.3f}",
                                    f"{per.expected:.3f}" if per.expected is not None
                                    else "--",
                                    f"{per.basis_bp:+.1f}" if per.comparable else "--",
                                    f"{per.policy_bp:+.0f}" if per.policy_bp is not None
                                    else "--",
                                ])
                                if per.comparable and per.basis_flagged:
                                    colours[(i, 6)] = P.critical
                                if per.policy_bp is not None:
                                    colours[(i, 7)] = pnl_colour(per.policy_bp, P)
                            C.table(["Contract", "Reference window", "Days", "Meetings",
                                     "BBSW %", "IB path %", "Basis bp", "Policy bp"],
                                    rows, numeric=(2, 3, 4, 5, 6, 7), colours=colours)
                            C.note("Policy bp is how much the interbank path moves inside "
                                   "that window — the policy risk the contract actually "
                                   "carries. Basis is what is left once that path is "
                                   "priced in. ! = quote not printing with the rest of "
                                   "the strip.")
                        with pc2:
                            st.markdown("### Basis by window")
                            comparable = [p_ for p_ in cv.comparable_periods
                                          if not p_.stale]
                            if comparable:
                                st.altair_chart(charts.bbsw_basis(comparable, P),
                                                width="stretch", theme=None)
                            else:
                                C.note("No window is covered by both strips, so there is "
                                       "nothing to compare.")

                        st.divider()
                        d1, d2 = st.columns([1, 1], gap="large")
                        with d1:
                            st.markdown("### Calendar spreads")
                            srows, scols = [], {}
                            for i, spd in enumerate(cv.spreads):
                                srows.append([
                                    spd.label, f"{spd.market_bp:+.1f}",
                                    f"{spd.path_bp:+.1f}" if spd.path_bp is not None
                                    else "--",
                                    f"{spd.diff_bp:+.1f}" if spd.diff_bp is not None
                                    else "--",
                                    f"{spd.meetings_between}",
                                ])
                                scols[(i, 1)] = pnl_colour(spd.market_bp, P)
                                if spd.diff_bp is not None:
                                    scols[(i, 3)] = pnl_colour(spd.diff_bp, P)
                            C.table(["Spread", "Market", "IB path", "Diff", "Mtgs"],
                                    srows, numeric=(1, 2, 3, 4), colours=scols)
                            C.note("The spread is how a desk holds this view on bills "
                                   "rather than outright. Diff is the market spread less "
                                   "what the interbank path implies — the part that is "
                                   "basis and convexity rather than policy, and the credit "
                                   "spread is common to both legs and largely cancels.")
                        with d2:
                            st.markdown("### Where the algebra closes")
                            # `determinate` is exactly the "one undecided meeting
                            # before the fix" case, which is the only one the
                            # probability is defined for -- but the step needs a
                            # readable path at today's date as well, so both are
                            # checked rather than inferred from each other.
                            solved = [x for x in cv.periods
                                      if x.determinate
                                      and x.cash_equivalent_step_bp is not None
                                      and x.cash_equivalent_prob is not None]
                            if solved:
                                C.table(
                                    ["Contract", "Meeting", "Step", "P(move)"],
                                    [[x.code, x.meetings_before[0].label,
                                      f"{x.cash_equivalent_step_bp:+.1f}bp",
                                      f"{x.cash_equivalent_prob:.0%}"] for x in solved],
                                    numeric=(2, 3),
                                )
                                C.note("Only windows holding exactly one meeting are "
                                       "determined, and these are read off the "
                                       "cash-equivalent rate — today's observed BBSW/cash "
                                       "spread stripped off the bill, which assumes that "
                                       "spread holds to the fix. That assumption is stated "
                                       "rather than hidden, and it is deliberately NOT the "
                                       "basis column above, which would be circular and "
                                       "would just hand back the interbank path.")
                            else:
                                C.note("No window in the quoted strip contains exactly one "
                                       "meeting, so none determines a step on its own. Use "
                                       "the interbank ladder for per-meeting pricing.")

                            st.markdown("")
                            C.note("An IR contract settles on a single 3-month BBSW fix, "
                                   "not an average, so there is no month to un-blend and "
                                   "no per-meeting probability to recover from the price "
                                   "itself. Everything here is the fix compared against "
                                   "the interbank path over the same 90 days.")

                        if spot["spot_basis_bp"] is not None:
                            C.note(
                                f"Today's observed spread is {spot['spot_basis_bp']:+.1f}bp "
                                f"— 3-month BBSW {spot['bbsw']:.3f}% less the "
                                f"{spot['cash']:.3f}% target.")
                        mb = cv.mean_basis_bp
                        hist = rba.historical_bbsw_ois_basis()
                        if mb is not None and hist:
                            hist_mean = sum(o.value for o in hist) / len(hist)
                            C.note(
                                f"Mean basis {mb:+.1f}bp across comparable contracts. The "
                                f"published BBSW/OIS spread averaged {hist_mean:.1f}bp over "
                                "2011–2022 before the RBA retired the series, which is the "
                                "only yardstick available for whether today's reading is "
                                "wide or narrow.")

                        C.note("Prices: ASX 30 day interbank cash rate futures (IB) and 90 "
                               "day bank bill futures (IR), from the exchange's own "
                               "end-of-day file. Delayed, and not a settlement source.")

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
        pth = S.setdefault("path", {})
        if not M.priced:
            st.info("Enter the points priced in the sidebar to begin.")
        else:
            s = M.summary
            C.section("Step 6 — path risk")
            if M.ladder:
                st.altair_chart(charts.mtm_ladder(M.ladder, P),
                                width="stretch", theme=None)

            st.divider()
            hdr, hlp = st.columns([1, 0.06], vertical_alignment="center")
            with hdr:
                st.markdown("### Stop-cost decomposition")
            with hlp:
                C.formula_help(
                    r"EV_{\text{exits}} = \frac{\sum_i n_i \cdot \text{P\&L}_i}{N}"
                    r"\qquad \text{effect}_b = \frac{\sum_{i \in b} n_i\,"
                    r"(\text{P\&L}_i - \text{P\&L}_i^{\,\text{ride on}})}{N}",
                    "Per-100-trade decomposition. Each trial is classified by which level "
                    "the path reaches FIRST, so the buckets never double-count. Each "
                    "barrier's effect compares the paths that exited there with what those "
                    "same paths would have made riding to expiry -- which is why a stop and "
                    "a target get separate lines: when your win rate is high, false triggers "
                    "dominate and the stop costs you on the many winning paths it interrupts, "
                    "while the target trades a capped gain for a shorter hold.",
                    "see the table", "stop")

            default_target, default_stop = pathmod.reassess_defaults(M.points, M.size, M.side)
            x1, x2, x3 = st.columns(3)
            with x1:
                pth["target_level"] = st.number_input(
                    "Target — reassess (bp)",
                    value=float(pth.get("target_level")
                                if pth.get("target_level") is not None else default_target),
                    step=0.5, format="%.2f", on_change=mark_dirty,
                    help="The level, in bp priced, where you'd take profit rather than ride "
                    "to the market bound. Defaults to halfway between entry and the bound.")
            with x2:
                pth["stop_level"] = st.number_input(
                    "Stop — thesis (bp)",
                    value=float(pth.get("stop_level")
                                if pth.get("stop_level") is not None else default_stop),
                    step=0.5, format="%.2f", on_change=mark_dirty,
                    help="The thesis-broken level, in bp priced: where the market's repricing "
                    "has invalidated your view, and you exit by decision rather than ride to "
                    "the bound. This is the level the Monte Carlo treats as the stop. "
                    "Defaults to halfway between entry and it.")
            with x3:
                # Explicit per-meeting key so the clear button below can reset this
                # widget to blank (a number_input otherwise keeps the last typed
                # value and cannot be emptied by hand), and so switching meetings
                # never inherits another meeting's typed value.
                _pt_key = f"pt_partial_{S.get('meeting')}"
                _pt_in, _pt_clr = st.columns([1, 0.24], vertical_alignment="center")
                with _pt_in:
                    pth["partial_target_level"] = st.number_input(
                        "Partial target (bp, optional)",
                        value=float(pth["partial_target_level"])
                        if pth.get("partial_target_level") is not None else None,
                        step=0.5, format="%.2f", placeholder="scale-out level",
                        on_change=mark_dirty, key=_pt_key,
                        help="Scale-out level, in bp priced, between entry and the full "
                        "target: when the market reaches it you bank the partial share below "
                        "and let the rest run to the full target (or the event). The Monte "
                        "Carlo moves the target barrier in to this level. Leave blank for a "
                        "single take-profit.")
                with _pt_clr:
                    if st.button("✕", key=_pt_key + "_clear",
                                 help="Reset to blank (single take-profit)",
                                 use_container_width=True):
                        st.session_state.pop(_pt_key, None)
                        pth["partial_target_level"] = None
                        mark_dirty()
                        st.rerun()
            y1, y2 = st.columns([1, 1])
            with y1:
                pth["hard_stop_level"] = st.number_input(
                    "Hard floor (bp, optional)",
                    value=float(pth["hard_stop_level"])
                    if pth.get("hard_stop_level") is not None else None,
                    step=0.5, format="%.2f", placeholder="worst-case floor",
                    on_change=mark_dirty,
                    help="A non-negotiable RISK floor beyond the thesis stop -- your worst "
                    "case if you hold past it or the order slips. It is a discipline line, "
                    "not a simulation barrier: the Monte Carlo still exits at the thesis "
                    "stop. The stop sweep below shows the EV of every stop level, so you "
                    "can see what holding to the floor instead would cost.")
            with y2:
                pth["partial_share"] = st.number_input(
                    "Partial share",
                    value=float(pth.get("partial_share", 0.5)),
                    min_value=0.0, max_value=1.0, step=0.1, format="%.2f",
                    on_change=mark_dirty,
                    help="How much of the position to bank at the partial target (0.5 = "
                    "half off, the rest runs to the full target or the event).")
            p1, p2 = st.columns([1, 3])
            with p1:
                cp_raw = pth.get("current_price")
                cp = st.number_input(
                    "Contract price now (optional)",
                    value=float(cp_raw) if cp_raw is not None else None,
                    step=0.005, format="%.4f", placeholder="e.g. 96.19",
                    on_change=mark_dirty,
                    help="What the contract you're actually trading marks at right now, with "
                    "your entry priced in. One bp of rate is 0.01 of price on both IB and IR, "
                    "so every exit below prices off this. Leave blank to hide the price "
                    "column.")
                pth["current_price"] = cp
            rebuild()

            if M.exit_levels:
                has_price = any(x.price is not None for x in M.exit_levels)
                has_pnl = any(x.pnl is not None for x in M.exit_levels)
                # Priced and Price sit together (the level, then the instrument
                # quote that level implies) ahead of the two P&L columns (the
                # level's move, then its dollar value) -- "what this is" before
                # "what it's worth", rather than interleaving the two.
                headers = ["Exit", "Implied", "Priced"]
                if has_price:
                    headers.append("Price")
                headers.append("P&L")
                if has_pnl:
                    headers.append("P&L (A$)")
                pnl_col = headers.index("P&L")
                rows, colours, marks = [], {}, []
                for i, x in enumerate(M.exit_levels):
                    if x.is_entry:
                        marks.append(i)
                    row = [x.label, f"{x.implied:.0%}", f"{x.level:g}bp"]
                    if has_price:
                        row.append("--" if x.price is None else f"{x.price:.4f}")
                    row.append("--" if x.is_entry else f"{x.mtm:+g}bp")
                    if has_pnl:
                        row.append("--" if x.pnl is None or x.is_entry
                                   else f"{x.pnl:+,.0f}")
                    rows.append(row)
                    if not x.is_entry:
                        colours[(i, pnl_col)] = pnl_colour(x.mtm, P)
                        if has_pnl:
                            colours[(i, len(headers) - 1)] = pnl_colour(x.mtm, P)
                    if x.structural:
                        colours[(i, 0)] = P.muted
                    if x.misplaced:
                        colours[(i, 0)] = P.critical
                C.table(headers, rows, numeric=tuple(range(1, len(headers))),
                        mark_rows=marks, colours=colours)

            # The decomposition's actual RESULT (the six counts and what they're
            # worth) sits directly under the exit map that feeds it, so the
            # section reads as one continuous answer: structure, then what it's
            # worth. Monte Carlo calibration -- the methodology behind where those
            # six counts come from -- follows afterward as its own section, for
            # whoever wants to verify or explore it, rather than interrupting the
            # result with the derivation.
            #
            # These six counts track the live Monte Carlo simulation
            # unconditionally: whatever was saved or previously typed is
            # overwritten with the fresh result on every render, not merely used
            # as a starting point. A soft default would let a number typed once
            # sit there silently disagreeing with a since-recalibrated simulation.
            # Only falls back to manual entry when no simulation is available to
            # defer to (no rate history, or an exit level flagged as misplaced).
            mc_defaults = M.monte_carlo.counts if M.monte_carlo else {}
            locked = bool(mc_defaults)
            fields = ("no_move_target_hit", "no_move_never_breached",
                      "no_move_falsely_stopped", "move_target_hit",
                      "move_stopped_early", "move_gapped_through")
            for f in fields:
                pth.setdefault(f, 0.0)      # older saved meetings may predate a field
            if locked:
                for f in fields:
                    pth[f] = mc_defaults[f]

            sc1, sc2 = st.columns([1, 1.3], gap="large")
            with sc1:
                if not locked:
                    st.caption("No simulation available (see the warning below) -- "
                               "entered by hand.")

                def _count_box(label: str, field: str, col, tail: str) -> None:
                    val = col.number_input(
                        label, value=float(pth[field]), step=1.0, format="%.0f",
                        disabled=locked, on_change=mark_dirty,
                        key=f"count_{S.get('meeting')}_{field}",
                        help=f"Out of 100 trials: {tail} "
                        + ("Tracks the Monte Carlo simulation below; there is no manual "
                           "override while it's running."
                           if locked else
                           "No simulation available, so this is your own estimate."))
                    if not locked:
                        pth[field] = val

                g1, g2 = st.columns(2)
                with g1:
                    st.caption("No move")
                    _count_box("Target hit", "no_move_target_hit", g1,
                               "the RBA doesn't move, and the path reaches your target "
                               "first, so you bank the capped gain instead of the full one.")
                    _count_box("Rode out", "no_move_never_breached", g1,
                               "the RBA doesn't move, and the price touches neither your "
                               "target nor your stop along the way. Your clean full win.")
                    _count_box("False stop", "no_move_falsely_stopped", g1,
                               "the RBA doesn't move, but noise in the price path touches "
                               "your stop anyway and knocks you out early -- a win turned "
                               "into a small loss purely by the stop being there.")
                with g2:
                    st.caption("Move")
                    _count_box("Target first", "move_target_hit", g2,
                               "the path reaches your target and you're out with the gain, "
                               "and only afterwards does the RBA move. Rare, but it is the "
                               "bucket that makes the target look free if you leave it at "
                               "zero.")
                    _count_box("Stopped", "move_stopped_early", g2,
                               "the RBA does move against you, and your stop catches it "
                               "early, capping the loss at the stop level instead of the "
                               "worse final price.")
                    _count_box("Gapped", "move_gapped_through", g2,
                               "the RBA moves against you so sharply the price jumps "
                               "straight past your stop without letting you exit there -- "
                               "the stop fails to protect you and you eat the full "
                               "uncapped loss.")
            rebuild()
            with sc2:
                if M.stop:
                    stp = M.stop
                    C.table(
                        ["Path", "Count", "P&L each", "Total"],
                        [[p_.label, f"{p_.count:.0f}", f"{p_.pnl_each:+g}", f"{p_.total:+g}"]
                         for p_ in stp.paths],
                        numeric=(1, 2, 3),
                        colours={(i, 3): pnl_colour(p_.total, P)
                                 for i, p_ in enumerate(stp.paths)},
                    )
                    e1, e2, e3 = st.columns(3)
                    e1.metric("EV with exits", f"{stp.ev_with_stop:+.2f}bp")
                    e2.metric("EV riding on", f"{stp.ev_without_stop:+.2f}bp")
                    e3.metric("Cost of the exits", f"{stp.cost_of_stop:+.2f}bp",
                              f"{stp.cost_as_share_of_edge:.0%} of edge"
                              if stp.cost_as_share_of_edge is not None else "",
                              delta_color="off")
                    f1, f2 = st.columns(2)
                    f1.metric("Stop's own effect", f"{stp.stop_effect:+.2f}bp")
                    f2.metric("Target's own effect", f"{stp.target_effect:+.2f}bp")
                    if not stp.counts_consistent:
                        msg = ""
                        if not stp.counts_sum_correctly:
                            msg += (f"Your six counts sum to {stp.counts_total:g}, not "
                                    f"{stp.trials:g} — EV above is still the correct "
                                    "weighted average of what you typed, but the split "
                                    "below isn't the out-of-100 read the labels promise. ")
                        msg += (f"They imply P(move) = {stp.implied_p_move:.1%}, not your q "
                                f"of {s.q:.1%}. Flagged rather than rescaled — a silent "
                                "rescale would hide a mis-specified path assumption. The two "
                                "effect figures only add up to the total cost while they "
                                "reconcile.")
                        st.warning(msg)
                else:
                    st.info("Set a stop level to run the decomposition.")

            st.divider()
            mhdr, mhlp = st.columns([1, 0.06], vertical_alignment="center")
            with mhdr:
                st.markdown("### Monte Carlo calibration")
            with mhlp:
                C.formula_help(
                    r"\text{level}_{t+1} = \text{level}_t + "
                    r"\mathcal{N}(0,\ \sigma_{\text{daily}}^2)"
                    r"\ \ \text{, then a jump to 0 or } size \text{ at the decision}",
                    "Two pieces: a driftless daily random walk between now and the meeting, "
                    "calibrated to 3-month BBSW's own realised volatility with decision days "
                    "excluded, then a discrete jump at the announcement -- that is what "
                    "actually produces a gap through the stop rather than a walk into it. "
                    "Touch probabilities from a large simulated batch are applied to a "
                    "100-trial population split exactly by q, so the counts always reconcile "
                    "with it.",
                    "see core.montecarlo", "mc")

            # v2's widget is read and rebuilt BEFORE v1's caption is written, even
            # though v1 renders on the left -- st.columns() containers accept
            # content in any order, and reading v1 first would show the vol source
            # from before this rerun's own edit (the same one-rerun lag `rebuild()`
            # exists to close everywhere else in this file).
            v1, v2 = st.columns(2, gap="large")
            with v2:
                override_raw = pth.get("vol_override_bp")
                pth["vol_override_bp"] = st.number_input(
                    "Vol override (bp/day, optional)",
                    value=float(override_raw) if override_raw is not None else None,
                    min_value=0.0, step=0.1, format="%.2f", placeholder="use historical",
                    on_change=mark_dirty,
                    help="Overrides the historical estimate -- useful with no quotable "
                    "history (a Custom DV01 contract), or to stress-test a calmer or wilder "
                    "path than the recent past. Replaces the whole per-day schedule with one "
                    "flat number.")
                pth["vol_uncertainty"] = st.checkbox(
                    "Add vol uncertainty", value=bool(pth.get("vol_uncertainty")),
                    on_change=mark_dirty,
                    help="Off by default. On, each simulated path draws its own volatility "
                    "from the sampling distribution of the estimate rather than treating it "
                    "as known exactly, which fattens the tails. With a year or more of "
                    "history the effect is a few percent; it matters most when history is "
                    "short. Leaving it off keeps a given seed exactly reproducible.")
            rebuild()
            with v1:
                vol = M.monte_carlo.vol if M.monte_carlo else None
                if vol and vol.source == "historical":
                    st.caption(f"Quiet-day vol: {vol.daily_vol_bp:.2f}bp/day from "
                               f"{vol.symbol or '3-month BBSW'}'s own last "
                               f"{vol.observations} quiet sessions (releases and decision "
                               "days excluded) · BBSW is a rate, so a basis point of it is "
                               "already a basis point of the event and no capture rescale "
                               "is applied")
                elif vol and vol.source == "override":
                    st.caption(f"Vol: {vol.daily_vol_bp:.2f}bp/day, typed override "
                               "(flat -- an override replaces the whole schedule)")
                else:
                    st.caption("Vol: no rate history available, and no override set")

            if M.monte_carlo is None:
                st.info("No volatility estimate available -- type an override above to run "
                        "the simulation.")
            else:
                mc = M.monte_carlo
                sched = mc.vol.schedule
                sched_note = ""
                if sched:
                    loud = sum(1 for s_ in sched if s_ > min(sched) + 1e-9)
                    flat_eq = (sum(s_ * s_ for s_ in sched) / len(sched)) ** 0.5
                    sched_note = (f" · {loud} of {len(sched)} days carry a release, so the "
                                  f"schedule is worth {flat_eq:.2f}bp/day flat")
                _scale = (f" · scale-out {mc.partial_share:.0%} at "
                          f"{mc.partial_target_level:g}bp"
                          if mc.partial_target_level is not None else "")
                st.caption(f"{mc.trading_days} trading days to the decision · "
                           f"{mc.mc_trials:,} simulated paths · touches target first "
                           f"{mc.p_target_first:.0%} of the time, stop first "
                           f"{mc.p_stop_first:.0%}, rides out clean {mc.p_rode_out:.0%}"
                           + _scale + sched_note)
                st.caption("Mark release dates")
                with st.container(key="tier_checks"):
                    tc1, tc2, _tsp = st.columns([2, 2, 8], gap="small")
                    pth["mark_tier1"] = tc1.checkbox(
                        "Tier 1", value=bool(pth.get("mark_tier1", True)),
                        on_change=mark_dirty,
                        help="Market-moving: Labour Force, quarterly CPI, and the RBA's own "
                             "decisions.")
                    pth["mark_tier2"] = tc2.checkbox(
                        "Tier 2", value=bool(pth.get("mark_tier2", False)),
                        on_change=mark_dirty,
                        help="Notable: the monthly CPI indicator, Wage Price Index, "
                             "National Accounts and the minutes.")
                selected_tiers = {t for t, on in
                                  ((1, pth["mark_tier1"]), (2, pth["mark_tier2"])) if on}
                show_releases = bool(selected_tiers)

                release_days, day_index = {}, {d: i for i, d in enumerate(mc.days)}
                if show_releases:
                    for e in M.calendar_events:
                        i = day_index.get(e.when)
                        if i is not None and e.release and e.weight in selected_tiers:
                            release_days.setdefault(i, [])
                            if e.release not in release_days[i]:
                                release_days[i].append(e.release)
                st.altair_chart(
                    charts.mc_fan_chart(
                        mc, M.points, M.size, M.side, P, height=420,
                        release_days=release_days or None,
                        family_colour=RELEASE_FAMILY_COLOUR),
                    width="stretch", theme=None)
                if show_releases and release_days:
                    shown = {n_ for names in release_days.values() for n_ in names}
                    C.legend([(CALENDAR_FAMILY_NAME[f], CALENDAR_FAMILY_TINT_SOLID[f])
                              for f in ("labour", "inflation", "activity", "rba")
                              if any(RELEASE_FAMILY.get(n_) == f for n_ in shown)])

                target_bound, stop_bound = pathmod.bound_levels(M.size, M.side)
                sv1, sv2 = st.columns(2, gap="medium")
                with sv1:
                    stop_levels = montecarlo.suggested_sweep_levels(
                        M.points, stop_bound, n=montecarlo.DEFAULT_SWEEP_LEVELS)
                    stop_sweep = montecarlo.stop_sweep(
                        M.points, M.size, M.side, M.q, pth["target_level"],
                        stop_levels, mc.vol, mc.trading_days,
                        n_seeds=montecarlo.DEFAULT_SWEEP_SEEDS,
                        partial_target_level=pth.get("partial_target_level"),
                        partial_share=float(pth.get("partial_share", 0.5)))
                    st.altair_chart(
                        charts.mc_sensitivity(stop_sweep, pth["stop_level"],
                                              "stop level (bp)", P),
                        width="stretch", theme=None)
                    C.note("EV as the stop moves, target held fixed at its current level, "
                           f"averaged over {montecarlo.DEFAULT_SWEEP_SEEDS} simulation "
                           "batches so a single batch's rounding noise doesn't look like "
                           "curve shape. Where this peaks is the stop that maximises EV, "
                           "not necessarily the widest or the tightest one.")
                with sv2:
                    tgt_levels = montecarlo.suggested_sweep_levels(
                        M.points, target_bound, n=montecarlo.DEFAULT_SWEEP_LEVELS)
                    tgt_sweep = montecarlo.target_sweep(
                        M.points, M.size, M.side, M.q, pth["stop_level"],
                        tgt_levels, mc.vol, mc.trading_days,
                        n_seeds=montecarlo.DEFAULT_SWEEP_SEEDS,
                        partial_target_level=pth.get("partial_target_level"),
                        partial_share=float(pth.get("partial_share", 0.5)))
                    st.altair_chart(
                        charts.mc_sensitivity(tgt_sweep, pth["target_level"],
                                              "target level (bp)", P),
                        width="stretch", theme=None)
                    C.note("EV as the target moves, stop held fixed at its current level. "
                           "Each curve moves one barrier at a time -- neither shows what "
                           "happens if you moved both together.")

                mults = M.vol_multipliers
                if mults:
                    with st.expander("How each release was weighted"):
                        rows, colours = [], {}
                        for i, (name, mm) in enumerate(
                                sorted(mults.items(), key=lambda kv: -kv[1].applied)):
                            rows.append([
                                name, f"{mm.n_event}", f"{mm.raw:.2f}x",
                                f"{mm.applied:.2f}x",
                                f"{mm.ci_low:.2f} - {mm.ci_high:.2f}",
                                "applied" if mm.usable else "too few to apply",
                            ])
                            colours[(i, 5)] = (P.good if mm.significant
                                               else (P.muted if mm.usable else P.serious))
                        C.table(["Release", "n", "Raw", "Applied", "95% CI", "Status"],
                                rows, numeric=(1, 2, 3), colours=colours)
                        C.note(
                            "Variance on that release's own publication days against quiet "
                            "days, estimated from 3-month BBSW's own history and from "
                            "nothing else -- no release borrows another's data. 'Applied' "
                            "is shrunk toward 1.0 by sample size, so a handful of prints "
                            "cannot assert a large multiplier on its own. Quarterly CPI is "
                            "the loudest release on the Australian calendar by a wide "
                            "margin, which is what the multiplier should show. Same-day "
                            "releases add excess variance rather than multiplying it.")

            pth["note"] = st.text_area("Path note", pth.get("note", ""), height=80,
                                       on_change=mark_dirty)

        st.divider()
        khdr, khlp = st.columns([1, 0.06], vertical_alignment="center")
        with khdr:
            C.section("Step 7 — kill criteria",
                      "Pre-commit to what invalidates the thesis, before you're in the "
                      "position and rationalising. Any two live and the estimate is stale.")
        with khlp:
            C.formula_help(
                r"\text{stop: price trigger} \;\neq\; \text{kill: thesis trigger}",
                "A stop fires on price alone -- if the mark-to-market hits your stop level, "
                "you're out, regardless of why. A kill criterion fires on reasoning alone -- "
                "it flags that the logic behind your probability estimate no longer holds, "
                "even if price hasn't moved yet. Checking one doesn't exit anything by "
                "itself; it only recomputes the count. Two or more live and the banner below "
                "marks the estimate stale, which is a prompt to go re-examine q -- not an "
                "automatic exit.",
                "stop fires on MTM · kill fires on reasoning · 2+ kills = stale, not "
                "stopped out",
                "kill_explainer")
        C.note(
            "The first three read live off RBA table F1 and colour themselves; the rest are "
            "manual toggles.")
        # The banner is painted below the list, not above it: the checkboxes are
        # what decide it, and they have not been read yet at this point.
        kill_banner = st.empty()
        raw_kills = S.setdefault("kill_criteria", [])
        remove_idx = None
        with st.container(key="kill_list"):
            for i, crit in enumerate(M.kill.criteria):
                raw = raw_kills[i]
                with st.container(border=True):
                    k1, k2, k3 = st.columns([0.12, 3.2, 0.12], gap="small",
                                            vertical_alignment="center")
                    if crit.is_auto:
                        with k1:
                            st.write("")
                        with k2:
                            reading = ("--" if crit.current is None
                                       else f"{crit.current:.2f}")
                            st.markdown(f"**{crit.label}**  \n"
                                        f'<span class="fomc-note">{crit.series} · '
                                        f"{crit.comparator} {crit.threshold:g} · "
                                        f"{reading} {crit.unit} · "
                                        f'{crit.status}</span>',
                                        unsafe_allow_html=True)
                    else:
                        with k1:
                            raw["triggered"] = st.checkbox(
                                "trig", value=bool(raw.get("triggered")),
                                key=f"kill_cb_{i}", on_change=mark_dirty,
                                label_visibility="collapsed")
                        with k2:
                            # First render only: fold any pre-existing separate note
                            # into the single field so nothing typed earlier is lost.
                            default_text = raw.get("label", "")
                            old_note = raw.get("note", "")
                            if old_note and old_note not in default_text:
                                default_text = (f"{default_text} — {old_note}"
                                                if default_text else old_note)
                            raw["label"] = st.text_input(
                                "Criterion", value=default_text, key=f"kill_label_{i}",
                                on_change=mark_dirty, label_visibility="collapsed",
                                placeholder="What would invalidate the thesis?")
                    with k3:
                        if st.button("✕", key=f"kill_remove_{i}",
                                     help="Remove this criterion"):
                            remove_idx = i

        if remove_idx is not None:
            del raw_kills[remove_idx]
            mark_dirty()
            st.rerun()

        if st.button("+ Add kill criterion"):
            raw_kills.append({
                "label": "", "series": None, "threshold": None,
                "comparator": "manual", "triggered": False, "note": "",
            })
            mark_dirty()
            st.rerun()

        M = rebuild()
        if M.kill.is_stale:
            kill_banner.error(
                f"**{M.kill.count} kill criteria live — the probability estimate is "
                f"stale.**  \n" + " · ".join(M.kill.triggered))

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
        events = [e for e in M.calendar_events if as_of <= e.when <= M.meeting.end]
        if events:
            C.table(["Date", "Event", "Tier", "Source"],
                    [[f"{e.when:%a %d %b}", e.label, e.weight, e.source] for e in events])
        else:
            st.caption("Nothing scheduled between today and the decision.")


# ----------------------------------------------------------- verdict rail

rebuild()

with verdict_slot:
    st.markdown('<div class="fomc-verdict">', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("#### Verdict on the numbers")

        if not M.priced:
            st.caption("Enter the points priced to produce a verdict.")
        else:
            s = M.summary
            cond = M.conditional_ev_at
            rung = (M.sizing_ladder.selected
                    if M.sizing_ladder and M.sizing_ladder.ok else None)

            def _sec(label: str) -> None:
                st.markdown(f'<div class="fomc-verdict-sec">{label}</div>',
                            unsafe_allow_html=True)

            # --- The trade -------------------------------------------------
            _sec("The trade")
            leg = "Receive" if (M.side == "fade") == (M.direction == "hike") else "Pay"
            _to_dec = (f"{M.days_to_meeting}d" if M.days_to_meeting > 0
                       else ("today" if M.days_to_meeting == 0
                             else f"decided {M.meeting.end:%d %b}"))
            rows = [
                C.verdict_row("Leg", f"{leg} — {M.side} {M.size:g}bp {M.direction}",
                              P.accent, emph=True),
                C.verdict_row("To the decision",
                              _to_dec + (" · in blackout" if M.in_blackout else "")),
            ]
            # The reassess levels -- the TP/SL you actually chose, distinct
            # from the market bounds the contract settles at.
            _tp = next((x for x in M.exit_levels
                        if x.role == "target" and not x.structural), None)
            _sl = next((x for x in M.exit_levels
                        if x.role == "stop" and not x.structural), None)
            _pt = next((x for x in M.exit_levels if x.label == "Target -- partial"), None)
            if _tp is not None:
                rows.append(C.verdict_row(
                    "Take profit (TP)",
                    f"{_tp.level:g}bp · {_tp.implied:.0%} · {_tp.mtm:+.0f}bp",
                    pnl_colour(_tp.mtm, P)))
            if _pt is not None:
                rows.append(C.verdict_row(
                    "Partial TP",
                    f"{M.stop.partial_share:.0%} off at {_pt.level:g}bp"
                    f" · {_pt.mtm:+.0f}bp" if M.stop else
                    f"at {_pt.level:g}bp · {_pt.mtm:+.0f}bp",
                    pnl_colour(_pt.mtm, P)))
            if _sl is not None:
                rows.append(C.verdict_row(
                    "Stop loss (SL)",
                    f"{_sl.level:g}bp · {_sl.implied:.0%} · {_sl.mtm:+.0f}bp",
                    pnl_colour(_sl.mtm, P)))
            # The hard floor: a discipline line, not a simulation barrier.
            _hf = next((x for x in M.exit_levels if x.label == "Stop -- hard floor"), None)
            if _hf is not None:
                rows.append(C.verdict_row(
                    "Hard floor",
                    f"{_hf.level:g}bp · {_hf.implied:.0%} · {_hf.mtm:+.0f}bp",
                    pnl_colour(_hf.mtm, P)))
            st.markdown("".join(rows), unsafe_allow_html=True)

            # --- The view --------------------------------------------------
            _sec("The view")
            rows = [
                C.verdict_row("Market-implied probability", f"{s.implied:.0%}"),
                C.verdict_row("Your probability", f"{s.q:.1%}", P.accent),
                C.verdict_row("Breakeven probability", f"{s.breakeven:.0%}"),
                C.verdict_row("Margin of safety", f"{s.margin * 100:.1f}pp"),
            ]
            st.markdown("".join(rows), unsafe_allow_html=True)

            # --- The edge --------------------------------------------------
            _sec("The edge")
            rows = [
                C.verdict_row("Expected value", f"{s.ev:+.2f}bp",
                              pnl_colour(s.ev, P), emph=True),
                C.verdict_row("Max gain / max loss",
                              f"{s.gain:+g} / {s.loss:+g}  (1 : {s.rr:.1f})"),
                C.verdict_row("EV given the bloc already voted yes",
                              (f"{cond:+.2f}bp"
                               if cond is not None and M.decomposition.p_centre > 0
                               else "--"),
                              pnl_colour(cond or 0, P), emph=True),
            ]
            st.markdown("".join(rows), unsafe_allow_html=True)

            # --- The size --------------------------------------------------
            if rung:
                _sec("The size")
                _util = (f"{rung.max_loss / M.daily_limit:.0%} of "
                         f"A${M.daily_limit:,.0f}" if M.daily_limit > 0
                         else "no cap set")
                rows = [
                    C.verdict_row(
                        "Sizing instrument",
                        f"{M.contract_label}  (A${M.dv01:,.2f}/bp)"
                        + (f"  ← A${M.quoted_dv01:,.2f} × {M.capture:.0%} capture"
                           if M.capture < 1.0 - 1e-9 else ""),
                        P.accent, emph=True),
                    C.verdict_row(f"{rung.label} Kelly size",
                                  f"{rung.contracts:,} lots  "
                                  f"(A${rung.position_dv01:,.0f}/bp)",
                                  P.accent, emph=True),
                    C.verdict_row("Max gain / max loss (A$)",
                                  f"+{rung.max_gain:,.0f} / -{rung.max_loss:,.0f}",
                                  emph=True),
                    C.verdict_row("Expected P&L (A$)", f"{rung.expected_pnl:+,.0f}",
                                  pnl_colour(rung.expected_pnl, P)),
                    C.verdict_row("Growth captured",
                                  f"{rung.growth_share:.0%} of full  "
                                  f"({rung.annualised:.0%} annualised)"),
                    C.verdict_row("Daily limit",
                                  f"{rung.status} · {_util}",
                                  P.good if rung.within_daily_limit else P.critical,
                                  emph=not rung.within_daily_limit),
                ]
                st.markdown("".join(rows), unsafe_allow_html=True)
            else:
                _sec("The size")
                st.markdown(C.verdict_row(
                    f"{sizingmod.fraction_label(M.kelly_fraction)} Kelly size",
                    f"~{max(0.0, s.kelly_fractional):.0%} of risk budget"),
                    unsafe_allow_html=True)

            # --- What to watch ---------------------------------------------
            _sec("What to watch")
            a = M.arithmetic
            # Short release names so a line never wraps: the full calendar name
            # would be "Labour Force (employment, unemployment rate)".
            _rel_short = {
                econ_calendar.LABOUR: "Labour Force",
                econ_calendar.CPI_Q: "CPI (quarterly)",
                econ_calendar.CPI_M: "Monthly CPI",
                econ_calendar.WPI: "WPI",
                econ_calendar.GDP: "GDP",
                econ_calendar.DECISION: "RBA decision",
                econ_calendar.SMP: "Decision + SMP",
                econ_calendar.MINUTES: "Minutes",
            }
            t1 = sorted((e for e in M.calendar_events
                         if e.weight == 1 and as_of <= e.when <= M.meeting.end),
                        key=lambda e: e.when)
            t2 = sorted((e for e in M.calendar_events
                         if e.weight == 2 and as_of <= e.when <= M.meeting.end),
                        key=lambda e: e.when)
            rows = [
                C.verdict_row(
                    "Vote count",
                    ("Roster not set — Vote count tab"
                     if a.total_voters == 0 else
                     f"{a.mover_count}/{a.total_voters} movers · need {a.votes_needed}")),
            ]
            if a.blocs:
                rows.append(C.verdict_row(
                    "Blocs",
                    " · ".join(f"{b.bloc} {b.count}" for b in a.blocs)))
            rows += [
                C.verdict_row(
                    "P(bloc) × P(centre)",
                    f"{M.decomposition.p_bloc:.0%} × {M.decomposition.p_centre:.0%} "
                    f"= {M.decomposition.q:.1%}",
                    P.accent, emph=True),
            ]
            # A manual q override that matches the decomposition adds nothing;
            # show it only when it genuinely disagrees with the vote read.
            _ov = M.decomposition.override_q
            if _ov is not None and abs(_ov - M.decomposition.q) > 0.0005:
                rows.append(C.verdict_row("Recorded q", f"{_ov:.1%}", P.accent, emph=True))
            rows += [
                C.verdict_row("Decision",
                              f"{M.meeting.end:%d %b %a} · {M.direction} {M.size:g}bp"
                              + (" · SMP" if M.meeting.has_smp else ""),
                              P.accent, emph=True),
                C.verdict_row(
                    "Tier 1 events",
                    ("Decision passed" if M.days_to_meeting < 0 else
                     ("None before the decision" if not t1 else
                      f"{len(t1)} before the decision — next {t1[0].when:%d %b}"))),
            ]
            if t1 and M.days_to_meeting >= 0:
                _n = (t1[0].when - as_of).days
                _name = _rel_short.get(t1[0].release, t1[0].release or t1[0].label)
                rows.append(C.verdict_row(
                    "Next release",
                    f"{_name} in {_n}d" if _n > 0 else f"{_name} today"))
            if t2 and M.days_to_meeting >= 0:
                rows.append(C.verdict_row("Tier 2 events",
                                          f"{len(t2)} · next {t2[0].when:%d %b}"))
            rows.append(C.verdict_row(
                "Pre-meeting information",
                "None — blackout" if M.in_blackout else "Blackout not yet open",
                emph=True))
            st.markdown("".join(rows), unsafe_allow_html=True)
            if t1:
                for e in t1:
                    st.markdown(
                        f'<div class="fomc-verdict-row">'
                        f'<span class="k">{e.when:%d %b %a} · '
                        f'{_rel_short.get(e.release, e.release or e.label)}</span>'
                        f'<span class="v"></span></div>',
                        unsafe_allow_html=True)

            if M.kill.is_stale:
                st.error(f"{M.kill.count} kill criteria live — q is stale.")
            for w in M.warnings:
                st.warning(w)

    st.markdown("</div>", unsafe_allow_html=True)

with RIGHT:
    if M.priced:
        md = report.render(M)
        stamp = datetime.now().strftime("%Y%m%dT%H%M")
        st.download_button("Export Markdown", md, file_name=f"{key}-{stamp}.md",
                           mime="text/markdown", width="stretch")
        if st.button("Save to runs/", width="stretch"):
            out = store.save_run(key, md, stamp)
            st.success(f"Wrote {out.relative_to(ROOT)}")


# ----------------------------------------------------------------- header

opens, closes = M.blackout
head = [
    (f"{M.label} RBA", None),
    (M.meeting.label, None),
    (f"{M.days_to_meeting}d to decision" if M.days_to_meeting >= 0 else "decided", None),
]
if not M.meeting.verified:
    head.append(("date unverified", P.warning))
head.append((f"blackout {opens:%d %b}-{closes:%d %b}"
             + (" · live" if M.in_blackout else ""),
             P.serious if M.in_blackout else None))
head.append(("SMP meeting" if M.meeting.has_smp else "no SMP", None))
if M.kill.is_stale:
    head.append((f"{M.kill.count} kill criteria live", P.critical))
with header_slot:
    C.chips(head)
