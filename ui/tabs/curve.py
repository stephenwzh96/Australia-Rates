"""Curve: the IB and IR strips, the meeting ladder, the bank-bill basis,
instrument history and the meeting-spread matrix."""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from core import clock
from core import strip as stripmod
from data import rba
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx, mark_dirty
from ui.theme import pnl_colour

IB, IR = "Interbank cash (IB)", "90 day bank bills (IR)"


def _trim(x: float, fmt: str) -> str:
    return format(x, fmt).rstrip("0").rstrip(".")


def render(ctx: Ctx) -> None:
    M, as_of, mk = ctx.M, ctx.as_of, ctx.market
    C.heading("What the strip has already priced", help=(
        r"r_{\text{after}} = \frac{r_{\text{month}} N - r_{\text{before}} n_1}{n_2}",
        "An IB contract settles on the month's average cash rate, so a month containing a "
        "decision prices a day-weighted blend of the old and new rate. Inverting the blend "
        "recovers the rate expected after the meeting. A late-month meeting is read off the "
        "FOLLOWING contract instead: with two days of capture, a 1bp error in spot becomes "
        "15bp of imaginary priced move, and the RBA sits late in its months far more often "
        "than the Fed does.",
        "step / move size = implied probability", "strip"))

    live = mk.ib_live
    if not live:
        if M.meeting.end < as_of:
            st.info(f"{M.meeting.label} has already happened and its contracts have expired. "
                    "Select a future meeting to price the strip.")
        else:
            st.warning("No interbank cash rate quotes came back. Everything else still works "
                       "from the numbers you have typed.")
            first = next((q for q in mk.ib_quotes if q.error), None)
            if first:
                C.note(f"{first.symbol}: {first.error}")
        _history_panel(ctx)
        return
    if mk.ib_ref and mk.ib_ref > as_of:
        C.note(f"Quotes are from {mk.ib_ref:%d %b %Y}, after the as-of date: the strip cannot "
               "be rewound, so it always shows the latest settlement.")

    # The controls row: what you look at, the rate it is measured from, and
    # the refresh -- one label style, one bottom edge, packed left.
    with C.bar("curve"):
        bar_view, bar_spot, bar_refresh, _ = st.columns([2.1, 0.95, 0.95, 1.9],
                                                       vertical_alignment="bottom")
        with bar_view:
            view = C.choice("Instrument", [IB, IR], default=IB, key="curve_view",
                            help="IB: the meeting-by-meeting ladder. IR: each bill contract's "
                                 "90-day window, its basis to the cash path, and the spreads.")
        with bar_spot:
            spot = st.number_input(
                "Spot cash (%)", key=ctx.spot_key,
                value=float(ctx.spot_derived) if ctx.spot_derived is not None else None,
                step=0.01, format="%.4f", placeholder="type the cash rate", width=120,
                help="Backed out of the strip (a meeting-free contract, or the front month "
                     "inverted against one); falls back to the RBA's published target. Every "
                     "step is measured from here.")
        with bar_refresh:
            st.button("Refresh quotes", width="content", on_click=src.refresh_curve_quotes)
    ctx.rebuild()

    if spot is None:
        st.warning("**Spot cash cannot be derived from the strip at this anchor.** A decision "
                   "has taken effect inside the front month and another lands before the next "
                   "meeting-free contract. Type the prevailing cash rate above.")
        _history_panel(ctx)
        return
    sp = ctx.path
    if not sp.ok:
        st.warning(sp.error or "The strip could not be decomposed.")
        _history_panel(ctx)
        return
    front = live[0].implied_rate
    if front is not None and abs(float(spot) - front) > 0.50:
        st.warning(f"Spot of {float(spot):.3f}% is {abs(float(spot) - front) * 100:.0f}bp from "
                   f"what {live[0].code} implies ({front:.3f}%). One of the two is wrong.")

    asof_fig = _asof_fig(ctx)
    if view == IB:
        _interbank(ctx, sp, float(spot), live, asof_fig)
    else:
        _bills(ctx, asof_fig)
    _history_panel(ctx)
    _spread_matrix(ctx, view)


def _asof_fig(ctx: Ctx) -> C.Fig:
    """The quotes' stamp: the date the strip settled, how much of it printed,
    and when the app last asked."""
    mk = ctx.market
    refreshed = src.fetch_clock().get("ib")
    when = ""
    if refreshed:
        mins = (clock.now() - refreshed).total_seconds() / 60
        when = f" · refreshed {refreshed:%H:%M}" + (" (just now)" if mins < 1 else
                                                    f" ({mins:.0f}m ago)")
    return C.Fig("Curve as of", mk.ib_ref.strftime("%d %b %Y") if mk.ib_ref else "--",
                 f"{len(mk.ib_live)}/{len(mk.ib_quotes)} interbank{when}")


def _interbank(ctx: Ctx, sp, spot: float, live, asof_fig: C.Fig) -> None:
    M, P, t, as_of = ctx.M, ctx.P, ctx.trade, ctx.as_of
    stale = ctx.market.ib_stale
    peak, trough = sp.peak, sp.trough
    C.board([
        asof_fig,
        C.Fig("Terminal", f"{sp.terminal:.3f}%" if sp.terminal else "--",
              f"{sp.total_bp:+.0f}bp total",
              help="Where the priced path ends inside the quoted strip."),
        C.Fig("Peak priced", f"{peak.cum_bp:+.0f}bp" if peak else "--",
              peak.meeting.end.strftime("%b %Y") if peak else "",
              pnl_colour(peak.cum_bp, P) if peak else None),
        C.Fig("Trough priced", f"{trough.cum_bp:+.0f}bp" if trough else "--",
              trough.meeting.end.strftime("%b %Y") if trough else "",
              pnl_colour(trough.cum_bp, P) if trough else None),
        C.Fig("Meetings priced", f"{len(sp.steps)}",
              f"to {sp.steps[-1].meeting.end:%b %Y}" if sp.steps else ""),
    ])
    if sp.excluded_count:
        C.note(f"Headline figures ignore {sp.excluded_count} meeting"
               f"{'s' if sp.excluded_count > 1 else ''} solved off contracts that are not "
               "printing. They stay in the ladder, marked.")

    lc, rc = st.columns([1.25, 1], gap="large")
    with lc:
        C.sub("The meeting ladder")
        rows, colours, marks = [], {}, []
        for i, stp in enumerate(sp.steps):
            if stp.key == M.meeting.key:
                marks.append(i)
            flag = " ~" if stp.ambiguous else (" !" if stp.stale else "")
            rows.append([stp.label, f"{(stp.meeting.end - as_of).days}d", stp.code + flag,
                         f"{stp.step_bp:+.1f}", f"{stp.cum_bp:+.1f}", f"{stp.prob:.0%}",
                         f"{stp.r_after:.3f}"])
            colours[(i, 3)] = pnl_colour(stp.step_bp, P)
            colours[(i, 4)] = pnl_colour(stp.cum_bp, P)
        C.table(["Meeting", "Out", "Contract", "Step", "Cum", "P(move)", "Rate after"],
                rows, numeric=(1, 3, 4, 5, 6), mark_rows=marks, colours=colours)
        C.note("▸ = the sidebar's meeting. ! = quote not printing with the strip; "
               "~ = two meetings share a contract month.")
    with rc:
        C.sub("The priced path")
        st.altair_chart(charts.priced_path(sp.steps, spot, P), width="stretch", theme=None)
        if sp.excluded_count:
            C.note("Dashed line, hollow dots = solved off a stale or ambiguous contract.")

    mine = sp.step_for(M.meeting.key)
    C.section(f"{M.meeting.label} — live vs the number you are sizing on")
    if mine is None:
        st.info("This meeting falls outside the quoted strip.")
    else:
        size = float(t.get("size") or 25)
        typed = t.get("points")
        figs, act = st.columns([3, 1], vertical_alignment="center")
        with figs:
            C.board([
                C.Fig("Priced now", f"{mine.step_bp:+.2f}bp",
                      f"{mine.prob:.1%} of a {size:.0f}bp move"),
                C.Fig("You have typed", f"{typed:+.2f}bp" if typed is not None else "--",
                      "sidebar → Points priced"),
                *([C.Fig("Difference", f"{mine.step_bp - float(typed):+.2f}bp", "live minus typed",
                         pnl_colour(mine.step_bp - float(typed), P))] if typed is not None else []),
            ])
        with act:
            if st.button("Use the live number", type="primary", width="stretch"):
                t["points"] = round(abs(mine.step_bp), 2)
                mark_dirty()
                st.rerun()
        C.note(f"Read off {mine.code} — "
               + (f"the following month, wholly at the new rate, because {mine.month:%b}'s "
                  f"contract captures only {mine.capture_share:.0%}." if mine.is_clean else
                  f"the meeting month, capturing {mine.capture_share:.0%} "
                  f"({mine.days_after} of {mine.days_in_month} days) at the new rate."))

    sc1, sc2 = st.columns(2, gap="large")
    with sc1:
        C.sub("The strip")
        rows, cols = [], {}
        for i, q in enumerate(live):
            delta = q.change_one_day
            delta = 0.0 if delta == 0 else delta
            rows.append([q.code, f"{q.month:%b %Y}", _trim(q.price, ".4f"),
                         f"{q.implied_rate:.3f}",
                         _trim(delta, "+.4f") if delta is not None else "--",
                         "stale" if q.code in stale else ""])
            if delta:
                cols[(i, 4)] = P.positive if delta > 0 else P.negative
        C.table(["Code", "Month", "Price", "Implied %", "Δ price", ""], rows,
                numeric=(2, 3, 4), colours=cols)
    with sc2:
        C.sub("Cross-checks")
        flagged = [c for c in sp.checks if c.flagged]
        if flagged:
            C.table(["Month", "Implied", "Carried", "Diff"],
                    [[c.month.strftime("%b %Y"), f"{c.implied:.3f}", f"{c.expected:.3f}",
                      f"{c.diff_bp:+.1f}bp"] for c in flagged], numeric=(1, 2, 3))
            C.note("Meeting-free months should reprint the carried rate. A gap means something "
                   "outside the calendar is in the price: month-end funding, a technical "
                   "adjustment to the ES rate, or a meeting date the market disagrees with.")
        else:
            C.good(f"✓ Every meeting-free month reprints the carried rate to within "
                   f"{stripmod.DRIFT_FLAG_BP:g}bp.", P)


def _bills(ctx: Ctx, asof_fig: C.Fig) -> None:
    P, mk = ctx.P, ctx.market
    if not mk.ir_live:
        st.warning("No bank bill quotes came back.")
        first = next((q for q in mk.ir_quotes if q.error), None)
        if first:
            C.note(f"{first.symbol}: {first.error}")
        return
    cv = ctx.bills
    if not cv.ok:
        st.warning(cv.error or "No bank bill curve available.")
        return

    mb, fr = cv.mean_basis_bp, cv.front
    C.board([
        asof_fig,
        C.Fig("Mean basis", f"{mb:+.1f}bp" if mb is not None else "--",
              f"{len(cv.comparable_periods)} comparable windows",
              help="The bill's implied BBSW less the interbank cash path averaged over the "
                   "same 90 days. BBSW is a bank credit rate and cash is not, so a positive "
                   "spread is normal; a wide one is funding stress."),
        C.Fig("Front window", f"{fr.basis_bp:+.1f}bp" if fr and fr.comparable else "--",
              fr.code if fr else ""),
        C.Fig("IB covers to", mk.horizon.strftime("%b %Y") if mk.horizon else "--",
              "basis stops there"),
        C.Fig("IR printing", f"{len(cv.periods)}",
              f"{cv.periods[0].code} – {cv.periods[-1].code}" if cv.periods else ""),
    ])

    pc1, pc2 = st.columns([2, 1], gap="large")
    with pc1:
        C.sub("What each contract spans")
        rows, colours = [], {}
        for i, per in enumerate(cv.periods):
            rows.append([per.code + (" !" if per.stale else ""), per.window_label,
                         f"{(per.covers_end - per.covers_start).days}",
                         f"{len(per.meetings_covered)} ({per.covered_label})"
                         if per.meetings_covered else "0",
                         f"{per.price:.4f}" if per.price is not None else "--",
                         f"{per.implied:.3f}",
                         f"{per.expected:.3f}" if per.expected is not None else "--",
                         f"{per.basis_bp:+.1f}" if per.comparable else "--",
                         f"{per.policy_bp:+.0f}" if per.policy_bp is not None else "--"])
            if per.comparable and per.basis_flagged:
                colours[(i, 7)] = P.critical
            if per.policy_bp is not None:
                colours[(i, 8)] = pnl_colour(per.policy_bp, P)
        C.table(["Contract", "Reference window", "Days", "Meetings", "Price", "BBSW %",
                 "IB path %", "Basis bp", "Policy bp"], rows, numeric=(2, 4, 5, 6, 7, 8),
                colours=colours)
        C.note("Policy bp is the interbank path's move inside the window; basis is what is "
               "left once that path is priced in. ! = quote not printing with the strip.")
    with pc2:
        C.sub("Basis by window")
        comparable = [p for p in cv.comparable_periods if not p.stale]
        if comparable:
            st.altair_chart(charts.bbsw_basis(comparable, P), width="stretch", theme=None)
        else:
            C.note("No window is covered by both strips.")

    _bill_scenarios(ctx)

    d1, d2 = st.columns(2, gap="large")
    with d1:
        C.sub("Calendar spreads")
        rows, cols = [], {}
        for i, spd in enumerate(cv.spreads):
            rows.append([spd.label, f"{spd.market_bp:+.1f}",
                         f"{spd.path_bp:+.1f}" if spd.path_bp is not None else "--",
                         f"{spd.diff_bp:+.1f}" if spd.diff_bp is not None else "--",
                         f"{spd.meetings_between}"])
            cols[(i, 1)] = pnl_colour(spd.market_bp, P)
            if spd.diff_bp is not None:
                cols[(i, 3)] = pnl_colour(spd.diff_bp, P)
        C.table(["Spread", "Market", "IB path", "Diff", "Mtgs"], rows, numeric=(1, 2, 3, 4),
                colours=cols)
    with d2:
        C.sub("Where the algebra closes")
        solved = [x for x in cv.periods if x.determinate and x.cash_equivalent_step_bp is not None
                  and x.cash_equivalent_prob is not None]
        if solved:
            C.table(["Contract", "Meeting", "Step", "P(move)"],
                    [[x.code, x.meetings_before[0].label, f"{x.cash_equivalent_step_bp:+.1f}bp",
                      f"{x.cash_equivalent_prob:.0%}"] for x in solved], numeric=(2, 3))
            C.note("Only a window with exactly one undecided meeting is determined, read off "
                   "the cash-equivalent rate: today's BBSW/cash spread stripped off the bill, "
                   "which assumes that spread holds to the fix.")
        else:
            C.note("No quoted window holds exactly one undecided meeting. Use the interbank "
                   "ladder.")

    spot = mk.spot
    hist = rba.historical_bbsw_ois_basis()
    bits = []
    if spot["spot_basis_bp"] is not None:
        bits.append(f"Today's 3-month BBSW {spot['bbsw']:.3f}% less the {spot['cash']:.3f}% "
                    f"target is {spot['spot_basis_bp']:+.1f}bp.")
    if mb is not None and hist:
        bits.append(f"The published BBSW/OIS spread averaged "
                    f"{sum(o.value for o in hist) / len(hist):.1f}bp over 2011–2022 before the "
                    "RBA retired it — the only yardstick for whether today's basis is wide.")
    C.note(" ".join(bits) + " Prices: ASX IB and IR end-of-day settlement, delayed. IR settles "
           "on a single 3-month BBSW fix, not an average, so there is no month to un-blend.")


def _bill_scenarios(ctx: Ctx) -> None:
    """The IR scenario grid (`ui.scenarios`), kept as its own hook."""
    from ui import scenarios
    scenarios.bill_grid(ctx)


def _history_panel(ctx: Ctx) -> None:
    """The rate each strip settles ON, daily, against the strip quoted today."""
    P, as_of = ctx.P, ctx.as_of
    C.heading("Instrument history", "the settlement rate each contract prices, daily", help=(
        "", "Neither ASX strip carries a price history to read a contract's own past off. "
            "What is available, keyless and daily, is the RBA's own published rate each "
            "contract settles on: AONIA for IB (its fix is the simple average across the "
            "delivery month) and 3-month BBSW for IR (its fix IS a single BBSW print). The "
            "dashed line is the strip quoted today, one point per delivery month — the check "
            "is whether it continues from where the rate has actually been printing.",
        "", "curve-hist"))
    with C.bar("history"):
        b1, _ = st.columns([1.6, 3], vertical_alignment="bottom")
        with b1:
            window = C.history_window("hist_window_curve", default="3Y",
                                      help="Scopes both history charts.")
    start = C.window_start(window, as_of)
    mk = ctx.market
    hc1, hc2 = st.columns(2, gap="medium")
    with hc1:
        ib_hist = src.capped(src.cash_rate_history(), as_of)
        if ib_hist:
            st.altair_chart(charts.instrument_history(
                ib_hist, [(q.month, q.implied_rate) for q in mk.ib_live], P, start,
                charts.Vintage(last_modified=ib_hist[-1][0]), title="IB — interbank cash rate"),
                width="stretch", theme=None)
            C.legend([("AONIA, daily (RBA F1)", P.ink_secondary),
                      ("Live strip, by delivery month", P.accent)])
        else:
            C.note("No RBA cash rate history available.")
    with hc2:
        ir_hist = src.capped(src.bbsw_history(), as_of)
        if ir_hist:
            st.altair_chart(charts.instrument_history(
                ir_hist, [(q.month, q.implied_rate) for q in mk.ir_live], P, start,
                charts.Vintage(last_modified=ir_hist[-1][0]), title="IR — 3-month BBSW"),
                width="stretch", theme=None)
            C.legend([("3-month BBSW, daily (RBA F1)", P.ink_secondary),
                      ("Live strip, by delivery month", P.accent)])
        else:
            C.note("No BBSW history available.")
    C.note("IB's fix is a monthly average, so one day's AONIA is an input to a settlement, not "
           "a settlement; IR's fix is a single day, so 100 − 3-month BBSW on any date is what "
           "that contract would settle at if that were its last trading day. Neither line "
           "carries the term premium a real futures quote would.")


def _spread_matrix(ctx: Ctx, view: str) -> None:
    from ui import meeting_spreads
    meeting_spreads.render(ctx, view == IB)
