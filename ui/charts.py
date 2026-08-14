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


def bbsw_basis(periods, p: Palette, height: int = 200) -> alt.LayerChart:
    """BBSW/OIS basis per bill contract: what IR prices, less the IB path.

    Only windows the IB strip actually covers are plotted. Bars rather
    than a line, because these are independent readings on separate contracts,
    not a continuous curve -- connecting them would imply a path between fix
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
            y=alt.Y("basis:Q", axis=alt.Axis(title="IR less IB path (bp)")),
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


# --------------------------------------------------------------------------
# --- The six specific chart renderers -------------------------------------
#
# Each takes its own `vintage`: the grid mixes two releases (see
# `core.gs_labour.CHART_RELEASES`), so one line across all six would date the
# JOLTS panels by the Employment Situation's calendar.

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


# ---------------------------------------------------------------------------
# Release-prep playbook charts
# ---------------------------------------------------------------------------


def full_employment(rows, total_z: float | None, total_prior_z: float | None,
                    prior_label: str, p: Palette,
                    height: int | None = None) -> alt.LayerChart:
    """Full-employment scorecard: each indicator's z-score at two dates.

    A dot per indicator per date, not a bar. These are LEVELS on a signed
    scale, and a bar's length from zero would invite reading the area as
    meaningful when only the position is -- the same reason `bbsw_basis` uses
    bars for independent readings and `priced_path` does not connect them.

    A rule joins each pair so the direction of travel reads at a glance, which
    is the whole point of plotting two dates: on the current Australian data
    every indicator sits on the tight side of its 2000-2020 average while
    almost every one has moved back toward it.

    The Total is drawn in the same units on its own row, separated by a rule.
    It is an unweighted mean, so it belongs on the same axis as its parts.

    EVERY indicator keeps its row, including the ones with no data yet. Altair
    only creates a category the data mentions, so an unscored indicator would
    silently vanish from the axis rather than showing as an empty row -- and a
    panel that quietly drops what it cannot measure is the wrong shape. The y
    scale domain is pinned to the full list, and the blanks carry a muted
    marker so an empty row reads as awaiting input rather than as a bug.
    """
    order = [r.indicator.label for r in rows] + ["Total"]
    blanks = [{"label": r.indicator.label, "z": 0.0} for r in rows if not r.ok]
    recs, links = [], []
    for r in rows:
        if not r.ok:
            continue
        recs.append({"label": r.indicator.label, "z": r.current.z,
                     "when": "Current", "value": r.current.value})
        if r.prior is not None:
            recs.append({"label": r.indicator.label, "z": r.prior.z,
                         "when": prior_label, "value": r.prior.value})
            links.append({"label": r.indicator.label,
                          "z": r.prior.z, "z2": r.current.z})
    if total_z is not None:
        recs.append({"label": "Total", "z": total_z, "when": "Current",
                     "value": total_z})
        if total_prior_z is not None:
            recs.append({"label": "Total", "z": total_prior_z,
                         "when": prior_label, "value": total_prior_z})
            links.append({"label": "Total", "z": total_prior_z, "z2": total_z})
    if not recs:
        return _apply(alt.layer(alt.Chart(pd.DataFrame({"z": []})).mark_point()),
                      p, height or 340)

    df = pd.DataFrame(recs)
    ldf = pd.DataFrame(links)
    bdf = pd.DataFrame(blanks)
    ysort = alt.SortField("order")
    df["order"] = df["label"].map({l: i for i, l in enumerate(order)})
    if not ldf.empty:
        ldf["order"] = ldf["label"].map({l: i for i, l in enumerate(order)})

    lo = min(-3.0, float(df["z"].min()) - 0.4)
    hi = max(4.0, float(df["z"].max()) + 0.4)
    xs = alt.Scale(domain=[lo, hi], nice=False)
    # Pinned so unscored indicators keep their row on the axis.
    ys = alt.Scale(domain=order)

    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
        color=p.ink, strokeWidth=1.2).encode(x=alt.X("x:Q", scale=xs))

    connectors = (
        alt.Chart(ldf).mark_rule(strokeWidth=1.4, opacity=0.45, color=p.muted)
        .encode(x=alt.X("z:Q", scale=xs), x2="z2:Q",
                y=alt.Y("label:N", sort=ysort, scale=ys,
                        axis=alt.Axis(title=None, labelLimit=260)))
        if not ldf.empty else
        alt.Chart(pd.DataFrame({"z": [], "z2": [], "label": []})).mark_rule()
        .encode(x=alt.X("z:Q", scale=xs)))

    dots = (
        alt.Chart(df).mark_circle(size=150, opacity=0.95)
        .encode(
            x=alt.X("z:Q", scale=xs,
                    axis=alt.Axis(title="z-score vs the 2000–2020 average",
                                  values=[-3, -2, -1, 0, 1, 2, 3, 4])),
            # labelLimit defaults to 180px, which truncates
            # "Vacancies-to-Unemployment" and "Medium-term Unemployment Rate"
            # to ellipses -- and an indicator you cannot read is not plotted.
            y=alt.Y("label:N", sort=ysort, scale=ys,
                    axis=alt.Axis(title=None, labelLimit=260)),
            color=alt.Color("when:N",
                            scale=alt.Scale(domain=[prior_label, "Current"],
                                            range=[p.muted, p.serious]),
                            legend=alt.Legend(title=None, orient="bottom")),
            tooltip=[alt.Tooltip("label:N", title="indicator"),
                     alt.Tooltip("when:N", title="date"),
                     alt.Tooltip("value:Q", format=".4f", title="reading"),
                     alt.Tooltip("z:Q", format="+.2f", title="z-score")],
        ))

    awaiting = (
        alt.Chart(bdf).mark_text(text="awaiting data", align="left", dx=6,
                                 fontSize=10, fontStyle="italic", color=p.muted)
        .encode(x=alt.X("z:Q", scale=xs),
                y=alt.Y("label:N", sort=ysort, scale=ys,
                        axis=alt.Axis(title=None, labelLimit=260)))
        if not bdf.empty else
        alt.Chart(pd.DataFrame({"z": [], "label": []})).mark_text()
        .encode(x=alt.X("z:Q", scale=xs)))

    n = len(order)
    return _apply(alt.layer(zero, connectors, dots, awaiting), p,
                  height or max(300, 30 * n))


# ---------------------------------------------------------------------------
# Inflation
# ---------------------------------------------------------------------------
# Five readings of one quarterly CPI. Colours are the eight validated
# categorical slots, taken in slot order per chart and never cycled, so a
# series keeps its hue when a sibling chart shows a different subset.

_EMPTY = pd.DataFrame({"when": pd.Series([], dtype="datetime64[ns]"),
                       "value": pd.Series([], dtype="float64"),
                       "series": pd.Series([], dtype="object")})


def _tidy(named: dict[str, list[tuple[date, float]]], order: list[str] | None = None
          ) -> pd.DataFrame:
    """`{name: [(date, value)]}` -> long form, in a stated series order."""
    rows = [{"when": pd.Timestamp(d), "value": float(v), "series": name}
            for name in (order or list(named))
            for d, v in named.get(name, [])
            if v == v]                      # drop NaN rather than plot a gap
    return pd.DataFrame(rows) if rows else _EMPTY.copy()


def _since(df: pd.DataFrame, start: date | None) -> pd.DataFrame:
    if start is None or df.empty:
        return df
    return df[df["when"] >= pd.Timestamp(start)]


def _lines(df: pd.DataFrame, order: list[str], p: Palette, y_title: str,
           y_format: str = ".1f", zero_rule: bool = False,
           y_domain: tuple[float, float] | None = None) -> list[alt.Chart]:
    """A multi-series line chart's layers: optional zero rule, then the lines.

    Legend always on for two or more series, per the accessibility rule that
    identity is never colour alone; the hover readout names the series too, so
    a reader who cannot separate two hues still gets the answer.
    """
    scale = alt.Scale(domain=order, range=list(p.categorical[:len(order)]))
    ys = alt.Scale(zero=False) if y_domain is None else alt.Scale(domain=list(y_domain))
    layers: list[alt.Chart] = []
    if zero_rule:
        layers.append(alt.Chart(pd.DataFrame({"y": [0.0]}))
                      .mark_rule(color=p.baseline, strokeWidth=1)
                      .encode(y=alt.Y("y:Q")))
    layers.append(
        alt.Chart(df).mark_line(strokeWidth=LINE_WIDTH, clip=True).encode(
            x=alt.X("when:T", axis=alt.Axis(title=None, format="%Y")),
            y=alt.Y("value:Q", scale=ys, axis=alt.Axis(title=y_title, format=y_format)),
            color=alt.Color("series:N", scale=scale, sort=order,
                            legend=alt.Legend(title=None, orient="bottom",
                                              columns=1, labelLimit=320)),
            tooltip=[alt.Tooltip("series:N", title=""),
                     alt.Tooltip("when:T", title="quarter", format="%b %Y"),
                     alt.Tooltip("value:Q", title="per cent", format=".2f")]))
    return layers


def cpi_above_threshold(points, p: Palette, threshold: float = 3.0,
                        start: date | None = None,
                        vintage: "Vintage | None" = None,
                        height: int = 300) -> alt.LayerChart:
    """Share of the CPI basket inflating faster than `threshold`, two ways.

    The two lines answer different questions and are meant to be read against
    each other: by count every expenditure class has equal say, so the line
    measures how WIDESPREAD a price rise is; by weight the classes carry their
    own share of the basket, so it measures how much of what people actually
    buy is rising. A gap between them says a few heavy classes are doing the
    work -- which is the case for treating a high headline as a relative-price
    story rather than a monetary one.
    """
    count, weight = "By number of items", "By weight of price categories"
    df = _since(_tidy({count: [(pt.when, pt.share_by_count) for pt in points],
                       weight: [(pt.when, pt.share_by_weight) for pt in points]},
                      [count, weight]), start)
    title = vintage_title(
        f"Share of CPI items with annualised inflation above {threshold:g}%",
        vintage, p)
    return _apply(alt.layer(*_lines(df, [count, weight], p, "% of basket"))
                  .properties(title=title), p, height)


def cpi_composition(buckets, cpi_line, order: list[str], p: Palette,
                    start: date | None = None, vintage: "Vintage | None" = None,
                    height: int = 300) -> alt.LayerChart:
    """Quarterly CPI change, split into the buckets that produced it.

    Stacked bars because the parts sum to the whole and the whole is the number
    the Board reacts to; the CPI line rides on top so the reader can see the
    stack reconcile to it. Bars carry a 1px surface gap between segments so
    adjacent fills stay separable where two hues are close.
    """
    df = _since(_tidy(buckets, order), start)
    line = _since(pd.DataFrame([{"when": pd.Timestamp(d), "value": float(v)}
                                for d, v in cpi_line]) if cpi_line
                  else _EMPTY.copy()[["when", "value"]], start)
    scale = alt.Scale(domain=order, range=list(p.categorical[:len(order)]))

    # A fixed bar width rather than Vega's automatic one. Forty-odd quarters in
    # a half-width column leaves about 8px each, and the surface stroke that
    # separates stacked segments then eats most of the bar -- at 1px it renders
    # the stack as a row of hairlines. 6px of fill with a half-pixel edge keeps
    # both the segment separation and the bar.
    bars = alt.Chart(df).mark_bar(size=6, stroke=p.surface, strokeWidth=0.5).encode(
        x=alt.X("when:T", axis=alt.Axis(title=None, format="%Y")),
        y=alt.Y("value:Q", stack="zero",
                axis=alt.Axis(title="percentage points", format=".1f")),
        # Legend to the RIGHT, not the bottom. Vega takes a bottom legend's
        # rows out of the same height budget as the plot, and with seven
        # buckets that left about 40px of plot -- the stack flattened to a line
        # and the y-axis dropped its labels. On the side it costs width, which
        # this chart has more of to give.
        color=alt.Color("series:N", scale=scale, sort=order,
                        legend=alt.Legend(title=None, orient="right",
                                          columns=1, labelLimit=150)),
        order=alt.Order("series:N", sort="ascending"),
        tooltip=[alt.Tooltip("series:N", title=""),
                 alt.Tooltip("when:T", title="quarter", format="%b %Y"),
                 alt.Tooltip("value:Q", title="pp", format="+.2f")])
    zero = (alt.Chart(pd.DataFrame({"y": [0.0]}))
            .mark_rule(color=p.baseline, strokeWidth=1).encode(y=alt.Y("y:Q")))
    cpi = alt.Chart(line).mark_line(strokeWidth=LINE_WIDTH, color=p.ink).encode(
        x=alt.X("when:T"), y=alt.Y("value:Q"),
        tooltip=[alt.Tooltip("when:T", title="quarter", format="%b %Y"),
                 alt.Tooltip("value:Q", title="CPI, % q/q", format="+.2f")])
    title = vintage_title("Contributions to CPI inflation — quarterly, "
                          "seasonally adjusted", vintage, p)
    return _apply(alt.layer(zero, bars, cpi).properties(title=title), p, height)


def cpi_underlying(series: dict[str, list[tuple[date, float]]], order: list[str],
                   p: Palette, start: date | None = None,
                   vintage: "Vintage | None" = None,
                   height: int = 300) -> alt.LayerChart:
    """Year-ended underlying inflation, quarterly and monthly measures together.

    The monthly series are short by construction -- the ABS only completed the
    monthly CPI in 2024 -- so they enter part-way across and stop rather than
    being back-filled from the quarterly ones, which measure a different trim.
    """
    df = _since(_tidy(series, order), start)
    band = (alt.Chart(pd.DataFrame({"lo": [2.0], "hi": [3.0]}))
            .mark_rect(color=p.grid, opacity=0.45)
            .encode(y=alt.Y("lo:Q"), y2=alt.Y2("hi:Q")))
    title = vintage_title("Underlying CPI inflation — year-ended", vintage, p)
    return _apply(alt.layer(band, *_lines(df, order, p, "%", zero_rule=True))
                  .properties(title=title), p, height)


def cpi_breadth(share, trimmed, share_mean: float | None, trimmed_mean: float | None,
                mean_label: str, p: Palette, start: date | None = None,
                vintage: "Vintage | None" = None,
                height: int = 150) -> alt.VConcatChart:
    """Breadth of the price rise beside the trimmed mean, on a shared time axis.

    The published version of this chart puts the two on a DUAL AXIS. That is
    the one construction worth refusing to copy: with two independent y-scales
    the crossings and the relative amplitudes are artefacts of where the
    scales were pinned, and sliding one axis changes which series appears to
    lead. Stacked panels sharing an x-axis answer the same question -- does
    breadth move with the trimmed mean, and is either above its own pre-COVID
    norm -- while every comparison stays real. Each panel keeps its own dotted
    reference line, which the dual-axis version could only draw twice.
    """
    def panel(rows, colour, y_title, mean, fmt):
        df = _since(pd.DataFrame([{"when": pd.Timestamp(d), "value": float(v)}
                                  for d, v in rows]) if rows
                    else _EMPTY.copy()[["when", "value"]], start)
        # The reference value rides in the AXIS TITLE rather than as a floating
        # label on the rule. A dashed line drawn at a series' own long-run mean
        # sits, by construction, in the middle of where that series spends its
        # time, so any label anchored to it lands on top of the data.
        axis_title = y_title if mean is None else \
            f"{y_title}  ·  mean {mean:{fmt}}"
        line = alt.Chart(df).mark_line(strokeWidth=1.5, color=colour).encode(
            x=alt.X("when:T", axis=alt.Axis(title=None, format="%Y")),
            y=alt.Y("value:Q", scale=alt.Scale(zero=False),
                    axis=alt.Axis(title=axis_title, format=fmt)),
            tooltip=[alt.Tooltip("when:T", title="quarter", format="%b %Y"),
                     alt.Tooltip("value:Q", title=y_title, format=".2f")])
        layers = [line]
        if mean is not None:
            layers.append(alt.Chart(pd.DataFrame({"y": [mean]})).mark_rule(
                color=p.muted, strokeDash=[4, 3], strokeWidth=1)
                .encode(y=alt.Y("y:Q")))
        return alt.layer(*layers).properties(height=height, width="container")

    top = panel(share, p.categorical[1], "% basket > 2.5% ann.", share_mean, ".0f")
    bottom = panel(trimmed, p.categorical[0], "trimmed mean % q/q",
                   trimmed_mean, ".2f")
    title = vintage_title(f"Breadth of CPI inflation — dashed lines are "
                          f"{mean_label} averages", vintage, p)
    return _apply(alt.vconcat(top, bottom, spacing=8).properties(title=title),
                  p, height)


def cpi_cycle(split, p: Palette, start: date | None = None,
              vintage: "Vintage | None" = None,
              height: int = 300) -> alt.LayerChart:
    """Year-ended inflation in the cycle-sensitive basket and its complement.

    The split is a judgement, not a measurement, and it is the argument the
    chart exists to have: if the cyclical line is the one running hot, domestic
    slack is the binding constraint and the cash rate is the instrument. If the
    non-cyclical line is doing the work -- an excise schedule, a regulated
    tariff, a world price -- then it is not, whatever the headline says.
    """
    cyc = f"'Cyclical' inflation  ({split.n_cyclical} classes)"
    non = f"'Non-cyclical' inflation  ({split.n_non_cyclical} classes)"
    df = _since(_tidy({cyc: split.cyclical, non: split.non_cyclical},
                      [cyc, non]), start)
    title = vintage_title("Domestic-cycle sensitive inflation — annual % change",
                          vintage, p)
    return _apply(alt.layer(*_lines(df, [cyc, non], p, "%", zero_rule=True))
                  .properties(title=title), p, height)
