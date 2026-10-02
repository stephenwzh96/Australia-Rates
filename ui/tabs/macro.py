"""Macro: the Australian macro-momentum z-score dashboard and its history."""

from __future__ import annotations

from datetime import date

import streamlit as st

from core import macro as macro_mod
from ui import charts
from ui import components as C
from ui import sources as src
from ui.context import Ctx

GROUP_TINT = {"Labour Market": "rgba(42,120,214,0.10)", "Inflation": "rgba(227,73,72,0.10)",
              "Housing": "rgba(27,175,122,0.10)", "Consumer": "rgba(250,178,25,0.12)",
              "Cyclical Economic Indicators": "rgba(137,135,129,0.14)"}
SLIDE, LIVE = "Slide — Aug 2026", "Live"


def render(ctx: Ctx) -> None:
    P = ctx.P
    bundle = {k: src.capped(v, ctx.as_of) for k, v in src.fetch_macro_bundle().items()}

    head, ctl = st.columns([3, 1.1], vertical_alignment="bottom")
    with head:
        C.heading("Australian macroeconomic indicators",
                  "each series as a z-score of its 2000–2020 momentum · positive = stronger",
                  help=(r"z = \frac{x_t - \mu_{2000\text{–}2020}}{\sigma_{2000\text{–}2020}}",
                        "x is the series' momentum transform: q/q % change, a 3-month average "
                        "against itself three months earlier (a point change for the labour "
                        "rates, annualised % for prices and dollar levels), or a plain 3-month "
                        "average for the survey indices. Unemployment and underemployment are "
                        "sign-flipped. Group = mean of members; Total = mean of every scored "
                        "member. μ and σ are the Westpac CIO slide's published 2000–2020 mean "
                        "and a σ backed out of its printed z; a wired feed moves the latest "
                        "reading forward and re-scores it against that same μ/σ — except the "
                        "rows marked §, whose public series is on another scale and is scored "
                        "on its own 2000–2020 μ/σ.",
                        "see core.macro", "macro-z"))
    with ctl:
        mode = C.choice("Readings", (LIVE, SLIDE), default=LIVE, key="macro_read_mode",
                        label_visibility="collapsed",
                        help="Live pulls the latest print for every wired series (the US "
                             "tab's only mode). Slide reproduces the Westpac CIO picture row for "
                             "row, for reconciling against it; the two commercial rows and the "
                             "two with no free feed stay on the slide either way.")
    live = mode == LIVE
    readings = macro_mod.dashboard(bundle if live else None)
    groups = macro_mod.groups(readings)
    total = macro_mod.total(readings)
    n_live = sum(1 for r in readings if r.live)

    C.board([C.Fig("Total", "—" if total is None else f"{total:+.2f}", "mean of the members")]
            + [C.Fig(g.group.replace("Cyclical Economic Indicators", "Cyclical"),
                     "—" if g.z is None else f"{g.z:+.2f}", f"{g.n} series",
                     P.positive if (g.z or 0) > 0 else P.negative if (g.z or 0) < 0 else None)
               for g in groups])
    C.vintage(f"Live · {n_live} of {len(readings)} rows moved past the Aug 2026 slide; the rest "
              "have no wired feed and stay on the slide reading." if live else
              f"Westpac CIO slide, Aug 2026 SMP round — reproduced row for row. "
              f"{sum(1 for k in macro_mod.BY_KEY if k in bundle)} rows have a keyless live feed "
              "(switch to Live).")

    chart_col, table_col = st.columns([0.62, 1.31], gap="small")
    with chart_col:
        st.altair_chart(charts.macro_dots(readings, P, groups, total, GROUP_TINT),
                        width="stretch", theme=None)
        C.legend([("Stronger than 2000–2020 average", P.positive), ("Weaker", P.negative),
                  ("Group mean", P.ink_secondary, "◆")])
    with table_col:
        _table(readings, groups, total, live, P)

    C.heading("Australian momentum signal", level="sub", help=(
        "", "Each group's contribution to the Total (its members' mean z times its share of the "
            "reporting count), stacked per quarter, with the Total as the line. Built only "
            "from rows with a wired or pasted history, so the last bar equals the Live group "
            "scores over that subset. A group with no free feed sits the chart out.",
        "", "macro-momentum"))
    window = C.history_window("macro_window", default="10Y", options=("5Y", "10Y", "15Y", "All"),
                              help="Scopes the momentum chart only.")
    hist = macro_mod.momentum_history(bundle, date(2000, 1, 1))
    if hist:
        st.altair_chart(charts.macro_momentum(hist, P, window_start=C.window_start(window,
                                                                                    ctx.as_of),
                                              recessions=src.au_recessions()),
                        width="stretch", theme=None)
    else:
        st.info("No wired histories yet — the momentum signal needs at least one live series.")


def _period(d: date | None, freq: str = "M") -> str:
    """The month (or quarter) a reading describes, so a stale input shows."""
    if d is None:
        return "—"
    return f"Q{(d.month - 1) // 3 + 1} {d:%y}" if freq == "Q" else f"{d:%b %y}"


def _num(v: float | None, live: bool) -> str:
    """Live: two decimals, as the US table. Slide: 4 significant figures, which
    keeps the slide's own varying precision (0.813, -0.0193, 43.7)."""
    if v is None:
        return "—"
    return f"{v:.2f}" if live else f"{v:.4g}"


def _table(readings, groups, total, live: bool, P) -> None:
    by_group = {g.group: g for g in groups}

    def zcell(z: float | None) -> str:
        if z is None:
            return "—"
        r, g, b = (int(c * 255) for c in charts._hex_rgb(P.positive if z >= 0 else P.negative))
        alpha = 0.14 + 0.5 * min(1.0, abs(z) / 3.0)
        return (f'<span class="fomc-zcell" style="background:rgba({r},{g},{b},{alpha:.2f})">'
                f'{z:+.2f}</span>')

    rows, marks, bg, colours = [], [], {}, {}
    for group in macro_mod.GROUPS:
        gs = by_group.get(group)
        marks.append(len(rows))
        bg[len(rows)] = GROUP_TINT.get(group, "rgba(137,135,129,0.10)")
        rows.append([group, "", "", "", "", zcell(gs.z if gs else None)])
        for r in readings:
            if r.indicator.group != group:
                continue
            ind = r.indicator
            mark = (" ‡" if ind.commercial else " †" if live and not r.live
                    else " §" if live and ind.own_baseline else "")
            colours[(len(rows), 2)] = P.muted
            rows.append([f'<span class="fomc-indent">{ind.label}{mark}</span>',
                         ind.transform_label,
                         _period(r.as_of, r.freq) if live else _period(ind.seed_asof),
                         _num(r.last, live), _num(r.hist_mean, live), zcell(r.z)])
    marks.append(len(rows))
    rows.append(["Total", "", "", "", "", zcell(total)])
    C.table(["Indicator", "Transform", "Period", "Last", "Mean 00–20", "Z"], rows,
            numeric=(3, 4, 5), mark_rows=marks, row_bg=bg, colours=colours, nowrap=True)
    C.note("‡ commercial, no free feed (ANZ-Indeed job ads, ANZ-Roy Morgan confidence): "
           "seeded from the slide; paste a history in meetings/_manual_series.json under "
           "macro:<key> to update. Westpac-MI sentiment and NAB conditions are read from RBA "
           "H3, Melbourne Institute expectations from G3, credit from D1."
           + (" † no free feed in the slide's form (CoreLogic values, the HSI in volume "
              "terms), still on the slide reading. § scored on the public series' own "
              "2000–2020 μ/σ — its scale differs from the slide's." if live else ""))
