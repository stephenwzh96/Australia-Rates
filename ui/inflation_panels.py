"""The US Inflation tab's panels in the Australian CPI: the sequential trend,
dispersion, the contribution stacks with the item monitor and the month's
largest contributors, and the drivers (expectations and wages)."""

from __future__ import annotations

import streamlit as st

from core import cpi_monthly, dispersion, econ_calendar
from core import inflation_drivers as drivers
from core import publication as pub
from ui import charts
from ui import components as C
from ui import contributions as contrib
from ui import sources as src
from ui.context import Ctx


def _capped(series, as_of, family):
    return src.published(series, family, as_of)


def trend(ctx: Ctx, start) -> None:
    as_of = ctx.as_of
    panel = src.fetch_cpi_monthly()
    monthly = cpi_monthly.monthly_trend(panel)
    quarterly = cpi_monthly.quarterly_trend(src.rba_table("g1"))
    series = [cpi_monthly.TrendSeries(
        s.code, s.label, s.panel,
        _capped(s.observations, as_of,
                pub.CPI_MONTHLY if s.panel == "Monthly" else pub.CPI_QUARTERLY),
        s.headline) for s in monthly + quarterly]
    live = [s for s in series if s.ok]
    if not live:
        st.warning("No CPI series could be loaded — the Data tab has each source's status.")
        return
    vintages = {"Monthly": charts.Vintage(src.next_monthly_cpi(ctx.today),
                                          src.cpi_monthly_vintage()),
                "Quarterly": charts.Vintage(src.next_release(econ_calendar.CPI_Q, ctx.today),
                                            src.cpi_published(as_of).get("as_at"))}
    figures = charts.inflation_trend(live, ctx.P, window_start=start, vintages=vintages)
    cols = st.columns(len(figures), gap="medium")
    for col, fig in zip(cols, figures):
        with col:
            st.plotly_chart(fig, width="stretch")
    readings = cpi_monthly.latest_readings(live)
    if readings:
        C.sub("Latest readings")
        C.table(["Indicator", "Cadence", "Period", "Change", "Annualised"],
                [[r["indicator"], r["panel"], r["date"].strftime("%b %Y"), f"{r['change']:+.2f}%",
                  f"{r['annualised']:+.1f}%"] for r in readings], numeric=(3, 4))
        C.note("The monthly CPI is the ABS's complete monthly measure, history from April 2024; "
               "the quarterly series run back to 1982 (RBA table G1). Seasonally adjusted "
               "except where marked original.")


def dispersion_panel(ctx: Ctx) -> None:
    P = ctx.P
    C.heading("CPI inflation dispersion", "quarterly change vs share of the basket above it",
              help=(r"x_t = 100\sum_c w_{c,t-1}\,\mathbb{1}[\pi_{c,t} > \pi_t] \qquad y_t = \pi_t",
                    "Each quarter is one point: the basket's change against the CPI weight whose "
                    "own price rose faster. The downward slope is arithmetic; read the vertical "
                    "gap between eras at the balanced share, which strips it out. Quarterly, "
                    "where the US scatter is monthly: Australia's expenditure classes are "
                    "published by quarter back to the 1980s.",
                    f"balanced share = {dispersion.BALANCED_SHARE:.0f}% · y is the basket "
                    "re-aggregated on its own (previous-quarter) weights — see core.dispersion",
                    "cpidisp"))
    cpi = src.cpi_published(ctx.as_of)
    if not cpi.get("classes"):
        st.info("The CPI expenditure classes are not loaded.")
        return
    disp = dispersion.from_classes(cpi["classes"], cpi["weights"])
    if not disp.ok:
        st.info("Not enough quarters to place the scatter.")
        return
    st.altair_chart(charts.cpi_dispersion(disp, P, label_months=4), width="stretch", theme=None)
    C.stat_row([(reg.label, "—" if reg.fit is None else f"{reg.fit.balanced_annualised:.1f}%",
                 None) for reg in disp.regimes])
    latest = f"Latest data: {disp.latest.month:%b %Y} quarter · " if disp.latest else ""
    C.note(f"{latest}{len(cpi['classes'])} expenditure classes · balanced-share pace "
           "annualised per era · Source: ABS.")


def contributions(ctx: Ctx, start) -> None:
    P, as_of = ctx.P, ctx.as_of
    panel = src.published_map(src.fetch_cpi_monthly(), pub.CPI_MONTHLY, as_of)
    nodes = src.cpi_tree()
    last = max((rows[-1][0] for rows in panel.values() if rows), default=None)
    vintage = charts.Vintage(src.next_monthly_cpi(ctx.today), last)
    C.section("CPI inflation contributions",
              "by expenditure group · the ABS's own contribution measures")
    figs = cpi_monthly.group_contributions(panel, nodes)
    c1, c2 = st.columns(2, gap="medium")
    for col, k in ((c1, "yoy"), (c2, "mom")):
        with col:
            st.plotly_chart(contrib.contribution_stack(figs[k], P, window_start=start,
                                                       as_of=as_of, vintage=vintage),
                            width="stretch")
    table = cpi_monthly.breakdown(panel, nodes, src.cpi_weights(), max_level=2)
    if table.empty:
        C.note("Australian CPI breakdowns — unavailable. Use the Data tab to refresh.")
    else:
        st.markdown(charts.cpi_breakdown_table(table, P, vintage), unsafe_allow_html=True)
        C.note("Source: ABS monthly CPI, seasonally adjusted where the ABS adjusts the item, "
               "original otherwise — hover any cell for its basis. Weights are the CPI basket "
               "weights from the latest annual reweight.")
    granular = cpi_monthly.class_contributions(panel, nodes)
    st.plotly_chart(contrib.granular_diverging(granular.between(None, as_of), P,
                                               vintage=vintage), width="stretch")


def drivers_panel(ctx: Ctx) -> None:
    """What feeds the next prints: expectations and the cost of labour."""
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.heading("Inflation drivers", "expectations and the cost of labour", help=(
            "", "RBA table G3 carries every expectations survey the Bank reads: consumers "
                "(Melbourne Institute) run highest and noisiest, market economists' two-year "
                "view is the anchor read, and the 10-year bond break-even is the long-run "
                "market measure. The Wage Price Index holds the mix of jobs fixed, as the US "
                "Employment Cost Index does.",
            "", "drivers"))
    with ctl:
        window = C.history_window("drivers_window",
                                  help="Scopes the expectations and WPI charts.")
    start = C.window_start(window, ctx.as_of)
    g3, h4 = src.rba_table("g3"), src.rba_table("h4")
    left, right = st.columns(2, gap="large")
    with left:
        lines = [drivers_line(ln, ctx.as_of, pub.EXPECTATIONS)
                 for ln in drivers.expectation_lines(g3)]
        st.plotly_chart(charts.inflation_expectations(
            lines, ctx.P, window_start=start,
            vintage=charts.Vintage(None, max((ln.observations[-1].date for ln in lines
                                              if ln.ok), default=None))), width="stretch")
        reads = drivers.expectation_readings(g3)
        if reads:
            C.table(["Survey", "Quarter", "Expected"],
                    [[r["label"], f"{r['date']:%b %Y}", f"{r['value']:.1f}%"] for r in reads],
                    numeric=(2,))
    with right:
        lines = [drivers_line(ln, ctx.as_of, pub.WPI) for ln in drivers.wpi_lines(h4)]
        st.plotly_chart(charts.wage_price_index(
            lines, ctx.P, window_start=start,
            vintage=charts.Vintage(src.next_release(econ_calendar.WPI, ctx.today), None)),
            width="stretch")


def drivers_line(ln, as_of, family):
    from core.consumer import Line
    return Line(ln.key, ln.label, src.published(ln.observations, family, as_of))
