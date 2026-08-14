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

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from core import bbsw as bbswmod
from core import contracts, econ_calendar, model, montecarlo, pricing, rba_calendar
from core import sizing as sizingmod
from core import strip as stripmod
from data import asx, rba
from export import report
from state import store
from ui import charts, components as C
from ui.theme import active as active_palette, pnl_colour

st.set_page_config(page_title="RBA event pricing", page_icon="◷", layout="wide")
C.inject_css()

P = active_palette()
TODAY = date.today()


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
def bbsw_history() -> list[tuple[date, float]]:
    """Daily 3-month BBSW, the volatility anchor.

    The ASX price feed has no history at all, so contract-price volatility --
    what the US version calibrates on -- simply does not exist here. A bank
    bill future is quoted as `100 - yield`, so a basis point in this series IS
    a basis point of contract price. It is the right scale, not a proxy.
    """
    return [(o.date, o.value) for o in fetch_rba_table().get(rba.BBSW_CODE, [])]


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
        step=0.5, format="%.2f", on_change=mark_dirty,
        help="What the market charges for the event. NOT the probability.")
    trade["points"] = pts if pts else None
    trade["size"] = st.number_input(
        "Size of the move (bp)", value=float(trade.get("size") or 25.0),
        step=5.0, format="%.0f", on_change=mark_dirty)

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
        step=25_000.0, format="%.0f", on_change=mark_dirty)
    sizing_state["daily_limit"] = st.number_input(
        "Daily loss limit (A$) — a veto, not a sizing input",
        value=float(sizing_state.get("daily_limit") or sizingmod.DEFAULT_DAILY_LIMIT),
        step=10_000.0, format="%.0f", on_change=mark_dirty)
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
            step=1.0, on_change=mark_dirty)

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
sel_key = sizing_state.get("contract") or "ib"
if sel_key.startswith("ir:"):
    sel_month = sel_key.partition(":")[2]
    match = next((q for q in ir_quotes if q.ok and f"{q.month:%Y-%m}" == sel_month), None)
    sizing_state["contract_yield"] = match.implied_rate if match else None

kill_values, kill_units = kill_series_values()

M = model.build(
    S, as_of=TODAY,
    rba_values=kill_values, rba_units=kill_units,
    all_meetings=MEETINGS,
    rate_history=bbsw_history(),
    rate_symbol="3-month BBSW (RBA F1)",
)

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
            "Vote count", "Data"]

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
            C.section("The two outcomes", "The payoff is capped on both sides.")
            C.table(["Outcome", "Settles at", "P&L"],
                    [[o.label, f"{o.settles_at:g}bp", f"{o.pnl:+.2f}bp"] for o in M.outcomes])
            st.caption(f"Risk/reward {s.rr:.2f} to 1 — {abs(s.loss):.2f}bp at risk "
                       f"for {s.gain:.2f}bp of gain.")

            st.altair_chart(charts.ev_curve(M.points, M.size, s.q, M.side, P),
                            use_container_width=True)

            st.divider()
            C.section("Which contract expresses this",
                      "Capture is a DV01 scaler, not a footnote.")
            C.table(
                ["Contract", "Month", "Days at new rate", "Capture", "Captured"],
                [[c.code, c.month_label, f"{c.days_at_new_rate}/{c.days_in_month}",
                  f"{c.share:.1%}", f"{c.captured_bp:.2f}bp"] for c in M.captures])
            if M.near_trap:
                near = next(c for c in M.captures if c.is_near)
                st.warning(
                    f"{near.code} captures only {near.share:.1%} of a {M.meeting.label} move. "
                    "The decision lands too late in the month for the meeting-month contract "
                    "to express the view — use the following month.")
            C.table(["Instrument", "Verdict", "Why"],
                    [[i.name, i.verdict, i.caveat] for i in contracts.INSTRUMENT_RANKING])

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
            C.section("How wrong can q be", "EV across the range you might believe.")
            C.table(["q", "EV", ""],
                    [[f"{r.q:.1%}", f"{r.ev:+.2f}bp",
                      "breakeven" if r.is_breakeven else ""] for r in M.sensitivity])

            st.divider()
            C.section("Position size", "Kelly on the drawdown, vetoed by the daily limit.")
            lad = M.sizing_ladder
            if lad is None or not lad.ok:
                st.warning(getattr(lad, "error", None)
                           or "No edge at this q — there is no size to take.")
            else:
                C.table(
                    ["Fraction", "f", "Risked", "Position DV01", "Lots", "Within limit"],
                    [[r.label, f"{r.f:.1%}", f"A${r.max_loss:,.0f}",
                      f"A${r.position_dv01:,.0f}", f"{r.contracts:,.0f}",
                      "yes" if r.within_daily_limit else "BREACH"]
                     for r in lad.rungs])
                a, b = st.columns(2)
                with a:
                    st.altair_chart(charts.kelly_growth(lad, P), use_container_width=True)
                with b:
                    st.altair_chart(charts.capital_at_risk(lad, P), use_container_width=True)
                st.caption(
                    f"{M.contract_label} — DV01 A${M.quoted_dv01:,.2f} per lot"
                    + (f", corrected to A${M.dv01:,.2f} per bp of the event "
                       f"({M.capture:.1%} capture)." if M.capture < 1 - 1e-9 else "."))

    # ---------------------------------------------------- 4. Path & kills
    elif section == "Path & kills":
        if not M.priced:
            st.info("Enter the points priced in the sidebar to begin.")
        else:
            C.section("Mark-to-market ladder", "What the position is worth as it moves.")
            st.altair_chart(charts.mtm_ladder(M.ladder, P), use_container_width=True)
            if M.exit_levels:
                C.table(["Level", "Role", "At", "MTM", "P&L"],
                        [[x.label, x.role, f"{x.level:g}bp", f"{x.mtm:+.2f}bp",
                          "—" if x.pnl is None else f"A${x.pnl:,.0f}"]
                         for x in M.exit_levels])

            if M.monte_carlo is not None:
                st.divider()
                C.section("Simulated paths", "Where the exits actually get touched.")
                st.plotly_chart(
                    charts.mc_fan_chart(M.monte_carlo, M.points, M.size, M.side, P),
                    use_container_width=True)
            elif M.quiet_sigma_bp:
                st.caption(
                    f"Quiet-day sigma {M.quiet_sigma_bp:.2f}bp, from daily 3-month BBSW. "
                    "Set a target and a stop in the meeting state to run the simulation.")

        st.divider()
        C.section("Kill criteria", "Pre-commit to what invalidates the thesis.")
        if M.kill.is_stale:
            st.error(f"{M.kill.count} kill criteria live — the probability estimate is stale.")
        C.table(["Criterion", "Test", "Current", "Status"],
                [[k.label,
                  "manual" if k.comparator == "manual"
                  else f"{k.comparator} {k.threshold:g}{k.unit}",
                  "—" if k.current is None else f"{k.current:.1f}{k.unit}",
                  k.status] for k in M.kill.criteria])
        C.note(
            "The first three read live off RBA table F1 and colour themselves; the rest are "
            "manual toggles. Two or more live at once and the app says the estimate is stale — "
            "the threshold is enforced here rather than left to memory, because the moment it "
            "matters is the moment you are least inclined to apply it.")

    # ------------------------------------------------------- 5. Vote count
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

        C.table(["Member", "Role", "Bloc", "Hawk–dove", "Mover"],
                [[r.get("name") or "—", r.get("role", ""), r.get("bloc", ""),
                  f"{float(r.get('score', 2.5)):.1f}",
                  "yes" if r.get("is_mover") else ""] for r in roster_rows])

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
        C.section("Sources", "Everything here is free and keyless.")

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
