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

import html
import math
from dataclasses import dataclass
from datetime import date, timedelta

import altair as alt
import pandas as pd
import plotly.graph_objects as go

from core import cpi_monthly as cpi_monthly_mod
from core import growth as growth_mod
from core import labor
from core import path as pathmod
from core import pricing, sizing
from core.pricing import Side

from .theme import Palette

LINE_WIDTH = 2
POINT_SIZE = 70
BAR_RADIUS = 4

# Navy line on the capacity-vs-unemployment chart: a moving average of the raw
# (grey) capacity prints, matching the labelled line on the bench chart
# (bench.jpg). Chosen to read clearly on both light and dark surfaces and to
# stay distinct from the grey raw-print line.
MA_BLUE = "#1f4e8c"


# Width the app renders a two-column panel into (the 1600px container split in
# half). Only used to size a legend's bottom margin, so an estimate is fine.
PANEL_WIDTH_PX = 780

# Legend metrics for the horizontal legend parked *under* the plot area.
# Anchoring it to the plot floor (paper y=0, yanchor="top") drew it straight
# over the x tick labels; it must hang off the figure floor instead
# (yref="container", yanchor="bottom"), and the bottom margin has to be tall
# enough for the rows it wraps to.
_LEGEND_ROW_PX = 19        # one wrapped row, incl. tracegroupgap
_LEGEND_SWATCH_PX = 12     # colour chip
_LEGEND_ITEM_GAP_PX = 13   # gap between entries
_LEGEND_CHAR_PX = 6.3      # conservative advance width at 11px
_LEGEND_PAD_PX = 12        # breathing room under the legend
_X_TICK_PX = 28            # x tick labels sitting under the axis
_LEGEND_SAFETY = 0.8       # estimate at 80% of the width -> ~1 row spare


def legend_rows(names: list[str], width_px: float) -> int:
    """Rows a horizontal legend wraps to when it is ``width_px`` wide."""
    if not names:
        return 0
    rows, used = 1, 0.0
    for n in names:
        w = (_LEGEND_SWATCH_PX + _LEGEND_ITEM_GAP_PX
             + _LEGEND_CHAR_PX * len(str(n)))
        if used and used + w > width_px:
            rows += 1
            used = w
        else:
            used += w
    return rows


def legend_bottom_margin(names: list[str], width_px: float) -> int:
    """Bottom margin (px) that clears both the x tick labels and the legend.

    Estimated at a fraction of the real width so a narrower browser column
    wraps into the spare row rather than colliding with the plot.
    """
    return int(_X_TICK_PX
               + legend_rows(names, width_px * _LEGEND_SAFETY) * _LEGEND_ROW_PX
               + _LEGEND_PAD_PX)


def bottom_legend(names: list[str], width_px: float = PANEL_WIDTH_PX
                  ) -> tuple[dict, int]:
    """``(legend config, bottom margin px)`` for a horizontal legend under a plot.

    Returned together on purpose: a legend parked below the plot area is only
    readable if the bottom margin is tall enough for the rows it wraps to, and
    splitting the two is what let ``inflation_trend`` anchor its legend to the
    *plot* floor (paper y=0) and draw it over the x tick labels. ``container``
    + ``yanchor="bottom"`` hangs it off the figure floor instead.
    """
    legend = dict(orientation="h", yref="container", y=0.0, x=0.0,
                  xanchor="left", yanchor="bottom", title=None,
                  itemsizing="constant", font=dict(size=11), tracegroupgap=4)
    return legend, legend_bottom_margin(names, width_px)


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
    """Hang a vintage line under a Plotly figure's title, in place."""
    if vintage is None or not vintage.line:
        return fig
    fig.update_layout(
        title=dict(subtitle=dict(text=vintage.line, font=subtitle_font(p))),
        margin=dict(t=top_margin),
    )
    return fig


def vintage_title(text: str, vintage: "Vintage | None", p: Palette,
                  **params) -> alt.TitleParams:
    """The Altair equivalent of :func:`apply_vintage`."""
    opts: dict = dict(fontSize=TITLE_SIZE, fontWeight=TITLE_WEIGHT, color=p.ink, dy=-5,
                      font=FONT, subtitleFont=FONT)
    opts.update(params)
    if vintage is not None and vintage.line:
        opts.update(subtitle=vintage.line, subtitleColor=p.ink_secondary,
                    subtitleFontSize=SUBTITLE_SIZE, subtitlePadding=4, limit=0)
    return alt.TitleParams(text, **opts)


#: The app's two typefaces (.streamlit/config.toml). Plotly charts pick the UI
#: face up from the Streamlit theme; Altair charts render with `theme=None`, so
#: they name it here or fall back to the browser's default sans.
FONT = "Archivo, 'Helvetica Neue', Arial, sans-serif"
FONT_NUM = "'JetBrains Mono', ui-monospace, monospace"

#: One type scale for every chart's own title, whichever library draws it.
#: The title matches the page's sub-heading (.fomc-h2: 0.86rem of a 15px base,
#: semibold), so a chart title never shouts over the section heading above it;
#: the subtitle -- a qualifier, a swatch legend, the vintage line -- sits one
#: step down in the secondary ink. Charts had drifted to five title sizes
#: (12, 13, 14, 15 and 17px); `tests/test_chart_type_scale.py` holds them here.
TITLE_SIZE = 14
TITLE_WEIGHT = 600
SUBTITLE_SIZE = 11


def title_font(p: Palette) -> dict:
    """A Plotly chart title's font."""
    return dict(size=TITLE_SIZE, color=p.ink, weight=TITLE_WEIGHT)


def subtitle_font(p: Palette) -> dict:
    """A Plotly chart subtitle's font."""
    return dict(size=SUBTITLE_SIZE, color=p.ink_secondary)


def _base(p: Palette) -> dict:
    return {
        "config": {
            "background": "transparent",
            "view": {"stroke": "transparent"},
            "axis": {
                "labelColor": p.muted, "titleColor": p.muted,
                "labelFont": FONT, "titleFont": FONT,
                "labelFontSize": 10, "titleFontSize": 10, "titleFontWeight": "normal",
                "gridColor": p.grid, "gridWidth": 1, "domainColor": p.baseline,
                "tickColor": p.baseline, "labelFlush": True,
            },
            "legend": {"labelColor": p.ink_secondary, "titleColor": p.muted,
                       "labelFont": FONT, "titleFont": FONT,
                       "labelFontSize": 10, "titleFontSize": 10},
            "text": {"color": p.ink_secondary, "fontSize": 10, "font": FONT},
        }
    }


def _apply(chart: alt.Chart, p: Palette, height: int) -> alt.Chart:
    cfg = _base(p)["config"]
    if isinstance(chart, (alt.VConcatChart, alt.HConcatChart, alt.ConcatChart)):
        return (
            chart
            .configure_view(**cfg["view"])
            .configure_axis(**cfg["axis"])
            .configure_legend(**cfg["legend"])
            .configure_text(**cfg["text"])
        )
    from altair.utils.schemapi import UndefinedType as _Undef
    existing = ({} if isinstance(chart.config, _Undef)
                else chart.config.to_dict())
    merged_view = {**existing.get("view", {}), **cfg["view"]}
    return (
        chart.properties(height=height, width="container")
        .configure_view(**merged_view)
        .configure_axis(**cfg["axis"])
        .configure_legend(**cfg["legend"])
        .configure_text(**cfg["text"])
    )


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
    # d3-format's `$` type emits a bare dollar sign with no way to prefix a
    # literal, so the money labels are formatted in pandas and drawn as text.
    # The axis and tooltips carry "A$" in their titles instead.
    rows = []
    for r in ladder.rungs:
        rows.append({"g": r.growth, "amount": r.max_loss, "kind": "Max loss A$",
                     "money": f"A${r.max_loss:,.0f}",
                     "rung": r.label, "selected": r.is_selected,
                     "ok": r.within_daily_limit, "contracts": r.contracts})
        rows.append({"g": r.growth, "amount": r.max_gain, "kind": "Max gain A$",
                     "money": f"A${r.max_gain:,.0f}",
                     "rung": r.label, "selected": r.is_selected,
                     "ok": r.within_daily_limit, "contracts": r.contracts})
    df = pd.DataFrame(rows)

    scale = alt.Scale(domain=["Max gain A$", "Max loss A$"],
                      range=[p.positive, p.negative])
    x_enc = alt.X("g:Q", axis=alt.Axis(format=".2%", title="expected growth per bet",
                                       tickCount=len(ladder.rungs)))
    y_enc = alt.Y("amount:Q", axis=alt.Axis(title="A$", format=",.0f"))
    tooltip = [alt.Tooltip("rung:N", title="Kelly"),
               alt.Tooltip("kind:N", title=""),
               alt.Tooltip("money:N", title="amount"),
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
        .encode(x=x_enc, y=y_enc, text=alt.Text("money:N"),
                color=alt.Color("kind:N", scale=scale, legend=None),
                opacity=alt.condition(alt.datum.selected, alt.value(1.0),
                                      alt.value(0.6)))
    )
    rung_names = (
        alt.Chart(df[df["kind"] == "Max loss A$"])
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


def instrument_history(history: list[tuple[date, float]],
                       curve: list[tuple[date, float]],
                       p: Palette, start: date | None = None,
                       vintage: "Vintage | None" = None,
                       title: str = "", height: int = 240) -> alt.LayerChart:
    """The rate a contract settles against, against the strip quoted today.

    The ASX feed carries no price history for either strip -- `days` is
    accepted but inert (see `data.asx`) -- so there is nothing to read a
    contract's own past PRICE off. What is available, keyless and daily, is
    the RBA's own published rate each contract settles ON: AONIA for IB,
    whose fix is the simple average of it across the delivery month, and
    3-month BBSW for IR, whose fix IS a single BBSW print. Both lines are
    therefore read directly off their own source rather than approximated
    from the other instrument. `curve` is the strip quoted today, one point
    per contract's own delivery month (which sits in the future relative to
    `history`), so the join is the actual cross-check: does the curve
    continue from where the rate has actually been printing, or has it
    already gapped away from it.

    `history` must be non-empty -- callers hold the chart back rather than
    invoke this on nothing, the same convention every other chart here uses.
    """
    hist_df = pd.DataFrame([{"when": pd.Timestamp(d), "value": float(v)}
                            for d, v in history])
    hist_df = _since(hist_df, start)
    curve_df = pd.DataFrame([{"when": pd.Timestamp(d), "value": float(v)}
                             for d, v in curve])
    axis_df = (pd.concat([hist_df[["when"]], curve_df[["when"]]], ignore_index=True)
              if len(curve_df) else hist_df)

    layers = [
        alt.Chart(hist_df).mark_line(strokeWidth=LINE_WIDTH, clip=True).encode(
            x=alt.X("when:T", axis=_time_axis(axis_df)),
            y=alt.Y("value:Q", axis=alt.Axis(title="%", format=".2f"),
                   scale=alt.Scale(zero=False)),
            color=alt.value(p.ink_secondary),
            tooltip=[alt.Tooltip("when:T", title="date"),
                     alt.Tooltip("value:Q", format=".3f", title="rate (%)")],
        )
    ]
    if len(curve_df):
        layers.append(
            alt.Chart(curve_df)
            .mark_line(strokeWidth=LINE_WIDTH, strokeDash=[4, 3], clip=True)
            .encode(x="when:T", y="value:Q", color=alt.value(p.accent))
        )
        layers.append(
            alt.Chart(curve_df).mark_point(
                filled=True, size=POINT_SIZE - 20, color=p.accent,
                stroke=p.surface, strokeWidth=1.5,
            ).encode(
                x="when:T", y="value:Q",
                tooltip=[alt.Tooltip("when:T", title="contract month"),
                         alt.Tooltip("value:Q", format=".3f", title="implied %")],
            )
        )

    chart = alt.layer(*layers)
    if title:
        chart = chart.properties(title=vintage_title(title, vintage, p))
    return _apply(chart, p, height)


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

    # With markers on, the axis is dated: `result.days` gives the real date
    # for each step, and the jump lands one slot past the last one.
    #
    # `row["day"]` is a column index into `SamplePath.levels`, where column 0
    # is the ENTRY level with no step taken yet -- it lands on `result.when`,
    # not on `result.days[0]` (the date of the FIRST step). `by_index` has to
    # carry `when` at position 0 for that reason, or every column reads one
    # trading day later than the level it actually holds.
    dated = (bool(release_days) and bool(getattr(result, "days", None))
            and getattr(result, "when", None) is not None)
    if dated:
        by_index = [result.when] + list(result.days) + [result.days[-1] + timedelta(days=1)]
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
            # `day_idx` indexes `result.days` directly (built from it in
            # app.py's `day_index` map), NOT the diffusion's `by_index` --
            # that array now carries `when` at position 0 and would read one
            # day late here if reused.
            if day_idx >= len(result.days):
                continue
            for n_ in names:
                rows.append({"at": result.days[day_idx], "release": n_,
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
            # No Vega legend: the caller renders the key with `components.legend`
            # beside the `?` that explains how to read the pair, which is where
            # the FOMC version puts it and keeps the two dashboards identical.
            color=alt.Color("when:N",
                            scale=alt.Scale(domain=[prior_label, "Current"],
                                            range=[p.muted, p.serious]),
                            legend=None),
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


def _time_axis(df: pd.DataFrame, cols: str = "when") -> alt.Axis:
    """X axis fitted to the span actually plotted.

    The history-window selector is only useful if the axis answers it: at 1Y a
    year-only axis prints "2026" four times, and at All a month-year axis is an
    unreadable smear. Short windows get month-year labels, longer history falls
    back to years -- the same rule and the same thresholds the FOMC version
    uses, so a reader switching tabs reads one axis convention.
    """
    if df.empty:
        return alt.Axis(title=None, format="%Y", tickCount=6)
    span = (df[cols].max() - df[cols].min()).days
    if span <= 3 * 366:
        return alt.Axis(title=None, format="%b %Y", tickCount=8, labelAngle=0)
    return alt.Axis(title=None, format="%Y", tickCount=6)


def _lines(df: pd.DataFrame, order: list[str], p: Palette, y_title: str,
           y_format: str = ".1f", zero_rule: bool = False,
           y_domain: tuple[float, float] | None = None,
           colors: dict[str, str] | None = None,
           dashes: dict[str, list[int]] | None = None) -> list[alt.Chart]:
    """A multi-series line chart's layers: optional zero rule, then the lines.

    No Vega legend on any of them. Every caller renders the key underneath with
    `components.legend`, which is what the FOMC version does and what keeps the
    two tabs identical -- and it also stops a legend from eating the plot's
    height, which a bottom-oriented Vega legend does. Identity is still never
    colour alone: the key is always present and the hover readout names the
    series.
    """
    if colors:
        # Caller-supplied per-series colours (e.g. the labour-flows panel),
        # falling back to the categorical cycle for any name not mapped.
        range_ = [colors.get(name, p.categorical[i % len(p.categorical)])
                  for i, name in enumerate(order)]
        scale = alt.Scale(domain=order, range=range_)
    else:
        scale = alt.Scale(domain=order, range=list(p.categorical[:len(order)]))
    # Per-series stroke dash (solid [1, 0] unless a caller overrides). Used by
    # the labour-flows panel to render the flow measures dotted against the
    # solid WPI line.
    dash_range = [(dashes or {}).get(name, [1, 0]) for name in order]
    dash = alt.StrokeDash("series:N", scale=alt.Scale(domain=order, range=dash_range))
    ys = alt.Scale(zero=False) if y_domain is None else alt.Scale(domain=list(y_domain))
    layers: list[alt.Chart] = []
    if zero_rule:
        layers.append(alt.Chart(pd.DataFrame({"y": [0.0]}))
                      .mark_rule(color=p.baseline, strokeWidth=1)
                      .encode(y=alt.Y("y:Q")))
    layers.append(
        alt.Chart(df).mark_line(strokeWidth=LINE_WIDTH, clip=True).encode(
            x=alt.X("when:T", axis=_time_axis(df)),
            y=alt.Y("value:Q", scale=ys, axis=alt.Axis(title=y_title, format=y_format)),
            color=alt.Color("series:N", scale=scale, sort=order, legend=None),
            strokeDash=dash,
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
    # Clip the CPI line to the span the STACK covers. The published headline
    # reaches back to 1987 and the decomposition only to 2011, when the last
    # expenditure class enters, so an unclipped line runs forty quarters past
    # its own explanation -- which reads as the buckets failing to account for
    # it rather than as data that does not exist yet.
    if not df.empty and not line.empty:
        line = line[(line["when"] >= df["when"].min())
                    & (line["when"] <= df["when"].max())]
    scale = alt.Scale(domain=order, range=list(p.categorical[:len(order)]))

    # Bar width set here rather than left to Vega, and scaled to how many
    # quarters the history window actually put on screen. Vega's automatic
    # width plus the surface stroke that separates stacked segments renders
    # forty quarters as a row of hairlines; a fixed width instead leaves four
    # quarters looking like stray ticks. Roughly three-quarters of each
    # quarter's slice of an assumed half-width column, clamped either side.
    n_bars = df["when"].nunique() or 1
    bar_w = max(3.0, min(30.0, 380.0 / n_bars * 0.75))
    bars = alt.Chart(df).mark_bar(size=bar_w, stroke=p.surface,
                                  strokeWidth=0.5).encode(
        x=alt.X("when:T", axis=_time_axis(df)),
        y=alt.Y("value:Q", stack="zero",
                axis=alt.Axis(title="percentage points", format=".1f")),
        # No Vega legend: seven buckets on the right cost the plot its width and
        # on the bottom cost it its height -- at 210px the stack flattened to a
        # line and the y-axis dropped its labels. The caller draws the key
        # underneath instead, which is free.
        color=alt.Color("series:N", scale=scale, sort=order, legend=None),
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
            x=alt.X("when:T", axis=_time_axis(df)),
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


# ---------------------------------------------------------------------------
# Labour market flows and capacity
# ---------------------------------------------------------------------------

def capacity_vs_unemployment(capacity, unemployment, p: Palette,
                             start: date | None = None,
                             vintage: "Vintage | None" = None,
                             height: int = 300,
                             capacity_ma=None) -> alt.LayerChart:
    """Capacity utilisation against the unemployment rate, utilisation inverted.

    Drawn to match the CIO Analytics bench chart (bench.jpg) exactly:

    * grey line   -- raw NAB monthly capacity prints (thin, de-emphasised)
    * navy line   -- 3-month trailing moving average of those prints (the
                     bench's labelled "NAB Capacity Utilization Rate, inverted")
    * red line    -- ABS unemployment rate, seasonally adjusted, untransformed
                     (verified against the bench: it plots the raw print)
    * left axis   -- fixed 70.0-87.5, inverted, 2.5pt ticks, "Percent"
    * right axis  -- fixed 3-9, 1pt ticks, "Percent"; the gridlines ride on
                     this axis, as they do on the bench
    * x starts at the capacity series' first month (Jan 2000 on the bench),
                     so unemployment is clipped to the same window

    Both axes extend past the bench range only if the data ever breaches it,
    so a record print is never clipped. Two y-scales is something this app
    otherwise refuses -- the Inflation tab's breadth chart is drawn as
    stacked panels for exactly that reason -- but here the two ARE the same
    quantity read two ways: firms running out of spare capacity and workers
    finding jobs, in percent, on levels a century apart. Inverting
    utilisation is what makes them comparable at all, and pinning it to a
    second axis is how the source does it.
    """
    cdf = _since(pd.DataFrame([{"when": pd.Timestamp(d), "value": float(v)}
                               for d, v in capacity]) if capacity
                 else _EMPTY.copy()[["when", "value"]], start)
    udf = _since(pd.DataFrame([{"when": pd.Timestamp(d), "urate": float(v)}
                               for d, v in unemployment]) if unemployment
                 else pd.DataFrame({"when": pd.to_datetime([]),
                                    "urate": []}), start)
    if not cdf.empty and not udf.empty:
        # The bench starts both lines at the capacity series' first month;
        # the ABS unemployment feed reaches back to 1978, so clip it.
        udf = udf[udf["when"] >= cdf["when"].min()]

    # Bench-pinned axes that only budge when a print falls outside them.
    # Ranges come from the data actually plotted (post history-window filter).
    mdf = (_since(pd.DataFrame([{"when": pd.Timestamp(d), "value": float(v)}
                                for d, v in capacity_ma]), start)
           if capacity_ma else None)
    left_vals = list(cdf["value"]) + (list(mdf["value"]) if mdf is not None else [])
    lo = math.floor(min([70.0] + left_vals) / 2.5) * 2.5
    hi = math.ceil(max([87.5] + left_vals) / 2.5) * 2.5
    left_ticks = [round(lo + 2.5 * i, 2) for i in range(int(round((hi - lo) / 2.5)) + 1)]
    right_vals = list(udf["urate"])
    rlo = math.floor(min([3.0] + right_vals))
    rhi = math.ceil(max([9.0] + right_vals))
    right_ticks = list(range(rlo, rhi + 1))

    # X axis: at full history the bench ticks every two years, 2000...2026.
    axis = _time_axis(udf if cdf.empty else cdf)
    src_df = udf if cdf.empty else cdf
    if not src_df.empty and (src_df["when"].max() - src_df["when"].min()).days > 8 * 366:
        first = src_df["when"].min().year
        first += first % 2
        last = src_df["when"].max().year + 1
        axis = alt.Axis(title=None, format="%Y", labelAngle=0, grid=False,
                        values=[pd.Timestamp(y, 1, 1).isoformat()
                                for y in range(first, last + 1, 2)])
    else:
        axis.grid = False

    left_scale = alt.Scale(domain=[lo, hi], reverse=True, zero=False, nice=False)
    left_axis = alt.Axis(title="Percent", values=left_ticks, format=".1f",
                         grid=False, titleAngle=0, titleAlign="left",
                         titleBaseline="bottom", titleY=-12)
    right_axis = alt.Axis(title="Percent", values=right_ticks, format="d",
                          orient="right", grid=True, titleAngle=0,
                          titleAlign="right", titleBaseline="bottom", titleY=-12)

    cap = alt.Chart(cdf).mark_line(strokeWidth=1.5,
                                   color=p.muted).encode(
        x=alt.X("when:T", axis=axis),
        y=alt.Y("value:Q", scale=left_scale, axis=left_axis),
        tooltip=[alt.Tooltip("when:T", title="month", format="%b %Y"),
                 alt.Tooltip("value:Q", title="capacity utilisation, %",
                             format=".1f")])

    # The bench's labelled navy line: a moving average of the raw (grey)
    # prints, computed in-app so it extends as each monthly print lands.
    # Axis-less: its scale is identical to the raw line's, and a second
    # axis object would overplot the left axis under resolve "independent".
    if mdf is not None:
        ma = alt.Chart(mdf).mark_line(strokeWidth=2.5,
                                      color=MA_BLUE, opacity=0.9).encode(
            x=alt.X("when:T", axis=axis),
            y=alt.Y("value:Q", scale=left_scale,
                    axis=None if not cdf.empty else left_axis),
            tooltip=[alt.Tooltip("when:T", title="month (MA)", format="%b %Y"),
                     alt.Tooltip("value:Q", title="capacity MA, %",
                                 format=".1f")])

    # A field name of its own ("urate") so the independent scale resolution
    # leaves the two capacity layers sharing one left axis.
    une = alt.Chart(udf).mark_line(strokeWidth=LINE_WIDTH,
                                   color=p.categorical[7]).encode(
        x=alt.X("when:T", axis=axis),
        y=alt.Y("urate:Q",
                scale=alt.Scale(domain=[rlo, rhi], zero=False, nice=False),
                axis=right_axis),
        tooltip=[alt.Tooltip("when:T", title="month", format="%b %Y"),
                 alt.Tooltip("urate:Q", title="unemployment rate, %",
                             format=".1f")])

    layers = [cap, une] if not cdf.empty else [une]
    if mdf is not None and not cdf.empty:
        layers = [cap, ma, une]
    elif mdf is not None:
        layers = [ma, une]
    chart = alt.layer(*layers)
    if not cdf.empty:
        chart = chart.resolve_scale(y="independent")
    title = vintage_title("Capacity utilisation against unemployment", vintage, p)
    return _apply(chart.properties(title=title), p, height)


def labour_flows(series: dict[str, list[tuple[date, float]]], order: list[str],
                 p: Palette, start: date | None = None,
                 vintage: "Vintage | None" = None,
                 height: int = 300,
                 colors: dict[str, str] | None = None,
                 dashes: dict[str, list[int]] | None = None) -> alt.LayerChart:
    """Job-finding, job-switching and wage growth, each as its own z-score.

    Z-scores because the three are in incompatible units -- a transition rate,
    a tenure share and a quarterly wage change -- and the question asked of
    them is where each sits against its own history, not how they compare in
    level. It is the same arithmetic the indicator panel above scores a single
    reading with, run across the whole series so it can be plotted.

    `colors`, when given, fixes each line's colour by series name (the flows
    panel uses it to keep WPI dark blue, job-finding grey, job-switching light
    blue) rather than letting them cycle the categorical palette. `dashes`
    does the same for line style -- the flows panel dots the two flow measures
    against the solid WPI line.
    """
    df = _since(_tidy(series, order), start)
    return _apply(
        alt.layer(*_lines(df, order, p, "z-score vs the series' own history",
                          y_format=".1f", zero_rule=True, colors=colors,
                          dashes=dashes))
        .properties(title=vintage_title(
            "Job-finding and job-switching against wage growth", vintage, p)),
        p, height)


# ---------------------------------------------------------------------------
# Macro dashboard  (Westpac CIO "Australian Macroeconomic Indicators")
# ---------------------------------------------------------------------------
# One fixed colour per group, reused by the dot table and the momentum bars so
# a group reads the same in both. Blue->red is the app's diverging pair; the
# other three are desaturated neutrals so the eye groups by row block, not hue.
MACRO_GROUP_ORDER = ("Labour Market", "Inflation", "Housing", "Consumer",
                     "Cyclical Economic Indicators")


def _macro_group_colours(p: Palette) -> dict[str, str]:
    return {
        "Labour Market": p.positive,
        "Inflation": p.negative,
        "Housing": p.accent,
        "Consumer": p.warning,
        "Cyclical Economic Indicators": p.ink_secondary,
    }


def _hex_rgb(h: str) -> tuple[float, float, float]:
    """`#rrggbb` -> (r, g, b) in 0..1. Used by app.py's heat-shaded Z cell."""
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _macro_time_format(dates: list[date]) -> tuple[str, int]:
    if not dates:
        return "%Y", 6
    return ("%b %Y", 8) if (max(dates) - min(dates)).days <= 3 * 366 else ("%Y", 6)


def macro_dots(readings, p: Palette, height: int = 560) -> alt.LayerChart:
    """The 'Australian Macroeconomic Indicators' dot plot: every indicator as a
    z-score of its own 2000-2020 momentum distribution, on one axis.

    A dumbbell per row where a live series carries history: the faint dot is the
    score three and six months ago, the filled dot is now, the rule between is
    the travel. A seed row has only the filled dot. Zero is the only reference
    the axis has -- a z of 0 is average momentum for that series. Colour is the
    sign of the current score (blue = stronger than average, red = weaker);
    rows run top-to-bottom in the slide's order.
    """
    rows, seg, order = [], [], []
    for r in readings:
        if not r.ok:
            continue
        lbl = r.indicator.label
        order.append(lbl)
        rows.append({
            "label": lbl, "group": r.indicator.group, "z": r.z,
            "sign": "up" if r.z >= 0 else "down",
            "last": r.last, "mean": r.hist_mean,
            "as_of": r.as_of.isoformat() if r.as_of else "",
            "kind": "live" if r.live else "seed",
        })
        z3, z6 = r.z_prev
        if z3 is not None:
            seg.append({"label": lbl, "z": z6 if z6 is not None else z3,
                        "z2": r.z, "sign": rows[-1]["sign"]})
    if not rows:
        return _apply(alt.layer(alt.Chart(pd.DataFrame({"x": []})).mark_point()),
                      p, height)
    df, sdf = pd.DataFrame(rows), pd.DataFrame(seg)

    dom = max(2.5, min(4.5, float(df["z"].abs().max()) + 0.3))
    x_scale = alt.Scale(domain=[-dom, dom])
    sign_scale = alt.Scale(domain=["up", "down"], range=[p.positive, p.negative])
    y_enc = alt.Y("label:N", sort=order, axis=alt.Axis(
        title=None, labelFontSize=10, labelLimit=240, grid=True, ticks=False,
        domain=False))
    x_enc = alt.X("z:Q", scale=x_scale, axis=alt.Axis(
        title="z-score of 2000\u20132020 momentum   \u2014   weaker \u2190 \u2192 stronger",
        values=[-3, -2, -1, 0, 1, 2, 3]))

    layers = [alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(x="x:Q")]
    if not sdf.empty:
        layers.append(alt.Chart(sdf).mark_rule(strokeWidth=2, opacity=0.4).encode(
            y=y_enc, x=alt.X("z:Q", scale=x_scale, title=None), x2="z2:Q",
            color=alt.Color("sign:N", scale=sign_scale, legend=None)))
        layers.append(alt.Chart(sdf).mark_point(
            filled=True, size=40, color=p.surface, strokeWidth=1.4).encode(
            y=y_enc, x=alt.X("z:Q", scale=x_scale, title=None),
            stroke=alt.Color("sign:N", scale=sign_scale, legend=None)))
    layers.append(alt.Chart(df).mark_point(
        filled=True, size=95, stroke=p.surface, strokeWidth=1.4).encode(
        y=y_enc, x=x_enc,
        color=alt.Color("sign:N", scale=sign_scale, legend=None),
        tooltip=[alt.Tooltip("label:N", title="indicator"),
                 alt.Tooltip("group:N", title="group"),
                 alt.Tooltip("z:Q", format="+.2f", title="z-score"),
                 alt.Tooltip("last:Q", format=".2f", title="latest transform"),
                 alt.Tooltip("mean:Q", format=".2f", title="2000\u20132020 mean"),
                 alt.Tooltip("as_of:N", title="as of"),
                 alt.Tooltip("kind:N", title="source")]))
    return _apply(alt.layer(*layers), p, height)


def macro_momentum(history, p: Palette, height: int = 340,
                   window_start: "date | None" = None) -> alt.LayerChart:
    """The 'Momentum Signal': each group's contribution to the Total as a
    stacked bar per quarter, with the Total itself as a line on top.

    The bars sum to the Total by construction -- each group contributes its
    members' mean z times its share of the reporting count -- so the line is
    never anything the stack does not already show; it is there so the aggregate
    reads at a glance without adding the segments by eye. Built only from
    indicators with a wired history, so a group with no free feed (Housing,
    currently) sits the chart out.
    """
    gcol = _macro_group_colours(p)
    rows, tot = [], []
    for pt in history:
        if window_start is not None and pt.month < window_start:
            continue
        for g, v in pt.contributions.items():
            rows.append({"month": pt.month, "group": g, "contrib": v})
        tot.append({"month": pt.month, "total": pt.total})
    if not rows:
        return _apply(alt.layer(alt.Chart(pd.DataFrame({"x": []})).mark_bar()),
                      p, height)
    df, tdf = pd.DataFrame(rows), pd.DataFrame(tot)

    xfmt, xticks = _macro_time_format(list(tdf["month"]))
    x_enc = alt.X("month:T", axis=alt.Axis(title=None, format=xfmt, tickCount=xticks))
    present = [g for g in MACRO_GROUP_ORDER if g in set(df["group"])]
    grp_scale = alt.Scale(domain=present, range=[gcol[g] for g in present])

    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color=p.baseline, strokeWidth=1).encode(y="y:Q")
    bars = alt.Chart(df).mark_bar().encode(
        x=x_enc,
        y=alt.Y("contrib:Q", stack="zero",
                axis=alt.Axis(title="group contribution to Total z", format="+.1f")),
        color=alt.Color("group:N", scale=grp_scale,
                        legend=alt.Legend(title=None, orient="bottom",
                                          direction="horizontal", labelFontSize=10)),
        order=alt.Order("group:N"),
        tooltip=[alt.Tooltip("month:T", title="quarter", format="%b %Y"),
                 alt.Tooltip("group:N", title="group"),
                 alt.Tooltip("contrib:Q", format="+.2f", title="contribution")])
    line = alt.Chart(tdf).mark_line(strokeWidth=2, color=p.ink,
                                    interpolate="monotone").encode(
        x=x_enc, y="total:Q",
        tooltip=[alt.Tooltip("month:T", title="quarter", format="%b %Y"),
                 alt.Tooltip("total:Q", format="+.2f", title="Total z")])
    return _apply(alt.layer(zero, bars, line), p, height)


# ---------------------------------------------------------------------------
# Release playbook
# ---------------------------------------------------------------------------

def playbook_levels(
    outcomes: list, entry: float, market_bp: float | None,
    fair: float, size: float, p: Palette,
    receive_level: float | None = None,
    height: int = 150,
) -> alt.LayerChart:
    """Horizontal bp axis (0–size) with pay, receive, market, fair and the
    three post-print scenario levels — one glance at the risk/reward layout."""
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
        pnl = o.pnl_usd or 0.0
        rows.append({"label": o.name.title(), "pnl": pnl,
                     "money": f"{'-' if pnl < 0 else '+'}A${abs(pnl):,.0f}",
                     "sign": "pos" if pnl >= 0 else "neg"})
    df = pd.DataFrame(rows)

    bar_colour = alt.Scale(
        domain=["pos", "neg"], range=[p.positive, p.negative])
    base = alt.Chart(df).encode(
            # d3's `$` emits a bare dollar sign, so the axis is titled in A$ and
        # the labels are formatted in pandas.
        alt.X("pnl:Q", title="A$ P&L at Kelly size",
              axis=alt.Axis(format="~s", tickCount=5)),
        alt.Y("label:N", title=None, sort=None),
        tooltip=[alt.Tooltip("label:N"), alt.Tooltip("money:N", title="P&L")],
    )
    bars = base.mark_bar(cornerRadiusTopRight=3, cornerRadiusBottomRight=3).encode(
        alt.Color("sign:N", scale=bar_colour, legend=None),
    )
    out_text = base.mark_text(align="left", dx=4, fontSize=11, fontWeight=500).encode(
        alt.Text("money:N"),
        alt.Color(value=p.ink),
    ).transform_filter("datum.pnl != 0")

    chart = (bars + out_text).configure_axisX(grid=False)
    return _apply(chart, p, height)


# ---------------------------------------------------------------------------
# Shared Plotly helpers (consumer, drivers, heat scales) -- from the US app
# ---------------------------------------------------------------------------

_CONSUMER_COLOURS = {
    "light": {"bar": "#bcc3d3", "3m": "#4a5670", "12m": "#9c2743",
              "spending": "#b3bbd0", "income": "#7d1d36", "ex_transfers": "#c0395a",
              "bubble": "#6b7486", "bubble_fill": "rgba(107,116,134,0.07)",
              "star": "#b3223f"},
    "dark": {"bar": "#4c5670", "3m": "#93a4c6", "12m": "#d4415f",
             "spending": "#4a5572", "income": "#e0708a", "ex_transfers": "#f0a3b5",
             "bubble": "#9aa3b7", "bubble_fill": "rgba(154,163,183,0.08)",
             "star": "#e05a75"},
}

#: Title, subtitle and footer geometry shared by the consumer charts, so two of
#: them side by side put their plot areas at the same height.
_CONSUMER_TOP = 78
_CONSUMER_READOUT_PX = 164       # right-hand "latest" block
_BARE_RIGHT_PX = 16              # right margin with no block
_CONSUMER_FOOTER_LINE_PX = 14


def _consumer_colours(p: Palette) -> dict[str, str]:
    return _CONSUMER_COLOURS["dark" if p.mode == "dark" else "light"]


def _monthly_ticks(span_days: int) -> tuple[str, str]:
    """(tick format, dtick) for a monthly chart: Jul-23 / Jan-24 up to four
    years, as in the published charts, then every year, then every other."""
    if span_days <= 400:
        return "%b-%y", "M2"
    if span_days <= 1600:
        return "%b-%y", "M6"
    if span_days <= 2600:
        return "%Y", "M12"
    return "%Y", "M24"


def _swatch_title(title: str, parts: list[tuple[str, str]], sep: str,
                  p: Palette, lead: str = "") -> dict:
    """A title whose subtitle IS the legend: each series named in its colour.

    The published charts carry no legend box at all, and a legend parked under
    the plot is the thing that ends up on the x tick labels.
    """
    names = sep.join(f'<span style="color:{c}"><b>{html.escape(t)}</b></span>'
                     for t, c in parts)
    return dict(text=title, x=0.0, xanchor="left", font=title_font(p),
                subtitle=dict(text=f"{lead}{names}", font=subtitle_font(p)))


def _footer(fig: go.Figure, lines: list[str], p: Palette, below: int = 0) -> int:
    """Source and vintage lines under the x axis; returns the bottom margin.

    `below` is anything else hung under the tick labels first -- an axis title.
    """
    lines = [ln for ln in lines if ln]
    for i, text in enumerate(lines):
        fig.add_annotation(xref="paper", yref="paper", x=0.0, y=0.0,
                           xanchor="left", yanchor="top", showarrow=False,
                           yshift=-(_X_TICK_PX + 4 + below + i * _CONSUMER_FOOTER_LINE_PX),
                           text=html.escape(text), align="left",
                           font=dict(size=10, color=p.muted))
    return _X_TICK_PX + 8 + below + len(lines) * _CONSUMER_FOOTER_LINE_PX


def _readout(fig: go.Figure, heading: str, rows: list[tuple[str, str, str]],
             p: Palette) -> None:
    """The latest print, parked right of the plot: heading, then label/value."""
    fig.add_annotation(xref="paper", yref="paper", x=1.0, y=0.86,
                       xanchor="left", yanchor="bottom", xshift=14,
                       showarrow=False, text=f"<u>{html.escape(heading)}</u>",
                       font=dict(size=13, color=p.ink))
    for i, (label, value, colour) in enumerate(rows):
        y = 0.86 - 0.075 * (i + 1)
        common = dict(xref="paper", yref="paper", x=1.0, y=y, yanchor="bottom",
                      showarrow=False, font=dict(size=12, color=colour))
        fig.add_annotation(xanchor="left", xshift=14, text=html.escape(label), **common)
        fig.add_annotation(xanchor="right", xshift=_CONSUMER_READOUT_PX - 6,
                           text=f"<b>{html.escape(value)}</b>", **common)


def _consumer_empty(fig: go.Figure, text: str, p: Palette, height: int) -> go.Figure:
    fig.add_annotation(text=text, showarrow=False, font=dict(color=p.ink))
    fig.update_layout(height=height, paper_bgcolor=p.surface, plot_bgcolor=p.plane)
    return fig


def _trim_lines(lines, window_start: date | None) -> tuple[dict, list]:
    """Each line's observations inside the window, and the lines left with any.

    Shared by every titled-legend time-series chart, so the window is applied
    the same way in all of them."""
    trimmed = {ln.key: [o for o in ln.observations
                        if window_start is None or o.date >= window_start]
               for ln in lines}
    return trimmed, [ln for ln in lines if trimmed[ln.key]]


def _monthly_frame(fig: go.Figure, p: Palette, height: int, dates: list[date],
                   title: dict, bottom: int, tickformat: str,
                   right: int = _CONSUMER_READOUT_PX, top: int = _CONSUMER_TOP,
                   zero_line: bool = True, hoverformat: str = "%b %Y",
                   pad_days: int = 20) -> None:
    """Axes and layout shared by the titled-legend time-series charts."""
    min_date, max_date = min(dates), max(dates)
    pad = timedelta(days=pad_days)
    if zero_line:
        # A shape counts toward autorange, so it is drawn only where zero
        # belongs on the axis anyway.
        fig.add_shape(type="line", xref="paper", yref="y", x0=0, x1=1, y0=0, y1=0,
                      line=dict(color=p.ink_secondary, width=1.2), layer="above")
    fig.update_layout(
        height=height, title=title,
        paper_bgcolor=p.surface, plot_bgcolor=p.plane,
        font=dict(color=p.ink, size=12),
        margin=dict(l=52, r=right, t=top, b=bottom),
        showlegend=False, hovermode="x unified", bargap=0.35,
    )
    fig.update_yaxes(title=None, gridcolor=p.grid, zeroline=False,
                     ticksuffix="%", tickformat=tickformat)
    xfmt, xdtick = _monthly_ticks((max_date - min_date).days)
    fig.update_xaxes(type="date", tickformat=xfmt, dtick=xdtick,
                     hoverformat=hoverformat, showgrid=False,
                     range=[min_date - pad, max_date + pad])


_DRIVER_COLOURS = {
    "light": {"point_3y": "#b9c0d0", "density_3y": "#4a5670", "umich_5_10": "#9c2743",
              "benefits": "#b9c0d0", "wages": "#4a5670", "total": "#9c2743"},
    "dark": {"point_3y": "#6b7590", "density_3y": "#93a4c6", "umich_5_10": "#d4415f",
             "benefits": "#6b7590", "wages": "#93a4c6", "total": "#d4415f"},
}
#: The pale slate is right for a line behind the others but too faint as type;
#: its name in the subtitle takes this tone instead.
_DRIVER_PALE_INK = {"light": "#98a1b6", "dark": "#8d97b0"}

#: Two subtitle lines in each of the pair, so their plot areas start level.
_DRIVER_TOP = 94
_DRIVER_RIGHT = 20


def _driver_colours(p: Palette) -> tuple[dict[str, str], str]:
    mode = "dark" if p.mode == "dark" else "light"
    return _DRIVER_COLOURS[mode], _DRIVER_PALE_INK[mode]


def _named(text: str, colour: str) -> str:
    return f'<span style="color:{colour}"><b>{html.escape(text)}</b></span>'


def _driver_title(title: str, subtitle: str, p: Palette) -> dict:
    return dict(text=title, x=0.0, xanchor="left", font=title_font(p),
                subtitle=dict(text=subtitle, font=subtitle_font(p)))


_HEAT_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (205, 82, 96)),
    (0.25, (220, 145, 115)),
    (0.50, (209, 195, 125)),
    (0.75, (168, 186, 114)),
    (0.90, (92, 142, 92)),
    (1.00, (34, 62, 43)),
)




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


def _readable_ink(fill: str) -> str:
    """Black or white on `fill`, whichever has more contrast."""
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _hex_rgb(fill))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / 0.0555) else "#111111"


# ---------------------------------------------------------------------------
# Labour: slack dashboard, diffusion, flows, channels (ported from the US app)
# ---------------------------------------------------------------------------







#: The channel wash behind the overall indicator's rows. Faint on purpose: the
#: wash only groups rows, identity is carried by the solid strip at the plot's
#: left edge, and the red and blue marks must stay the loudest thing in a row.
#: Dark needs a little more to register against a near-black page.
_SLACK_WASH = {"light": 0.07, "dark": 0.13}
_SLACK_STRIP_PX = 4
_SLACK_LABEL_PX = 150       # right margin for the channel names and tallies


def labor_slack(readings, p: Palette, height: int = 640) -> alt.LayerChart:
    """The labour-market slack dashboard: every indicator as a percentile of
    its own 2000-present distribution, on one More Slack -> Less Slack axis.

    Rows are grouped by channel, most important first (labor.CHANNELS order),
    each group washed in its channel colour and tallied in the right margin,
    so which way each channel is travelling reads at a glance."""
    by_key = {r.indicator.key: r for r in readings if r.ok}
    colours, ink = channel_colours(p), channel_ink(p)
    rows, seg, bands, tallies = [], [], [], []
    for ch in labor.CHANNELS:
        members = [by_key[k] for k in ch.members if k in by_key]
        if not members:
            continue
        for r in members:
            rows.append({
                "label": r.indicator.label, "pct": r.current_pct,
                "travel": r.travel, "value": r.current_value,
                "as_of": r.as_of.isoformat() if r.as_of else "",
                "approx": "yes" if r.indicator.approximate else "no",
                "n": r.n_obs, "channel": ch.label,
            })
            bands.append({"label": r.indicator.label, "channel": ch.key})
            if r.year_ago_pct is not None:
                seg.append({"label": r.indicator.label, "pct": r.year_ago_pct,
                            "pct2": r.current_pct, "travel": r.travel})
        up = sum(r.travel == "tightening" for r in members)
        down = sum(r.travel == "cooling" for r in members)
        flat = len(members) - up - down
        single = len(members) == 1
        # A one-indicator channel has no second row for its tally (Australia's
        # Firing and Participation hold one series each), so the tally rides on
        # the name's own row instead of overprinting it.
        tallies.append({"first": members[0].indicator.label,
                        "second": members[min(1, len(members) - 1)].indicator.label,
                        "name": (f"{ch.label}  ▲{up} ▼{down} ·{flat}" if single else ch.label),
                        "key": ch.key, "up": f"▲{up}", "down": f"▼{down}",
                        "flat": f"·{flat}", "single": single})
    df, sdf = pd.DataFrame(rows), pd.DataFrame(seg)
    bdf, tdf = pd.DataFrame(bands), pd.DataFrame(tallies)
    order = [x["label"] for x in rows]
    keys = [c.key for c in labor.CHANNELS]

    travel_scale = alt.Scale(
        domain=["cooling", "tightening", "flat"],
        range=[p.negative, p.positive, p.muted])
    # One explicit row domain shared by every layer: layers that encode rows
    # through different fields would otherwise merge their sorts and fall back
    # to alphabetical, scattering the channel groups.
    rows_scale = alt.Scale(domain=order, paddingInner=0, paddingOuter=0)
    row_axis = alt.Axis(title=None, labelFontSize=10, labelLimit=190, grid=False,
                        ticks=False, domain=False, labelPadding=8)
    y_enc = alt.Y("label:N", sort=None, scale=rows_scale, axis=row_axis)
    x_enc = alt.X("pct:Q", scale=alt.Scale(domain=[0, 100]),
                  axis=alt.Axis(title="Percentile of 2000-present   —   more slack ← → less slack",
                                format="d", values=[0, 25, 50, 75, 100]))
    wash = alt.Scale(domain=keys, range=[colours[k] for k in keys])
    mode = "dark" if p.mode == "dark" else "light"

    layers = [
        # The wash: one full-height band per row, so a channel's rows join up.
        alt.Chart(bdf).mark_rect(opacity=_SLACK_WASH[mode]).encode(
            y=y_enc, x=alt.datum(0), x2=alt.datum(100),
            color=alt.Color("channel:N", scale=wash, legend=None)),
        # Identity: a solid strip at the plot's left edge, in the channel colour.
        alt.Chart(bdf).mark_rect().encode(
            y=y_enc, x=alt.value(0), x2=alt.value(_SLACK_STRIP_PX),
            color=alt.Color("channel:N", scale=wash, legend=None)),
        alt.Chart(pd.DataFrame({"x": [25, 50, 75]})).mark_rule(
            color=p.baseline, strokeWidth=1, strokeDash=[3, 3], opacity=0.8).encode(x="x:Q"),
    ]
    if not sdf.empty:
        layers.append(
            alt.Chart(sdf).mark_rule(strokeWidth=2.4, opacity=0.8).encode(
                y=y_enc, x=x_enc, x2="pct2:Q",
                color=alt.Color("travel:N", scale=travel_scale, legend=None))
        )
        layers.append(
            alt.Chart(sdf).mark_point(
                filled=True, size=52, color=p.surface, strokeWidth=1.6).encode(
                y=y_enc, x=x_enc,
                stroke=alt.Color("travel:N", scale=travel_scale, legend=None))
        )
    layers.append(
        alt.Chart(df).mark_point(filled=True, size=100, stroke=p.surface, strokeWidth=1.8,
                                 opacity=1).encode(
            y=y_enc, x=x_enc,
            color=alt.Color("travel:N", scale=travel_scale, legend=None),
            tooltip=[alt.Tooltip("label:N", title="indicator"),
                     alt.Tooltip("channel:N", title="channel"),
                     alt.Tooltip("pct:Q", format=".2f", title="percentile"),
                     alt.Tooltip("value:Q", format=".2f", title="latest value"),
                     alt.Tooltip("travel:N", title="vs a year ago"),
                     alt.Tooltip("as_of:N", title="as of"),
                     alt.Tooltip("n:Q", title="observations"),
                     alt.Tooltip("approx:N", title="approximated")])
    )
    if not tdf.empty:
        # The margin key: channel name in its type colour on the group's first
        # row, the tally of tightening / cooling / unchanged on the second.
        name_ink = alt.Scale(domain=keys, range=[ink[k] for k in keys])
        layers.append(alt.Chart(tdf).mark_text(
            align="left", baseline="middle", fontSize=10.5, fontWeight="bold", font=FONT, dx=12
        ).encode(y=alt.Y("first:N", sort=None, scale=rows_scale, axis=row_axis), x=alt.datum(100),
                 text="name:N",
                 color=alt.Color("key:N", scale=name_ink, legend=None)))
        for field_, colour, offset in (("up", p.positive, 0), ("down", p.negative, 30),
                                       ("flat", p.muted, 60)):
            layers.append(alt.Chart(tdf[~tdf["single"]]).mark_text(
                align="left", baseline="middle", fontSize=10, font=FONT_NUM, color=colour,
                dx=12 + offset
            ).encode(y=alt.Y("second:N", sort=None, scale=rows_scale, axis=row_axis), x=alt.datum(100),
                     text=f"{field_}:N"))
    chart = alt.layer(*layers).resolve_scale(color="independent")
    return _apply(chart, p, height).properties(
        padding={"left": 4, "top": 4, "bottom": 4, "right": _SLACK_LABEL_PX})


def labor_diffusion(points, p: Palette, height: int = 260,
                    window_start: "date | None" = None,
                    recessions: "list[tuple[date, float]] | None" = None,
                    ) -> alt.LayerChart:
    """Share of indicators tightening -- how BROAD the move is."""
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

    bands = None
    if recessions:
        rec = recessions
        if window_start is not None:
            rec = [(d, v) for d, v in recessions if d >= window_start]
        rdf = _gs_rec_bands(rec)
        if not rdf.empty:
            bands = (alt.Chart(rdf).mark_rect(opacity=0.12, color="#7f7f7f")
                     .encode(x="start:T", x2="end:T"))

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


# ------------------------------------------ unemployment decomposition (Labour tab)

_DECOMP_COLS = 3
_DECOMP_TOP = 84


def _with_gaps(points: list[tuple[date, float]]) -> tuple[list, list]:
    """x/y with a None wherever a month is missing, so the line breaks there
    instead of drawing a straight run across the hole."""
    xs: list = []
    ys: list = []
    prev = None
    for d, v in points:
        if prev is not None and (d.year * 12 + d.month) - (prev.year * 12 + prev.month) > 1:
            xs.append(None)
            ys.append(None)
        xs.append(d)
        ys.append(v)
        prev = d
    return xs, ys


def unemployment_decomposition(decomp, p: Palette, height: int = 470,
                               window_start: date | None = None,
                               vintage: "Vintage | None" = None) -> go.Figure:
    """Six small panels: each flow's bp contribution to the rate change."""
    from plotly.subplots import make_subplots

    colours, pale_ink = _driver_colours(p)
    monthly_colour, smooth_colour = colours["point_3y"], colours["umich_5_10"]
    comps = [c for c in decomp.components if c.ok] if decomp is not None else []
    if not comps:
        return _consumer_empty(go.Figure(), "No labour-flow data", p, height)

    def cut(points):
        return [(d, v) for d, v in points if window_start is None or d >= window_start]

    rows = math.ceil(len(comps) / _DECOMP_COLS)
    fig = make_subplots(rows=rows, cols=_DECOMP_COLS,
                        subplot_titles=[c.label for c in comps],
                        horizontal_spacing=0.07, vertical_spacing=0.16)
    all_dates: list[date] = []
    for i, c in enumerate(comps):
        r, col = i // _DECOMP_COLS + 1, i % _DECOMP_COLS + 1
        mx, my = _with_gaps(cut(c.monthly))
        sx, sy = _with_gaps(cut(c.smoothed))
        all_dates += [d for d in mx if d is not None]
        fig.add_trace(go.Scatter(
            x=mx, y=my, mode="lines", name="Monthly", connectgaps=False,
            line=dict(width=1.1, color=monthly_colour),
            hovertemplate=f"{c.label}, monthly: %{{y:.1f}}bp<extra></extra>"), row=r, col=col)
        fig.add_trace(go.Scatter(
            x=sx, y=sy, mode="lines", name="3 Month Average", connectgaps=False,
            line=dict(width=2.4, color=smooth_colour),
            hovertemplate=f"{c.label}, 3-mo avg: %{{y:.1f}}bp<extra></extra>"), row=r, col=col)
    if not all_dates:
        return _consumer_empty(go.Figure(), "No labour-flow data in this window", p, height)

    subtitle = ("<i>Basis point (bp) contribution to the unemployment rate change  ·</i>  "
                f"{_named('Monthly', pale_ink)} <i>and</i> "
                f"{_named('3 Month Average', smooth_colour)}")
    latest = max(all_dates)
    bottom = _footer(fig, [f"Latest data: {latest:%B %Y}  ·  ABS gross flows (matched-sample rates x "
                           "SA stocks) over the labour force  ·  Source: ABS, RBA",
                           vintage.line if vintage else ""], p)
    fig.update_layout(
        height=height,
        title=dict(text="Unemployment Rate Decomposition", x=0.0, xanchor="left",
                   font=title_font(p), subtitle=dict(text=subtitle, font=subtitle_font(p))),
        paper_bgcolor=p.surface, plot_bgcolor=p.plane,
        font=dict(color=p.ink, size=11),
        margin=dict(l=44, r=16, t=_DECOMP_TOP, b=bottom),
        showlegend=False, hovermode="x unified")
    # Panel titles are annotations make_subplots adds; restyle them as labels.
    for ann in fig.layout.annotations:
        if ann.text in {c.label for c in comps}:
            ann.font = dict(size=12, color=p.ink_secondary)
            ann.yshift = -2
    min_date, max_date = min(all_dates), max(all_dates)
    xfmt, xdtick = _monthly_ticks((max_date - min_date).days)
    fig.update_xaxes(type="date", tickformat=xfmt, dtick=xdtick, hoverformat="%b %Y",
                     showgrid=False, range=[min_date - timedelta(days=15),
                                            max_date + timedelta(days=15)],
                     tickfont=dict(size=10))
    fig.update_yaxes(gridcolor=p.grid, zeroline=False, nticks=5, tickformat=",.1~f",
                     tickfont=dict(size=10))
    return fig


def _gs_rec_bands(rec: list[tuple[date, float]]) -> pd.DataFrame:
    """Run-length-encode recession months into (start, end) rects."""
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
    """X-axis label format + tick count fitted to the plotted span."""
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
    """Single-line chart with technical-recession shading."""
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
    """Dual-line chart: two y-axes, recession shading, compact legend."""
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
        .mark_bar(strokeWidth=0, binSpacing=0)
        .encode(
            x=alt.X("date:T", timeUnit="yearmonth",
                    axis=alt.Axis(title=None, format=xfmt,
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


_SLACK_MORE = "#bf5a67"        # red: more slack
_SLACK_LESS = "#537bb9"        # blue: less slack
_SLACK_MORE_DARK = "#a8414f"   # the source's own tone
_SLACK_LESS_DARK = "#33507d"   # the source's own tone

_SLACK_GAMMA = 0.75


def _slack_targets(p: Palette) -> tuple[str, str]:
    """(more-slack, less-slack) fills at full saturation, for this theme."""
    if p.mode == "dark":
        return _SLACK_MORE_DARK, _SLACK_LESS_DARK
    return _SLACK_MORE, _SLACK_LESS


def slack_heat_fill(pct: float, p: Palette) -> str:
    """Diverging fill for a slack percentile: 0 red, 50 neutral, 100 blue."""
    more, less = _slack_targets(p)
    t = min(1.0, abs(pct - 50.0) / 50.0) ** _SLACK_GAMMA
    return _lerp_hex(p.surface, less if pct >= 50.0 else more, t)


def labor_slack_heatmap(heat: "labor.Heatmap", p: Palette) -> str:
    """The slack heatmap as HTML, matching the source grid's layout."""
    if not heat.rows:
        return ""

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    head = "".join(
        f"<th class='h'>{m.strftime('%b')}<br><span class='yr'>{m.strftime('%y')}</span></th>"
        for m in heat.months)

    body: list[str] = []
    for row in heat.rows:
        ind = row.indicator
        mark = ("<span class='approx' title='approximated \u2014 not an exact "
                "match for the source row'>~</span>") if ind.approximate else ""
        cells: list[str] = []
        for cell in row.cells:
            if cell.pct is None:
                cells.append("<td class='c na'></td>")
                continue
            fill = slack_heat_fill(cell.pct, p)
            tip = (f"{ind.label} · {cell.month.strftime('%b %Y')}: "
                   f"{cell.pct:.0f}th percentile"
                   f" ({'less' if cell.pct >= 50 else 'more'} slack than "
                   f"{cell.pct if cell.pct >= 50 else 100 - cell.pct:.0f}% of "
                   f"months since {labor.BASE_START.year})")
            if cell.value is not None:
                tip += f" · value {cell.value:,.2f}"
            cells.append(
                f"<td class='c' title='{esc(tip)}' "
                f"style='background:{fill};color:{p.ink}'>"
                f"{cell.pct:.0f}</td>")
        body.append(f"<tr><td class='nm'>{esc(ind.label)}{mark}</td>"
                    f"{''.join(cells)}</tr>")

    more, less = _slack_targets(p)
    stops = "".join(
        f"<span class='g' style='background:{slack_heat_fill(v, p)}'></span>"
        for v in range(0, 101, 5))
    legend = (
        f"<div class='fomc-heatmap-legend'>"
        f"<span class='gl'>more slack</span>{stops}"
        f"<span class='gl'>less slack</span>"
        f"<span class='gt'>&nbsp;· percentile against each series' own history "
        f"since {labor.BASE_START.year} · blank = not yet published</span></div>")

    return (f"<table class='fomc-slackmap'>"
            f"<thead><tr><th class='nm'></th>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>{legend}")


# ------------------------------------------ labour diffusion by channel (Labour tab)

#: One hue per channel from the first seven slots of the house categorical ramp
#: (positive and accent are slots 1 and 3). Each channel keeps the hue it had
#: before Stated demand split out of Hiring, which took slot 7, violet. In
#: stacking order -- blue, orange, violet, aqua, yellow, magenta, green -- the
#: set validates as adjacent pairs against both page surfaces: worst CVD dE
#: 9.1 light / 8.4 dark, normal-vision 19.6 / 19.3. Aqua, yellow and magenta
#: sit under 3:1 on the light page, so every value is also printed in the
#: readout and the hover rather than left to the colour.
_CHANNEL_COLOURS = {
    "light": {"firing": "#2a78d6", "hiring": "#eb6834", "demand": "#4a3aa7",
              "employment": "#1baf7a", "confidence": "#eda100",
              "participation": "#e87ba4", "slack": "#008300"},
    "dark": {"firing": "#3987e5", "hiring": "#d95926", "demand": "#9085e9",
             "employment": "#199e70", "confidence": "#c98500",
             "participation": "#d55181", "slack": "#008300"},
}
#: Each channel's colour as TYPE (readout, panel titles, subtitle): the same
#: hue stepped until it clears 4.5:1 on that mode's page. The mark colours
#: are tuned for fills and several are too pale, or on black too dark, to read.
_CHANNEL_INK = {
    "light": {"firing": "#1f64bd", "hiring": "#b84a1c", "demand": "#4a3aa7",
              "employment": "#0e7a54", "confidence": "#8a5e00",
              "participation": "#b8446f", "slack": "#008300"},
    "dark": {"firing": "#3987e5", "hiring": "#d95926", "demand": "#9085e9",
             "employment": "#199e70", "confidence": "#c98500",
             "participation": "#d55181", "slack": "#2fa82f"},
}

#: The page's sub-heading names both charts, so each figure opens on its colour
#: key alone: one line for the bars, the panel titles for the breadth grid.
_CHANNEL_TOP = 56
_CHANNEL_BREADTH_TOP = 30
#: Past this many months a 2px surface seam between segments eats the bar.
_CHANNEL_SEAM_MONTHS = 120

_BASIS_SHORT = {"yoy": "y/y", "mom": "m/m"}


def channel_colours(p: Palette) -> dict[str, str]:
    return _CHANNEL_COLOURS["dark" if p.mode == "dark" else "light"]


def channel_ink(p: Palette) -> dict[str, str]:
    return _CHANNEL_INK["dark" if p.mode == "dark" else "light"]


def _in_window(points, window_start: date | None):
    return [pt for pt in points if window_start is None or pt.when >= window_start]


def _recession_rects(fig: go.Figure, recessions, lo: date, hi: date, **where) -> None:
    """Technical-recession months as grey bands, clipped to the plotted span."""
    rdf = _gs_rec_bands(recessions or [])
    for start, end in zip(rdf.get("start", []), rdf.get("end", [])):
        s, e = max(start.date(), lo), min(end.date(), hi)
        if s < e:
            fig.add_vrect(x0=s, x1=e, fillcolor="#7f7f7f", opacity=0.12,
                          line_width=0, layer="below", **where)


_NBSP = " "
_HOVER_LABEL_W = max(len(i.label) for i in labor.INDICATORS)


def _hover_num(v: float | None) -> str:
    """Two decimals throughout; claims-sized levels in thousands so the column
    stays narrow (202,750 -> 202.75k)."""
    if v is None:
        return "—"
    return f"{v / 1000:,.2f}k" if abs(v) >= 10_000 else f"{v:,.2f}"


def _pad(text: str, width: int, right: bool = False) -> str:
    """Pad with non-breaking spaces, which a hover label does not collapse."""
    fill = _NBSP * max(0, width - len(text))
    return fill + text if right else text + fill


def _channel_hover(pt, ch, basis: str, p: Palette, ink: dict[str, str]) -> str:
    """One bar segment's hover card: the channel's reading, then each indicator
    in it -- its share of the bar, which way it moved in each averaged month,
    and its latest value against the one it is compared with."""
    arrow = {1.0: (p.positive, "▲"), 0.5: (p.muted, "·"), 0.0: (p.negative, "▼"),
             None: (p.muted, _NBSP)}
    vs = "yr ago" if basis == "yoy" else "mth ago"
    breadth = pt.breadth[ch.key]
    muted = f"<span style='color:{p.muted}'>"
    lines = [
        f"<span style='color:{ink[ch.key]}'><b>{html.escape(ch.label)}</b></span>"
        f"{_NBSP * 2}<b>{pt.contribution[ch.key]:+.2f} pp</b>",
        f"{muted}{pt.when:%b %Y} · {_BASIS_SHORT[basis]} · {labor.DIFFUSION_SMOOTH}-mo avg"
        f" · breadth {'—' if breadth is None else f'{breadth:.2f}%'}"
        f" of {pt.reporting[ch.key]}</span>",
        f"{muted}{_pad('', _HOVER_LABEL_W)}{_pad('pp', 7, right=True)}{_NBSP * 2}"
        + _NBSP.join(f"{m:%b}" for m in pt.months) + f"{_NBSP * 2}latest vs {vs}</span>",
    ]
    for key in ch.members:
        comp = pt.components[key]
        marks = _NBSP.join(f"<span style='color:{arrow[v][0]}'>{_NBSP}{arrow[v][1]}{_NBSP}</span>"
                           for v in comp.votes)
        lines.append(
            f"{html.escape(_pad(labor.BY_KEY[key].label, _HOVER_LABEL_W))}"
            f"{_pad(f'{comp.contribution:+.2f}', 7, right=True)}{_NBSP * 2}{marks}{_NBSP * 2}"
            f"{_hover_num(comp.now)} {muted}vs</span> {_hover_num(comp.prior)}")
    return "<br>".join(lines)


def labor_channels(points, p: Palette, basis: str = "yoy", height: int = 430,
                   window_start: date | None = None,
                   recessions: "list[tuple[date, float]] | None" = None,
                   vintage: "Vintage | None" = None) -> go.Figure:
    """The diffusion index split into its channels: bars from the 50 line that
    add up, month by month, to the headline drawn over them."""
    fig = go.Figure()
    pts = _in_window(points, window_start)
    if not pts:
        return _consumer_empty(fig, "No diffusion history in this window", p, height)

    colours, ink = channel_colours(p), channel_ink(p)
    whens = [pt.when for pt in pts]
    seam = len(pts) <= _CHANNEL_SEAM_MONTHS
    for ch in labor.CHANNELS:
        fig.add_trace(go.Bar(
            x=whens, y=[pt.contribution[ch.key] for pt in pts], name=ch.label,
            marker=dict(color=colours[ch.key],
                        line=dict(color=p.surface, width=1.0 if seam else 0)),
            hovertext=[_channel_hover(pt, ch, basis, p, ink) for pt in pts],
            hovertemplate="%{hovertext}<extra></extra>",
            hoverlabel=dict(bordercolor=colours[ch.key])))
    # Straight segments, not a spline: the line is the sum of the bars, and a
    # spline would overshoot their net between months.
    fig.add_trace(go.Scatter(
        x=whens, y=[pt.share - 50.0 for pt in pts], name="Headline", mode="lines",
        line=dict(color=p.ink, width=2.2),
        customdata=[[pt.share, pt.share - 50.0, pt.n] for pt in pts],
        hovertemplate=("<b>Headline  %{customdata[0]:.2f}% tightening</b><br>"
                       "<span style='color:" + p.muted + "'>%{x|%b %Y} · "
                       f"{_BASIS_SHORT[basis]} · {labor.DIFFUSION_SMOOTH}-mo avg · "
                       "%{customdata[1]:+.2f} pp from 50 · n = %{customdata[2]}</span>"
                       "<extra></extra>"),
        hoverlabel=dict(bordercolor=p.ink)))

    # Symmetric about 50 so a bar's length reads the same above and below; the
    # ticks are labelled as the share itself, the bars as distance from 50.
    ups = [sum(v for v in pt.contribution.values() if v > 0) for pt in pts]
    downs = [sum(v for v in pt.contribution.values() if v < 0) for pt in pts]
    reach = max([abs(v) for v in ups + downs] + [abs(pt.share - 50) for pt in pts] + [10.0])
    top = min(50.0, 10.0 * math.ceil(reach / 10.0 + 0.05))
    step = 10 if top <= 30 else 25
    ticks = [v for v in range(-50, 51, step) if abs(v) <= top]

    lo, hi = min(whens), max(whens)
    _recession_rects(fig, recessions, lo, hi)
    latest = pts[-1]
    _readout(fig, f"{latest.when:%b %Y} · {_BASIS_SHORT[basis]}",
             [("Headline", f"{latest.share:.0f}%", p.ink)]
             + [(ch.label, f"{latest.contribution[ch.key]:+.1f}", ink[ch.key])
                for ch in labor.CHANNELS], p)

    names = "  ·  ".join(f'<span style="color:{ink[ch.key]}"><b>{html.escape(ch.label)}</b></span>'
                         for ch in labor.CHANNELS)
    title = dict(text=(f"{names}<br><span style='color:{p.muted};font-size:11px'>"
                       "Net contribution, pp from 50  ·  line = headline</span>"),
                 x=0.0, xanchor="left", y=1.0, yanchor="top", pad=dict(t=6),
                 font=dict(size=12, color=p.ink_secondary))
    bottom = _footer(fig, [f"Latest data: {latest.when:%B %Y}  ·  {latest.n} indicators "
                           f"reporting  ·  {labor.DIFFUSION_SMOOTH}-month average  ·  "
                           "Shading: technical recessions (two falling GDP quarters)  ·  Source: ABS, RBA, NAB",
                           vintage.line if vintage else ""], p)
    _monthly_frame(fig, p, height, whens, title, bottom, "d", top=_CHANNEL_TOP)
    # One card per segment, not one per month: the card is the breakdown of the
    # channel under the pointer. The number face keeps its columns aligned.
    fig.update_layout(barmode="relative", bargap=0.18 if seam else 0.0,
                      hovermode="closest",
                      hoverlabel=dict(bgcolor=p.surface, font=dict(family=FONT_NUM, size=11,
                                                                    color=p.ink),
                                      align="left"))
    fig.update_yaxes(range=[-top, top], tickvals=ticks,
                     ticktext=[f"{50 + v:.0f}" for v in ticks], ticksuffix="")
    return fig


def labor_channel_breadth(points, p: Palette, height: int = 360,
                          window_start: date | None = None,
                          recessions: "list[tuple[date, float]] | None" = None,
                          ) -> go.Figure:
    """Each channel's own diffusion, 0-100: the other factor in its bar.

    The bars are weight x breadth, and the weight is only how many indicators a
    channel holds. These panels take the weight out, so eight Slack series
    moving a little are not mistaken for two Participation series moving hard.
    """
    from plotly.subplots import make_subplots

    pts = _in_window(points, window_start)
    if not pts:
        return _consumer_empty(go.Figure(), "No diffusion history in this window", p, height)
    colours, ink = channel_colours(p), channel_ink(p)
    whens = [pt.when for pt in pts]
    latest = pts[-1]

    def panel_title(ch) -> str:
        # Name and latest value only: four panels a row leave no room for more,
        # and each channel's series count is in the section's ? popover.
        b = latest.breadth[ch.key]
        tail = f"{b:.0f}%" if b is not None else "—"
        return (f'<span style="color:{ink[ch.key]}"><b>{html.escape(ch.label)}</b></span>'
                f'<span style="color:{p.muted}">  ·  </span><b>{tail}</b>')

    cols = 4
    fig = make_subplots(rows=math.ceil(len(labor.CHANNELS) / cols), cols=cols,
                        subplot_titles=[panel_title(ch) for ch in labor.CHANNELS],
                        shared_xaxes=True, shared_yaxes=True,
                        horizontal_spacing=0.03, vertical_spacing=0.2)
    lo, hi = min(whens), max(whens)
    for i, ch in enumerate(labor.CHANNELS):
        r, c = i // cols + 1, i % cols + 1
        xs, ys = _with_gaps([(pt.when, pt.breadth[ch.key]) for pt in pts
                             if pt.breadth[ch.key] is not None])
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", name=ch.label, connectgaps=False,
            line=dict(color=colours[ch.key], width=2),
            hovertemplate=f"{ch.label}: %{{y:.2f}}% tightening<extra></extra>"),
            row=r, col=c)
        fig.add_hline(y=50, line=dict(color=p.baseline, width=1, dash="dot"),
                      row=r, col=c)
        _recession_rects(fig, recessions, lo, hi, row=r, col=c)
    # Panel titles are annotations make_subplots centres; set them flush left
    # over their own panel, the way the page's headings sit.
    for i, ann in enumerate(fig.layout.annotations[:len(labor.CHANNELS)]):
        axis = fig.layout["xaxis" if i == 0 else f"xaxis{i + 1}"]
        ann.update(x=axis.domain[0], xanchor="left", font=dict(size=12, color=p.ink))
    fig.update_layout(
        height=height,
        paper_bgcolor=p.surface, plot_bgcolor=p.plane, font=dict(color=p.ink, size=11),
        margin=dict(l=44, r=16, t=_CHANNEL_BREADTH_TOP, b=34),
        showlegend=False, hovermode="x unified")
    xfmt, xdtick = _monthly_ticks((hi - lo).days)
    fig.update_xaxes(type="date", tickformat=xfmt, dtick=xdtick, hoverformat="%b %Y",
                     showgrid=False, tickfont=dict(size=10),
                     range=[lo - timedelta(days=20), hi + timedelta(days=20)])
    fig.update_yaxes(range=[0, 100], tickvals=[0, 50, 100], gridcolor=p.grid,
                     zeroline=False, tickfont=dict(size=10))
    return fig


# ---------------------------------------------------------------------------
# Labour market grid, employment by component, WPI heatmap (AU twins)
# ---------------------------------------------------------------------------

def au_unemployment_chart(data, p: Palette, height: int = 220,
                          vintage: "Vintage | None" = None) -> alt.LayerChart:
    return _gs_line(data.get("UR", []), "Unemployment Rate", "Percent",
                    data.get("RECESSION", []), p, height, vintage=vintage)


def au_participation_chart(data, p: Palette, height: int = 220,
                           vintage: "Vintage | None" = None) -> alt.LayerChart:
    return _gs_line(data.get("PR", []), "Labour Force Participation Rate", "Percent",
                    data.get("RECESSION", []), p, height, vintage=vintage)


def au_emratio_chart(data, p: Palette, height: int = 220,
                     vintage: "Vintage | None" = None) -> alt.LayerChart:
    return _gs_line(data.get("EPOP", []), "Employment-to-Population Ratio", "Percent",
                    data.get("RECESSION", []), p, height, vintage=vintage)


def au_jwgap_chart(data, p: Palette, height: int = 220) -> alt.LayerChart:
    return _gs_line(data.get("JWGAP", []), "Jobs-Workers Gap*", "Percent of labour force",
                    data.get("RECESSION", []), p, height, show_title=False)


def au_job_loss_switching_chart(data, p: Palette, height: int = 220,
                                vintage: "Vintage | None" = None) -> alt.LayerChart:
    """The US layoffs-and-quits pair, from what Australia publishes: the
    employed-to-unemployed flow rate and the job-switching proxy."""
    return _gs_dual_line(left_series=data.get("JOB_LOSS", []),
                         right_series=data.get("SWITCHING", []),
                         title="Job Loss Rate & Job Switching",
                         left_label="Emp to unemp, % of employed",
                         right_label="Under a year in job, %",
                         rec=data.get("RECESSION", []), p=p, height=height, vintage=vintage)


def au_employment_change_chart(data, p: Palette, height: int = 220,
                               vintage: "Vintage | None" = None,
                               since: date | None = date(2023, 1, 1)) -> alt.LayerChart:
    series = [(d, v) for d, v in data.get("EMP_CHANGE_3MA", []) if since is None or d >= since]
    return _gs_bar(series, "Net Employment Gains", "Thousands (3m avg)", p, height,
                   vintage=vintage)


def _clip_window(series, window_start: date | None):
    if window_start is None:
        return list(series or [])
    return [(d, v) for d, v in (series or []) if d >= window_start]


def employment_components_chart(comp: dict, p: Palette, height: int = 380,
                                window_start: date | None = None,
                                recessions: list | None = None) -> alt.LayerChart:
    """Quarterly change in filled jobs ('000) by component, stacked, with the
    total and the market sector as lines -- the US payrolls-by-component chart
    on the ABS Labour Account."""
    from core import jobs_wages as jw
    colour = {"market": p.positive, "health_ed": p.accent, "public_admin": p.muted}
    bar_rows = []
    for order, (key, label) in enumerate(jw.COMPONENTS):
        for d, v in _clip_window(comp.get(key, []), window_start):
            bar_rows.append({"date": d, "v": v, "component": label, "colour": colour[key],
                             "order": order})
    bdf = pd.DataFrame(bar_rows)
    line_rows = []
    for key, label, ink, dash in (("total", "Total filled jobs", p.ink, [0]),
                                  ("market", "Market sector", p.negative, [4, 3])):
        for d, v in _clip_window(comp.get(key, []), window_start):
            line_rows.append({"date": d, "v": v, "line": label, "colour": ink, "dash": dash})
    ldf = pd.DataFrame(line_rows)
    for df in (bdf, ldf):
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
    xfmt, xticks = _gs_time_format(list(bdf["date"]) + list(ldf["date"])
                                   if not (bdf.empty and ldf.empty) else [])
    y = alt.Y("v:Q", axis=alt.Axis(title="Quarterly change ('000 jobs)", format=",.0f",
                                   titleColor=p.muted, labelColor=p.muted, gridColor=p.grid),
              scale=alt.Scale(nice=True))
    x = alt.X("date:T", timeUnit="yearquarter",
              axis=alt.Axis(title=None, format=xfmt, tickCount=xticks, labelColor=p.muted,
                            gridColor=p.grid))
    layers = []
    rdf = _gs_rec_bands(_clip_window(recessions or [], window_start))
    if not rdf.empty:
        layers.append(alt.Chart(rdf).mark_rect(opacity=0.12, color="#7f7f7f")
                      .encode(x="start:T", x2="end:T"))
    if not bdf.empty:
        layers.append(alt.Chart(bdf).mark_bar(strokeWidth=0, binSpacing=1).encode(
            x=x, y=y, color=alt.Color("colour:N", scale=None), order=alt.Order("order:Q"),
            tooltip=[alt.Tooltip("date:T", title="Quarter", format="%b %Y"),
                     alt.Tooltip("component:N", title="Component"),
                     alt.Tooltip("v:Q", title="Change ('000)", format=",.1f")]))
    if not ldf.empty:
        layers.append(alt.Chart(ldf).mark_line(strokeWidth=2.5, interpolate="monotone").encode(
            x=alt.X("date:T", timeUnit="yearquarter", bandPosition=0.5,
                    axis=alt.Axis(title=None, format=xfmt, tickCount=xticks)),
            y=y, color=alt.Color("colour:N", scale=None),
            strokeDash=alt.StrokeDash("dash:N", scale=None),
            tooltip=[alt.Tooltip("date:T", title="Quarter", format="%b %Y"),
                     alt.Tooltip("line:N", title="Series"),
                     alt.Tooltip("v:Q", title="Change ('000)", format=",.1f")]))
    return (alt.layer(*layers).properties(height=height, width="container")
            .configure_view(stroke="transparent")
            .configure_axis(**{k: v for k, v in _base(p)["config"]["axis"].items()}))


def _esc_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _quarter_label(d: date) -> str:
    return f"{['Mar', 'Jun', 'Sep', 'Dec'][(d.month - 1) // 3]}-{d:%y}"


def wpi_heatmap_table(wpi: dict, shares: dict, p: Palette) -> str:
    """WPI year-ended growth by industry, the latest eight quarters newest first,
    each row coloured on its own scale (row min deep red -> row max dark green),
    as the US sector wage heatmap colours AHE."""
    from core import jobs_wages as jw
    anchor = [d for d, _ in wpi.get("7|TOT", [])]
    if not anchor:
        return ""
    cols = anchor[-jw.HEATMAP_QUARTERS:][::-1]
    head = "".join(f"<th class='h'>{_quarter_label(d)}</th>" for d in cols)
    body = []
    for row in jw.WAGE_ROWS:
        yoy = dict(wpi.get(row.key, []))
        live = [v for v in (yoy.get(d) for d in cols) if v is not None]
        if not live:
            continue
        vmin, vmax = min(live), max(live)
        share = shares.get(row.jobs_key) if row.jobs_key else None
        cls = "agg" if row.level < 2 else ""
        share_td = (f"<td class='sh {cls}'>{share:.0f}%</td>" if share is not None
                    else f"<td class='sh {cls}'>—</td>")
        indent = "&nbsp;&nbsp;" * row.level
        cells = []
        for i, d in enumerate(cols):
            v = yoy.get(d)
            if v is None:
                cells.append("<td class='c na'>—</td>")
                continue
            t = 0.5 if vmax == vmin else (v - vmin) / (vmax - vmin)
            fill = _heat_colour(t)
            tip = f"{_esc_html(row.label)} · {_quarter_label(d)}: {v:.1f}% y/y"
            bold = " bold" if i == 0 else ""
            cells.append(f"<td class='c{bold}' title='{tip}' "
                         f"style='background:{fill};color:{_readable_ink(fill)}'>{v:.1f}</td>")
        body.append(f"<tr>{share_td}<td class='nm {cls}'>{indent}{_esc_html(row.label)}</td>"
                    f"{''.join(cells)}</tr>")
    stops = "".join(f"<span class='g' style='background:{_heat_colour(t)}'></span>"
                    for t in (i / 16 for i in range(17)))
    legend = ("<div class='fomc-heatmap-legend'><span class='gl'>low</span>" + stops
              + "<span class='gl'>high</span><span class='gt'>&nbsp;· colour scaled within "
                "each row (row min → row max)</span></div>")
    return ("<table class='fomc-heatmap'><thead><tr><th class='sh'>share of jobs</th>"
            f"<th class='nm'>Industry</th>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>{legend}")


# ---------------------------------------------------------------------------
# Inflation: trend, breadth windows, dispersion, breakdown, drivers (from the US)
# ---------------------------------------------------------------------------



def inflation_trend(series_list, p: Palette, height: int = 420,
                    window_start: date | None = None,
                    vintages: "dict[str, Vintage] | None" = None) -> list[go.Figure]:
    """Sequential underlying inflation trend -- the monthly CPI beside the
    quarterly CPI, as the US pairs CPI with PCE. Each panel draws its own
    target pace: 2.5% a year (the band's midpoint) in that panel's period."""
    palette_cols = [p.ink_secondary, p.positive, p.negative, p.accent,
                    p.muted, p.critical, p.warning]

    figs: list[go.Figure] = []
    for panel, title in (("Monthly", "Monthly CPI — month-over-month % change"),
                         ("Quarterly", "Quarterly CPI — quarter-on-quarter % change")):
        start = {"Monthly": date(2024, 4, 1), "Quarterly": date(1982, 1, 1)}[panel]
        per_year = 12 if panel == "Monthly" else 4
        target = cpi_monthly_mod.target_pace(per_year)
        cutoff = start if window_start is None else max(start, window_start)
        panel_series = [s for s in series_list if s.panel == panel]

        rows = []
        for s in panel_series:
            for d, v in s.observations:
                if d >= cutoff:
                    rows.append({"date": d, "value": v, "series": s.label})
        vintage = (vintages or {}).get(panel)
        fig = go.Figure()
        if not rows:
            fig.add_annotation(text=f"No {panel} data", showarrow=False,
                               font=dict(color=p.ink))
            figs.append(fig)
            continue

        df = pd.DataFrame(rows)
        # The range spans EVERY series, including the ones that start hidden.
        # A fixed range is not recomputed when the legend toggles a trace on,
        # so scoping it to the default-visible lines would clip a measure the
        # moment someone switched it on. On this data the Fed's alternative
        # measures sit inside the headline/core envelope in every history
        # window, so the default view gives up nothing by covering them.
        y_min = math.floor((df["value"].min() - 0.1) * 10) / 10
        y_max = math.ceil((df["value"].max() + 0.1) * 10) / 10

        series_names = sorted(df["series"].unique())
        colour = {name: palette_cols[i % len(palette_cols)]
                  for i, name in enumerate(series_names)}

        min_date, max_date = df["date"].min(), df["date"].max()

        for s in panel_series:
            obs = [(d, v) for d, v in s.observations if d >= cutoff]
            if not obs:
                continue
            # Only the headline/core measures are drawn by default. The Fed's
            # alternative measures stay in the legend but start switched off,
            # so the default view is four readable lines rather than seven
            # that overlap across the whole history. "legendonly" keeps them
            # out of the unified hover box as well as out of the plot.
            fig.add_trace(go.Scatter(
                x=[d for d, _ in obs],
                y=[v for _, v in obs],
                name=s.label, mode="lines",
                visible=(True if s.headline else "legendonly"),
                # Straight segments: a spline through monthly prints overshoots
                # them and draws readings that never printed.
                line=dict(width=1.8, color=colour[s.label]),
                hovertemplate=f"{s.label}: %{{y:+.2f}}%<extra></extra>",
            ))

        fig.add_shape(type="line", xref="x", yref="y",
                      x0=min_date, x1=max_date, y0=0, y1=0,
                      line=dict(color=p.baseline, width=1))
        # The pace that compounds to 2% a year -- the target, in the chart's
        # own month-over-month units. Zero is no inflation, not the target.
        fig.add_shape(type="line", xref="x", yref="y",
                      x0=min_date, x1=max_date, y0=target, y1=target,
                      line=dict(color=p.muted, width=1, dash="dash"))
        unit = "m/m" if per_year == 12 else "q/q"
        fig.add_annotation(x=max_date, y=target, xanchor="right", yanchor="bottom",
                           text=f"2.5% a year ({target:.2f}% {unit})", showarrow=False,
                           font=dict(size=10, color=p.muted))

        # Legend names include the traces that start hidden: a "legendonly"
        # trace is absent from the plot but still occupies legend width, so it
        # has to be counted when sizing the bottom margin.
        legend, legend_margin = bottom_legend(series_names)
        fig.update_layout(
            height=height,
            title=dict(text=title, x=0.0, xanchor="left", font=title_font(p)),
            paper_bgcolor=p.surface, plot_bgcolor=p.plane,
            font=dict(color=p.ink, size=12),
            margin=dict(l=56, r=16, t=46, b=legend_margin),
            showlegend=True,
            legend=legend,
            hovermode="x unified",
        )
        fig.update_yaxes(title=dict(text="% change, month ago" if per_year == 12
                                    else "% change, quarter ago"),
                         range=[y_min, y_max], gridcolor=p.grid,
                         zeroline=False, tickformat=".1f")
        fig.update_xaxes(type="date", tickformat="%b %Y" if per_year == 12 else "%Y",
                         hoverformat="%b %Y",
                         gridcolor=p.grid, range=[min_date, max_date])
        figs.append(apply_vintage(fig, vintage, p))

    return figs


def cpi_breadth_windows(series_by_window, p: Palette, height: int = 430,
                norm: float | None = None,
                window_start: date | None = None,
                threshold: float = 3.0,
                vintage: "Vintage | None" = None) -> go.Figure:
    """Breadth of CPI inflation -- what share of the basket is running hot.
    The US chart's 12-month and 6-month-annualised windows, on the quarterly
    expenditure classes: four quarters and two quarters annualised."""
    colour = {12: p.positive, 6: p.critical}
    dash = {12: None, 6: "dot"}
    label = {12: "Year-ended", 6: "Two quarters, annualised"}

    fig = go.Figure()
    windows = sorted(series_by_window, reverse=True)
    trimmed = {w: [(d, v) for d, v in series_by_window.get(w, [])
                   if window_start is None or d >= window_start]
               for w in windows}
    live = [w for w in windows if trimmed[w]]

    if not live:
        fig.add_annotation(text="No breadth data", showarrow=False,
                           font=dict(color=p.ink))
        fig.update_layout(height=height, paper_bgcolor=p.surface,
                          plot_bgcolor=p.plane)
        return fig

    all_dates = [d for w in live for d, _ in trimmed[w]]
    min_date, max_date = min(all_dates), max(all_dates)

    if norm is not None:
        fig.add_shape(type="line", xref="x", yref="y",
                      x0=min_date, x1=max_date, y0=norm, y1=norm,
                      line=dict(color=p.muted, width=1, dash="dash"))
        fig.add_annotation(
            x=min_date, y=norm, xref="x", yref="y",
            text=f"1993-2019 average · {norm:.0f}%",
            showarrow=False, xanchor="left", yanchor="bottom",
            font=dict(size=11, color=p.muted))

    for w in live:
        obs = trimmed[w]
        fig.add_trace(go.Scatter(
            x=[d for d, _ in obs], y=[v for _, v in obs],
            name=label.get(w, f"{w}-month"), mode="lines",
            line=dict(width=2.0 if w == 12 else 1.6, color=colour.get(w, p.ink),
                      dash=dash.get(w)),
            hovertemplate=(f"{label.get(w, str(w))}: "
                           "%{y:.0f}% of the basket<extra></extra>"),
        ))
        last_d, last_v = obs[-1]
        fig.add_trace(go.Scatter(
            x=[last_d], y=[last_v], mode="markers+text",
            marker=dict(size=7, color=colour.get(w, p.ink)),
            text=[f" {last_v:.0f}%"], textposition="middle right",
            textfont=dict(size=12, color=colour.get(w, p.ink)),
            showlegend=False, hoverinfo="skip", cliponaxis=False,
        ))

    legend_names = [label.get(w, f"{w}-month") for w in live]
    legend, legend_margin = bottom_legend(legend_names)
    fig.update_layout(
        height=height,
        title=dict(text=f"Share of the CPI basket rising faster than "
                        f"{threshold:g}% a year",
                   x=0.0, xanchor="left", font=title_font(p)),
        paper_bgcolor=p.surface, plot_bgcolor=p.plane,
        font=dict(color=p.ink, size=12),
        margin=dict(l=56, r=64, t=46, b=legend_margin),
        showlegend=True,
        legend=legend,
        hovermode="x unified",
    )
    fig.update_yaxes(title=dict(text="% of basket"), range=[0, 100],
                     gridcolor=p.grid, zeroline=False, ticksuffix="%",
                     dtick=25)
    xfmt, xticks = _gs_time_format(all_dates)
    fig.update_xaxes(type="date", tickformat=xfmt, nticks=xticks,
                     hoverformat="%b %Y",
                     gridcolor=p.grid, range=[min_date, max_date])
    return apply_vintage(fig, vintage, p)


_DISP_SHAPES = ("circle", "triangle-up", "diamond")

# Mark area in square pixels, one per regime, oldest first. The history is a
# backdrop and stays small; the surge and the current regime are what the eye is
# meant to find, and a triangle or a diamond reads smaller than a circle of the
# same area because so much of its bounding box is empty.
_DISP_SIZES = (34, 78, 104)

# Sample count for each era's fitted curve. Enough that the bend reads as a
# curve rather than a polyline at any panel width.
_DISP_FIT_STEPS = 48


def _dispersion_colours(p: Palette) -> list[str]:
    """One colour per regime, oldest first: history, the surge, now."""
    return [p.muted, p.warning, p.negative]


def cpi_dispersion(disp, p: Palette,
                   label_months: int = 5, height: int = 460) -> alt.LayerChart:
    """The CPI inflation dispersion scatter, one point per quarter."""
    colours = _dispersion_colours(p)
    labels = [r.label for r in disp.regimes]

    pts, lines = [], []
    for i, reg in enumerate(disp.regimes):
        for pt in reg.points:
            pts.append({
                "regime": reg.label, "share": pt.share, "mom": pt.mom_pct,
                "month": pt.month.strftime("%b %Y"),
                "n": pt.n_categories, "coverage": pt.coverage,
            })
        if reg.fit is not None:
            # The trend is linear in log-odds of the share, so it draws as a
            # curve: sample it across the era's own range rather than joining
            # the two endpoints, which would straighten out the whole point.
            lo = min(q.share for q in reg.points)
            hi = max(q.share for q in reg.points)
            step = (hi - lo) / (_DISP_FIT_STEPS - 1) if hi > lo else 0.0
            for k in range(_DISP_FIT_STEPS):
                x = lo + step * k
                lines.append({"regime": reg.label, "share": x,
                              "mom": reg.fit.at(x)})
    if not pts:
        return alt.LayerChart()

    df = pd.DataFrame(pts)
    colour = alt.Color("regime:N",
                       scale=alt.Scale(domain=labels, range=colours),
                       legend=alt.Legend(title=None, orient="top", direction="horizontal",
                                         symbolType="circle", columns=3))
    shape = alt.Shape("regime:N",
                      scale=alt.Scale(domain=labels, range=list(_DISP_SHAPES)),
                      legend=None)
    size = alt.Size("regime:N",
                    scale=alt.Scale(domain=labels, range=list(_DISP_SIZES)),
                    legend=None)
    # Both axes are already in percent units, so the label carries the sign
    # rather than a d3 percent format (which would multiply by 100 again).
    x_enc = alt.X("share:Q", scale=alt.Scale(domain=[0, 100]),
                  axis=alt.Axis(title="Share of the basket with relative price "
                                      "increases (> CPI inflation)",
                                values=[0, 20, 40, 60, 80, 100],
                                labelExpr="datum.value + '%'"))
    y_enc = alt.Y("mom:Q", axis=alt.Axis(title="Q/Q percent change", tickMinStep=0.2,
                                         labelExpr="format(datum.value, '.1f') + '%'"))
    tooltip = [alt.Tooltip("month:N", title="quarter"),
               alt.Tooltip("regime:N", title="era"),
               alt.Tooltip("mom:Q", title="q/q %", format=".2f"),
               alt.Tooltip("share:Q", title="share above", format=".0f"),
               alt.Tooltip("n:Q", title="classes"),
               alt.Tooltip("coverage:Q", title="basket covered %", format=".0f")]

    # The source draws the history as open circles and the two recent eras
    # filled, which is what lets a handful of coloured marks read against three
    # hundred grey ones. That needs two layers -- `filled` is a mark property,
    # not something an encoding can vary per point.
    history, recent = labels[0], labels[1:]
    layers = [
        alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(
            color=p.baseline, strokeWidth=1).encode(y="y:Q"),
        alt.Chart(df[df["regime"] == history]).mark_point(
            filled=False, strokeWidth=1.1, opacity=0.55).encode(
            x=x_enc, y=y_enc, color=colour, shape=shape, size=size, tooltip=tooltip),
        alt.Chart(df[df["regime"].isin(recent)]).mark_point(
            filled=True, opacity=0.85).encode(
            x=x_enc, y=y_enc,
            color=alt.Color("regime:N", scale=alt.Scale(domain=labels, range=colours),
                            legend=None),
            shape=shape, size=size, tooltip=tooltip),
    ]
    if lines:
        layers.append(
            alt.Chart(pd.DataFrame(lines)).mark_line(
                strokeWidth=1.6, strokeDash=[5, 3], interpolate="basis").encode(
                x="share:Q", y="mom:Q",
                color=alt.Color("regime:N",
                                scale=alt.Scale(domain=labels, range=colours),
                                legend=None)))

    current = disp.regimes[-1].points[-label_months:] if disp.regimes[-1].points else ()
    if current:
        ldf = pd.DataFrame([{"share": q.share, "mom": q.mom_pct,
                             "tag": q.month.strftime("%b '%y")} for q in current])
        # The newest month is the one the eye is hunting for on a scatter of
        # three hundred marks. Its own diamond is redrawn in the blue at the
        # era's size, on top of the era's red ones, so the mark the label
        # belongs to is findable even when the text itself is lost in the
        # grey cloud of history.
        layers.append(alt.Chart(pd.DataFrame([{"share": current[-1].share,
                                               "mom": current[-1].mom_pct}])
                                ).mark_point(filled=True, shape="diamond",
                                             size=_DISP_SIZES[-1],
                                             color=p.positive,
                                             opacity=1.0).encode(
            x="share:Q", y="mom:Q"))
        # Then the tags: the older four in the era red, the newest in the same
        # blue, bold, and drawn last so it sits on top.
        older, latest = ldf.iloc[:-1], ldf.iloc[-1:]
        if not older.empty:
            layers.append(alt.Chart(older).mark_text(
                align="left", dx=7, dy=-5, fontSize=9, fontWeight=600,
                color=p.negative).encode(x="share:Q", y="mom:Q", text="tag:N"))
        layers.append(alt.Chart(latest).mark_text(
            align="left", dx=7, dy=-5, fontSize=9, fontWeight="bold",
            color=p.positive).encode(x="share:Q", y="mom:Q", text="tag:N"))

    return _apply(alt.layer(*layers), p, height)


_CPI_HOT = "#a32a25"      # saturated +, sampled from the source's energy block
_CPI_COLD = "#4d86bf"     # saturated -, sampled from the source's motor-fuel dips

_CPI_CAP = 3.0

_CPI_GAMMA = 0.65


def _cpi_heat(value: float, p: Palette) -> str:
    """Signed value -> cell fill, blended out of the theme's own surface."""
    t = min(1.0, abs(value) / _CPI_CAP) ** _CPI_GAMMA
    return _lerp_hex(p.surface, _CPI_HOT if value >= 0 else _CPI_COLD, t)


def cpi_breakdown_table(table, p: Palette,
                        vintage: "Vintage | None" = None) -> str:
    """The CPI breakdown monitor as HTML, matching the source's layout."""
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
            basis = ("seasonally adjusted" if br.row.basis == "SA" else
                     "original (the ABS adjusts no series for this item)")
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

    stops = "".join(
        f"<span class='g' style='background:{_cpi_heat(v, p)}'></span>"
        for v in (-_CPI_CAP + i * (2 * _CPI_CAP / 24) for i in range(25)))
    key = (f"<div class='fomc-heatmap-legend'>"
           f"<span class='gl'>&minus;{_CPI_CAP:.0f}pp</span>{stops}"
           f"<span class='gl'>+{_CPI_CAP:.0f}pp</span>"
           f"<span class='gt'>&nbsp;· m/m % change, one shared scale, "
           f"saturating beyond ±{_CPI_CAP:.0f}pp</span></div>")

    navy = "#93b7e4" if p.mode == "dark" else "#1f3b66"
    return (
        f"<div class='fomc-cpi-head' style='color:{navy}'>"
        f"Australian CPI Breakdowns: m/m % change</div>"
        + "".join(f"<div class='fomc-cpi-meta' style='color:{navy}'>{esc(m)}</div>"
                  for m in meta)
        + f"<div class='fomc-scroll'><table class='fomc-cpi'>"
          f"<thead><tr><th class='nm'></th><th class='w'>Weights</th>"
          f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        + key)


def inflation_expectations(lines, p: Palette, height: int = 440,
                                     window_start: date | None = None,
                                     vintage: "Vintage | None" = None) -> go.Figure:
    """Melbourne Institute consumers (1 year), market economists (2 year) and
    the 10-year bond break-even, from RBA table G3."""
    colours, pale_ink = _driver_colours(p)
    fig = go.Figure()
    trimmed, live = _trim_lines(lines, window_start)
    if not live:
        return _consumer_empty(fig, "No inflation expectations data", p, height)

    hover_name = {"consumer_1y": "Consumers, 1-yr", "market_2y": "Market economists, 2-yr",
                  "breakeven_10y": "10-yr break-even"}
    colours = {"consumer_1y": colours["point_3y"], "market_2y": colours["density_3y"],
               "breakeven_10y": colours["umich_5_10"]}
    for ln in live:
        obs = trimmed[ln.key]
        fig.add_trace(go.Scatter(
            x=[o.date for o in obs], y=[o.value for o in obs], name=ln.label, mode="lines",
            line=dict(width=2.6, color=colours[ln.key]),
            hovertemplate=f"{hover_name[ln.key]}: %{{y:.2f}}%<extra></extra>"))

    ink = {"consumer_1y": pale_ink, "market_2y": colours["market_2y"],
           "breakeven_10y": colours["breakeven_10y"]}
    subtitle = ("<i>1 year ahead from</i> "
                f"{_named('consumers (Melbourne Institute)', ink['consumer_1y'])}, "
                f"<i>2 years from</i><br>{_named('market economists', ink['market_2y'])}"
                f"<i>, and the</i> {_named('10-year bond break-even', ink['breakeven_10y'])}")
    latest = max(trimmed[ln.key][-1].date for ln in live)
    bottom = _footer(fig, [f"Latest data: {_q_label(latest)}  ·  Source: RBA table G3, "
                           "Melbourne Institute",
                           vintage.line if vintage else ""], p)
    dates = [o.date for ln in live for o in trimmed[ln.key]]
    _monthly_frame(fig, p, height, dates,
                   _driver_title("Inflation Expectations", subtitle, p),
                   bottom, ".1f", right=_DRIVER_RIGHT, top=_DRIVER_TOP, zero_line=False,
                   hoverformat="%Y Q%q", pad_days=45)
    values = [o.value for ln in live for o in trimmed[ln.key]]
    # The floor sits half a step under the lowest tick, so that tick's label
    # clears the first year label in the shared corner.
    fig.update_yaxes(dtick=0.5, range=[math.floor(min(values) * 2 - 0.5) / 2 - 0.25,
                                       math.ceil(max(values) * 2 + 0.5) / 2])
    return fig


def _q_label(d: date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def wage_price_index(lines, p: Palette, height: int = 440,
                          window_start: date | None = None,
                          vintage: "Vintage | None" = None) -> go.Figure:
    """WPI: quarter-on-quarter change, total, private and public sector."""
    colours, pale_ink = _driver_colours(p)
    fig = go.Figure()
    trimmed, live = _trim_lines(lines, window_start)
    if not live:
        return _consumer_empty(fig, "No Wage Price Index data", p, height)

    colours = {"public": colours["benefits"], "private": colours["wages"],
               "total": colours["total"]}
    for ln in live:
        obs = trimmed[ln.key]
        fig.add_trace(go.Scatter(
            x=[o.date for o in obs], y=[o.value for o in obs], name=ln.label, mode="lines",
            line=dict(width=3.0 if ln.key == "total" else 2.4, color=colours[ln.key]),
            hovertemplate=f"{ln.label}: %{{y:.2f}}%<extra></extra>"))

    subtitle = ("<i>Q/Q percent change, hourly rates excluding bonuses, for</i><br>"
                f"{_named('All sectors', colours['total'])}, "
                f"{_named('Private', colours['private'])}, <i>and</i> "
                f"{_named('Public', pale_ink)}")
    latest = max(trimmed[ln.key][-1].date for ln in live)
    bottom = _footer(fig, [f"Latest data: {_q_label(latest)}  ·  Seasonally adjusted"
                           "  ·  Source: ABS via RBA table H4",
                           vintage.line if vintage else ""], p)
    dates = [o.date for ln in live for o in trimmed[ln.key]]
    _monthly_frame(fig, p, height, dates,
                   _driver_title("Wage Price Index", subtitle, p),
                   bottom, ".1f", right=_DRIVER_RIGHT, top=_DRIVER_TOP, zero_line=False,
                   hoverformat="%Y Q%q", pad_days=45)
    values = [o.value for ln in live for o in trimmed[ln.key]]
    fig.update_yaxes(dtick=0.2, range=[min(0.0, math.floor(min(values) * 5) / 5) - 0.1,
                                       math.ceil(max(values) * 5 + 0.5) / 5])
    return fig


# ---------------------------------------------------------------------------
# Growth: activity tracker, household spending and finances (from the US app)
# ---------------------------------------------------------------------------

#: The published chart's own colours, which are the point of it: the weekly
#: index in a pale rose that the eye reads as noise, its 13-week average in a
#: saturated crimson that reads as signal, and the two quarterly accounts in
#: neutral slates behind both. Palette roles (positive/negative) would recolour
#: this as agreement and disagreement, which is not what the lines mean.
_GROWTH_COLOURS = {
    "light": {growth_mod.HOURS: "#d98fa3", growth_mod.GDI: "#b9c0d0",
              growth_mod.GDP: "#4a5670", growth_mod.HOURS_SMOOTH: "#9c2743"},
    "dark": {growth_mod.HOURS: "#e0a0b2", growth_mod.GDI: "#aeb6c9",
             growth_mod.GDP: "#93a4c6", growth_mod.HOURS_SMOOTH: "#d4415f"},
}
_GROWTH_WIDTHS = {growth_mod.HOURS: 1.2, growth_mod.GDI: 1.5,
                  growth_mod.GDP: 1.5, growth_mod.HOURS_SMOOTH: 3.2}


#: (tick format, dtick) by plotted span, in days. An explicit month dtick, not
#: a tick COUNT: plotly places a count freely, so several ticks can land inside
#: one year and "%Y" then labels them all identically -- "2026 2026 2026" along
#: the axis. A month dtick anchors one tick per period, and the shorter windows
#: switch to "%b %Y" because a year label repeated by design is the same defect.
_GROWTH_TICKS: tuple[tuple[int, str, str], ...] = (
    (400, "%b %Y", "M2"),
    (800, "%b %Y", "M3"),
    (1300, "%b %Y", "M6"),
    (4400, "%Y", "M12"),
)
_GROWTH_TICKS_WIDE = ("%Y", "M24")


def _growth_ticks(span_days: int) -> tuple[str, str]:
    for limit, fmt, dtick in _GROWTH_TICKS:
        if span_days <= limit:
            return fmt, dtick
    return _GROWTH_TICKS_WIDE


def _quarter_close(d: date) -> date:
    """The start of the quarter after `d` -- where its step tread ends.

    A quarterly point dated on the quarter's FIRST day, drawn as a step, would
    otherwise stop the line three months short of what the series actually
    covers, and the chart's right edge would look like missing data.
    """
    m = d.month - 1 + 3
    return date(d.year + m // 12, m % 12 + 1, 1)


def au_growth(series_list, p: Palette, height: int = 460,
              window_start: date | None = None,
              vintage: "Vintage | None" = None,
              width_px: float = PANEL_WIDTH_PX) -> go.Figure:
    """Australian economic growth: monthly hours worked against the accounts."""
    colours = _GROWTH_COLOURS["dark" if p.mode == "dark" else "light"]
    fig = go.Figure()

    trimmed = {s.code: [o for o in s.observations
                        if window_start is None or o.date >= window_start]
               for s in series_list}
    live = [s for s in series_list if trimmed[s.code]]
    if not live:
        fig.add_annotation(text="No growth data", showarrow=False,
                           font=dict(color=p.ink))
        fig.update_layout(height=height, paper_bgcolor=p.surface,
                          plot_bgcolor=p.plane)
        return fig

    all_dates: list[date] = []
    for s in live:
        obs = trimmed[s.code]
        xs = [o.date for o in obs]
        ys = [o.value for o in obs]
        step = s.cadence == "quarterly"
        if step:
            xs, ys = xs + [_quarter_close(xs[-1])], ys + [ys[-1]]
        all_dates += xs
        fig.add_trace(go.Scatter(
            x=xs, y=ys, name=s.label, mode="lines",
            line=dict(width=_GROWTH_WIDTHS.get(s.code, 1.5),
                      color=colours.get(s.code, p.ink),
                      shape="hv" if step else "linear"),
            hovertemplate=f"{s.label}: %{{y:.2f}}%<extra></extra>",
        ))

    min_date, max_date = min(all_dates), max(all_dates)
    values = [o.value for s in live for o in trimmed[s.code]]
    y_min = math.floor(min(values) - 0.6)
    y_max = math.ceil(max(values) + 0.6)

    units = "Year-over-year percent change"
    # In a half-width panel the vintage line takes a line of its own.
    narrow = width_px < PANEL_WIDTH_PX
    subtitle = units
    if vintage and vintage.line:
        subtitle += ("<br>" if narrow else "  ·  ") + vintage.line
    top = 92 if narrow and vintage and vintage.line else 70
    legend, legend_margin = bottom_legend([s.label for s in live], width_px)
    fig.update_layout(
        height=height,
        title=dict(text="Australian Economic Growth", x=0.0, xanchor="left",
                   font=title_font(p),
                   subtitle=dict(text=subtitle, font=subtitle_font(p))),
        paper_bgcolor=p.surface, plot_bgcolor=p.plane,
        font=dict(color=p.ink, size=12),
        margin=dict(l=52, r=20, t=top, b=legend_margin),
        showlegend=True,
        legend=legend,
        hovermode="x unified",
    )
    fig.update_yaxes(title=None, range=[y_min, y_max], dtick=2,
                     gridcolor=p.grid, zeroline=False, ticksuffix="%")
    # Year boundaries as their own dotted rules, not the tick grid: the tick
    # spacing changes with the window (every 2 months at 1Y, every 2 years at
    # All), and the year marks should not.
    for year in range(min_date.year + 1, max_date.year + 1):
        fig.add_shape(type="line", xref="x", yref="paper",
                      x0=date(year, 1, 1), x1=date(year, 1, 1), y0=0, y1=1,
                      line=dict(color=p.muted, width=1, dash="dot"),
                      layer="below")

    xfmt, xdtick = _growth_ticks((max_date - min_date).days)
    fig.update_xaxes(type="date", tickformat=xfmt, dtick=xdtick,
                     hoverformat="%d %b %Y", showgrid=False,
                     range=[min_date, max_date])
    return fig


def household_spending(lines, p: Palette, height: int = 440,
                         window_start: date | None = None,
                         vintage: "Vintage | None" = None,
                         readout: bool = True) -> go.Figure:
    """Household spending (ABS HSI): 1-month AR bars, 3-month AR and 12-month
    lines -- the US retail control group panel.

    `readout=False` drops the latest-print block on the right -- the caller
    shows those figures on a board above -- and the plot takes its width."""
    col = _consumer_colours(p)
    colour = {"1m": col["bar"], "3m": col["3m"], "12m": col["12m"]}
    fig = go.Figure()
    trimmed, live = _trim_lines(lines, window_start)
    if not live:
        return _consumer_empty(fig, "No household spending data", p, height)

    for ln in live:
        obs = trimmed[ln.key]
        xs, ys = [o.date for o in obs], [o.value for o in obs]
        hover = f"{ln.label}: %{{y:.1f}}%<extra></extra>"
        if ln.key == "1m":
            fig.add_trace(go.Bar(x=xs, y=ys, name=ln.label, marker_color=colour[ln.key],
                                 marker_line_width=0, hovertemplate=hover))
        else:
            fig.add_trace(go.Scatter(x=xs, y=ys, name=ln.label, mode="lines",
                                     line=dict(width=3.0, color=colour[ln.key]),
                                     hovertemplate=hover))

    # The legend's own tone for the bars is one step darker than the bars: the
    # pale fill that sits quietly behind the lines is too faint as type.
    title = _swatch_title("Household Spending",
                          [(ln.label, p.muted if ln.key == "1m" else colour[ln.key])
                           for ln in live], "  |  ", p)
    latest = max((trimmed[ln.key][-1].date for ln in live))
    by_key = {ln.key: ln for ln in live}
    if readout:
        _readout(fig, f"{latest:%B %Y}",
                 [(f"{by_key[k].label.split(' (')[0]}:", f"{trimmed[k][-1].value:.1f}%",
                   p.ink_secondary)
                  for k in ("1m", "3m", "12m")
                  if k in by_key and trimmed[k][-1].date == latest],
                 p)
    bottom = _footer(fig, [f"Latest data: {latest:%B %Y}  ·  Household Spending Indicator, "
                           "current price, SA  ·  Source: ABS",
                           vintage.line if vintage else ""], p)
    dates = [o.date for ln in live for o in trimmed[ln.key]]
    _monthly_frame(fig, p, height, dates, title, bottom, ".0f",
                   right=_CONSUMER_READOUT_PX if readout else _BARE_RIGHT_PX)
    return fig


def household_finances(lines, p: Palette, height: int = 440,
                       window_start: date | None = None,
                       vintage: "Vintage | None" = None,
                       readout: bool = True) -> go.Figure:
    """Real household consumption and real disposable income, year-ended,
    quarterly (RBA H2) -- the US panel's spending and income at the cadence
    Australia publishes them. `readout=False` as for `household_spending`."""
    col = _consumer_colours(p)
    fig = go.Figure()
    trimmed, live = _trim_lines(lines, window_start)
    if not live:
        return _consumer_empty(fig, "No household income data", p, height)

    for ln in live:
        obs = trimmed[ln.key]
        xs, ys = [o.date for o in obs], [o.value for o in obs]
        hover = f"{ln.label}: %{{y:.2f}}%<extra></extra>"
        if ln.key == "spending":
            fig.add_trace(go.Bar(x=xs, y=ys, name=ln.label, marker_color=col["spending"],
                                 marker_line_width=0, hovertemplate=hover))
        else:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, name=ln.label, mode="lines",
                line=dict(width=3.0, color=col[ln.key],
                          dash="dash" if ln.key == "ex_transfers" else None),
                hovertemplate=hover))

    order = {"income": 0, "spending": 1}
    named = sorted(live, key=lambda ln: order[ln.key])
    tone = consumer_tones(p)
    # Without the readout block the chart sits in a half-width panel, where the
    # full names would run off the edge.
    compact = {"income": "Real disposable income", "spending": "Consumption"}
    parts = [(ln.label if readout else compact[ln.key], tone[ln.key]) for ln in named]
    names = ", ".join(f'<span style="color:{c}"><b>{html.escape(t)}</b></span>'
                      for t, c in parts[:-1])
    last = parts[-1]
    tail = f'<span style="color:{last[1]}"><b>{html.escape(last[0])}</b></span>'
    subtitle = (f"<i>Year-ended percent change in</i> {names} <i>and</i> {tail}"
                if len(parts) > 1 else f"<i>Year-ended percent change in</i> {tail}")
    title = dict(text="Australian Household Finances", x=0.0, xanchor="left",
                 font=title_font(p), subtitle=dict(text=subtitle, font=subtitle_font(p)))

    latest = max(trimmed[ln.key][-1].date for ln in live)
    short = {"income": "Income:", "spending": "Consumption:"}
    if readout:
        _readout(fig, f"{latest:%B %Y}",
                 [(short[ln.key], f"{trimmed[ln.key][-1].value:+.1f}%", tone[ln.key])
                  for ln in named if trimmed[ln.key][-1].date == latest], p)
    bottom = _footer(fig, [f"Latest data: {_q_label(latest)}  ·  Real, seasonally adjusted  ·  "
                           "Source: ABS national accounts via RBA table H2",
                           vintage.line if vintage else ""], p)
    dates = [o.date for ln in live for o in trimmed[ln.key]]
    _monthly_frame(fig, p, height, dates, title, bottom, ".1f",
                   right=_CONSUMER_READOUT_PX if readout else _BARE_RIGHT_PX,
                   hoverformat="%Y Q%q", pad_days=45)
    return fig


def consumer_tones(p: Palette) -> dict[str, str]:
    """The household chart's own colour for each line, for a board beside it."""
    col = _consumer_colours(p)
    return {"income": col["income"], "ex_transfers": col["ex_transfers"],
            "spending": p.muted}


# ------------------------------------------------ retail categories (bubbles)

#: The bubble chart runs the full width, like the charts above it. Only used
#: to place labels in pixel space before Plotly lays the figure out.
BUBBLE_PANEL_PX = 880
_BUBBLE_MAX_PX = 140      # the largest category's diameter at panel width
_BUBBLE_MIN_PX = 16
_BUBBLE_FENCE = 2.0       # interquartile ranges beyond the quartiles = off-scale
_BUBBLE_LABEL_CHAR_PX = 6.6
_BUBBLE_LABEL_H_PX = 15
_BUBBLE_LABEL_PAD_PX = 2  # clear air kept round every label


def _quartiles(values: list[float]) -> tuple[float, float]:
    s = sorted(values)
    def q(f: float) -> float:
        i = f * (len(s) - 1)
        lo = math.floor(i)
        return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (i - lo)
    return q(0.25), q(0.75)


def _fence(values: list[float]) -> tuple[float, float]:
    """The range beyond which a category is drawn as an off-scale arrow.

    Gasoline is the case in point: a 15% month for gas would otherwise set the
    axis and crush every other category into a band at the bottom. A Tukey
    fence at two interquartile ranges keeps the ordinary spread on the chart
    and sends only a genuine outlier off it.
    """
    q1, q3 = _quartiles(values)
    spread = max(q3 - q1, 1.0)
    return q1 - _BUBBLE_FENCE * spread, q3 + _BUBBLE_FENCE * spread


def _axis_range(values: list[float], step: float, pad: float) -> tuple[float, float]:
    lo = math.floor((min(values) - pad) / step) * step
    hi = math.ceil((max(values) + pad) / step) * step
    return min(lo, 0.0), max(hi, 0.0)


def _rect_overlap(a: tuple, b: tuple) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _circle_overlap(rect: tuple, cx: float, cy: float, r: float) -> float:
    """Rough area a label shares with a disc: its overlap with the disc's
    bounding square, scaled by the disc's share of that square."""
    return _rect_overlap(rect, (cx - r, cy - r, cx + r, cy + r)) * math.pi / 4


def _label_slots(r: float, w: float, gap: float = 3.0) -> list[tuple]:
    """Candidate (dx, dy, xanchor, yanchor, far) offsets around a disc of radius r,
    in order of preference: the four sides, the diagonals, then a second ring
    further out for a crowded patch, and the centre when the label fits inside."""
    slots = []
    for far, ring in ((False, r + gap), (True, r + gap + 18)):
        d = ring * 0.72
        slots += [(0, -ring, "center", "bottom", far), (ring, 0, "left", "middle", far),
                  (0, ring, "center", "top", far), (-ring, 0, "right", "middle", far),
                  (d, -d, "left", "bottom", far), (-d, -d, "right", "bottom", far),
                  (d, d, "left", "top", far), (-d, d, "right", "top", far)]
    if r * 2 > w + 8:
        slots.insert(0, (0, 0, "center", "middle", False))
    return slots


def _place_labels(items: list[dict], plot_w: float,
                  plot_h: float) -> list[tuple[float, float, str, str, bool]]:
    """Greedy label placement in pixel space, biggest disc first.

    Each label tries the slots from `_label_slots` and keeps the one that
    collides least with: labels already placed (hard), the plot edge (hard),
    and other discs' outlines (soft -- text over an outline is legible, text
    over text is not). Returns (dx, dy, xanchor, yanchor, far) per item, in
    order; `far` marks a label pushed out to the second ring, which gets a
    leader line back to its disc.
    """
    placed: list[tuple] = []
    out: dict[int, tuple] = {}
    h = _BUBBLE_LABEL_H_PX
    for i in sorted(range(len(items)), key=lambda k: -items[k]["r"]):
        it = items[i]
        cx, cy, r = it["px"], it["py"], it["r"]
        w = len(it["label"]) * it.get("char_px", _BUBBLE_LABEL_CHAR_PX) + 4
        best, best_rect, best_score = None, None, float("inf")
        for rank, (dx, dy, xa, ya, far) in enumerate(_label_slots(r, w)):
            x0 = cx + dx - (w / 2 if xa == "center" else (w if xa == "right" else 0))
            y0 = cy + dy - (h / 2 if ya == "middle" else (h if ya == "bottom" else 0))
            pad = _BUBBLE_LABEL_PAD_PX
            rect = (x0 - pad, y0 - pad, x0 + w + pad, y0 + h + pad)
            score = rank * 25.0
            score += sum(_rect_overlap(rect, o) for o in placed) * 12
            score += sum(_circle_overlap(rect, o["px"], o["py"], o["r"])
                         for j, o in enumerate(items) if j != i) * 0.6
            outside = (max(0, -x0) + max(0, rect[2] - plot_w)
                       + max(0, -y0) + max(0, rect[3] - plot_h))
            score += outside * h * 12
            if score < best_score:
                best, best_rect, best_score = (dx, dy, xa, ya, far), rect, score
        placed.append(best_rect)
        out[i] = best
    return [out[i] for i in range(len(items))]


def spending_bubbles(snap, p: Palette, height: int = 600,
                   vintage: "Vintage | None" = None,
                   width_px: float = BUBBLE_PANEL_PX) -> go.Figure:
    """HSI categories in the latest month: y/y across, m/m up, sized by spend."""
    col = _consumer_colours(p)
    fig = go.Figure()
    if snap is None or not snap.categories:
        return _consumer_empty(fig, "No household spending category data", p, height)

    cats = snap.categories
    x_fence = _fence([c.yoy for c in cats])
    y_fence = _fence([c.mom for c in cats])

    def inside(pt) -> bool:
        return x_fence[0] <= pt.yoy <= x_fence[1] and y_fence[0] <= pt.mom <= y_fence[1]

    shown = [c for c in cats if inside(c)]
    off = [c for c in cats if not inside(c)]
    xs = [c.yoy for c in shown] + [snap.overall.yoy]
    ys = [c.mom for c in shown] + [snap.overall.mom]
    x0, x1 = _axis_range(xs, 5.0, 2.5)
    y0, y1 = _axis_range(ys, 1.0, 0.6)
    # Half a step under the lowest y tick, so its label clears the first x
    # tick in the corner the two axes share.
    y0 -= 0.5

    # The fence decides which categories may set the axes; the arrow is only
    # for one that then falls outside them. A category past the fence that
    # still lands inside the plot is drawn as an ordinary bubble -- otherwise
    # it would lose its bubble and get a zero-length arrow on its own spot.
    def on_axes(pt) -> bool:
        return x0 <= pt.yoy <= x1 and y0 <= pt.mom <= y1

    shown += [c for c in off if on_axes(c)]
    off = [c for c in off if not on_axes(c)]

    margin = dict(l=64, r=24, t=_CONSUMER_TOP, b=0)
    lines = ["Adjusted for seasonal, holiday and trading-day differences  ·  "
             "area is share of spending  ·  Source: ABS Household Spending Indicator",
             vintage.line if vintage else ""]
    margin["b"] = _footer(fig, lines, p, below=22)      # 22px: the x axis title
    plot_w = width_px - margin["l"] - margin["r"]
    plot_h = height - margin["t"] - margin["b"]

    def to_px(x: float, y: float) -> tuple[float, float]:
        return ((x - x0) / (x1 - x0) * plot_w, (y1 - y) / (y1 - y0) * plot_h)

    biggest = max(c.sales for c in cats)
    max_px = _BUBBLE_MAX_PX * min(1.0, plot_w / 720)

    def diameter(c) -> float:
        return max(_BUBBLE_MIN_PX, max_px * math.sqrt(c.sales / biggest))

    fig.add_trace(go.Scatter(
        x=[c.yoy for c in shown], y=[c.mom for c in shown], mode="markers",
        marker=dict(size=[diameter(c) for c in shown], sizemode="diameter",
                    color=col["bubble_fill"], line=dict(color=col["bubble"], width=1.3)),
        customdata=[[c.label, c.share] for c in shown],
        hovertemplate=("<b>%{customdata[0]}</b><br>m/m: %{y:+.1f}%<br>y/y: %{x:+.1f}%"
                       "<br>share of spending: %{customdata[1]:.1f}%<extra></extra>"),
        showlegend=False))
    fig.add_trace(go.Scatter(
        x=[snap.overall.yoy], y=[snap.overall.mom], mode="markers",
        marker=dict(symbol="star", size=20, color=col["star"],
                    line=dict(width=0)),
        hovertemplate=("<b>Overall</b><br>m/m: %{y:+.1f}%<br>y/y: %{x:+.1f}%"
                       "<extra></extra>"),
        showlegend=False))

    items = []
    for c in shown:
        px, py = to_px(c.yoy, c.mom)
        items.append(dict(label=c.label, px=px, py=py, r=diameter(c) / 2))
    sx, sy = to_px(snap.overall.yoy, snap.overall.mom)
    overall_item = dict(label="Overall", px=sx, py=sy, r=10, char_px=8.4)
    slots = _place_labels(items + [overall_item], plot_w, plot_h)
    for it, (dx, dy, xa, ya, far), pt in zip(items + [overall_item], slots,
                                             shown + [snap.overall]):
        is_overall = it is overall_item
        text = f"<b>{it['label']}</b>" if is_overall else html.escape(it["label"])
        font = dict(size=14 if is_overall else 11,
                    color=col["star"] if is_overall else p.ink_secondary)
        if far:
            # A label pushed out of a crowd keeps a hairline back to its disc,
            # so it cannot be read as naming the neighbour it now sits beside.
            fig.add_annotation(
                x=pt.yoy, y=pt.mom, xref="x", yref="y", showarrow=True,
                ax=dx, ay=dy, axref="pixel", ayref="pixel", arrowhead=0,
                arrowwidth=0.8, arrowcolor=p.muted, standoff=it["r"],
                xanchor=xa, yanchor=ya, text=text, font=font)
        else:
            fig.add_annotation(
                x=pt.yoy, y=pt.mom, xref="x", yref="y", showarrow=False,
                xshift=dx, yshift=-dy, xanchor=xa, yanchor=ya, text=text, font=font)

    # Off-scale categories: an arrow on the edge they left by, their real
    # coordinates written beside it.
    for c in off:
        ex = min(max(c.yoy, x0), x1)
        ey = min(max(c.mom, y0), y1)
        vx = (c.yoy > x1) - (c.yoy < x0)
        vy = (c.mom > y1) - (c.mom < y0)
        inset_x = (x1 - x0) * 0.04
        inset_y = (y1 - y0) * 0.05
        hx, hy = ex - vx * inset_x, ey - vy * inset_y
        fig.add_annotation(
            x=hx, y=hy, xref="x", yref="y", showarrow=True, arrowhead=2,
            arrowsize=1.1, arrowwidth=2.2, arrowcolor=p.ink_secondary,
            ax=-vx * 40, ay=vy * 40, standoff=0,
            text=(f"{html.escape(c.label)}<br>({c.yoy:+.0f}% y/y, {c.mom:+.0f}% m/m)"),
            font=dict(size=11, color=p.ink_secondary), align="center",
            yanchor="top" if vy > 0 else "bottom",
            xanchor="right" if vx > 0 and vy == 0 else "center")

    fig.update_layout(
        height=height,
        title=dict(text="Household Spending by Category", x=0.0, xanchor="left",
                   font=title_font(p),
                   subtitle=dict(text=f"<i>{snap.month:%B %Y} adjusted data, percent change</i>",
                                 font=subtitle_font(p))),
        paper_bgcolor=p.surface, plot_bgcolor=p.plane,
        font=dict(color=p.ink, size=12), margin=margin, showlegend=False,
        hovermode="closest")
    fig.add_shape(type="line", xref="x", yref="paper", x0=0, x1=0, y0=0, y1=1,
                  line=dict(color=p.ink_secondary, width=1.2), layer="below")
    fig.add_shape(type="line", xref="paper", yref="y", x0=0, x1=1, y0=0, y1=0,
                  line=dict(color=p.ink_secondary, width=1.2), layer="below")
    fig.update_xaxes(range=[x0, x1], dtick=5.0, ticksuffix="%", zeroline=False,
                     gridcolor=p.grid,
                     title=dict(text="<b>Year-over-Year</b>", standoff=6,
                                font=dict(size=13, color=p.ink_secondary)))
    fig.update_yaxes(range=[y0, y1], dtick=1.0, ticksuffix="%", zeroline=False,
                     gridcolor=p.grid,
                     title=dict(text="<b>Month-over-Month</b>", standoff=6,
                                font=dict(size=13, color=p.ink_secondary)))
    return fig
