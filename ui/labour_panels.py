"""The US Labour tab's panels in Australian series: the overall slack indicator
and its heatmap, diffusion and its channels, the labour-market grid, the
unemployment-rate decomposition, employment by component and the wage heatmap.

Layout and controls follow `ui/tabs/labour.py` in the FOMC app line for line;
the data is `core.labor` (AU indicators), `core.labor_flows` (ABS gross flows
rebuilt on the stocks) and `core.jobs_wages` (ABS Labour Account and WPI).
"""

from __future__ import annotations

import textwrap
from datetime import date

import streamlit as st

from core import econ_calendar, jobs_wages, labor, labor_flows
from core import publication as pub
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx


def _live(ctx: Ctx) -> dict:
    """Every series cut to what had printed by the as-of: exact Labour Force
    days from the ABS release pages, each other family's late rule
    (`core.publication`)."""
    return labor.as_published(src.fetch_labor_series(), ctx.as_of, {},
                              src.fetch_lf_release_dates())


def _vintage(ctx: Ctx, release: str = econ_calendar.LABOUR) -> charts.Vintage:
    data = src.fetch_labor_series()
    last = max((s[-1][0] for s in data.values() if s), default=None)
    return charts.Vintage(next_release=src.next_release(release, ctx.today), last_modified=last)


# ------------------------------------------------------------ overall + heatmap

def slack_and_diffusion(ctx: Ctx):
    """Draws the dashboard through the definitions table; returns the history
    window's start, which scopes every chart below it."""
    P, as_of = ctx.P, ctx.as_of
    live = _live(ctx)
    composite = _vintage(ctx)
    head, ctl = st.columns([3, 1], vertical_alignment="bottom")
    with head:
        C.heading("Australian labour: overall indicator", composite.line, help=(
            r"\text{slack pct} = \begin{cases} P(x) & \text{high} = \text{tighter} \\"
            r"100 - P(x) & \text{high} = \text{more slack}\end{cases}",
            "Every indicator is percentile-ranked against its own 2000-present history, then "
            "flipped where high means more slack, so incompatible units share one axis. "
            "Position is where slack sits; colour is which way it moved. The engine is the "
            "US dashboard's; the indicators are what Australia publishes — the Labour Force "
            "and its gross flows, vacancies, the NAB survey, wages and sentiment.",
            "see core.labor", "labor"))
    with ctl:
        window = st.selectbox("Change window", labor.CHANGE_WINDOWS,
                              index=labor.CHANGE_WINDOWS.index(labor.CHANGE_MONTHS),
                              format_func=lambda m: f"{m}-month change",
                              help="Applies to the 'Change' rows. 12 months is the steadier "
                                   "read; shorter turns faster but is noisier.")
    latest_readings = labor.dashboard(live, change_months=window)
    if not any(r.ok for r in latest_readings):
        st.warning("No labour series could be loaded — check the network; the Data tab has "
                   "each source's status.")
        return None
    month = labor.complete_month(live, window)
    full = labor.through_month(live, month)
    readings = labor.dashboard(full, change_months=window)
    summary = labor.summarise(readings)
    stalled = labor.stalled(live, window)
    C.board([
        C.Fig("Median percentile", f"{summary['median_pct']:.0f}%",
              help="Under 50 is more slack than its own history."),
        C.Fig("Tightening", f"{summary['tightening']} of {summary['n']}", colour=P.positive),
        C.Fig("Cooling", f"{summary['cooling']} of {summary['n']}", colour=P.negative),
        C.Fig("Little changed", f"{summary['flat']} of {summary['n']}"),
        C.Fig("Data through", f"{month:%b %Y}" if month else "—",
              help="The latest month every live indicator has printed. The dots, the counts "
                   "and the diffusion charts stop here, so a month never mixes fresh and "
                   "stale rows; the heatmap runs each series to its latest print."),
    ])
    C.legend([("Cooling — toward more slack", P.negative),
              ("Tightening — toward less slack", P.positive), ("Little changed", P.muted)])
    st.altair_chart(charts.labor_slack(readings, P, height=26 * len(readings) + 60),
                    width="stretch", theme=None)
    C.stat_row(_waiting_on(live, month, window, as_of, P))
    filled = f"{month:%B %Y}" if month else "the latest print"
    note = (f"Hollow dot a year ago, filled dot {filled}. Moves under "
            f"{labor.FLAT_BAND_PCT:.0f} percentile points read as unchanged.")
    if stalled:
        note += (" Stalled — shown at their last print and kept out of the month gate: "
                 + ", ".join(i.label for i in stalled)
                 + " (Labour Force Detailed paused with the April 2026 survey changes).")
    C.note(note)

    st.divider()
    head, ctl = st.columns([3, 1], vertical_alignment="bottom")
    with head:
        C.heading("Labour market slack heatmap", "historical percentiles · red = more slack, "
                                                 "blue = less", help=(
            r"\text{cell}_{i,m} = P_i(x_m)\ \text{(flipped where high = more slack)}",
            "The dashboard above, month by month, ranked against the same 2000-present pool. "
            "A blank cell is a month not yet printed — never a carry-forward, except that a "
            "quarterly survey stands for the months of its quarter.",
            f"pool = {labor.BASE_START:%Y}-present · change rows use the {window}-month window",
            "slackheat"))
    with ctl:
        months = st.selectbox("Months", labor.HEATMAP_WINDOWS,
                              index=labor.HEATMAP_WINDOWS.index(labor.HEATMAP_MONTHS),
                              format_func=lambda m: f"{m} months", key="labour_heat_months")
    heat = labor.heatmap(live, change_months=window, months=months)
    if heat.ok:
        st.markdown(charts.labor_slack_heatmap(heat, P), unsafe_allow_html=True)
        C.note("Sources: ABS, RBA, NAB, Westpac-Melbourne Institute. Rows marked ~ are "
               "approximations of the US row they stand in for — see “Series, direction and "
               "definitions” below.")
    else:
        st.info("Not enough history loaded to rank the indicators.")

    st.divider()
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.section("Recent history", "diffusion, labour-market charts and employment")
    with ctl:
        hist = C.history_window("hist_window_labour",
                                help="Scopes the diffusion, labour-market and employment "
                                     "charts. The percentile dashboard above always ranks "
                                     "against its full history.")
    start = C.window_start(hist, as_of)
    recessions = src.au_recessions()

    points = labor.diffusion(full, change_months=window)
    if points:
        C.heading("Share of indicators tightening", composite.line, level="sub", help=(
            r"\text{share}_t = 100 \cdot \frac{n_{\text{improved}} + \tfrac12 n_{\text{unchanged}}}"
            r"{n_{\text{reporting}}}",
            f"A diffusion index over the same indicators, then a {labor.DIFFUSION_SMOOTH}-month "
            "average. 'Improved' means toward less slack in that series' own direction; ties "
            "count a half. Above 50, more indicators are improving than deteriorating. It "
            "measures how BROAD a move is, not how big.",
            "see core.labor.diffusion", "diffusion"))
        latest = points[-1]
        C.board([
            C.Fig("Month-over-month", f"{latest.mom:.0f}%" if latest.mom is not None else "—"),
            C.Fig("Year-over-year", f"{latest.yoy:.0f}%" if latest.yoy is not None else "—",
                  help="The smoother read, and the one to trust at a turn."),
            C.Fig("Latest month", latest.when.strftime("%b %Y"),
                  help="A month shows only once every live indicator has printed it, so the "
                       "slowest release — the gross flows or the NAB survey — sets the pace."),
        ])
        st.altair_chart(charts.labor_diffusion(points, P, window_start=start,
                                               recessions=recessions),
                        width="stretch", theme=None)
        _channels(ctx, full, window, start, recessions, composite)

    with st.expander("Series, direction and definitions"):
        rows, colours = [], {}
        channel = {c.key: c for c in labor.CHANNELS}
        channel_ink = charts.channel_ink(P)
        for i, r in enumerate(latest_readings):
            ind = r.indicator
            ch = channel[labor.CHANNEL_OF[ind.key]]
            transform = {"level": "level", "diff3": f"{window}m change (pp)",
                         "pct3": f"{window}m change (%)",
                         "share": f"% of {ind.denominator}"}[ind.transform]
            rows.append([ind.label, ch.label, f"{r.current_pct:.0f}%" if r.ok else "—",
                         f"{r.as_of:%b %Y}" if r.as_of else "—", ind.release,
                         "high = tighter" if ind.direction > 0 else "high = more slack",
                         transform, "approximated" if ind.approximate else "exact", ind.source])
            colours[(i, 1)] = channel_ink[ch.key]
            colours[(i, 3)] = P.muted
            if r.ok:
                colours[(i, 5)] = P.positive if ind.direction > 0 else P.negative
            colours[(i, 7)] = P.warning if ind.approximate else P.muted
        C.table(["Indicator", "Channel", "Percentile", "Period", "Release", "Direction",
                 "Transform", "Match", "Source"], rows, numeric=(2,), colours=colours,
                nowrap=True)
        C.note("Direction maps each series onto the shared slack axis. Approximated rows stand "
               "in for a US row Australia has no counterpart to (claims, JOLTS, the regional "
               "Fed surveys, the Atlanta Fed wage tracker); each one's note is in core/labor.py.")
        for ind in labor.INDICATORS:
            if ind.note:
                C.note(f"**{ind.label}** — {ind.note}")
    return start


def _waiting_on(live, month, window: int, as_of: date, P) -> list[tuple[str, str, str | None]]:
    """The month in waiting, and each release it waits on, soonest first."""
    if month is None:
        return []
    nxt = labor.month_after(month)
    pending: dict[str, int] = {}
    for ind in labor.INDICATORS:
        have = labor.indicator_month(ind, live, window)
        if have is not None and have < nxt and ind not in labor.stalled(live, window):
            pending[ind.release] = pending.get(ind.release, 0) + 1
    if not pending:
        return [(f"{nxt:%b %Y}", "complete", P.good)]
    items = [(f"{nxt:%b %Y} waits on", f"{sum(pending.values())} of {len(labor.INDICATORS)}",
              None)]
    lag = {i.release: i.lag_days for i in labor.INDICATORS}
    for release, n in sorted(pending.items(), key=lambda kv: lag[kv[0]]):
        from datetime import timedelta
        due = nxt + timedelta(days=lag[release])
        items.append((f"{release} ×{n}", f"≈ {due:%a %d %b}", None))
    return items


_BASIS_LABELS = {"yoy": "Year-over-year", "mom": "Month-over-month"}


def _wrap(text: str) -> str:
    return textwrap.fill(text, 64, initial_indent="  ", subsequent_indent="  ")


def _channel_key() -> str:
    labels = {i.key: i.label for i in labor.INDICATORS}
    return "\n\n".join(f"{c.label} ({len(c.members)})\n"
                       f"{_wrap(' · '.join(labels[k] for k in c.members))}\n{_wrap(c.reads)}"
                       for c in labor.CHANNELS)


def _channel_sizes() -> str:
    n = len(labor.INDICATORS)
    width = max(len(c.label) for c in labor.CHANNELS)
    return "\n".join(f"{c.label:<{width}}  {len(c.members)} of {n}  "
                     f"weight {len(c.members) / n:4.0%}" for c in labor.CHANNELS)


def _channels(ctx: Ctx, data, window: int, start, recessions, vintage) -> None:
    P = ctx.P
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.heading("Diffusion by channel", "which margin is moving the index", level="sub",
                  help=(r"\text{share}_t - 50 = \sum_{c} \frac{n_c}{n}"
                        r"\big(\text{breadth}_{c,t} - 50\big)",
                        "An accounting identity, not a model: every indicator votes 1 if it "
                        "tightened, a half if unchanged and 0 if it cooled, and the votes are "
                        "summed by channel. The bars add up exactly to the headline line. Read "
                        "Hiring against Stated demand: vacancies and surveys that rise while "
                        "job finding does not are demand that is not turning into jobs.",
                        _channel_key(), "diffusion-channels"))
    with ctl:
        basis = C.choice("Basis", list(labor.DIFFUSION_BASES), default="yoy",
                         key="labour_channel_basis", format_func=_BASIS_LABELS.get,
                         label_visibility="collapsed",
                         help="Year-over-year is the smoother read; month-over-month turns "
                              "first but is mostly noise.")
    points = labor.channel_contributions(data, basis=basis, change_months=window)
    if not points:
        st.info("Not enough indicators report to split the diffusion index.")
        return
    st.plotly_chart(charts.labor_channels(points, P, basis=basis, window_start=start,
                                          recessions=recessions, vintage=vintage),
                    width="stretch")
    C.heading("Breadth inside each channel", "its own share tightening, weight taken out",
              level="sub", help=(r"\text{bar}_c = \frac{n_c}{n}\big(\text{breadth}_c - 50\big)",
                                 "Each panel is the breadth alone, 0-100. A small channel moves "
                                 "in coarse steps — Firing and Participation hold one series "
                                 "each here, so read them as that series' direction.",
                                 _channel_sizes(), "channel-breadth"))
    st.plotly_chart(charts.labor_channel_breadth(points, P, window_start=start,
                                                 recessions=recessions), width="stretch")
    C.note("Channel weights follow the indicator count. The gross-flow rows begin in late 2007, "
           "so Firing and Hiring report from then.")


# ------------------------------------------------------------ labour market grid

#: The grid's series by release: the vacancies gap prints with the quarterly
#: vacancies survey, job loss with the gross flows, recessions with the GDP.
GRID_FAMILY = {"UR": pub.LABOUR_FORCE, "PR": pub.LABOUR_FORCE, "EPOP": pub.LABOUR_FORCE,
               "EMP_CHANGE": pub.LABOUR_FORCE, "EMP_CHANGE_3MA": pub.LABOUR_FORCE,
               "JWGAP": pub.VACANCIES, "JOB_LOSS": pub.LF_DETAILED,
               "SWITCHING": pub.LF_DETAILED, "RECESSION": pub.NATIONAL_ACCOUNTS}

def labour_market(ctx: Ctx, start) -> None:
    P = ctx.P
    C.section("Labour market")
    data = src.fetch_labour_grid()
    keep = {"RECESSION"}
    data = src.published_map(data, GRID_FAMILY, ctx.as_of)
    data = {k: [(d, v) for d, v in s if k in keep or start is None or d >= start]
            for k, s in data.items()}
    if start is not None:
        data["RECESSION"] = [(d, v) for d, v in data["RECESSION"] if d >= start]
    lf_v = _vintage(ctx)
    grid = [(charts.au_unemployment_chart, "u"), (charts.au_participation_chart, "pr"),
            (charts.au_emratio_chart, "epop"), (None, "jwgap"),
            (charts.au_job_loss_switching_chart, "flows"),
            (charts.au_employment_change_chart, "change")]
    for i in range(0, len(grid), 2):
        cols = st.columns(2, gap="medium")
        for col, (fn, key) in zip(cols, grid[i:i + 2]):
            with col:
                if fn is not None:
                    st.altair_chart(fn(data, P, vintage=lf_v), width="stretch", theme=None)
                    continue
                C.heading("Jobs-workers gap", _vintage(ctx, econ_calendar.LABOUR).line,
                          level="sub", help=(
                              r"\text{gap} = \frac{E + V - L}{L} \times 100",
                              "Employed workers plus job vacancies minus the labour force, as "
                              "a percent of the labour force. Below zero, there are more "
                              "workers than jobs. Quarterly, at the vacancy survey month.",
                              "E = employed · V = job vacancies · L = labour force (RBA H5)",
                              "au-jwgap"))
                st.altair_chart(charts.au_jwgap_chart(data, P), width="stretch", theme=None)
    C.note("Shading: technical recessions — two or more consecutive quarters of falling real "
           "GDP (RBA H1). Job loss is the employed-to-unemployed flow rate; job switching the "
           "share employed under a year with their current employer. Australia publishes no "
           "layoffs or quits rate.")


def decomposition(ctx: Ctx) -> None:
    head, ctl = st.columns([2, 1.2], vertical_alignment="bottom")
    with head:
        C.heading("Unemployment rate decomposition", "which flow moved the rate", help=(
            r"\Delta u_t = \frac{EU + NU - UE - UN}{L_t} + U_{t-1}\Big(\frac{1}{L_t}"
            r" - \frac{1}{L_{t-1}}\Big) + \varepsilon_t",
            "Each gross flow between employment, unemployment and non-participation, over the "
            "labour force, is its contribution to the month's change in the rate. The ABS "
            "publishes the flows unadjusted and for the matched sample only, so each is rebuilt "
            "as a transition rate (stable seasonals removed) times last month's seasonally "
            "adjusted stock. The residual carries the rotation groups and the stocks' own "
            "seasonal adjustment.",
            "bp = 10,000 × rate × stock(t−1) ÷ L(t) · rates ABS LMS1 · stocks RBA H5",
            "u-decomp"))
    with ctl:
        window = C.history_window("u_decomp_window", help="Scopes the decomposition only.")
    start = C.window_start(window, ctx.as_of)
    rates = src.published_map(src.flow_transition_rates(src.gross_flows()),
                              pub.LF_DETAILED, ctx.as_of)
    stocks = src.published_map(src.decomposition_stocks(), pub.LABOUR_FORCE, ctx.as_of)
    decomp = labor_flows.decompose(rates, stocks)
    if not decomp.ok:
        st.info("The ABS gross-flow cube could not be loaded.")
        return
    st.plotly_chart(charts.unemployment_decomposition(decomp, ctx.P, window_start=start,
                                                      vintage=_vintage(ctx)), width="stretch")


def employment_and_wages(ctx: Ctx, start) -> None:
    P = ctx.P
    jobs = src.fetch_jobs_panel()
    jobs = src.published_map(jobs, pub.LABOUR_ACCOUNT, ctx.as_of)
    comp = jobs_wages.components(jobs)
    last = max((s[-1][0] for s in jobs.values() if s), default=None)
    st.divider()
    C.heading("Employment by component", _quarter_note(last, "ABS Labour Account"), help=(
        r"\Delta\text{Jobs} = \Delta(\text{Market}) + \Delta(\text{Health \& Ed}) "
        r"+ \Delta(\text{Public admin})",
        "Quarterly change in filled jobs split into three components that sum to the total: "
        "the market sector, health care and education, and public administration — the US "
        "payrolls split (private ex health & education, education & health, government) on "
        "the ABS Labour Account, and the non-market question the RBA itself asks.",
        "Market = Total − (O + P + Q) · Health & Ed = P + Q · Public admin = O", "jobs-comp"))
    if not comp:
        st.info("The ABS Labour Account could not be loaded.")
    else:
        st.altair_chart(charts.employment_components_chart(comp, P, window_start=start,
                                                           recessions=src.au_recessions()),
                        width="stretch", theme=None)
        C.legend([("Market sector", P.positive), ("Health care & education", P.accent),
                  ("Public administration", P.muted), ("Total filled jobs", P.ink),
                  ("Market sector (line)", P.negative)])

    wpi = src.published_map(src.fetch_wpi_panel(), pub.WPI, ctx.as_of)
    w_last = max((s[-1][0] for s in wpi.values() if s), default=None)
    nxt = src.next_release(econ_calendar.WPI, ctx.today)
    C.heading("Industry wage-growth heatmap",
              _quarter_note(w_last, "ABS Wage Price Index")
              + (f"  ·  Next release: {nxt:%d %b %Y}" if nxt else ""),
              help=(r"\text{WPI}\ \%\ y/y = 100\left(\frac{\text{WPI}_t}{\text{WPI}_{t-4}} - 1\right)",
                    "Wage Price Index by industry, % change from a year ago, the latest eight "
                    "quarters, newest first. Each row is coloured on its own scale: its minimum "
                    "deep red, its maximum dark green. WPI holds the mix of jobs fixed, so a "
                    "shift toward higher-paid work cannot pass for wage growth — the US table "
                    "has to use average hourly earnings, which it can.",
                    "Shares: industry filled jobs ÷ total (ABS Labour Account)", "wpi-heat"))
    table = charts.wpi_heatmap_table(wpi, jobs_wages.shares(jobs), P)
    if table:
        st.markdown(table, unsafe_allow_html=True)
        C.note("Wage Price Index, total hourly rates of pay excluding bonuses, original. "
               "Source: ABS.")
    else:
        st.info("The ABS Wage Price Index panel could not be loaded.")


def _quarter_note(first_month: date | None, source: str) -> str:
    """"Data to the Jun quarter 2026 · <source>": the ABS data API dates a
    quarter on its first month, which is not a publication date."""
    if first_month is None:
        return source
    end_month = first_month.month + 2
    return (f"Data to the {date(first_month.year, end_month, 1):%b} quarter "
            f"{first_month.year}  ·  {source}")
