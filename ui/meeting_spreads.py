"""The meeting-spread matrix as a Streamlit panel."""

from __future__ import annotations

import html

import streamlit as st

from core import meeting_spreads as ms

from . import components as C
from .theme import pnl_colour, tint


def render(ctx, interbank: bool) -> None:
    """The matrix for the Curve tab's current view: one row per RBA meeting off
    the IB strip, or one per bill contract off the IR strip."""
    spot = ctx.spot
    if interbank:
        mx = ms.from_strip(ctx.path) if ctx.path is not None else ms.SpreadMatrix([], "meeting")
        axis_note = "interbank cash · one row per meeting"
    else:
        mx = (ms.from_bills(ctx.bills, float(spot)) if ctx.bills is not None and spot is not None
              else ms.SpreadMatrix([], "contract", spot))
        axis_note = "90 day bank bills · one row per contract"
    C.section("Meeting spread matrix", axis_note)
    meeting_spread_matrix(mx, ctx.P, "msx")


def meeting_spread_matrix(mx, p, key: str) -> None:
    """Every forward against every other, as a grid."""
    if not mx.ok:
        C.note("No forward path to spread yet -- the strip has to solve first.")
        return

    with C.bar(f"{key}_controls"):
        h1, h2, h3, h4, _ = st.columns([1, 1, 1, 1, 1], vertical_alignment="center")
    with h1:
        heat = st.toggle("Heatmap", value=True, key=f"{key}_heat",
                         help="Shade each cell by the size of the spread. Off, "
                              "the sign moves to the number's own colour.")
    with h2:
        half = C.choice(
            "Half", ["Full matrix", "Upper only"], default="Full matrix",
            key=f"{key}_half", label_visibility="collapsed",
            help="The grid is antisymmetric -- the lower triangle is the upper "
                 "one negated. 'Upper only' drops the mirror.")
    with h3:
        dp = C.choice(
            "Precision", ["1dp", "2dp"], default="2dp", key=f"{key}_dp",
            label_visibility="collapsed")
    with h4:
        span = st.selectbox(
            "Span", ["All", "Nearest 10", "Nearest 8", "Nearest 6"],
            key=f"{key}_span", label_visibility="collapsed", width=130,
            help="How far down the strip to carry the grid.")

    mx = mx.head(None if span == "All" else int(span.split()[-1]))
    nodes = mx.nodes
    digits = 1 if dp == "1dp" else 2

    _fs, _em = mx.first_spread, mx.end_move
    _pk, _tr = mx.peak, mx.trough
    C.board([
        C.Fig("1st spread", f"{_fs:+.2f}bp" if _fs is not None else "--", "cash to the front",
              help="Spot against the nearest forward -- what is priced for the next "
                   "decision, before anything compounds."),
        C.Fig("Cycle end move", f"{_em:+.2f}bp" if _em is not None else "--",
              "cash to the back",
              help="Spot against the last forward still printing. The same number the "
                   "ladder calls the total priced move -- a stale back contract stays on "
                   "the grid, marked, but does not end the cycle."),
        C.Fig("Cycle peak", f"{_pk.rate:.4f}%" if _pk else "--", _pk.label if _pk else ""),
        C.Fig("Cycle trough", f"{_tr.rate:.4f}%" if _tr else "--", _tr.label if _tr else ""),
    ])

    def _head(n) -> str:
        flag = "" if n.reliable else " !"
        return (f'<div style="line-height:1.3;white-space:nowrap">{html.escape(n.label)}'
                f'{flag}<br><span style="opacity:.62;font-weight:400">'
                f'{n.rate:.4f}%</span></div>')

    headers = ["Row \\ Col", "Contract"] + [_head(n) for n in nodes]
    rows, colours, fills = [], {}, {}
    extreme = mx.extreme_bp
    for i, row_node in enumerate(nodes):
        cells = [_head(row_node), row_node.tag + ("" if row_node.reliable else " !")]
        for j in range(len(nodes)):
            if half == "Upper only" and j <= i:
                cells.append("")
                continue
            v = mx.bp(i, j)
            if i == j:
                cells.append(f"{0:.{digits}f}")
                colours[(i, j + 2)] = p.muted
                continue
            cells.append(f"{v:+.{digits}f}")
            if heat and extreme:
                a = 0.05 + 0.33 * min(1.0, abs(v) / extreme)
                fills[(i, j + 2)] = tint(pnl_colour(v, p), a)
            elif not heat:
                colours[(i, j + 2)] = pnl_colour(v, p)
        rows.append(cells)

    C.table(headers, rows, numeric=tuple(range(2, len(nodes) + 2)),
            colours=colours, cell_bg=fills, nowrap=True)

    C.legend([("hike (+bp)", p.positive), ("cut (-bp)", p.negative),
              ("diagonal / self", p.muted)])
    axis = ("one row per RBA meeting, at the cash rate the IB strip solves for it"
            if mx.axis == "meeting" else
            "one row per IR contract, at the 3-month BBSW it prices for its fix "
            "-- a bill spans 90 days, about one and a half meetings, so this axis "
            "is the contract, not the meeting, and every cell off spot carries "
            "the bill's credit spread over cash")
    C.note(f"Spread = (column rate - row rate) x 100bp. Read a row as "
           f"\u201cshort this, long that\u201d: {axis}. "
           + ("A ! marks a rate solved off a contract that is not printing; it "
              "still shows, but it sets neither the peak nor the trough."
              if any(not n.reliable for n in nodes) else ""))
