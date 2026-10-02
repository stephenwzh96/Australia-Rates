"""The IR scenario grid: each bank bill contract's settlement price under a
ladder of policy paths, against where it trades now. The US version builds the
same grid for SR3; the settlement maths is in `core.bill_scenarios`."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core import bill_scenarios as sc
from core.contracts import effective_date
from ui import components as C
from ui.context import Ctx
from ui.theme import pnl_colour, tint

#: Default ladder depth: a move at the first meeting, the first two, ... four.
LADDER_DEPTH = 4
#: Contracts the grid opens on: the front one still to fix and the next five.
DEFAULT_CONTRACTS = 6

_SHOW_HELP = (
    "**Price**: what each contract settles at if the row's path is delivered.\n\n"
    "**vs market**: that price less where the contract trades now, in bp.\n"
    "- Negative: the contract falls if the path happens. The market prices less "
    "than the path; in rate terms the path sits that many bp above the market "
    "(payers gain).\n"
    "- Positive: the contract rallies (receivers gain).\n\n"
    "The BBSW − cash basis moves every row one for one: set it at 0 and the bill "
    "sits on the cash path, which makes the market look further from every path "
    "than it is.")


def _basis_default(cv, spot_basis_bp: float | None) -> tuple[float, str]:
    """The basis the grid opens on and where it came from: the mean basis the
    strip prices across the windows both strips cover, else today's observed
    BBSW-cash spread -- rounded to the box's 0.5bp step."""
    mb = cv.mean_basis_bp
    if mb is not None:
        value = round(mb * 2) / 2 + 0.0
        return value, (f"Opens on the mean basis the strip prices, {mb:+.1f}bp across "
                       f"{len(cv.comparable_periods)} windows, rounded to {value:+.1f}bp. At "
                       "this value the IB path row sits close to the market.")
    if spot_basis_bp is not None:
        value = round(spot_basis_bp * 2) / 2 + 0.0
        return value, (f"No window is covered by both strips, so it opens on today's observed "
                       f"3-month BBSW less the cash target, {spot_basis_bp:+.1f}bp.")
    return 0.0, "No basis available; 0 assumed."


def bill_grid(ctx: Ctx) -> None:
    P, as_of, cv = ctx.P, ctx.as_of, ctx.bills
    C.heading("Scenario prices", "what each contract settles at if the path is delivered",
              help=(r"P = 100 - \Big(\frac{1}{90}\sum_{d=\text{fix}}^{\text{fix}+89} r_d"
                    r" + \text{basis}\Big)",
                    "Each row is a policy path: the move at each meeting, in bp. An IR "
                    "contract settles on one 3-month BBSW fix, and 3-month BBSW is the cash "
                    "rate expected over the bill's own 90 days plus a bank credit spread. "
                    "So each day of the bill's span takes today's cash rate plus every move "
                    "already in force that day, the 90 days are averaged, and the basis is "
                    "added. A meeting inside the span moves the fix only for the days after "
                    "it takes effect; one after the fix not at all.",
                    "base = spot cash · move counts from the day after the decision · "
                    "simple ACT/365 day-weighted mean over the bill's 90 days", "ir-scenarios"))

    spot = ctx.spot
    if spot is None:
        C.note("Type a spot cash rate in the control bar to price the scenarios.")
        return
    contracts_all = [sc.Contract(p.code, p.fix_date, p.covers_start, p.covers_end, p.price)
                     for p in cv.periods if p.fix_date > as_of]
    if not contracts_all:
        C.note("No IR contract is still to fix.")
        return

    direction = str(ctx.trade.get("direction") or "hike")
    basis_default, basis_why = _basis_default(cv, ctx.market.spot.get("spot_basis_bp"))
    with C.bar("ir_scenarios"):
        c1, c2, c3, c4, _ = st.columns([0.8, 0.85, 0.9, 1.15, 1.3], vertical_alignment="bottom")
    with c1:
        size = st.number_input("Move size (bp)", min_value=1.0, max_value=sc.MAX_MOVE_BP,
                               value=float(ctx.trade.get("size") or 25.0), step=5.0,
                               format="%.0f", key="ir_scn_size", width=100)
    with c2:
        way = C.choice("Direction", ["Hike", "Cut"],
                       default="Cut" if direction == "cut" else "Hike", key="ir_scn_way")
    with c3:
        # Keyed on the default, so a new strip re-seeds the box rather than
        # holding a number typed against yesterday's market.
        basis = st.number_input(
            "BBSW − cash (bp)", value=basis_default, step=0.5, format="%.1f",
            key=f"ir_scn_basis_{basis_default:.1f}", width=110,
            help=basis_why + " Held constant to every fix; each scenario price moves 1bp lower "
                             "for each 1bp more basis.")
    with c4:
        show = C.choice("Show", ["Price", "vs market (bp)"], default="Price",
                        key="ir_scn_show", help=_SHOW_HELP)

    codes = [c.code for c in contracts_all]
    picked = st.multiselect("Contracts", codes, default=codes[:DEFAULT_CONTRACTS],
                            key=f"ir_scn_contracts_{'-'.join(codes)}",
                            help=f"Columns of the grid. Opens on the next {DEFAULT_CONTRACTS} "
                                 "contracts still to fix; every live quarterly IR is offered.")
    contracts = [c for c in contracts_all if c.code in picked]
    if not contracts:
        C.note("Pick at least one contract.")
        return

    horizon = max(c.end for c in contracts)
    future = [m for m in ctx.meetings if as_of < effective_date(m.end) < horizon]
    if not future:
        C.note("No RBA decision falls before the last picked contract's bill matures: every "
               "row would be the same.")
        return

    sign = -1 if way == "Cut" else 1
    names = {m.key: m.end.strftime("%d %b %y") for m in future}
    seed_key = f"ir_scn_rows_{ctx.key}_{'-'.join(names)}_{size:g}_{sign}"
    ladder = sc.default_paths(future, size, sign, LADDER_DEPTH)
    # Float columns with NaN for "no move", so a blank cell renders blank and
    # stays numeric to edit.
    seed = pd.DataFrame([{names[k]: (v or float("nan")) for k, v in row.items()}
                         for row in ladder], columns=list(names.values()), dtype=float)
    r1, r2 = st.columns([1, 0.16], vertical_alignment="bottom")
    with r1:
        C.note(f"Paths: the bp moved at each meeting, one path per row (an empty cell is no "
               f"move; type any size, either sign, to mix hikes and cuts). Rows can be added "
               f"or deleted. Seeded with the {way.lower()} ladder over the next "
               f"{min(LADDER_DEPTH, len(future))} meetings.")
    with r2:
        if st.button("Reset", key="ir_scn_reset", help="Back to the default ladder."):
            st.session_state.pop(seed_key, None)
    edited = st.data_editor(
        seed, key=seed_key, num_rows="dynamic", hide_index=True, width="stretch",
        column_config={n: st.column_config.NumberColumn(
            n, step=float(size), format="%+.0f", min_value=-sc.MAX_MOVE_BP,
            max_value=sc.MAX_MOVE_BP, help=f"bp moved at the {n} decision")
            for n in names.values()})

    by_name = {v: k for k, v in names.items()}
    paths: list[sc.Path] = []
    for _, row in edited.iterrows():
        moves = {by_name[col]: sc.clean_move(row.get(col)) for col in by_name}
        moves = {k: v for k, v in moves.items() if v}
        paths.append(sc.Path(moves, sc.path_label(moves, future, size)))
    if not paths:
        C.note("Add a path to price.")
        return

    grid = sc.build(contracts, [sc.Path({}, "No change")] + paths, future, float(spot), basis,
                    as_of)
    if not grid.ok:
        C.note(grid.note or "Nothing to price.")
        return

    by_code = {p.code: p for p in cv.periods}
    head = ["Path"] + [f'<span title="fix {c.fix:%d %b} · bill {c.start:%d %b} – '
                       f'{c.end:%d %b %y}">{c.code}</span>' for c in grid.contracts]
    rows, colours, marks = [], {}, []
    rows.append(["Market"] + [f"{c.market:.3f}" if c.market is not None else "–"
                              for c in grid.contracts])
    marks.append(0)
    ib_cells = []
    for c in grid.contracts:
        per = by_code.get(c.code)
        ib_cells.append(None if per is None or not per.comparable or per.expected is None
                        else 100.0 - (per.expected + basis / 100.0))
    if any(v is not None for v in ib_cells):
        rows.append(["IB path"] + [
            "–" if v is None else (f"{v:.3f}" if show == "Price"
                                   else f"{(v - c.market) * 100:+.1f}" if c.market else "–")
            for v, c in zip(ib_cells, grid.contracts)])
    # In each contract's column, the path whose price sits nearest the market:
    # what the strip is pricing, read straight off the grid.
    nearest: dict[int, int] = {}
    for j, c in enumerate(grid.contracts):
        gaps = [(abs(r.cells[j].price - c.market), i) for i, r in enumerate(grid.rows)
                if c.market is not None and r.cells[j].price is not None]
        if gaps:
            nearest[j] = min(gaps)[1]
    first_path = len(rows)
    cell_bg = {(first_path + i, j + 1): tint(P.select, 0.16) for j, i in nearest.items()}
    for row in grid.rows:
        cells = []
        for j, (cell, c) in enumerate(zip(row.cells, grid.contracts)):
            if show == "Price":
                cells.append("–" if cell.price is None else f"{cell.price:.3f}")
            else:
                d = cell.vs(c.market)
                cells.append("–" if d is None else f"{d:+.1f}")
                if d is not None:
                    colours[(len(rows), j + 1)] = pnl_colour(d, P)
        rows.append([row.label] + cells)
    C.table(head, rows, numeric=tuple(range(1, len(head))), colours=colours, mark_rows=marks,
            cell_bg=cell_bg, nowrap=True)

    C.note(f"Base: spot cash {float(spot):.3f}% plus {basis:+.1f}bp BBSW − cash, held flat to "
           "every fix. "
           + ("vs market: positive = the contract rallies (receivers gain). " if show != "Price"
              else "")
           + "IB path = the interbank strip's path averaged over each bill's 90 days plus the "
             "same basis. Shaded: the path nearest the market in each contract. Hover a "
             "contract for its fix and span.")
