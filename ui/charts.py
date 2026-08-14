"""Altair charts.

Form choices, per the dataviz method:
  EV vs q      -> line + signed area. The job is polarity (where does EV cross
                  zero), so the diverging pair carries the sign and two labelled
                  rules carry the two probabilities that matter.
  Kelly decay  -> bars. The job is magnitude across a small ordered set.
  MTM ladder   -> horizontal bars, diverging by sign, one row per contract level.
  FRED series  -> single-series line with a threshold rule.

Single-series charts carry no legend (the title names the series); the aqua
"your q" rule is always direct-labelled, which is the secondary encoding its
CVD band requires.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import altair as alt
import pandas as pd
import plotly.graph_objects as go

from core import path as pathmod
from core import pricing, sizing
from core import inflation as inflation_mod
from core import payroll_wages
from core import cpi_breakdown
from core.pricing import Side

from .theme import Palette

LINE_WIDTH = 2
POINT_SIZE = 70
BAR_RADIUS = 4


# ---------------------------------------------------------------------------
# Source vintage line
# ---------------------------------------------------------------------------
# The terminal's CPI monitor heads every screen with when the number last
# landed and when the next one is due, which is the pair a pre-meeting reader
# actually needs: whether what they are looking at is current, and how long it
# stays the latest word. The same line rides under every Inflation-tab chart
# title so a stale panel is visible rather than assumed fresh.

@dataclass(frozen=True)
class Vintage:
    """When a chart's data last landed, and when its source next publishes."""

    next_release: date | None = None
    last_modified: date | None = None

    @property
    def line(self) -> str:
        """One horizontal line, or "" when neither date is known."""
        parts = []
        if self.next_release:
            parts.append(f"Next release: {self.next_release:%d %b %Y}")
        if self.last_modified:
            parts.append(f"Last modified: {self.last_modified:%d %b %Y}")
        return "  ·  ".join(parts)


def apply_vintage(fig: go.Figure, vintage: "Vintage | None", p: Palette,
                  top_margin: int = 64) -> go.Figure:
    """Hang a vintage line under a Plotly figure's title, in place.

    A no-op when there is nothing to say, so a chart whose source has never
    been fetched keeps its original title and spacing instead of reserving
    room for a blank line.
    """
    if vintage is None or not vintage.line:
        return fig
    fig.update_layout(
        title=dict(subtitle=dict(text=vintage.line,
                                 font=dict(size=11, color=p.ink_secondary))),
        margin=dict(t=top_margin),
    )
    return fig


def vintage_title(text: str, vintage: "Vintage | None", p: Palette,
                  **params) -> alt.TitleParams:
    """The Altair equivalent of :func:`apply_vintage`.

    Altair titles carry a native ``subtitle``, so the line rides in the same
    slot it does on the Plotly charts and the two families read alike. Same
    degrade rule: an unknown vintage leaves a plain title with no gap under it.
    """
    opts: dict = dict(fontSize=12, fontWeight="normal", color=p.ink, dy=-5)
    opts.update(params)
    if vintage is not None and vintage.line:
        # limit=0 drops the explicit pixel cap, so Vega clamps to the view width
        # instead of a much smaller default -- without it the line ellipsises to
        # "Next release: 04 Sep 2026  ·  Last modifie…" inside the half-width GS
        # panels, losing the date it exists to show. Vega still trims to the
        # chart box on a genuinely cramped viewport, exactly as the chart TITLES
        # already do there, so the line can never spill past its own chart.
        opts.update(subtitle=vintage.line, subtitleColor=p.muted,
                    subtitleFontSize=10, subtitlePadding=4, limit=0)
    return alt.TitleParams(text, **opts)


def _base(p: Palette) -> dict:
    return {
        "config": {
            "background": "transparent",
            "view": {"stroke": "transparent"},
            "axis": {
                "labelColor": p.muted, "titleColor": p.muted,
                "labelFontSize": 10, "titleFontSize": 10, "titleFontWeight": "normal",
                "gridColor": p.grid, "gridWidth": 1, "domainColor": p.baseline,
                "tickColor": p.baseline, "labelFlush": True,
            },
            "legend": {"labelColor": p.ink_secondary, "titleColor": p.muted,
                       "labelFontSize": 10, "titleFontSize": 10},
            "text": {"color": p.ink_secondary, "fontSize": 10},
        }
    }


def _apply(chart: alt.Chart, p: Palette, height: int) -> alt.Chart:
    cfg = _base(p)["config"]
    if isinstance(chart, (alt.VConcatChart, alt.HConcatChart, alt.ConcatChart)):
        # Concat charts reject a top-level `height`/`width` property -- those
        # live on each sub-chart instead. The main chart's height is set where
        # it is built and every leaf already carries a responsive width, so we
        # only apply the theme configs here.
        return (
            chart
            .configure_view(**cfg["view"])
            .configure_axis(**cfg["axis"])
            .configure_legend(**cfg["legend"])
            .configure_text(**cfg["text"])
        )
    return (
        chart.properties(height=height, width="container")
        .configure_view(**cfg["view"])
        .configure_axis(**cfg["axis"])
        .configure_legend(**cfg["legend"])
        .configure_text(**cfg["text"])
    )


# --------------------------------------------------------------------------

def ev_curve(points: float, size: float, q: float, side: Side, p: Palette,
             height: int = 210) -> alt.LayerChart:
    """EV across the full range of q, with breakeven and your estimate marked."""
    grid = [i / 200 for i in range(0, 201)]
    df = pd.DataFrame({
        "q": grid,
        "ev": [pricing.expected_value(points, size, g, side) for g in grid],
    })
    df["sign"] = df["ev"].apply(lambda v: "positive" if v >= 0 else "negative")
    be = pricing.breakeven_probability(points, size)
    ev_here = pricing.expected_value(points, size, q, side)

    area = (
        alt.Chart(df).mark_area(opacity=0.13, interpolate="monotone")
        .encode(
            x=alt.X("q:Q", axis=alt.Axis(format="%", title="your probability of the move", tickCount=6)),
            y=alt.Y("ev:Q", axis=alt.Axis(title="expected value (bp)")),
            y2=alt.datum(0),
            color=alt.Color("sign:N", scale=alt.Scale(
                domain=["positive", "negative"], range=[p.positive, p.negative]), legend=None),
        )
    )
    line = (
        alt.Chart(df).mark_line(strokeWidth=LINE_WIDTH, interpolate="monotone")
        .encode(
            x="q:Q", y="ev:Q",
            color=alt.Color("sign:N", scale=alt.Scale(
                domain=["positive", "negative"], range=[p.positive, p.negative]), legend=None),
        )
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(y="y:Q")

    be_df = pd.DataFrame({"q": [be], "label": [f"breakeven / market  {be:.1%}"]})
    be_rule = alt.Chart(be_df).mark_rule(
        color=p.muted, strokeWidth=1, strokeDash=[4, 3]).encode(x="q:Q")
    be_text = alt.Chart(be_df).mark_text(
        align="right", dx=-5, dy=-6, baseline="top", fontSize=10, color=p.muted,
    ).encode(x="q:Q", y=alt.datum(0), text="label:N")

    q_df = pd.DataFrame({"q": [q], "ev": [ev_here],
                         "label": [f"your q  {q:.1%}   {ev_here:+.2f}bp"]})
    q_rule = alt.Chart(q_df).mark_rule(color=p.accent, strokeWidth=LINE_WIDTH).encode(x="q:Q")
    q_dot = alt.Chart(q_df).mark_point(
        filled=True, size=POINT_SIZE, color=p.accent,
        stroke=p.surface, strokeWidth=2,
    ).encode(x="q:Q", y="ev:Q")
    q_text = alt.Chart(q_df).mark_text(
        align="left", dx=7, dy=-9, fontSize=10, fontWeight="bold", color=p.accent,
    ).encode(x="q:Q", y="ev:Q", text="label:N")

    hover = (
        alt.Chart(df).mark_rule(color=p.muted, strokeWidth=1, opacity=0)
        .encode(
            x="q:Q",
            opacity=alt.condition(alt.selection_point(on="pointerover", nearest=True,
                                                      fields=["q"], empty=False),
                                  alt.value(0.35), alt.value(0)),
            tooltip=[alt.Tooltip("q:Q", format=".1%", title="q"),
                     alt.Tooltip("ev:Q", format="+.2f", title="EV (bp)")],
        )
        .add_params(alt.selection_point(on="pointerover", nearest=True, fields=["q"], empty=False))
    )

    return _apply(
        alt.layer(area, line, zero, be_rule, be_text, q_rule, q_dot, q_text, hover), p, height
    )


def kelly_growth(ladder, p: Palette, height: int = 230) -> alt.LayerChart:
    """The classic Kelly curve: expected log growth against bankroll fraction.

    The shape is the whole argument for sizing down. It is flat approaching the
    peak -- so under-betting costs almost nothing -- and falls off a cliff past
    it, reaching zero growth at the over-betting point while carrying maximum
    risk. Rungs are plotted on the curve so the give-up is a distance you can
    see rather than a number to take on trust.
    """
    curve = sizing.growth_curve(ladder.f_star, ladder.p_win, ladder.b)
    df = pd.DataFrame(curve, columns=["f", "growth"])
    peak_df = pd.DataFrame({"f": [ladder.f_star], "growth": [ladder.peak_growth],
                            "label": [f"full Kelly {ladder.f_star:.0%}"]})

    line = (
        alt.Chart(df).mark_line(strokeWidth=LINE_WIDTH, interpolate="monotone")
        .encode(
            x=alt.X("f:Q", axis=alt.Axis(format="%", title="share of risk budget staked",
                                         tickCount=6)),
            y=alt.Y("growth:Q", axis=alt.Axis(title="expected log growth", format=".3f")),
            color=alt.value(p.ink_secondary),
        )
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(y="y:Q")

    peak_rule = alt.Chart(peak_df).mark_rule(
        color=p.muted, strokeWidth=1, strokeDash=[4, 3]).encode(x="f:Q")
    peak_text = alt.Chart(peak_df).mark_text(
        align="left", dx=5, dy=-4, baseline="bottom", fontSize=10, color=p.muted,
    ).encode(x="f:Q", y="growth:Q", text="label:N")
    layers = [zero, line, peak_rule, peak_text]

    # The over-betting point: same growth as not trading, maximum risk.
    if ladder.zero_growth_f is not None:
        over = pd.DataFrame({"f": [ladder.zero_growth_f], "growth": [0.0],
                             "label": [f"zero growth {ladder.zero_growth_f:.0%}"]})
        layers.append(alt.Chart(over).mark_rule(
            color=p.critical, strokeWidth=1, strokeDash=[2, 3]).encode(x="f:Q"))
        layers.append(alt.Chart(over).mark_text(
            align="right", dx=-5, dy=-6, baseline="top", fontSize=10, color=p.critical,
        ).encode(x="f:Q", y=alt.datum(0), text="label:N"))

    rung_df = pd.DataFrame([
        {"f": r.f, "growth": r.growth, "label": r.label,
         "selected": r.is_selected, "share": r.growth_share}
        for r in ladder.rungs
    ])
    tooltip = [alt.Tooltip("label:N", title="Kelly"),
               alt.Tooltip("f:Q", format=".1%", title="of budget"),
               alt.Tooltip("share:Q", format=".0%", title="of peak growth")]
    layers.append(
        alt.Chart(rung_df).mark_point(filled=True, size=POINT_SIZE,
                                      stroke=p.surface, strokeWidth=2)
        .encode(x="f:Q", y="growth:Q",
                color=alt.condition(alt.datum.selected, alt.value(p.accent),
                                    alt.value(p.positive)),
                tooltip=tooltip)
    )
    layers.append(
        alt.Chart(rung_df).mark_text(align="center", dy=13, fontSize=10)
        .encode(x="f:Q", y="growth:Q", text="label:N",
                color=alt.condition(alt.datum.selected, alt.value(p.accent),
                                    alt.value(p.ink_secondary)))
    )
    return _apply(alt.layer(*layers), p, height)


def capital_at_risk(ladder, p: Palette, height: int = 230) -> alt.LayerChart:
    """Capital at risk against Kelly growth -- the sizing trade-off, directly.

    x is growth per bet, y is dollars, one point per rung. Read left to right it
    answers the only question that matters here: buying the next slice of growth
    costs how much more downside? The gain line pulls away from the loss line,
    but both steepen sharply toward full Kelly, which is where the daily limit
    usually bites.

    Both series are dollars, so they share one axis. `g/bet` is deliberately NOT
    plotted as a third series -- it is the x-axis, and a percentage drawn on a
    dollar scale just pins a flat line to zero.
    """
    rows = []
    for r in ladder.rungs:
        rows.append({"g": r.growth, "amount": r.max_loss, "kind": "Max loss $",
                     "rung": r.label, "selected": r.is_selected,
                     "ok": r.within_daily_limit, "contracts": r.contracts})
        rows.append({"g": r.growth, "amount": r.max_gain, "kind": "Max gain $",
                     "rung": r.label, "selected": r.is_selected,
                     "ok": r.within_daily_limit, "contracts": r.contracts})
    df = pd.DataFrame(rows)

    scale = alt.Scale(domain=["Max gain $", "Max loss $"],
                      range=[p.positive, p.negative])
    x_enc = alt.X("g:Q", axis=alt.Axis(format=".2%", title="expected growth per bet",
                                       tickCount=len(ladder.rungs)))
    y_enc = alt.Y("amount:Q", axis=alt.Axis(title="$", format="$,.0f"))
    tooltip = [alt.Tooltip("rung:N", title="Kelly"),
               alt.Tooltip("kind:N", title=""),
               alt.Tooltip("amount:Q", format="$,.0f", title="amount"),
               alt.Tooltip("g:Q", format=".2%", title="g/bet"),
               alt.Tooltip("contracts:Q", title="lots"),
               alt.Tooltip("ok:N", title="within daily limit")]

    lines = (
        alt.Chart(df).mark_line(strokeWidth=LINE_WIDTH, interpolate="monotone")
        .encode(x=x_enc, y=y_enc,
                color=alt.Color("kind:N", scale=scale,
                                legend=alt.Legend(title=None, orient="bottom")))
    )
    # Hollow marks for a rung the daily limit forbids: a real Kelly size, but
    # not one you may put on, so it must not read as available.
    dots_ok = (
        alt.Chart(df[df["ok"]]).mark_point(filled=True, size=POINT_SIZE,
                                           stroke=p.surface, strokeWidth=1.5)
        .encode(x=x_enc, y=y_enc, color=alt.Color("kind:N", scale=scale, legend=None),
                tooltip=tooltip)
    )
    layers = [lines, dots_ok]
    if (~df["ok"]).any():
        layers.append(
            alt.Chart(df[~df["ok"]]).mark_point(filled=False, size=POINT_SIZE,
                                                strokeWidth=2)
            .encode(x=x_enc, y=y_enc,
                    stroke=alt.Color("kind:N", scale=scale, legend=None),
                    tooltip=tooltip)
        )

    labels = (
        alt.Chart(df).mark_text(align="left", dx=8, dy=-7, fontSize=10,
                                fontWeight="bold")
        .encode(x=x_enc, y=y_enc, text=alt.Text("amount:Q", format="$,.0f"),
                color=alt.Color("kind:N", scale=scale, legend=None),
                opacity=alt.condition(alt.datum.selected, alt.value(1.0),
                                      alt.value(0.6)))
    )
    rung_names = (
        alt.Chart(df[df["kind"] == "Max loss $"])
        .mark_text(align="center", dy=16, fontSize=10, color=p.muted)
        .encode(x=x_enc, y=y_enc, text="rung:N")
    )
    layers += [labels, rung_names]
    return _apply(alt.layer(*layers), p, height)


def mtm_ladder(rows, p: Palette, height: int | None = None) -> alt.LayerChart:
    """Adverse-path ladder. Diverging by sign; entry row marked."""
    df = pd.DataFrame([
        {"level": f"{r.level:g}bp", "order": r.level, "implied": r.implied,
         "mtm": r.mtm, "entry": r.is_entry}
        for r in rows
    ])
    h = height or max(110, 26 * len(df) + 30)

    bars = (
        alt.Chart(df).mark_bar(cornerRadiusEnd=BAR_RADIUS, height=13)
        .encode(
            y=alt.Y("level:N", sort=alt.SortField("order"), axis=alt.Axis(title=None)),
            x=alt.X("mtm:Q", axis=alt.Axis(title="mark-to-market (bp)")),
            color=alt.condition(alt.datum.mtm >= 0, alt.value(p.positive), alt.value(p.negative)),
            opacity=alt.condition(alt.datum.entry, alt.value(0.35), alt.value(0.9)),
            tooltip=[alt.Tooltip("level:N", title="contract"),
                     alt.Tooltip("implied:Q", format=".0%", title="implied"),
                     alt.Tooltip("mtm:Q", format="+.1f", title="MTM (bp)")],
        )
    )
    df["label"] = df.apply(lambda r: f"{r['implied']:.0%}"
                           + ("   entry" if r["entry"] else ""), axis=1)
    labels = alt.Chart(df).mark_text(
        align="left", dx=6, fontSize=10, color=p.ink_secondary,
    ).encode(y=alt.Y("level:N", sort=alt.SortField("order")),
             x=alt.datum(0), text="label:N")
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(x="x:Q")

    return _apply(alt.layer(bars, zero, labels), p, h)


def priced_path(steps, spot: float, p: Palette, height: int = 240) -> alt.LayerChart:
    """The market's own path: implied policy rate meeting by meeting.

    Step interpolation, because that is literally what the instrument is -- the
    rate holds flat between meetings and jumps on the effective date. Per-meeting
    steps are direct-labelled and coloured by sign; the line itself stays neutral
    so it never competes with the aqua "your estimate" rule used elsewhere.

    A step solved off a stale or ambiguous contract is drawn differently -- a
    dashed line into it and a hollow dot -- rather than with the same solid
    confidence as a clean read. The ladder table already flags these with "!"
    or "~"; the chart has to agree with it, not just the headline Terminal and
    Peak numbers, which already exclude these steps (see `StripPath.reliable_steps`).
    """
    rows = [{"x": "spot", "order": -1, "rate": spot, "step": 0.0,
             "cum": 0.0, "label": "", "reliable": True, "label_colour": "muted"}]
    for i, s in enumerate(steps):
        rows.append({
            "x": s.meeting.end.strftime("%b %y"), "order": i, "rate": s.r_after,
            "step": s.step_bp, "cum": s.cum_bp,
            "label": (f"{s.step_bp:+.0f}" if abs(s.step_bp) >= 0.5 else "")
                     + ("" if s.reliable else " !"),
            "reliable": s.reliable,
            # A nested alt.condition() (reliable ? sign-colour : muted) isn't
            # valid in this Altair version -- precompute the category instead,
            # same idiom as the "sign" field in ev_curve().
            "label_colour": ("positive" if s.step_bp >= 0 else "negative")
                            if s.reliable else "muted",
        })
    df = pd.DataFrame(rows)
    order = alt.SortField("order")

    # A single mark_line cannot switch stroke style partway through, so the
    # solid-to-dashed transition is two layers sharing one boundary row: solid
    # runs through the last reliable point, dashed picks up from there.
    last_reliable = df.loc[df["reliable"], "order"].max()
    solid_df = df[df["order"] <= last_reliable]
    dashed_df = df[df["order"] >= last_reliable]

    x_enc = alt.X("x:N", sort=order, axis=alt.Axis(title=None, labelAngle=-45))
    y_enc = alt.Y("rate:Q", axis=alt.Axis(title="implied policy rate (%)", format=".2f"),
                 scale=alt.Scale(zero=False, nice=True))

    solid_line = (
        alt.Chart(solid_df).mark_line(strokeWidth=LINE_WIDTH, interpolate="step-after")
        .encode(x=x_enc, y=y_enc, color=alt.value(p.ink_secondary))
    )
    layers = [solid_line]
    if len(dashed_df) > 1:
        layers.append(
            alt.Chart(dashed_df)
            .mark_line(strokeWidth=LINE_WIDTH, interpolate="step-after", strokeDash=[4, 3])
            .encode(x=alt.X("x:N", sort=order), y="rate:Q", color=alt.value(p.muted))
        )

    tooltip = [alt.Tooltip("x:N", title="meeting"),
               alt.Tooltip("rate:Q", format=".3f", title="rate after (%)"),
               alt.Tooltip("step:Q", format="+.1f", title="step (bp)"),
               alt.Tooltip("cum:Q", format="+.1f", title="cumulative (bp)"),
               alt.Tooltip("reliable:N", title="reliable")]

    reliable_df = df[df["reliable"]]
    layers.append(
        alt.Chart(reliable_df).mark_point(
            filled=True, size=POINT_SIZE - 20, stroke=p.surface, strokeWidth=1.5,
        ).encode(
            x=alt.X("x:N", sort=order), y="rate:Q",
            color=alt.condition(alt.datum.step >= 0, alt.value(p.positive), alt.value(p.negative)),
            tooltip=tooltip,
        )
    )
    unreliable_df = df[~df["reliable"]]
    if len(unreliable_df):
        layers.append(
            alt.Chart(unreliable_df).mark_point(
                filled=False, size=POINT_SIZE - 20, strokeWidth=1.5,
            ).encode(
                x=alt.X("x:N", sort=order), y="rate:Q",
                stroke=alt.condition(alt.datum.step >= 0, alt.value(p.positive), alt.value(p.negative)),
                tooltip=tooltip,
            )
        )

    layers.append(
        alt.Chart(df).mark_text(align="center", dy=-11, fontSize=10, fontWeight="bold")
        .encode(
            x=alt.X("x:N", sort=order), y="rate:Q", text="label:N",
            color=alt.Color("label_colour:N", legend=None, scale=alt.Scale(
                domain=["positive", "negative", "muted"],
                range=[p.positive, p.negative, p.muted])),
        )
    )
    layers.insert(0, alt.Chart(pd.DataFrame({"y": [spot]})).mark_rule(
        color=p.baseline, strokeWidth=1, strokeDash=[4, 3]).encode(y="y:Q"))

    return _apply(alt.layer(*layers), p, height)


def sofr_basis(periods, p: Palette, height: int = 200) -> alt.LayerChart:
    """SOFR/EFFR basis per IMM window: what SR3 prices, less the ZQ path.

    Only windows the fed funds strip actually covers are plotted. Bars rather
    than a line, because these are independent readings on separate contracts,
    not a continuous curve -- connecting them would imply a path between IMM
    dates that no instrument here quotes.
    """
    df = pd.DataFrame([
        {"code": s.code, "order": i, "basis": s.basis_bp,
         "label": f"{s.basis_bp:+.1f}"}
        for i, s in enumerate(periods)
    ])
    order = alt.SortField("order")

    bars = (
        alt.Chart(df).mark_bar(cornerRadiusEnd=BAR_RADIUS, size=26)
        .encode(
            x=alt.X("code:N", sort=order, axis=alt.Axis(title=None, labelAngle=0)),
            y=alt.Y("basis:Q", axis=alt.Axis(title="SR3 less ZQ path (bp)")),
            color=alt.condition(alt.datum.basis >= 0, alt.value(p.positive), alt.value(p.negative)),
            opacity=alt.value(0.85),
            tooltip=[alt.Tooltip("code:N", title="contract"),
                     alt.Tooltip("basis:Q", format="+.2f", title="basis (bp)")],
        )
    )
    labels = (
        alt.Chart(df).mark_text(align="center", dy=-8, fontSize=10, fontWeight="bold")
        .encode(
            x=alt.X("code:N", sort=order), y="basis:Q", text="label:N",
            color=alt.condition(alt.datum.basis >= 0, alt.value(p.positive), alt.value(p.negative)),
        )
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(y="y:Q")

    return _apply(alt.layer(zero, bars, labels), p, height)


def mc_fan_chart(result, points: float, size: float, side: Side, p: Palette,
                 height: int = 260, release_days: dict[int, list[str]] | None = None,
                 family_colour: dict[str, str] | None = None) -> alt.LayerChart:
    """A sample of simulated pre-announcement paths, so the touch counts in
    the table above are a distance you can see rather than a number to take
    on trust. Solid = the driftless diffusion; dashed = the decision jump for
    paths that never touched either barrier, landing at 0 or `size` --
    discontinuous by construction, so it is drawn differently rather than as
    one more day of drift. Colour is the same positive/negative P&L pair used
    everywhere else in this app, not a new encoding for this one chart.

    `release_days` maps a day index on the x-axis to the releases printing
    that day; when supplied, each gets a vertical dotted rule. The x-axis
    switches to real dates at the same time -- a marker saying "CPI" against
    "day 8" is a puzzle, against "12 Aug" it is a date you can act on. Colours
    come from the caller's own release-family palette (`family_colour`) so the
    markers match the economic-calendar table on the Pricing tab rather than
    introducing a second colour language for the same events.
    """
    diffusion_rows, jump_rows = [], []
    for i, sp in enumerate(result.sample_paths):
        sign = "positive" if pathmod.mtm_at(points, sp.terminal_level, side) >= 0 else "negative"
        for day, level in enumerate(sp.levels):
            diffusion_rows.append({"path": i, "day": day, "level": level, "sign": sign})
        if sp.exit_reason == "rode_out":
            last_day = len(sp.levels) - 1
            jump_rows.append({"path": i, "day": last_day, "level": sp.levels[-1], "sign": sign})
            jump_rows.append({"path": i, "day": last_day + 1,
                             "level": sp.terminal_level, "sign": sign})
    ddf = pd.DataFrame(diffusion_rows)
    scale = alt.Scale(domain=["positive", "negative"], range=[p.positive, p.negative])

    # With markers on, the axis is dated: `result.days` gives the real date for
    # each step, and the jump lands one slot past the last one.
    dated = bool(release_days) and bool(getattr(result, "days", None))
    if dated:
        by_index = list(result.days) + [result.days[-1] + timedelta(days=1)]
        for row in diffusion_rows:
            row["at"] = by_index[min(row["day"], len(by_index) - 1)]
        for row in jump_rows:
            row["at"] = by_index[min(row["day"], len(by_index) - 1)]
        ddf = pd.DataFrame(diffusion_rows)
        x_enc = alt.X("at:T", axis=alt.Axis(title=None, format="%d %b", tickCount=6))
    else:
        x_enc = alt.X("day:Q", axis=alt.Axis(title="trading days to the decision",
                                             tickMinStep=1))
    y_enc = alt.Y("level:Q", axis=alt.Axis(title="priced level (bp)"))

    # Opacity is tuned against DEFAULT_SAMPLE_PATHS (core.montecarlo): more
    # lines need less ink each or the region where every path starts (they
    # all begin at `points`) saturates to a solid block and the fan's
    # density gradient -- the whole reason to plot individual paths instead
    # of a percentile band -- disappears.
    layers = [
        alt.Chart(ddf).mark_line(strokeWidth=1, opacity=0.28, interpolate="linear")
        .encode(x=x_enc, y=y_enc, detail="path:N",
                color=alt.Color("sign:N", scale=scale, legend=None))
    ]
    if jump_rows:
        layers.append(
            alt.Chart(pd.DataFrame(jump_rows))
            .mark_line(strokeWidth=1, opacity=0.28, strokeDash=[3, 2])
            .encode(x=x_enc, y=y_enc, detail="path:N",
                    color=alt.Color("sign:N", scale=scale, legend=None))
        )

    bounds = alt.Chart(pd.DataFrame({"y": [0.0, size]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(y="y:Q")

    ref_df = pd.DataFrame([
        {"y": points, "label": f"entry {points:g}bp", "colour": p.accent},
        {"y": result.target_level, "label": f"target {result.target_level:g}bp", "colour": p.muted},
        {"y": result.stop_level, "label": f"stop {result.stop_level:g}bp", "colour": p.muted},
    ])
    marker_layers = []
    if dated:
        palette = family_colour or {}
        rows = []
        for day_idx, names in sorted(release_days.items()):
            if day_idx >= len(by_index):
                continue
            for n_ in names:
                rows.append({"at": by_index[day_idx], "release": n_,
                             "colour": palette.get(n_, p.muted),
                             "label": " · ".join(names)})
        if rows:
            mdf = pd.DataFrame(rows).drop_duplicates(subset=["at", "release"])
            # One rule per DAY, coloured by the first release on it -- two rules
            # on the same date would just overdraw. The tooltip carries the full
            # list, so a doubled-up day is still readable.
            per_day = mdf.drop_duplicates(subset=["at"])
            marker_layers.append(
                alt.Chart(per_day).mark_rule(strokeWidth=1, strokeDash=[2, 3],
                                             opacity=0.75)
                .encode(x="at:T",
                        color=alt.Color("colour:N", scale=None, legend=None),
                        tooltip=[alt.Tooltip("at:T", title="date", format="%a %d %b"),
                                 alt.Tooltip("label:N", title="prints")])
            )
            # No per-marker text. Thirteen of thirty-one days carry a release
            # in a typical window, and rotated labels at that density collide
            # into noise on a half-width chart. Identity comes from the family
            # colour plus the hover tooltip, with a legend under the chart --
            # the same division of labour the economic-calendar table already
            # uses, rather than a second convention for the same events.

    rules = alt.Chart(ref_df).mark_rule(strokeWidth=1, strokeDash=[4, 3]).encode(
        y="y:Q", color=alt.Color("colour:N", scale=None, legend=None))
    # The level labels anchor to the left edge. On a dated axis that is a real
    # date, not the number 0, or Vega drops them onto the 1970 epoch.
    anchor = alt.datum(by_index[0]) if dated else alt.datum(0)
    labels = alt.Chart(ref_df).mark_text(
        align="left", dx=4, dy=-5, fontSize=10, fontWeight="bold",
    ).encode(x=anchor, y="y:Q", text="label:N",
             color=alt.Color("colour:N", scale=None, legend=None))

    return _apply(alt.layer(*marker_layers, *layers, bounds, rules, labels), p, height)


def mc_sensitivity(sweep, current_level: float, axis_title: str, p: Palette,
                   height: int = 220) -> alt.LayerChart:
    """EV as one barrier moves, the other held fixed -- the answer to "if I
    move my stop/target, what happens to EV", read straight off the curve
    instead of re-typing counts by hand at every candidate level.
    """
    df = pd.DataFrame([{"level": s.level, "ev": s.ev_with_exits, "p_hit": s.p_hit_first}
                       for s in sweep])
    x_enc = alt.X("level:Q", axis=alt.Axis(title=axis_title))
    y_enc = alt.Y("ev:Q", axis=alt.Axis(title="EV with exits (bp)"))

    # A gradient line needs building by hand: binding a quantitative field to
    # `color` on a plain mark_line(), with no shared `detail`, makes Vega-Lite
    # treat every distinct colour value as its own line -- one point each, so
    # every "line" degenerates to a zero-length M-then-Z path and nothing
    # draws. Exploding into explicit two-point segments (each its own detail
    # group, coloured by its own midpoint p_hit) gives Vega-Lite a real line
    # to draw per segment; consecutive segments sharing endpoints is what
    # makes the whole thing read as one continuous, coloured curve.
    seg_rows = []
    for i in range(len(df) - 1):
        a, b = df.iloc[i], df.iloc[i + 1]
        mid = (a["p_hit"] + b["p_hit"]) / 2.0
        seg_rows.append({"level": a["level"], "ev": a["ev"], "seg": i, "p_hit": mid})
        seg_rows.append({"level": b["level"], "ev": b["ev"], "seg": i, "p_hit": mid})
    seg_df = pd.DataFrame(seg_rows)

    # Sequential encoding (magnitude, not identity): one hue, light to dark,
    # from Palette.seq_low/seq_high. Bolder than the usual line weight so the
    # gradient is actually legible rather than a hairline tint shift; the
    # No colorbar -- the hover tooltip's own P(touched first) figure is
    # already the non-colour readout of this value, so a legend would just
    # repeat it while eating width from the plot.
    line = (
        alt.Chart(seg_df).mark_line(strokeWidth=3.5)
        .encode(
            x=x_enc, y=y_enc, detail="seg:N",
            color=alt.Color(
                "p_hit:Q",
                scale=alt.Scale(domain=[0, 1], range=[p.seq_low, p.seq_high]),
                legend=None,
            ),
        )
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(y="y:Q")

    nearest = (df["level"] - current_level).abs().idxmin()
    cur_df = df.loc[[nearest]].copy()
    cur_df["label"] = cur_df["ev"].apply(lambda v: f"current  {v:+.2f}bp")
    cur_dot = (
        alt.Chart(cur_df).mark_point(filled=True, size=POINT_SIZE, color=p.accent,
                                     stroke=p.surface, strokeWidth=2)
        .encode(x=x_enc, y=y_enc)
    )
    cur_text = (
        alt.Chart(cur_df).mark_text(align="left", dx=7, dy=-9, fontSize=10,
                                    fontWeight="bold", color=p.accent)
        .encode(x=x_enc, y=y_enc, text="label:N")
    )

    sel = alt.selection_point(on="pointerover", nearest=True, fields=["level"], empty=False)
    hover = (
        alt.Chart(df).mark_rule(color=p.muted, strokeWidth=1, opacity=0)
        .encode(
            x=x_enc,
            opacity=alt.condition(sel, alt.value(0.35), alt.value(0)),
            tooltip=[alt.Tooltip("level:Q", format=".1f", title=axis_title),
                     alt.Tooltip("ev:Q", format="+.2f", title="EV (bp)"),
                     alt.Tooltip("p_hit:Q", format=".0%", title="P(touched first)")],
        )
        .add_params(sel)
    )
    return _apply(alt.layer(zero, line, cur_dot, cur_text, hover), p, height)


def sparkline(observations, threshold: float | None, triggered: bool,
              p: Palette, height: int = 80) -> alt.LayerChart:
    """Single-series line with an optional threshold rule."""
    df = pd.DataFrame([{"date": o.date, "value": o.value} for o in observations])
    colour = p.critical if triggered else p.ink_secondary

    line = (
        alt.Chart(df).mark_line(strokeWidth=LINE_WIDTH)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format="%b %y", tickCount=4)),
            y=alt.Y("value:Q", axis=alt.Axis(title=None), scale=alt.Scale(zero=False)),
            color=alt.value(colour),
            tooltip=[alt.Tooltip("date:T", title="date"),
                     alt.Tooltip("value:Q", format=".2f", title="value")],
        )
    )
    layers = [line]
    if threshold is not None:
        rule = alt.Chart(pd.DataFrame({"y": [threshold]})).mark_rule(
            color=p.critical, strokeWidth=1, strokeDash=[4, 3]).encode(y="y:Q")
        layers.append(rule)
    return _apply(alt.layer(*layers), p, height)


def labor_slack(readings, p: Palette, height: int = 620) -> alt.LayerChart:
    """The labour-market slack dashboard: every indicator as a percentile of
    its own 1995-present distribution, on one More Slack -> Less Slack axis.

    A dumbbell per row rather than a bar: the pair of dots carries a change
    over a year, and a bar from zero would imply the axis has a meaningful
    origin, which a percentile does not. The hollow dot is a year ago, the
    filled dot is now, and the connecting rule is coloured by which way the
    row travelled -- so the eye reads position (where slack sits) and
    direction (which way it is going) without needing to consult a table.
    """
    rows, seg = [], []
    for r in readings:
        if not r.ok:
            continue
        rows.append({
            "label": r.indicator.label, "pct": r.current_pct,
            "travel": r.travel, "value": r.current_value,
            "as_of": r.as_of.isoformat() if r.as_of else "",
            "approx": "yes" if r.indicator.approximate else "no",
            "n": r.n_obs,
        })
        if r.year_ago_pct is not None:
            seg.append({"label": r.indicator.label, "pct": r.year_ago_pct,
                        "pct2": r.current_pct, "travel": r.travel})
    df, sdf = pd.DataFrame(rows), pd.DataFrame(seg)
    order = [x["label"] for x in rows]

    travel_scale = alt.Scale(
        domain=["cooling", "tightening", "flat"],
        range=[p.negative, p.positive, p.muted])
    y_enc = alt.Y("label:N", sort=order, axis=alt.Axis(
        title=None, labelFontSize=10, labelLimit=190, grid=True, ticks=False,
        domain=False))
    x_enc = alt.X("pct:Q", scale=alt.Scale(domain=[0, 100]),
                  axis=alt.Axis(title="Percentile of 1995-present   —   more slack ← → less slack",
                                format="d", values=[0, 25, 50, 75, 100]))

    layers = []
    # The 50th percentile is the only reference an axis of ranks really has.
    layers.append(alt.Chart(pd.DataFrame({"x": [50]})).mark_rule(
        color=p.baseline, strokeWidth=1, strokeDash=[3, 3]).encode(x="x:Q"))
    if not sdf.empty:
        layers.append(
            alt.Chart(sdf).mark_rule(strokeWidth=2, opacity=0.55).encode(
                y=y_enc, x=x_enc, x2="pct2:Q",
                color=alt.Color("travel:N", scale=travel_scale, legend=None))
        )
        # Year ago: hollow, so the filled "now" dot is unambiguously the end
        # of the journey without needing an arrowhead.
        layers.append(
            alt.Chart(sdf).mark_point(
                filled=True, size=52, color=p.surface, strokeWidth=1.5).encode(
                y=y_enc, x=x_enc,
                stroke=alt.Color("travel:N", scale=travel_scale, legend=None))
        )
    layers.append(
        alt.Chart(df).mark_point(filled=True, size=95, stroke=p.surface,
                                 strokeWidth=1.5).encode(
            y=y_enc, x=x_enc,
            color=alt.Color("travel:N", scale=travel_scale, legend=None),
            tooltip=[alt.Tooltip("label:N", title="indicator"),
                     alt.Tooltip("pct:Q", format=".0f", title="percentile"),
                     alt.Tooltip("value:Q", format=".2f", title="latest value"),
                     alt.Tooltip("travel:N", title="vs a year ago"),
                     alt.Tooltip("as_of:N", title="as of"),
                     alt.Tooltip("n:Q", title="observations"),
                     alt.Tooltip("approx:N", title="approximated")])
    )
    return _apply(alt.layer(*layers), p, height)


def labor_diffusion(points, p: Palette, height: int = 260,
                    window_start: "date | None" = None,
                    recessions: "list[tuple[date, float]] | None" = None,
                    ) -> alt.LayerChart:
    """Share of indicators tightening -- how BROAD the move is.

    The companion to the slack dumbbells, answering the other half of the
    question. A percentile says where each indicator sits against its own
    history; this says how many of them are moving the same way now. A market
    can be historically tight and broadly deteriorating at once, and the turn
    shows up here before it shows up in the levels.

    Two lines, so a legend is mandatory. The noisy month-over-month line is
    deliberately recessive and the smoother year-over-year line carries the
    ink: both are the same measurement at different horizons, and the eye
    should land on the one that is actually readable.

    `window_start` (a cutoff date) limits the view to the most recent N years
    when the user picks a history window; None (the default) plots the full
    series. The labour Overall Indicator percentile dashboard above is exempt
    from this window by design -- it ranks each reading against its full
    1995-present history -- so only this diffusion chart honours it.
    """
    rows = []
    for pt in points:
        if window_start is not None and pt.when < window_start:
            continue
        if pt.mom is not None:
            rows.append({"when": pt.when, "share": pt.mom, "basis": "Month-over-month"})
        if pt.yoy is not None:
            rows.append({"when": pt.when, "share": pt.yoy, "basis": "Year-over-year"})
    df = pd.DataFrame(rows)

    scale = alt.Scale(domain=["Month-over-month", "Year-over-year"],
                      range=[p.muted, p.ink_secondary])
    xfmt, xticks = _gs_time_format(list(df["when"]) if not df.empty else [])
    x_enc = alt.X("when:T", axis=alt.Axis(title="Date", titleColor=p.muted,
                                          format=xfmt, tickCount=xticks))
    y_enc = alt.Y("share:Q", scale=alt.Scale(domain=[0, 100]),
                  axis=alt.Axis(title="share of indicators tightening", format="d",
                                values=[0, 25, 50, 75, 100]))

    # NBER recession shading, same grey bands as the GS labour charts. Trimmed
    # to the same window as the lines: layered charts union the x-domain across
    # layers, so an untrimmed 1955-present band set would stretch the axis back
    # to 1955 and squeeze the visible lines into a sliver.
    bands = None
    if recessions:
        rec = recessions
        if window_start is not None:
            rec = [(d, v) for d, v in recessions if d >= window_start]
        rdf = _gs_rec_bands(rec)
        if not rdf.empty:
            bands = (alt.Chart(rdf).mark_rect(opacity=0.12, color="#7f7f7f")
                     .encode(x="start:T", x2="end:T"))

    # 50% is the only line that means anything here: above it more indicators
    # are improving than deteriorating, below it the reverse.
    half = alt.Chart(pd.DataFrame({"y": [50]})).mark_rule(
        color=p.baseline, strokeWidth=1, strokeDash=[4, 3]).encode(y="y:Q")
    lines = (
        alt.Chart(df).mark_line(strokeWidth=1.6, interpolate="monotone")
        .encode(x=x_enc, y=y_enc,
                color=alt.Color("basis:N", scale=scale,
                                legend=alt.Legend(title=None, orient="bottom-left",
                                                  direction="horizontal",
                                                  labelColor=p.ink_secondary,
                                                  labelFontSize=10, padding=4)),
                opacity=alt.condition(alt.datum.basis == "Year-over-year",
                                      alt.value(1.0), alt.value(0.55)))
    )
    sel = alt.selection_point(on="pointerover", nearest=True, fields=["when"], empty=False)
    hover = (
        alt.Chart(df).mark_rule(color=p.muted, strokeWidth=1, opacity=0)
        .encode(x=x_enc,
                opacity=alt.condition(sel, alt.value(0.35), alt.value(0)),
                tooltip=[alt.Tooltip("when:T", title="month", format="%b %Y"),
                         alt.Tooltip("basis:N", title="basis"),
                         alt.Tooltip("share:Q", format=".0f", title="% tightening")])
        .add_params(sel)
    )
    layers = [b for b in (bands, half, lines, hover) if b is not None]
    return _apply(alt.layer(*layers), p, height)


# --------------------------------------------------------------------------
def inflation_trend(series_list, p: Palette, height: int = 420,
                    window_start: date | None = None,
                    vintages: "dict[str, Vintage] | None" = None) -> list[go.Figure]:
    """Sequential core inflation trend -- CPI then PCE, stacked vertically.

    Returns two separate Plotly figures (one per panel) so each scrolls
    under the next, matching the FRBSF contribution charts below. History is
    trimmed to each panel's FRBSF horizon (CPI from 1999, PCE from 1960) via
    ``inflation.PANEL_HISTORY_START`` so the trend lines up with the
    contribution charts directly beneath it. ``window_start`` (a cutoff date)
    further limits the view to the most recent N years when the user picks a
    history window.

    ``vintages`` maps a panel key ("CPI" / "PCE") to its :class:`Vintage`, so
    each panel reports the release schedule and fetch date of its OWN source
    rather than a single line for two independently-published statistics.
    """
    palette_cols = [p.ink_secondary, p.positive, p.negative, p.accent,
                    p.muted, p.critical, p.warning]

    figs: list[go.Figure] = []
    for panel, title in (("CPI", "CPI — Month-over-month % change"),
                         ("PCE", "PCE — Month-over-month % change")):
        start = inflation_mod.PANEL_HISTORY_START.get(panel, date(1900, 1, 1))
        # Window cutoff = the later of the panel horizon and the user's chosen
        # history window (None when "All" is selected -> only the panel horizon).
        cutoff = start if window_start is None else max(start, window_start)
        panel_series = [s for s in series_list if s.panel == panel]

        rows = []
        for s in panel_series:
            for o in s.observations:
                if o.date >= cutoff:
                    rows.append({"date": o.date, "value": o.value,
                                 "series": s.label})
        vintage = (vintages or {}).get(panel)
        fig = go.Figure()
        if not rows:
            fig.add_annotation(text=f"No {panel} data", showarrow=False,
                               font=dict(color=p.ink))
            figs.append(fig)
            continue

        df = pd.DataFrame(rows)
        y_min = math.floor((df["value"].min() - 0.1) * 10) / 10
        y_max = math.ceil((df["value"].max() + 0.1) * 10) / 10

        # Stable colour per series, consistent wherever it appears.
        series_names = sorted(df["series"].unique())
        colour = {name: palette_cols[i % len(palette_cols)]
                  for i, name in enumerate(series_names)}

        min_date, max_date = df["date"].min(), df["date"].max()

        for s in panel_series:
            obs = [o for o in s.observations if o.date >= cutoff]
            if not obs:
                continue
            fig.add_trace(go.Scatter(
                x=[o.date for o in obs],
                y=[o.value for o in obs],
                name=s.label, mode="lines",
                line=dict(width=1.8, color=colour[s.label], shape="spline"),
                hovertemplate=f"{s.label}<br>%{{x|%b %Y}}: %{{y:+.2f}}%<extra></extra>",
            ))

        # Zero reference line drawn across the full x-range.
        fig.add_shape(type="line", xref="x", yref="y",
                      x0=min_date, x1=max_date, y0=0, y1=0,
                      line=dict(color=p.baseline, width=1))

        fig.update_layout(
            height=height,
            title=dict(text=title, x=0.0, xanchor="left",
                       font=dict(size=14, color=p.ink)),
            paper_bgcolor=p.surface, plot_bgcolor=p.plane,
            font=dict(color=p.ink, size=12),
            margin=dict(l=56, r=16, t=46, b=96),
            showlegend=True,
            # Horizontal, left-anchored legend under the chart -- matches the
            # FRBSF contribution charts below. The b=78 bottom margin keeps the
            # legend clear of the x-axis year labels (same gap as those charts).
            legend=dict(orientation="h", x=0.0, xanchor="left",
                        y=0.0, yanchor="top", title=None, itemsizing="constant",
                        font=dict(color=p.ink, size=11), tracegroupgap=4),
            hovermode="x unified",
        )
        fig.update_yaxes(title=dict(text="% change, month ago"),
                         range=[y_min, y_max], gridcolor=p.grid,
                         zeroline=False, tickformat=".1f")
        fig.update_xaxes(type="date", tickformat="%Y", gridcolor=p.grid,
                         range=[min_date, max_date])
        figs.append(apply_vintage(fig, vintage, p))

    return figs


# ===========================================================================
# GS Labour Market Dashboard  (Exhibit 3, July 2026 page 5)
# Six charts: Unemployment Rate, LFPR, E/P Ratio, Jobs-Workers Gap,
# Layoff+Quits Rate, Net Job Gains.  2-per-row layout in the Labour tab.
# ===========================================================================

def _gs_rec_bands(rec: list[tuple[date, float]]) -> pd.DataFrame:
    """Run-length-encode NBER recession months into (start, end) rects.

    Dates are converted to datetime64 -- altair serialises datetime64 to ISO
    strings, while a raw object-dtype column of Python dates survives to_dict
    and can break downstream JSON serialisation (notably vl-convert).
    """
    if not rec:
        return pd.DataFrame(columns=["start", "end"])
    runs: list[tuple[date, date]] = []
    cur: date | None = None
    for d, v in rec:
        if v >= 0.5:
            if cur is None:
                cur = d
        elif cur is not None:
            runs.append((cur, d))
            cur = None
    if cur is not None:
        runs.append((cur, rec[-1][0]))
    df = pd.DataFrame(runs, columns=["start", "end"])
    if not df.empty:
        df["start"] = pd.to_datetime(df["start"])
        df["end"] = pd.to_datetime(df["end"])
    return df


def _gs_time_format(dates: list[date]) -> tuple[str, int]:
    """X-axis label format + tick count fitted to the plotted span.

    Short windows (<= ~3 years) get month-year labels so the actual months
    are readable; longer history falls back to year-only labels.
    """
    if not dates:
        return "%Y", 6
    span = (max(dates) - min(dates)).days
    if span <= 3 * 366:
        return "%b %Y", 8
    return "%Y", 6


def _gs_line(series: list[tuple[date, float]], title: str, ylabel: str,
             rec: list[tuple[date, float]], p: Palette, height: int,
             invert: bool = False, show_title: bool = True,
             vintage: "Vintage | None" = None) -> alt.LayerChart:
    """Single-line chart with NBER recession shading.

    `show_title=False` suppresses the in-chart title (used when the caller
    renders the title as a Streamlit header with a `?` help popover instead);
    the tooltip still uses `title` either way. `vintage` rides under the title,
    so it too is suppressed when there is no title to hang it under -- those
    callers render the line beside their own header with `components.vintage`.
    """
    df = pd.DataFrame(series, columns=["date", "v"]).dropna()
    rdf = _gs_rec_bands(rec)
    bands = (
        alt.Chart(rdf).mark_rect(opacity=0.12, color="#7f7f7f")
        .encode(x="start:T", x2="end:T")
        if not rdf.empty
        else alt.Chart(pd.DataFrame({"start": [], "end": []})).mark_rect(opacity=0)
    )
    xfmt, xticks = _gs_time_format([d for d, _ in series])
    line = (
        alt.Chart(df)
        .mark_line(strokeWidth=2, interpolate="monotone", color=p.positive)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format=xfmt,
                                            tickCount=xticks,
                                            labelColor=p.muted, gridColor=p.grid)),
            y=alt.Y("v:Q", axis=alt.Axis(title=ylabel, titleColor=p.muted,
                                         labelColor=p.muted, gridColor=p.grid),
                    scale=alt.Scale(nice=True, padding=0.05, zero=False,
                                    reverse=invert)),
            tooltip=[
                alt.Tooltip("date:T", title=title, format="%b %Y"),
                alt.Tooltip("v:Q", title=ylabel, format=".1f"),
            ],
        )
    )
    props: dict = {"height": height, "width": "container"}
    if show_title:
        props["title"] = vintage_title(title, vintage, p)
    return (
        alt.layer(bands, line)
        .properties(**props)
        .configure_view(stroke="transparent")
        .configure_axis(**{k: v for k, v in _base(p)["config"]["axis"].items()})
    )


def _gs_dual_line(left_series: list[tuple[date, float]],
                  right_series: list[tuple[date, float]],
                  title: str, left_label: str, right_label: str,
                  rec: list[tuple[date, float]], p: Palette, height: int,
                  left_reverse: bool = False,
                  vintage: "Vintage | None" = None) -> alt.LayerChart:
    """Dual-line chart: two y-axes, NBER shading, compact legend."""
    ldf = pd.DataFrame(left_series, columns=["date", "v"]).dropna()
    rdf = pd.DataFrame(right_series, columns=["date", "v"]).dropna()

    rdfb = _gs_rec_bands(rec)
    bands = (
        alt.Chart(rdfb).mark_rect(opacity=0.12, color="#7f7f7f")
        .encode(x="start:T", x2="end:T")
        if not rdfb.empty
        else alt.Chart(pd.DataFrame({"start": [], "end": []})).mark_rect(opacity=0)
    )

    xfmt, xticks = _gs_time_format(
        [d for d, _ in left_series] + [d for d, _ in right_series])

    ll = (
        alt.Chart(ldf)
        .mark_line(strokeWidth=2, interpolate="monotone", color=p.positive)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format=xfmt,
                                            tickCount=xticks,
                                            labelColor=p.muted, gridColor=p.grid)),
            y=alt.Y("v:Q", axis=alt.Axis(title=left_label, titleColor=p.muted,
                                         labelColor=p.muted, gridColor=p.grid),
                    scale=alt.Scale(nice=True, padding=0.05, zero=False,
                                    reverse=left_reverse)),
            tooltip=[alt.Tooltip("date:T", title=left_label, format="%b %Y"),
                     alt.Tooltip("v:Q", title=left_label, format=".2f")],
        )
    )
    rl = (
        alt.Chart(rdf)
        .mark_line(strokeWidth=2, interpolate="monotone", color=p.negative,
                   strokeDash=[4, 3])
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format=xfmt,
                                            tickCount=xticks,
                                            gridColor="transparent")),
            y=alt.Y("v:Q",
                    axis=alt.Axis(title=right_label, titleColor=p.muted,
                                  labelColor=p.muted, orient="right",
                                  gridColor="transparent"),
                    scale=alt.Scale(zero=False, nice=True, padding=0.1)),
            tooltip=[alt.Tooltip("date:T", title=right_label, format="%b %Y"),
                     alt.Tooltip("v:Q", title=right_label, format=".2f")],
        )
    )
    return (
        alt.layer(bands, ll, rl)
        .resolve_scale(y="independent")
        .properties(title=vintage_title(title, vintage, p),
                    height=height, width="container")
        .configure_view(stroke="transparent")
        .configure_axis(**{k: v for k, v in _base(p)["config"]["axis"].items()})
    )


def _gs_bar(series: list[tuple[date, float]], title: str, ylabel: str,
            p: Palette, height: int,
            vintage: "Vintage | None" = None) -> alt.LayerChart:
    """Bar chart — positive bars blue, negative red (for net job gains)."""
    df = pd.DataFrame(series, columns=["date", "v"]).dropna()
    df["color"] = df["v"].apply(lambda v: p.positive if v >= 0 else p.negative)
    xfmt, xticks = _gs_time_format(list(df["date"]))
    bars = (
        alt.Chart(df)
        .mark_bar(strokeWidth=0)
        .encode(
            x=alt.X("date:T", axis=alt.Axis(title=None, format=xfmt,
                                            tickCount=xticks, labelColor=p.muted,
                                            gridColor=p.grid)),
            y=alt.Y("v:Q", axis=alt.Axis(title=ylabel, titleColor=p.muted,
                                         labelColor=p.muted, gridColor=p.grid),
                    scale=alt.Scale(nice=True, padding=0.05)),
            color=alt.Color("color:N", scale=None, legend=None),
            tooltip=[
                alt.Tooltip("date:T", title="Month", format="%b %Y"),
                alt.Tooltip("v:Q", title=ylabel, format=",.0f"),
            ],
        )
    )
    return (
        bars
        .properties(title=vintage_title(title, vintage, p),
                    height=height, width="container")
        .configure_view(stroke="transparent")
        .configure_axis(**{k: v for k, v in _base(p)["config"]["axis"].items()})
    )


# --- The six specific chart renderers -------------------------------------
#
# Each takes its own `vintage`: the grid mixes two releases (see
# `core.gs_labour.CHART_RELEASES`), so one line across all six would date the
# JOLTS panels by the Employment Situation's calendar.

def gs_unemployment_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 220, vintage: "Vintage | None" = None,
) -> alt.LayerChart:
    return _gs_line(
        data.get("UNRATE_level", []), "Unemployment Rate", "Percent",
        data.get("USRECDP_monthly", []), p, height, vintage=vintage,
    )


def gs_lfpr_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 220, vintage: "Vintage | None" = None,
) -> alt.LayerChart:
    return _gs_line(
        data.get("CIVPART_level", []), "Labour Force Participation Rate", "Percent",
        data.get("USRECDP_monthly", []), p, height, vintage=vintage,
    )


def gs_emratio_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 220, vintage: "Vintage | None" = None,
) -> alt.LayerChart:
    return _gs_line(
        data.get("EMRATIO_level", []), "Employment-to-Population Ratio", "Percent",
        data.get("USRECDP_monthly", []), p, height, vintage=vintage,
    )


def gs_jwgap_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 220,
) -> alt.LayerChart:
    # No `vintage`: this chart's title lives in app.py as a Streamlit header
    # (it carries a `?` popover), so its line is rendered there too.
    return _gs_line(
        data.get("GS_JOBS_WORKERS_GAP", []), "Jobs-Workers Gap*",
        "Percent of labour force",
        data.get("USRECDP_monthly", []), p, height,
        show_title=False,   # title + `?` rendered as a header in app.py
    )


def gs_layoff_quits_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 220, vintage: "Vintage | None" = None,
) -> alt.LayerChart:
    return _gs_dual_line(
        left_series=data.get("JTSLDR_level", []),
        right_series=data.get("JTSQUR_level", []),
        title="Layoff Rate & Quits Rate",
        left_label="Layoff Rate (%)",
        right_label="Quits Rate (%)",
        rec=data.get("USRECDP_monthly", []),
        p=p, height=height, vintage=vintage,
    )


def gs_net_job_gains_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 220, vintage: "Vintage | None" = None,
) -> alt.LayerChart:
    return _gs_bar(
        data.get("GS_NET_JOB_GAINS_3MA", []),
        "Net Job Gains", "Thousands (3m avg)",
        p, height, vintage=vintage,
    )


# ===========================================================================
# Payrolls & wages  (Westpac-style charts at the bottom of the Labour tab)
# ---------------------------------------------------------------------------
# Chart 1: monthly change in nonfarm payrolls (millions), stacked by
# component (Private ex Health & Education, Education & Health Services,
# Government) with Total and Private-ex-HE 3-month moving-average lines.
# Chart 2: sector wage-growth heatmap, % y/y by sector over the latest
# 12 months, coloured on a fixed 2.0-5.0% scale so the colour means the
# same thing every month.
# ===========================================================================

# Component stack, bottom to top; the private-ex-HE line shares its
# component's colour family only in the HTML legend, not here.
PAYROLL_STACK: tuple[tuple[str, str], ...] = (
    ("priv_ex_he", "Private (ex Health & Education)"),
    ("ehs", "Education & Health Services"),
    ("government", "Government"),
)


def _payroll_bar_colour(key: str, p: Palette) -> str:
    return {"priv_ex_he": p.positive, "ehs": p.accent,
            "government": p.muted}[key]


def _clip_window(series: list[tuple[date, float]],
                 window_start: date | None) -> list[tuple[date, float]]:
    """History-window cutoff, the same convention the GS charts use."""
    if window_start is None:
        return series
    return [(d, v) for d, v in series if d >= window_start]


def payroll_components_chart(
    data: dict[str, list[tuple[date, float]]],
    p: Palette, height: int = 380,
    window_start: date | None = None,
) -> alt.LayerChart:
    """US NFP by select components, MoM (millions).

    Stacked bars, one per component, summing to the total payroll change;
    two trailing 3-month-average lines overlaid (Total, Private ex H&E).
    `window_start` scopes the bars AND the lines AND the recession bands,
    exactly like the diffusion and GS charts above it -- layered charts union
    x-domains, so an untrimmed band set would stretch the axis backwards.
    """
    # -- bars -------------------------------------------------------------
    bar_rows: list[dict] = []
    for order, (key, label) in enumerate(PAYROLL_STACK):
        for d, v in _clip_window(data.get(f"NFP_{key.upper()}_MOM", []),
                                 window_start):
            bar_rows.append({
                "date": d, "v": v, "component": label,
                "colour": _payroll_bar_colour(key, p),
                "order": order,
            })
    bdf = pd.DataFrame(bar_rows)
    if not bdf.empty:
        bdf["date"] = pd.to_datetime(bdf["date"])

    # -- 3mma lines -------------------------------------------------------
    # Red and white, per the user's spec, drawn AFTER the bars so they sit on
    # top of the stack (layer order below: bands -> bars -> lines). Total is
    # the near-white ink; Private (ex H&E) is the red dashed line so the two
    # read against the coloured bars and against each other.
    # The line layer carries its OWN x encoding with bandPosition=0.5: on the
    # banded yearmonth scale a point defaults to the band's left edge, which
    # would end the lines at the last bar's LEFT edge; centring puts the
    # endpoints in the middle of each bar, so the lines run through the bars
    # and finish on the most recent one.
    line_rows: list[dict] = []
    for d, v in _clip_window(data.get("NFP_TOTAL_MA3", []), window_start):
        line_rows.append({"date": d, "v": v, "line": "Total (3mma)",
                          "colour": p.ink, "dash": [0]})
    for d, v in _clip_window(data.get("NFP_PRIV_EX_HE_MA3", []), window_start):
        line_rows.append({"date": d, "v": v,
                          "line": "Private (ex H&E) (3mma)",
                          "colour": p.negative, "dash": [4, 3]})
    ldf = pd.DataFrame(line_rows)
    if not ldf.empty:
        ldf["date"] = pd.to_datetime(ldf["date"])

    xfmt, xticks = _gs_time_format(
        list(bdf["date"]) + list(ldf["date"]) if not (bdf.empty and ldf.empty)
        else [])

    bands = None
    rec = _clip_window(data.get("USRECDP_monthly", []), window_start)
    rdf = _gs_rec_bands(rec)
    if not rdf.empty:
        bands = (alt.Chart(rdf).mark_rect(opacity=0.12, color="#7f7f7f")
                 .encode(x="start:T", x2="end:T"))

    # Banded yearmonth x so each bar FILLS its month (Vega's default
    # continuousBandSize=5 would otherwise draw 5px bars with big gaps); the
    # 3mma lines and the recession rects share the same band positions.
    x_enc = alt.X("date:T", timeUnit="yearmonth",
                  axis=alt.Axis(title=None, format=xfmt, tickCount=xticks,
                                labelColor=p.muted, gridColor=p.grid))

    bars = None
    if not bdf.empty:
        bars = (
            alt.Chart(bdf).mark_bar(strokeWidth=0, binSpacing=0)
            .encode(
                x=x_enc,
                y=alt.Y("v:Q",
                        axis=alt.Axis(title="Monthly change (millions)",
                                      format=".1f", titleColor=p.muted,
                                      labelColor=p.muted, gridColor=p.grid),
                        scale=alt.Scale(nice=True)),
                color=alt.Color("colour:N", scale=None),
                order=alt.Order("order:Q"),
                tooltip=[
                    alt.Tooltip("date:T", title="Month", format="%b %Y"),
                    alt.Tooltip("component:N", title="Component"),
                    alt.Tooltip("v:Q", title="Change (millions)", format=".3f"),
                ],
            )
        )

    lines = None
    if not ldf.empty:
        lines = (
            alt.Chart(ldf).mark_line(strokeWidth=2.5, interpolate="monotone")
            .encode(
                # Own x encoding: same banded yearmonth scale as the bars,
                # but points centred in each band so the lines run through
                # the bars and end on the most recent one (see above).
                x=alt.X("date:T", timeUnit="yearmonth", bandPosition=0.5,
                        axis=alt.Axis(title=None, format=xfmt,
                                      tickCount=xticks, labelColor=p.muted,
                                      gridColor=p.grid)),
                y=alt.Y("v:Q",
                        axis=alt.Axis(title="Monthly change (millions)",
                                      format=".1f", titleColor=p.muted,
                                      labelColor=p.muted, gridColor=p.grid),
                        scale=alt.Scale(nice=True)),
                color=alt.Color("colour:N", scale=None),
                strokeDash=alt.StrokeDash("dash:N", scale=None),
                tooltip=[
                    alt.Tooltip("date:T", title="Month", format="%b %Y"),
                    alt.Tooltip("line:N", title="Series"),
                    alt.Tooltip("v:Q", title="3m avg (millions)", format=".3f"),
                ],
            )
        )

    return (
        alt.layer(*[b for b in (bands, bars, lines) if b is not None])
        .properties(height=height, width="container")
        .configure_view(stroke="transparent")
        .configure_axis(**{k: v for k, v in _base(p)["config"]["axis"].items()})
    )


# ---------------------------------------------------------------------------
# Sector wage-growth heatmap (rendered as an HTML table)
# ---------------------------------------------------------------------------
# The source chart is a TABLE -- columns [share, Component, Jul-26 ... Aug-25]
# (newest first), cells coloured by % y/y. Altair cannot reproduce a
# multi-column table cleanly, so this renders the same thing as an HTML
# table (the app's tables are HTML too), with the source's per-row colour
# scale: each row's own min maps to deep red and its own max to dark green,
# through orange and yellow -- conditional formatting per row, exactly like
# the source image.

# Per-row gradient stops (t = 0..1 within the row's own min..max range),
# sampled from the source image: deep red -> orange -> yellow -> green.
_HEAT_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (205, 82, 96)),
    (0.25, (220, 145, 115)),
    (0.50, (209, 195, 125)),
    (0.75, (168, 186, 114)),
    (0.90, (92, 142, 92)),
    (1.00, (34, 62, 43)),
)


def _hex_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _lerp_hex(a: str, b: str, t: float) -> str:
    ca, cb = _hex_rgb(a), _hex_rgb(b)
    t = min(1.0, max(0.0, t))
    return "#" + "".join(
        f"{round((x + (y - x) * t) * 255):02x}" for x, y in zip(ca, cb))


def _heat_colour(t: float) -> str:
    """t in [0,1] -> RGB hex through the source's red->green gradient."""
    for (t0, c0), (t1, c1) in zip(_HEAT_STOPS, _HEAT_STOPS[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0)
            return _lerp_hex("#%02x%02x%02x" % c0, "#%02x%02x%02x" % c1, k)
    return "#%02x%02x%02x" % _HEAT_STOPS[-1][1]


def wage_heatmap_table(
    data: dict[str, list[tuple[date, float]]],
    p: Palette,
) -> str:
    """Sector AHE heatmap as HTML, matching the source table's layout.

    Columns: [share | Component | Jul-26 | Jun-26 | ... | Aug-25], newest
    month first. Each row's cells are coloured against the row's OWN min-max
    (min = deep red, max = dark green), the source's conditional-formatting
    convention; the newest column is bold. Returns a fragment safe for
    `st.markdown(..., unsafe_allow_html=True)`.
    """
    months = payroll_wages.HEATMAP_MONTHS
    anchor = [d for d, _ in data.get("WAGE_TOTAL_YOY", [])]
    if not anchor:
        return ""

    # Newest month FIRST, back to 12 months ago -- the source's column order.
    cols = anchor[-months:][::-1]
    month_head = "".join(
        f"<th class='h'>{d.strftime('%b-%y')}</th>" for d in cols)

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    body: list[str] = []
    for row in payroll_wages.WAGE_ROWS:
        yoy = dict(data.get(f"WAGE_{row.key.upper()}_YOY", []))
        shares = dict(data.get(f"SHARE_{row.key.upper()}", []))
        share = shares.get(max(shares)) if shares else None
        vals = [yoy.get(d) for d in cols]
        live = [v for v in vals if v is not None]
        vmin, vmax = (min(live), max(live)) if live else (None, None)

        # Master/controlling rows (level 0-1: Total private, the two
        # aggregates) get their share + component cells shaded as one grey
        # band so the hierarchy reads at a glance; detail rows stay unshaded.
        master = row.level in (0, 1)
        cls = "agg" if master else ""
        share_td = (f"<td class='sh {cls}'>{share:.0f}%</td>"
                    if share is not None else "<td class='sh {cls}'>—</td>")
        indent = "&nbsp;&nbsp;" * row.level
        name_td = (f"<td class='nm {cls}'>{indent}{esc(row.label)}</td>")

        cells: list[str] = []
        for i, d in enumerate(cols):
            v = yoy.get(d)
            if v is None:
                cells.append("<td class='c na'>—</td>")
                continue
            if vmax == vmin:
                t = 0.5
            else:
                t = (v - vmin) / (vmax - vmin)
            fill = _heat_colour(t)
            ink = _readable_ink(fill)
            bold = " bold" if i == 0 else ""          # newest month bold
            tip = (f"{esc(row.label)} · {d.strftime('%b %y')}: {v:.2f}% y/y"
                   f" (share {share:.0f}% of NFP)" if share is not None
                   else f"{esc(row.label)} · {d.strftime('%b %y')}: {v:.2f}% y/y")
            cells.append(
                f"<td class='c{bold}' title='{tip}' "
                f"style='background:{fill};color:{ink}'>{v:.2f}</td>")
        body.append(
            f"<tr>{share_td}{name_td}{''.join(cells)}</tr>")

    # A compact gradient scale under the table so the colour language is
    # explicit, per the dataviz rule that a sequential encoding is never
    # colour-alone.
    stops = "".join(
        f"<span class='g' style='background:{_heat_colour(t)}'></span>"
        for t in (i / 16 for i in range(17)))
    legend = (
        f"<div class='fomc-heatmap-legend'>"
        f"<span class='gl'>low</span>{stops}<span class='gl'>high</span>"
        f"<span class='gt'>&nbsp;· colour scaled within each row "
        f"(row min → row max)</span></div>")

    return (
        f"<table class='fomc-heatmap'>"
        f"<thead><tr><th class='sh'>share</th><th class='nm'>Component</th>"
        f"{month_head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>{legend}")


# ---------------------------------------------------------------------------
# US CPI breakdowns (rendered as an HTML table)
# ---------------------------------------------------------------------------
# The source is a terminal monitor, not a chart: the CPI item tree indented
# down the left, its relative-importance weights, and the last six monthly
# prints as a diverging red/blue heat grid. Reproduced as an HTML table for
# the same reason the wage heatmap is -- a multi-column indented table is not
# something Altair or Plotly render cleanly, and the app's other tables are
# HTML already.

# Signed, diverging: red = prices rose, blue = prices fell. Not the app's
# blue-positive P&L pair -- this is the source's own colour language, where
# red means hot, and reading it against the P&L palette would be a mis-cue.
_CPI_HOT = "#a32a25"      # saturated +, sampled from the source's energy block
_CPI_COLD = "#4d86bf"     # saturated -, sampled from the source's motor-fuel dips

# Magnitude at which the colour saturates, in percentage points. A monthly CPI
# component print of ±3pp is already an extreme move, and capping there is what
# keeps an ordinary 0.3 vs 0.9 difference visible instead of washing the whole
# grid out against a 20pp motor-fuel swing. Fixed rather than data-derived, so
# a cell means the same thing from one release to the next.
_CPI_CAP = 3.0

# Sub-linear ramp, matched to the source's own saturation: without it every
# core-services cell (0.1-0.5pp against a 3pp cap) would sit in the first
# eighth of the scale and read as blank.
_CPI_GAMMA = 0.65


def _readable_ink(fill: str) -> str:
    """Black or white on `fill`, whichever has more contrast.

    Picked against the FILL rather than against the theme: these cells are
    painted with the source's own red and blue, so a mid-tone steel blue needs
    dark text in BOTH themes, and the saturated red needs light text in both.
    Deciding from `Palette.ink` instead would hand light theme near-black text
    on a dark red cell. Proper sRGB relative luminance, since the choice turns
    on a ~4.5:1 boundary the eyeballed version straddles.
    """
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _hex_rgb(fill))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / 0.0555) else "#111111"


def _cpi_heat(value: float, p: Palette) -> str:
    """Signed value -> cell fill, blended out of the theme's own surface.

    Blending from `p.surface` rather than from white keeps the light theme
    pixel-faithful to the source (whose ground IS white) while letting the
    dark theme darken the neutral end instead of stamping a white grid onto a
    dark page. Both themes keep the source's red and blue arms.
    """
    t = min(1.0, abs(value) / _CPI_CAP) ** _CPI_GAMMA
    return _lerp_hex(p.surface, _CPI_HOT if value >= 0 else _CPI_COLD, t)


def cpi_breakdown_table(table: "cpi_breakdown.Breakdown", p: Palette,
                        vintage: "Vintage | None" = None) -> str:
    """The CPI breakdown monitor as HTML, matching the source's layout.

    Columns: [item | Weights | 7/2026 | 6/2026 | ... ], newest month first,
    every row's cells on one shared diverging scale. Returns a fragment safe
    for `st.markdown(..., unsafe_allow_html=True)`; an empty table returns "".

    The vintage is stacked on two lines here rather than run together on one
    as it is under the chart titles -- that is how the source monitor heads
    this screen, and this table is a replica of it.
    """
    if table.empty:
        return ""

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    head = "".join(f"<th class='c'>{d.month}/{d.year}</th>" for d in table.months)

    body: list[str] = []
    for br in table.rows:
        cells: list[str] = []
        for i, d in enumerate(table.months):
            v = br.values[i]
            if v is None:
                cells.append("<td class='c na'>&mdash;</td>")
                continue
            fill = _cpi_heat(v, p)
            ink = _readable_ink(fill)
            basis = {cpi_breakdown.SA: "seasonally adjusted",
                     cpi_breakdown.NSA: "not seasonally adjusted (BLS "
                                        "publishes no SA series)",
                     cpi_breakdown.INFERRED: "inferred from the published "
                                             "parent"}[br.row.basis]
            tip = (f"{esc(br.row.label)} · {d:%b %Y}: {v:+.3f}% m/m · "
                   f"weight {br.row.weight:.1f} · {basis} · {br.row.item}")
            cells.append(f"<td class='c' title='{tip}' "
                         f"style='background:{fill};color:{ink}'>{v:.3f}</td>")
        body.append(
            f"<tr class='l{br.row.level}'>"
            f"<td class='nm'>{esc(br.label)}</td>"
            f"<td class='w'>{br.row.weight:.1f}</td>"
            f"{''.join(cells)}</tr>")

    meta = []
    if vintage and vintage.next_release:
        meta.append(f"Next release: {vintage.next_release:%d %b %Y}")
    if vintage and vintage.last_modified:
        meta.append(f"Last modified: {vintage.last_modified:%d %b %Y}")

    # The colour scale spelled out, per the dataviz rule that a magnitude
    # encoding never travels colour-alone.
    stops = "".join(
        f"<span class='g' style='background:{_cpi_heat(v, p)}'></span>"
        for v in (-_CPI_CAP + i * (2 * _CPI_CAP / 24) for i in range(25)))
    key = (f"<div class='fomc-heatmap-legend'>"
           f"<span class='gl'>&minus;{_CPI_CAP:.0f}pp</span>{stops}"
           f"<span class='gl'>+{_CPI_CAP:.0f}pp</span>"
           f"<span class='gt'>&nbsp;· m/m % change, one shared scale, "
           f"saturating beyond ±{_CPI_CAP:.0f}pp</span></div>")

    # The source heads the monitor in navy; lightened in dark theme so it stays
    # a heading rather than disappearing into the page.
    navy = "#93b7e4" if p.mode == "dark" else "#1f3b66"
    return (
        f"<div class='fomc-cpi-head' style='color:{navy}'>"
        f"US CPI Breakdowns: m/m % change</div>"
        + "".join(f"<div class='fomc-cpi-meta' style='color:{navy}'>{esc(m)}</div>"
                  for m in meta)
        + f"<div class='fomc-scroll'><table class='fomc-cpi'>"
          f"<thead><tr><th class='nm'></th><th class='w'>Weights</th>"
          f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        + key)


# ---------------------------------------------------------------------------
# Release-prep playbook charts
# ---------------------------------------------------------------------------

def playbook_levels(
    outcomes: list, entry: float, market_bp: float | None,
    fair: float, size: float, p: Palette,
    receive_level: float | None = None,
    height: int = 150,
) -> alt.LayerChart:
    """Horizontal bp axis (0–size) with pay, receive, market, fair and the
    three post-print scenario levels — one glance at the risk/reward layout.

    Reference markers (pay / receive / market / fair) sit on the bottom row,
    the scenario outcomes on the row above: a dovish outcome routinely lands
    almost exactly on the pay level, and base exactly on the market, so one
    row would pile the dots into a single blob. The receive marker only draws
    when the card prices a receive leg.
    """
    rows: list[dict] = []
    for label, level, kind in (
        ("Pay", entry, "entry"),
        ("Receive", receive_level, "receive"),
        ("Market", market_bp, "market"),
        ("Fair (q)", fair, "reference"),
    ):
        if level is None:
            continue
        rows.append({"label": label, "level": level, "kind": kind, "row": 0.0})
    for o in outcomes:
        if o.action == "no move set":
            continue
        rows.append({"label": o.name.title(), "level": o.new_points,
                     "kind": "scenario", "row": 0.62})
    df = pd.DataFrame(rows)

    colour_map = alt.Scale(
        domain=["entry", "receive", "market", "scenario", "reference"],
        range=[p.accent, p.warning, p.muted, p.negative, p.positive])
    base = alt.Chart(df).encode(
        alt.X("level:Q", scale=alt.Scale(domain=[0, size], nice=False),
              title="bp priced on the event", axis=alt.Axis(tickCount=6)),
        alt.Y("row:Q", scale=alt.Scale(domain=[0, 1], nice=False), axis=None),
        alt.Color("kind:N", scale=colour_map, legend=None),
    )
    dots = base.mark_circle(size=72).encode(
        alt.Tooltip(["label:N", "level:Q"]),
    )
    ref_text = base.mark_text(
        align="left", dx=6, dy=-9, fontSize=10,
    ).encode(alt.Text("label:N")).transform_filter("datum.row == 0")
    scen_text = base.mark_text(
        align="left", dx=6, dy=12, fontSize=10,
    ).encode(alt.Text("label:N")).transform_filter("datum.row > 0")
    chart = dots + ref_text + scen_text
    return _apply(chart, p, height)


def playbook_pnl(
    outcomes: list, p: Palette,
    height: int = 150,
) -> alt.LayerChart:
    """Horizontal bars — $ P&L of the planned position under each scenario,
    compact enough to sit beside the level chart."""
    rows = []
    for o in outcomes:
        if o.action == "no move set":
            continue
        rows.append({"label": o.name.title(),
                     "pnl": o.pnl_usd or 0.0,
                     "sign": "pos" if (o.pnl_usd or 0) >= 0 else "neg"})
    df = pd.DataFrame(rows)

    bar_colour = alt.Scale(
        domain=["pos", "neg"], range=[p.positive, p.negative])
    base = alt.Chart(df).encode(
        alt.X("pnl:Q", title="$ P&L at Kelly size",
              axis=alt.Axis(format="$.0s")),
        alt.Y("label:N", title=None, sort=None),
        alt.Tooltip(["label:N", "pnl:Q"]),
    )
    bars = base.mark_bar(cornerRadiusTopRight=3, cornerRadiusBottomRight=3).encode(
        alt.Color("sign:N", scale=bar_colour, legend=None),
    )
    # Value labels outside the bar, to the right.
    out_text = base.mark_text(align="left", dx=4, fontSize=11, fontWeight=500).encode(
        alt.Text("pnl:Q", format="$.0s"),
        alt.Color(value=p.ink),
    ).transform_filter("datum.pnl != 0")

    chart = (bars + out_text).configure_axisX(grid=False)
    return _apply(chart, p, height)
