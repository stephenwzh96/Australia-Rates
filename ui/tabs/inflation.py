"""Inflation: sequential trend, breadth and dispersion, contributions and the
underlying measures, then the drivers -- the US Inflation tab's layout, in the
Australian CPI."""

from __future__ import annotations

from datetime import date

import streamlit as st

from core import econ_calendar
from core import inflation as infl
from ui import charts
from ui import components as C
from ui import inflation_panels as panels
from ui import sources as src
from ui.context import Ctx


def render(ctx: Ctx) -> None:
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.section("Sequential underlying inflation trend",
                  "month-over-month % change · dashed line = the 2.5% annual pace")
    with ctl:
        window = C.history_window("inflation_window",
                                  help="Scopes the trend, breadth, composition and underlying "
                                       "charts.")
    start = C.window_start(window, ctx.as_of)
    panels.trend(ctx, start)

    cpi = src.cpi_series()
    vintage = charts.Vintage(next_release=src.next_release(econ_calendar.CPI_Q, ctx.today),
                             last_modified=cpi["as_at"])
    if not cpi["leaves"]:
        st.warning("No CPI class data. The ABS workbooks did not download — the Data tab has "
                   "the detail and a refresh button.")
    else:
        breadth_col, disp_col = st.columns(2, gap="large")
        with breadth_col:
            _breadth(ctx, cpi, start, vintage)
        with disp_col:
            panels.dispersion_panel(ctx)
        panels.contributions(ctx, start)
        _basket(ctx, cpi, start, vintage)
    panels.drivers_panel(ctx)


def _breadth(ctx: Ctx, cpi: dict, start, vintage) -> None:
    P = ctx.P
    C.heading("Breadth of CPI inflation", "share of the basket rising faster than the threshold",
              help=("", "How much of the basket is moving, not how fast the headline moved: a "
                        "headline pulled up by a few classes reads very differently from one "
                        "where most prices are rising. The 87 expenditure classes are counted "
                        "equally, over four quarters and over two quarters annualised — the US "
                        "chart's 12-month and 6-month windows at a quarterly cadence.", "",
                    "breadth"))
    threshold = C.choice("Threshold", (2.5, 3.0, 4.0), default=3.0,
                         format_func=lambda v: f"above {v:g}% annualised",
                         key="breadth_threshold", label_visibility="collapsed")
    min_live = int(0.8 * len(cpi["leaves"]))
    windows = {w: [(pt.when, pt.share_by_count)
                   for pt in infl.breadth(cpi["classes"], cpi["weights"], float(threshold), q,
                                          min_live) if pt.when <= ctx.as_of]
               for w, q in ((12, 4), (6, 2))}
    norm = infl.mean_over(windows[12], date(1993, 1, 1), date(2019, 12, 31))
    st.plotly_chart(charts.cpi_breadth_windows(windows, P, norm=norm, window_start=start,
                                               threshold=float(threshold), vintage=vintage),
                    width="stretch")
    y, h = windows[12], windows[6]
    top = max(((d, v) for d, v in y if d >= date(2020, 1, 1)), key=lambda t: t[1], default=None)
    if y and h:
        C.stat_row([("Year-ended", f"{y[-1][1]:.0f}%", None),
                    ("2 quarters a.r.", f"{h[-1][1]:.0f}%", None),
                    ("1993–2019 avg", f"{norm:.0f}%" if norm is not None else "—", None),
                    ("Post-2020 peak", f"{top[1]:.0f}% ({top[0]:%b %Y})" if top else "—", None)])


def _basket(ctx: Ctx, cpi: dict, start, vintage) -> None:
    """The four Australian CPI charts the US tab has no single twin for: the
    composition stack, the measures the Board targets, breadth against the
    trimmed mean, and the cyclical split."""
    P = ctx.P
    buckets, cyclical = src.cpi_classification()
    classes, weights = cpi["classes"], cpi["weights"]
    C.heading("The basket and the measures the Board targets",
              f"{len(cpi['leaves'])} expenditure classes at {cpi['as_at']:%b %Y} · residual "
              f"{cpi['residual']:+.2f} index points", level="sub", help=(
                  "", "The classes are recovered from the ABS contribution hierarchy (a parent's "
                      "contribution is the sum of its children's), not a hardcoded list. Weights "
                      "are the latest published basket held fixed back through history. The "
                      "composition buckets and the cyclical split are analytical "
                      "classifications, not published series, and live in "
                      "meetings/_cpi_classification.json as data you can edit; a class no "
                      "bucket claims lands in a residual rather than disappearing.",
                  "", "basket"))
    r1a, r1b = st.columns(2, gap="medium")
    with r1a:
        if not buckets:
            st.info("No bucket classification — see meetings/_cpi_classification.json.")
        else:
            grouped = infl.bucket_contributions(weights, buckets, cpi["leaves"])
            pp = infl.contribution_to_change(grouped, infl.sub_index(weights, cpi["leaves"]))
            order = [b.name for b in buckets]
            if any(pp.get(infl.RESIDUAL)):
                order.append(infl.RESIDUAL)
            st.altair_chart(charts.cpi_composition(pp, cpi["headline_q"], order, P, start,
                                                   vintage), width="stretch", theme=None)
            C.legend([(n, P.categorical[i % len(P.categorical)]) for i, n in enumerate(order)]
                     + [("CPI, % q/q", P.ink)])
    with r1b:
        q, m, x = ("Quarterly trimmed mean CPI", "Monthly trimmed mean CPI",
                   "Monthly CPI ex volatiles and holiday travel")
        st.altair_chart(charts.cpi_underlying({q: cpi["trimmed_yr"], m: cpi["monthly_trimmed_yr"],
                                               x: cpi["monthly_ex_volatiles_yr"]},
                                              [q, m, x], P, start, vintage),
                        width="stretch", theme=None)
        C.legend([(q, P.categorical[0]), (m, P.categorical[1]), (x, P.categorical[2]),
                  ("2–3% target band", P.grid)])
    r2a, r2b = st.columns(2, gap="medium")
    with r2a:
        wide = infl.breadth(classes, weights, infl.ABOVE_MIDPOINT, 1, int(0.8 * len(cpi["leaves"])))
        share = [(pt.when, pt.share_by_weight) for pt in wide]
        lo, hi = date(1993, 1, 1), date(2019, 12, 31)
        st.altair_chart(charts.cpi_breadth(share, cpi["trimmed_q"], infl.mean_over(share, lo, hi),
                                           infl.mean_over(cpi["trimmed_q"], lo, hi), "1993–2019",
                                           P, start, vintage), width="stretch", theme=None)
        C.legend([("% of basket rising > 2.5% annualised", P.categorical[1]),
                  ("Trimmed mean, % q/q", P.categorical[0]), ("1993–2019 average", P.muted)])
    with r2b:
        if not cyclical:
            st.info("No cyclical classification — see meetings/_cpi_classification.json.")
        else:
            split = infl.cycle_split(weights, cyclical, cpi["leaves"])
            st.altair_chart(charts.cpi_cycle(split, P, start, vintage), width="stretch",
                            theme=None)
            C.legend([(f"'Cyclical' — {split.n_cyclical} classes", P.categorical[0]),
                      (f"'Non-cyclical' — {split.n_non_cyclical} classes", P.categorical[1])])
    r3a, r3b = st.columns(2, gap="medium")
    with r3a:
        pts = [pt for pt in infl.breadth(classes, weights, infl.ABOVE_BAND, 1,
                                         int(0.8 * len(cpi["leaves"]))) if pt.when <= ctx.as_of]
        st.altair_chart(charts.cpi_above_threshold(pts, P, infl.ABOVE_BAND, start, vintage),
                        width="stretch", theme=None)
        C.legend([("By number of items", P.categorical[0]),
                  ("By weight of price categories", P.categorical[1])])
    with r3b:
        C.note("**What is a judgement here, and what is not.** The breadth counts and the "
               "underlying measures are arithmetic — a threshold, a count, a weight, and the "
               "ABS's own trim. The composition buckets and the cyclical split are not "
               "published series: they are analytical classifications, different houses draw "
               "them differently, and they live in meetings/_cpi_classification.json as data "
               "you can edit.")
    C.note("Quarterly series are seasonally adjusted (ABS Appendix 1a); the monthly measures "
           "start where the complete monthly CPI does. The breadth-vs-trimmed-mean chart is two "
           "stacked panels rather than a dual axis, where crossings would be an artefact of "
           "where the axes were pinned.")
