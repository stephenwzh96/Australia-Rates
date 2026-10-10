"""Inflation-contribution charts -- the US FRBSF views, on the ABS's own
monthly contribution measures.

The US tab stacks the FRBSF goods/services contributions to headline CPI (12
month and 1 month), shows the BLS item monitor, then the month's largest and
smallest core contributors. The ABS publishes each expenditure group's
contribution to the monthly and the annual change directly, so the same
three views are built exactly: stacks by group, the item monitor, and the
largest and smallest class contributors.
"""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go

from core.cpi_monthly import ContributionFigure

from . import charts
from .charts import bottom_legend
from .theme import Palette

#: One colour per CPI group, so a group keeps its colour in every stack and in
#: the granular bars. Housing (the shelter twin) in the US chart's dark blue,
#: food in its sky blue, transport in its steel, services groups in its green
#: family -- the FRBSF language mapped onto the ABS groups.
GROUP_COLOURS: dict[str, str] = {
    "Food and non-alcoholic beverages": "#88cde5",
    "Alcohol and tobacco": "#6d6aaa",
    "Clothing and footwear": "#fcc62d",
    "Housing": "#2e5e8b",
    "Furnishings, household equipment and services": "#e09100",
    "Health": "#b63b36",
    "Transport": "#6291a6",
    "Communication": "#474747",
    "Recreation and culture": "#8baf3e",
    "Education": "#1E5932",
    "Insurance and financial services": "#fa7b00",
}
_FALLBACK = ["#2a78d6", "#e34948", "#1baf7a", "#e09100", "#7b5ea7", "#2f9e9e"]

#: Legend names short enough that eleven groups wrap to three rows in a half
#: panel; the ABS's full names stay in the hover.
SHORT: dict[str, str] = {
    "Food and non-alcoholic beverages": "Food", "Alcohol and tobacco": "Alcohol & tobacco",
    "Clothing and footwear": "Clothing", "Housing": "Housing",
    "Furnishings, household equipment and services": "Furnishings",
    "Health": "Health", "Transport": "Transport", "Communication": "Communication",
    "Recreation and culture": "Recreation", "Education": "Education",
    "Insurance and financial services": "Insurance & financial",
}

#: The width a half-panel chart actually renders into.
HALF_PX = 390

#: Stacking order, bottom first: housing (the biggest and steadiest), then the
#: services groups, then goods, with food and transport -- the volatile ones --
#: on top where their swings read against the line.
_ORDER = ("Housing", "Insurance and financial services", "Health", "Education",
          "Recreation and culture", "Communication", "Furnishings, household equipment and services",
          "Clothing and footwear", "Alcohol and tobacco", "Food and non-alcoholic beverages",
          "Transport")


def group_colour(name: str) -> str | None:
    """The colour of a group, or of the group a "Group: class" label sits in."""
    return GROUP_COLOURS.get(name.split(": ", 1)[0])


def _placeholder(title: str, p: Palette, msg: str = "No data available") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=f"{msg}<br><span style='font-size:11px;opacity:0.7'>{title}</span>",
                       showarrow=False, font=dict(color=p.ink, size=13))
    fig.update_layout(height=200, paper_bgcolor=p.surface, plot_bgcolor=p.plane,
                      font=dict(color=p.ink), margin=dict(l=20, r=20, t=20, b=20))
    return fig


def _tick_format(dates) -> tuple[str, str | None]:
    """(tickformat, dtick): month and year when a bare year would repeat across
    the window. A bare year also pins one tick a year -- left to itself Plotly
    ticked a 29-month window every six months and printed each year twice."""
    live = [d for d in dates if d is not None]
    if not live:
        return "%Y", "M12"
    lo, hi = min(live), max(live)
    if (hi.year - lo.year) * 12 + (hi.month - lo.month) <= 24:
        return "%b %Y", None
    return "%Y", "M12"


def contribution_stack(fig: ContributionFigure, p: Palette, height: int = 440,
                       window_start: date | None = None, as_of: date | None = None,
                       vintage=None, width: int = HALF_PX) -> go.Figure:
    """Stacked signed contribution bars with the headline rate overlaid."""
    if fig.empty:
        return _placeholder(fig.title, p)
    view = fig.between(window_start, as_of)
    if view.empty:
        return _placeholder(fig.title, p, "No months in the selected window.")
    order = {n: i for i, n in enumerate(_ORDER)}
    comps = sorted(view.components, key=lambda n: order.get(n, len(order)))
    dates = list(view.dates)
    figobj = go.Figure()
    free = 0
    for name in comps:
        colour = group_colour(name)
        if colour is None:
            colour = _FALLBACK[free % len(_FALLBACK)]
            free += 1
        figobj.add_trace(go.Bar(x=dates, y=view.components[name], name=SHORT.get(name, name),
                                marker_color=colour,
                                hovertemplate=f"{name}: %{{y:.2f}} pp<extra></extra>"))
    figobj.add_trace(go.Scatter(x=dates, y=view.total, name="Headline (Total)", mode="lines",
                                line=dict(color=p.ink, width=2, dash="dot"),
                                hovertemplate="Headline: %{y:.2f}%<extra></extra>"))
    legend, legend_margin = bottom_legend([SHORT.get(c, c) for c in comps] + ["Headline (Total)"],
                                          width)
    figobj.update_layout(barmode="relative", height=height,
                         title=dict(text=fig.title, x=0.0, font=charts.title_font(p)),
                         paper_bgcolor=p.surface, plot_bgcolor=p.plane,
                         font=dict(color=p.ink, size=12),
                         margin=dict(l=58, r=18, t=46, b=legend_margin),
                         legend=legend, hovermode="x unified")
    tickformat, dtick = _tick_format(dates)
    figobj.update_xaxes(type="date", tickformat=tickformat, dtick=dtick, hoverformat="%b %Y",
                        gridcolor=p.grid)
    figobj.update_yaxes(title=dict(text="Contribution (pp) · inflation (%)"),
                        gridcolor=p.grid, zeroline=False, tickformat=".1f")
    return charts.apply_vintage(figobj, vintage, p)


def granular_diverging(fig: ContributionFigure, p: Palette, top: int = 18,
                       height: int = 760, vintage=None) -> go.Figure:
    """The month's largest and smallest class contributors, diverging."""
    if fig.empty:
        return _placeholder(fig.title, p)
    last = len(fig.dates) - 1
    items = [(n, fig.components[n][last]) for n in fig.components
             if fig.components[n][last] is not None]
    items.sort(key=lambda kv: kv[1], reverse=True)
    pos = [it for it in items if it[1] >= 0][:top]
    neg = [it for it in items if it[1] < 0][-top:]
    sel = list(reversed(pos + neg))
    names = [n.split(": ", 1)[1] if ": " in n else n for n, _ in sel]
    vals = [v for _, v in sel]
    colours = [group_colour(n) or (p.positive if v >= 0 else p.negative) for (n, _), v in
               zip(sel, vals)]
    month = fig.dates[last]
    figobj = go.Figure(go.Bar(x=vals, y=names, orientation="h", marker_color=colours,
                              hovertemplate="%{y}<br>%{x:.3f} pp<extra></extra>"))
    figobj.update_layout(height=height,
                         title=dict(text=f"{fig.title} · {month:%b %Y}", x=0.0,
                                    font=charts.title_font(p)),
                         paper_bgcolor=p.surface, plot_bgcolor=p.plane,
                         font=dict(color=p.ink, size=12), margin=dict(l=240, r=24, t=46, b=54),
                         showlegend=False)
    figobj.update_xaxes(title=dict(text="Contribution to monthly CPI (pp, original)"),
                        gridcolor=p.grid, zeroline=True, zerolinecolor=p.baseline,
                        zerolinewidth=1, tickformat=".2f")
    figobj.update_yaxes(gridcolor=p.grid, automargin=True)
    return charts.apply_vintage(figobj, vintage, p)
