"""Labour: the full-employment panel and its slack heatmap, diffusion, the
labour-market charts, the unemployment-rate decomposition, employment and
wages -- the US Labour tab's layout, in Australian series."""

from __future__ import annotations

from datetime import date

import streamlit as st

from core import employment as empmod
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx, mark_dirty

#: The panel compares each indicator against an earlier reading. Fixed
#: lookbacks rather than a date box: a named span is a decision a reader can
#: repeat, and the published version of this panel compares against the prior
#: year end.
COMPARE_WINDOWS = ("3M", "6M", "1Y", "2Y", "Prior year end")


def _num(x):
    """Text input to float, or None -- an empty manual field stays empty rather
    than collapsing to 0.0 and scoring as a real reading."""
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _compare_date(choice: str, anchor: date) -> date:
    """The date the second dot reads at."""
    months = {"3M": 3, "6M": 6, "1Y": 12, "2Y": 24}
    if choice not in months:
        return date(anchor.year - 1, 12, 31)          # prior year end
    year, month = anchor.year, anchor.month - months[choice]
    while month <= 0:
        year, month = year - 1, month + 12
    days = [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(anchor.day, days[month - 1]))


def render(ctx: Ctx) -> None:
    data = {k: (src.capped(v, ctx.as_of) if not k.startswith("_") else v)
            for k, v in src.abs_series().items()}
    _full_employment(ctx, data)

    from ui import labour_panels
    labour_panels.slack_and_diffusion(ctx)

    st.divider()
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.section("Recent history", "capacity, flows, the labour-market charts and employment")
    with ctl:
        hist = C.history_window("hist_window_labour", default="10Y",
                                help="Scopes the charts below. The full-employment panel "
                                     "always scores against its full window.")
    start = C.window_start(hist, ctx.as_of)
    _capacity_and_flows(ctx, data, start)
    labour_panels.labour_market(ctx, start)
    labour_panels.decomposition(ctx)
    labour_panels.employment_and_wages(ctx, start)


def _full_employment(ctx: Ctx, data: dict) -> None:
    P, S = ctx.P, ctx.S
    asof_data = src.data_asof(data)
    head, ctl = st.columns([3, 1.3], vertical_alignment="bottom")
    with head:
        C.heading("Australian labour: full employment indicators",
                  charts.Vintage(next_release=src.next_release("Labour Force", ctx.today),
                                 last_modified=asof_data).line,
                  help=(r"z = \frac{x - \mu_{[\text{start},\,\text{end}]}}"
                        r"{\sigma_{[\text{start},\,\text{end}]}}"
                        r"\qquad z \mapsto -z \ \text{ for a slack measure}",
                        "Every row is a z-score: how far today's reading sits from that series' "
                        "own window average, in standard deviations. Right of zero is a tighter "
                        "labour market than that average, so the slack measures have their sign "
                        "flipped. Deliberately a z-score rather than the RBA's gap-from-trend "
                        "version of the same idea: the RBA publishes neither the filters it "
                        "detrends with nor how it rescales, so that chart can only be read off, "
                        "never rebuilt. Here every term comes from the data and one date range.",
                        "see core.employment", "employment"))
    with ctl:
        compare = C.choice("Compare against", COMPARE_WINDOWS, default="Prior year end",
                           key="compare_window_labour", label_visibility="collapsed",
                           help="The second dot on each row, and the direction of travel every "
                                "row's rule shows. The published version of this panel "
                                "compares against the prior year end.")
    emp_state = S.setdefault("employment", {})
    prior = _compare_date(compare, asof_data)
    emp_state["prior_date"] = prior.isoformat()

    w_start = int(emp_state.get("window_start") or 2000)
    w_end = int(emp_state.get("window_end") or 2020)
    manual = emp_state.setdefault("manual", {})
    wstart, wend = date(w_start, 1, 1), date(w_end, 12, 31)
    rows = []
    for ind in empmod.INDICATORS:
        if ind.manual:
            m = manual.setdefault(ind.key, {})
            rows.append(empmod.manual_row(ind, _num(m.get("value")), _num(m.get("mean")),
                                          _num(m.get("sd")), _num(m.get("prior")), asof_data,
                                          prior))
        else:
            rows.append(empmod.score(ind, data.get(ind.key, []), prior, wstart, wend))
    panel = empmod.Panel(rows, prior, (wstart, wend))
    prior_label = f"{prior:%b %Y}"

    if panel.total_z is not None:
        C.board([
            C.Fig("Total (mean z)", f"{panel.total_z:+.2f}",
                  "" if panel.total_delta is None else f"{panel.total_delta:+.2f} since {prior_label}",
                  P.positive if panel.total_z > 0 else P.negative,
                  help="Unweighted mean of every scored row. Above zero is a labour market "
                       "tighter than its own window average."),
            C.Fig("Tighter than average", f"{panel.n_tighter} of {panel.n_scored}",
                  colour=P.positive),
            C.Fig(f"Tightening since {prior_label}", f"{panel.n_tightening} of {panel.n_scored}"),
            C.Fig("Scoring window", f"{w_start}–{w_end}", "change below"),
            C.Fig("Data through", f"{asof_data:%b %Y}"),
        ])
    C.legend([(f"{prior_label} — {compare.lower()}", P.muted), ("Current", P.serious)])
    st.altair_chart(charts.full_employment(rows, panel.total_z, panel.total_prior_z,
                                           prior_label, P), width="stretch", theme=None)
    C.note(f"Muted dot is {prior_label}, the coloured dot the latest print, the rule between them "
           f"the travel since. Right of zero is tighter than the {w_start}–{w_end} average. The "
           "Total is an unweighted mean because these indicators overlap heavily — "
           "underutilisation is literally unemployment plus underemployment.")

    stale = panel.oldest_reading
    if stale is not None and panel.as_of and stale.current.when < panel.as_of:
        C.note(f"The panel is only as current as its stalest row: {stale.indicator.label} stands "
               f"at {stale.current.when:%b %Y} against {panel.as_of:%b %Y} for the rest.")

    with st.expander("Series, scoring window and the rows with no free feed"):
        C.table(["Indicator", "Last", "As of", "z now", f"z {prior_label}", "Change", "Source"],
                [[r.indicator.label,
                  "—" if not r.ok else f"{r.current.value:,.{r.indicator.decimals}f}{r.indicator.unit}",
                  "—" if not r.ok else f"{r.current.when:%b %Y}",
                  "—" if not r.ok else f"{r.current.z:+.2f}",
                  "—" if not r.ok or r.prior is None else f"{r.prior.z:+.2f}",
                  "—" if r.delta_z is None else f"{r.delta_z:+.2f}",
                  r.indicator.source] for r in rows],
                numeric=(1, 3, 4, 5), nowrap=True)
        with C.bar("emp_window"):
            a, b, _ = st.columns([1, 1, 3], vertical_alignment="bottom")
            with a:
                emp_state["window_start"] = int(st.number_input(
                    "Window start", value=w_start, min_value=1978,
                    max_value=ctx.today.year - 5, step=1, on_change=mark_dirty, width=110))
            with b:
                emp_state["window_end"] = int(st.number_input(
                    "Window end", value=w_end, min_value=int(emp_state["window_start"]) + 4,
                    max_value=ctx.today.year, step=1, on_change=mark_dirty, width=110))
        C.note("2000–2020 by default: two cycles, stopping before the pandemic would put its own "
               "extremes into the yardstick. Commercial rows (NAB's survey, ANZ-Indeed) take a "
               "typed reading plus the window mean and sd — a z cannot be typed directly, on "
               "purpose: nothing would show what it was measured against.")
        for ind in empmod.INDICATORS:
            if not ind.manual:
                continue
            m = manual.setdefault(ind.key, {})
            st.markdown(f"**{ind.label}** — {ind.source}")
            q1, q2, q3, q4 = st.columns(4)
            for col, k, lbl in ((q1, "value", "Latest"), (q2, "prior", prior_label),
                                (q3, "mean", f"{w_start}–{w_end} mean"), (q4, "sd", "window sd")):
                with col:
                    raw = st.text_input(lbl, value=str(m.get(k) or ""), key=f"emp_{ind.key}_{k}",
                                        on_change=mark_dirty)
                    m[k] = raw.strip() or None
        if panel.missing:
            st.caption("Not scored: " + ", ".join(f"{r.indicator.label} ({r.error})"
                                                  for r in panel.missing))


def _capacity_and_flows(ctx: Ctx, data: dict, start) -> None:
    P = ctx.P
    monyy = lambda d: d.strftime("%b '%y")
    lr1, lr2 = st.columns(2, gap="medium")
    vintage = charts.Vintage(next_release=src.next_release("Labour Force", ctx.today),
                             last_modified=src.data_asof(data))
    with lr1:
        capacity = src.capped(src.manual_series(src.NAB_CAPACITY), ctx.as_of)
        capacity_ma = (src.trailing_mean(capacity, src.CAPACITY_MA_WINDOW) if capacity else None)
        unemployment = data.get("unemployment", [])
        st.altair_chart(charts.capacity_vs_unemployment(capacity, unemployment, P, start,
                                                        vintage, capacity_ma=capacity_ma),
                        width="stretch", theme=None)
        C.legend(([(f"NAB capacity utilisation, inverted, lhs (to {monyy(capacity[-1][0])})",
                    charts.MA_BLUE)] if capacity else [])
                 + [("Unemployment rate, rhs"
                     + (f" (to {monyy(unemployment[-1][0])})" if unemployment else ""),
                     P.categorical[7])])
        C.note("NAB's series is commercial with no public feed; when it is not loaded only the "
               "unemployment rate is drawn." if not capacity else
               f"The navy line is a trailing {src.CAPACITY_MA_WINDOW}-month average of the raw "
               "prints (grey). Utilisation 70.0–87.5 inverted on the left, unemployment 3–9 on "
               "the right.")
    with lr2:
        flows = src.flow_series()
        if not flows.get("finding"):
            st.info("No flow data — the ABS gross-flows cube did not download. See the Data tab.")
            return
        f_lbl = f"Job-finding (fwd 1q, to {monyy(flows['finding'][-1][0])})"
        s_lbl = f"Job-switching (fwd 1q, to {monyy(flows['switching'][-1][0])})"
        w_lbl = f"Quarterly WPI growth (to {monyy(flows['wages'][-1][0])})"
        fkeys = {f_lbl: flows["z_finding"], s_lbl: flows["z_switching"], w_lbl: flows["z_wages"]}
        order = [k for k, v in fkeys.items() if v]
        dark = P.mode == "dark"
        colours = {w_lbl: "#5b9bd5" if dark else "#08203f", f_lbl: "#a8a8a8" if dark else "#8a8a8a",
                   s_lbl: "#9ecbff" if dark else "#6baed6"}
        dashes = {w_lbl: [1, 0], f_lbl: [2, 3], s_lbl: [2, 3]}
        st.altair_chart(charts.labour_flows(fkeys, order, P, start, vintage, colors=colours,
                                            dashes=dashes), width="stretch", theme=None)
        C.legend([(n, colours.get(n, P.categorical[i])) for i, n in enumerate(order)])
        C.note("The flow measures are plotted one quarter ahead, so each lines up with the wage "
               "print it precedes. Job-switching is a proxy — Australia publishes no quits rate, "
               "so it counts everyone under a year with their current employer.")
