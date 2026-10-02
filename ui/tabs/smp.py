"""SMP & forecasts: the Statement's forecast table and its revisions, the
cash-rate assumption against the market, your named read of the Board, and the
balanced-approach rule run on the Statement's forecasts -- the US SEP & dots
tab, for a central bank that publishes forecasts but no dots."""

from __future__ import annotations

import hashlib

import streamlit as st

from core import dots as dots_mod
from core import roster as R
from core import smp as smp_mod
from core import taylor
from core import taylor_heatmap as th
from core import publication as pub
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx, mark_dirty

TAYLOR_WIDGET_KEYS = ("taylor_r_star", "taylor_u_star", "taylor_current", "taylor_y1_tm",
                      "taylor_y1_u", "taylor_y2_tm", "taylor_y2_u")
DOTS_WIDGET_KEYS = ("dots_labels",)
PER_MEETING_WIDGET_KEYS = TAYLOR_WIDGET_KEYS + DOTS_WIDGET_KEYS
TAYLOR_SEED_KEYS = ("taylor_y1_tm", "taylor_y1_u", "taylor_y2_tm", "taylor_y2_u")
TAYLOR_VINTAGE_KEY = "_taylor_seed_vintage"
FALLBACK = {"tm": (3.0, 2.6), "u": (4.6, 4.7)}


def render(ctx: Ctx) -> None:
    released = tuple(((m.end.year, m.end.month), m.end) for m in ctx.meetings if m.has_smp)
    rounds = src.fetch_smp_rounds(ctx.as_of, released)
    latest = rounds[0] if rounds else None
    prior = rounds[1] if len(rounds) > 1 else None
    table = smp_mod.build(latest, prior)
    _forecasts(ctx, table)
    st.divider()
    _cash_path(ctx, latest, prior)
    st.divider()
    _named_dots(ctx, latest)
    st.divider()
    _taylor(ctx, table, latest)
    st.divider()
    _taylor_heatmap(ctx)


def _forecasts(ctx: Ctx, table) -> None:
    P = ctx.P
    C.heading("Statement on Monetary Policy forecasts",
              f"{table.latest} Statement · change vs {table.prior}" if table.prior
              else "the Statement's detailed forecast table",
              help=("", "The RBA's forecasts from the Outlook chapter of each Statement: year-"
                        "ended growth, the labour market at quarter averages, wages, inflation "
                        "and the cash-rate assumption, at half-yearly horizons. Change is "
                        "against the previous Statement on the same horizon. The first "
                        "column(s) are usually history the Statement shades.", "", "smp"))
    if not table.ok:
        C.note("The Statement's forecast table could not be read — the RBA site may be "
               "unreachable; nothing is cached yet for this round.")
        return
    rows, colours, bg = [], {}, {}
    for row in table.rows:
        rows.append([row.metric.label] + [f"{c.value:.1f}" if c.value is not None else "—"
                                          for c in row.cells])
        chg = len(rows)
        rows.append(['<span class="fomc-indent">Change</span>']
                    + [f"{c.change:+.1f}" if c.change is not None else "" for c in row.cells])
        bg[chg] = "rgba(128,128,128,0.06)"
        for ci, c in enumerate(row.cells):
            if c.change:
                colours[(chg, ci + 1)] = (P.negative if c.change * row.metric.higher_is_tighter > 0
                                          else P.positive)
    C.table(["Forecast"] + table.columns, rows, numeric=tuple(range(1, len(table.columns) + 1)),
            colours=colours, row_bg=bg)
    C.note("Change colours: red = hawkish revision, blue = dovish (unemployment and "
           "underutilisation read inversely). Year-ended % unless a rate. Source: RBA.")


def _cash_path(ctx: Ctx, latest, prior) -> None:
    P = ctx.P
    C.heading("The cash-rate assumption", "the Statement's market path, then and now", help=(
        "", "The RBA publishes no dots. Its forecasts assume the cash rate follows market "
            "pricing on the day they are finalised, so the published path is the market's, "
            "not the Board's. The read is how far pricing has moved since: the diamond is "
            "the IB strip today, averaged over each horizon's quarter.", "", "smp-path"))
    if latest is None:
        C.note("No Statement round could be read.")
        return
    ib = {q.month: q.implied_rate for q in ctx.market.ib_live}
    points = smp_mod.cash_path(latest, prior, ib, ctx.as_of)
    chart_col, table_col = st.columns([1.6, 1], gap="large")
    with chart_col:
        st.altair_chart(charts.smp_cash_path(points, P, latest.label,
                                             prior.label if prior else ""),
                        width="stretch", theme=None)
        C.legend([(prior.label if prior else "previous", P.muted), (latest.label, P.ink),
                  ("IB strip today", P.accent), ("hawkish revision", P.negative),
                  ("dovish revision", P.positive)])
    with table_col:
        rows, colours = [], {}
        for i, pt in enumerate(points):
            move = (None if pt.latest is None or pt.prior is None else pt.latest - pt.prior)
            gap = (None if pt.latest is None or pt.market is None else pt.market - pt.latest)
            rows.append([pt.horizon, "—" if pt.prior is None else f"{pt.prior:.1f}%",
                         "—" if pt.latest is None else f"{pt.latest:.1f}%",
                         "—" if move is None else f"{move * 100:+.0f}bp",
                         "—" if pt.market is None else f"{pt.market:.2f}%",
                         "—" if gap is None else f"{gap * 100:+.0f}bp"])
            if move:
                colours[(i, 3)] = P.negative if move > 0 else P.positive
            if gap:
                colours[(i, 5)] = P.negative if gap > 0 else P.positive
        C.table(["Horizon", prior.label if prior else "Prior", latest.label, "Revision",
                 "IB today", "Since"], rows, numeric=(1, 2, 3, 4, 5), colours=colours)
        C.note("Since = the strip today less the Statement's assumption: positive means the "
               "market has priced more tightening since the forecasts were finalised.")


def _dots_state(ctx: Ctx) -> dict:
    year = ctx.M.meeting.end.year
    dots = ctx.S.setdefault("dots", {})
    dots.setdefault("values", {})
    dots.setdefault("labels", list(dots_mod.default_labels(year)[:2]))
    return dots


def _named_dots(ctx: Ctx, latest) -> None:
    P, S = ctx.P, ctx.S
    year = ctx.M.meeting.end.year
    dots = _dots_state(ctx)
    roster = list(S.get("roster") or [])
    head, pick = st.columns([1.6, 1], vertical_alignment="bottom")
    with head:
        C.heading("Your named read", "one dot per Board member — type or edit the horizon columns",
                  help=("", "The RBA publishes neither dots nor named votes, so both the name on "
                            "a dot and its level are your judgement — a disciplined place to write "
                            "down where you think each member would set the cash rate by each "
                            "horizon. Values snap to 5bp, the grid the cash rate moves on. Every "
                            "member votes at every meeting, so the median is the Board's.",
                        "", "named-dots"))
    options = list(dict.fromkeys(list(dots_mod.default_labels(year)) + [str(x) for x in dots["labels"]]))
    with pick:
        picked = st.multiselect("Horizons", options,
                                default=[o for o in options if o in dots["labels"]],
                                key="dots_labels", on_change=mark_dirty,
                                label_visibility="collapsed", placeholder="Choose horizons")
    labels = [o for o in options if o in picked]
    dots["labels"] = labels
    if not roster:
        st.info("Add Board members on the Vote count tab to attribute dots.")
        return
    rows = []
    for i, r in enumerate(roster):
        name = dots_mod.seat_name(r, i)
        held = dots["values"].get(name) or {}
        row = {"name": name, "role": str(r.get("role", "") or ""),
               "group": dots_mod.group_of(r)}
        score = R.active_score(r)
        row["score"] = float("nan") if score is None else float(score)
        row.update({lab: held.get(lab, float("nan")) for lab in labels})
        rows.append(row)
    identity = hashlib.sha1("|".join(f"{x['name']}:{x['role']}" for x in rows).encode()).hexdigest()[:10]
    edited = st.data_editor(
        rows, key=f"dots_editor_{identity}", width="stretch", hide_index=True,
        height=min(38 + 35 * len(rows), 520), disabled=["name", "role", "group", "score"],
        column_order=("name", "role", "group", "score", *labels),
        column_config={
            "name": st.column_config.TextColumn("Member", width=170),
            "role": st.column_config.TextColumn("Seat", width=170),
            "group": st.column_config.TextColumn("Group", width=100),
            "score": st.column_config.NumberColumn("Hawk-dove", format="%.1f", width=90),
            **{lab: st.column_config.NumberColumn(
                lab, min_value=0.0, max_value=10.0, step=dots_mod.DOT_STEP, format="%.2f",
                width=100, help=f"Cash rate you think this member pencils in for {lab}, e.g. "
                                "4.35. Blank = no dot.") for lab in labels},
        }, on_change=mark_dirty)
    values = {}
    for row in edited:
        name = str(row.get("name", "")).strip()
        keep = {k: v for k, v in (dots["values"].get(name) or {}).items() if k not in labels}
        typed = {lab: dots_mod.snap(row.get(lab)) for lab in labels}
        merged = {**keep, **{k: v for k, v in typed.items() if v is not None}}
        if merged:
            values[name] = merged
    dots["values"] = values

    panels = [dots_mod.build(roster, lab, {n: v.get(lab) for n, v in values.items()})
              for lab in labels]
    drawn = [p for p in panels if p.ok]
    if not drawn:
        C.note("No dots typed in yet — fill a horizon column above and its panel appears.")
        return
    assumption = {}
    if latest is not None:
        for h, v in zip(latest.horizons, next((vals for k, vals in latest.rows.items()
                                              if k.startswith("Cash rate")), [])):
            assumption[h] = v
    cash = ctx.market.spot.get("cash")
    for start in range(0, len(drawn), 2):
        cols = st.columns(2, gap="large")
        for col, panel in zip(cols, drawn[start:start + 2]):
            with col:
                st.altair_chart(charts.named_dots(panel, P, f"{panel.label} — named read"),
                                width="stretch", theme=None)
                lo, hi = panel.span
                C.stat_row([("Median", dots_mod.fmt(panel.median) + "%", P.ink),
                            ("Range", f"{dots_mod.fmt(lo)}–{dots_mod.fmt(hi)}%", None),
                            ("Dots", f"{panel.count} of {len(roster)}", None)])
                ref = assumption.get(panel.label)
                bits = []
                if ref is not None and panel.median is not None:
                    bits.append(f"median {(panel.median - ref) * 100:+.0f}bp vs the "
                                f"{latest.label} Statement's {ref:.1f}% assumption")
                if cash is not None:
                    up, at, down = dots_mod.reconcile_split(panel, cash)
                    bits.append(f"{up} above, {at} at, {down} below today's {cash:.2f}%")
                if bits:
                    C.note("; ".join(bits).capitalize() + ".")


def _anchor(table, label: str, column: str, fallback: float) -> float:
    v = table.value(label, column) if table.ok else None
    return float(v) if v is not None else fallback


def _seed_taylor(state, seeds: dict, release) -> None:
    """Write the seeds, re-seeding the forecast-derived ones when the Statement
    round turns over (the US module's rule)."""
    if state.get(TAYLOR_VINTAGE_KEY) != release:
        for k in TAYLOR_SEED_KEYS:
            state.pop(k, None)
        state[TAYLOR_VINTAGE_KEY] = release
    for k, v in seeds.items():
        state.setdefault(k, v)


def _axis(default, anchors):
    lo = min(min(default), *(round(a, 1) for a in anchors))
    hi = max(max(default), *(round(a, 1) for a in anchors))
    return tuple(round(lo + 0.1 * i, 2) for i in range(int(round((hi - lo) / 0.1)) + 1))


def _taylor(ctx: Ctx, table, latest) -> None:
    P = ctx.P
    y1 = ctx.M.meeting.end.year
    y2 = y1 + 1
    c1, c2 = f"Dec {y1}", f"Dec {y2}"
    cash = ctx.market.spot.get("cash")
    seeds = {
        "taylor_y1_tm": _anchor(table, "Trimmed mean inflation", c1, FALLBACK["tm"][0]),
        "taylor_y1_u": _anchor(table, "Unemployment rate", c1, FALLBACK["u"][0]),
        "taylor_y2_tm": _anchor(table, "Trimmed mean inflation", c2, FALLBACK["tm"][1]),
        "taylor_y2_u": _anchor(table, "Unemployment rate", c2, FALLBACK["u"][1]),
        "taylor_r_star": taylor.DEFAULT_PARAMS.r_star,
        "taylor_u_star": taylor.DEFAULT_PARAMS.u_star,
        "taylor_current": round(cash, 2) if cash is not None else taylor.DEFAULT_CURRENT_RATE,
    }
    _seed_taylor(st.session_state, seeds, latest.key if latest else None)

    C.section("Cash rate: balanced-approach rule",
              "on the Statement's trimmed mean and unemployment forecasts")
    g = st.columns(7)
    with g[0]:
        r_star = st.number_input("r* (real, %)", min_value=-1.0, max_value=4.0, step=0.25,
                                 format="%.2f", key="taylor_r_star")
    with g[1]:
        u_star = st.number_input("u* (NAIRU, %)", min_value=3.0, max_value=6.0, step=0.25,
                                 format="%.2f", key="taylor_u_star")
    with g[2]:
        current = st.number_input("Cash rate now (%)", min_value=0.0, max_value=10.0, step=0.05,
                                  format="%.2f", key="taylor_current",
                                  help="The target the shading is measured against; seeds "
                                       "from the latest RBA print.")
    with g[3]:
        tm1 = st.number_input(f"{c1} trimmed mean", min_value=0.0, max_value=8.0, step=0.1,
                              format="%.1f", key="taylor_y1_tm")
    with g[4]:
        u1 = st.number_input(f"{c1} unemployment", min_value=2.0, max_value=9.0, step=0.1,
                             format="%.1f", key="taylor_y1_u")
    with g[5]:
        tm2 = st.number_input(f"{c2} trimmed mean", min_value=0.0, max_value=8.0, step=0.1,
                              format="%.1f", key="taylor_y2_tm")
    with g[6]:
        u2 = st.number_input(f"{c2} unemployment", min_value=2.0, max_value=9.0, step=0.1,
                             format="%.1f", key="taylor_y2_u")
    params = taylor.RuleParams(r_star=r_star, u_star=u_star)
    C.note(f"π* {params.pi_star:g}% (the band's midpoint) · forecast inputs seed from the "
           f"{latest.label if latest else 'latest'} Statement's year-end horizons; edit to run "
           "the rule on your own.")
    grid = taylor.build(core_pce_axis=_axis(taylor.CORE_PCE_AXIS, (tm1, tm2)),
                        unemployment_axis=_axis(taylor.UNEMPLOYMENT_AXIS, (u1, u2)),
                        params=params, band=taylor.HoldBand(current_rate=current),
                        anchors=[(f"SMP {c1}", tm1, u1), (f"SMP {c2}", tm2, u2)])
    a1, a2 = grid.anchors
    grid_col, read_col = st.columns([1.5, 1], gap="large")
    with grid_col:
        st.markdown(charts.taylor_grid_table(grid, P), unsafe_allow_html=True)
        C.note(f"Cash = r* + π + 0.5(π − π*) + 2(u* − u) · r* {r_star:.2f}%, π* "
               f"{params.pi_star:g}%, u* {u_star:.2f}%")
    with read_col:
        for anchor, tm, u, key in ((a1, tm1, u1, "taylor_y1"), (a2, tm2, u2, "taylor_y2")):
            placed = anchor.cell is not None
            move = ("" if not placed else "no change" if anchor.cell.stance == "hold"
                    else f"{abs(anchor.cell.moves):.1f} × 25bp {anchor.cell.stance}")
            C.labelled_metric(
                f"Rule on the {anchor.label.split(' ', 1)[1]} forecast",
                anchor.cell.label if placed else "off grid",
                r"\text{cash} = r^* + \pi + 0.5(\pi - \pi^*) + 2(u^* - u)",
                "The balanced-approach rule on this horizon's forecast pair.",
                (f"trimmed mean {tm:.1f}%, u {u:.1f}% → {anchor.cell.label} ({move})" if placed
                 else f"trimmed mean {tm:.1f}%, u {u:.1f}% is outside the plotted axes"),
                key, colour=(None if not placed else P.negative if anchor.cell.stance == "hike"
                             else P.positive if anchor.cell.stance == "cut" else None))
        whip = grid.whiplash_bp
        C.labelled_metric(
            f"Implied path, {c1} → {c2}", f"{whip:+.0f}bp" if whip is not None else "—",
            r"\Delta = \text{cash}_{y2} - \text{cash}_{y1}",
            "The rule's direction of travel between the two forecast horizons.",
            (f"{a2.cell.label} − {a1.cell.label} = {whip:+.0f}bp" if whip is not None
             else "one of the two anchors is off the grid"),
            "taylorwhip", colour=None if whip is None else (P.positive if whip < 0 else P.negative))
        C.note(f"Across {sum(len(r) for r in grid.rows)} combinations the rule calls for a cut in "
               f"{grid.count('cut')}, no change in {grid.count('hold')} and a hike in "
               f"{grid.count('hike')} against {grid.band.current_rate:.2f}%.")


def _taylor_heatmap(ctx: Ctx) -> None:
    P = ctx.P
    C.heading("Taylor rule cash rate prescription heatmap",
              "five real neutral rates against five NAIRUs",
              help=("", "Computed here — no Australian equivalent of the Atlanta Fed's utility "
                        "is published. The balanced-approach rule is run on the latest "
                        "year-ended trimmed mean and unemployment rate across the range of r* "
                        "and NAIRU the RBA's own estimates span. When the whole grid sits on "
                        "one side of the cash rate, the gap between the rule and the Board does "
                        "not hang on one input.", "", "taylorheat"))
    tm = src.cpi_published(ctx.as_of).get("trimmed_yr") or []
    ur = src.published(src.rba_table("h5").get("GLFSURSA", []), pub.LABOUR_FORCE, ctx.as_of)
    heat = th.build(tm[-1][1] if tm else None, ur[-1][1] if ur else None,
                    ctx.market.spot.get("cash"))
    if not heat.ok:
        C.note(f"Heatmap unavailable — {heat.error}.")
        return
    grid_col, read_col = st.columns([1.7, 1], gap="large")
    with grid_col:
        st.markdown(charts.taylor_heatmap_table(heat, P), unsafe_allow_html=True)
        C.note(f"Cash = r* + π + {heat.gap_weight:g}(u* − u) + 0.5(π − π*) · π "
               f"{heat.inflation:.1f}% ({heat.inflation_measure}) · u {heat.unemployment:.1f}% · "
               f"target {heat.pi_star:g}% · shaded against {heat.funds_rate:.2f}%")
    with read_col:
        lo, hi = heat.span
        median = heat.median_bp
        C.labelled_metric("Rule vs the cash rate", f"{median:+.0f}bp" if median is not None else "—",
                          r"\text{median}(r^{TR}) - \text{cash}",
                          "The middle cell of the grid, against the cash rate.",
                          f"{heat.n} prescriptions run {lo:.2f}–{hi:.2f}%", "taylorheatmed",
                          colour=None if median is None else (P.negative if median > 0
                                                              else P.positive))
        C.labelled_metric("Combinations above policy", f"{heat.above} of {heat.n}",
                          r"\#\{\, r^{TR} - \text{cash} > 0.25 \,\}",
                          "How many (r*, u*) pairs prescribe more than 25bp above the cash "
                          "rate. One cell is a view; the whole grid is a conclusion.",
                          f"{heat.above} above, {heat.below} below, "
                          f"{heat.n - heat.above - heat.below} within 25bp", "taylorheatcount",
                          colour=(P.negative if heat.above == heat.n
                                  else P.positive if heat.below == heat.n else None))
        widest = max(heat.cells, key=lambda c: c.diff_bp)
        narrowest = min(heat.cells, key=lambda c: c.diff_bp)
        C.labelled_metric("Widest and narrowest cell",
                          f"{widest.diff_bp:+.0f} / {narrowest.diff_bp:+.0f}bp",
                          r"r^{TR} = r^* + \pi + 2(u^* - u) + 0.5(\pi - \pi^*)",
                          "The two corners of the disagreement.",
                          f"widest — r* {widest.r_star.label} × {widest.gap.label}: "
                          f"{th.worked_example(widest, heat)}\nnarrowest — r* "
                          f"{narrowest.r_star.label} × {narrowest.gap.label}: "
                          f"{th.worked_example(narrowest, heat)}", "taylorheatspan")
        C.note(th.summarise(heat))
