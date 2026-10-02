"""Sizing: sensitivity to q and the Kelly ladder in DV01, lots and dollars."""

from __future__ import annotations

import streamlit as st

from core import sizing
from ui import charts
from ui import components as C
from ui.context import Ctx
from ui.theme import pnl_colour


def render(ctx: Ctx) -> None:
    M, P = ctx.M, ctx.P
    if not M.priced:
        st.info("Price the trade first.")
        return
    s = M.summary
    C.section("Sensitivity to your probability",
              "q is the only input you control" if M.moves is None
              else "how the position responds to being wrong about how often it wins")
    if M.moves is not None:
        C.note(f"In moves mode q is the chance the position wins at all: "
               f"{M.moves.moves_you:g} moves against {M.moves.moves_priced:.2f} priced clears "
               f"breakeven {M.moves.p_win:.1%} of the time.")
    chart_col, table_col = st.columns([1.6, 1], gap="medium")
    with chart_col:
        st.altair_chart(charts.ev_curve(M.points, M.size, s.q, M.side, P),
                        width="stretch", theme=None)
    with table_col:
        mine = [abs(r.q - s.q) < 1e-9 for r in M.sensitivity]
        C.table(["Your q", "EV (bp)", ""],
                [[f"{r.q:.0%}", f"{r.ev:+.2f}",
                  "breakeven" if r.is_breakeven else ("yours" if m else "")]
                 for r, m in zip(M.sensitivity, mine)],
                numeric=(1,), mark_rows=[i for i, (r, m) in enumerate(zip(M.sensitivity, mine))
                                         if r.is_breakeven or m],
                colours={(i, 1): pnl_colour(r.ev, P) for i, r in enumerate(M.sensitivity)})
        C.note(f"{s.margin * 100:.1f}pp between your estimate ({s.q:.0%}) and breakeven "
               f"({s.breakeven:.0%}): your margin of safety.")

    lad = M.sizing_ladder
    worked = "price the trade first"
    if lad and lad.ok and lad.selected:
        r = lad.selected
        worked = (f"loss$   = {r.f:.2%} x A${M.max_drawdown:,.0f} = A${r.max_loss:,.0f}\n"
                  f"DV01pos = A${r.max_loss:,.0f} / {lad.loss_bp:g}bp = A${r.position_dv01:,.0f} per bp\n"
                  f"lots    = A${r.position_dv01:,.0f} / A${M.dv01:,.2f} = {r.contracts:,}")
    C.heading("How big, in DV01, lots and dollars",
              f"Kelly on A${M.max_drawdown:,.0f} drawdown · A${M.daily_limit:,.0f} daily veto · "
              f"{M.contract_label} at A${M.dv01:,.2f}/bp/lot",
              help=(r"\text{loss}_\$ = f \times \text{drawdown} \Rightarrow "
                    r"\text{DV01}_{pos} = \frac{\text{loss}_\$}{\text{loss}_{bp}} \Rightarrow "
                    r"\text{lots} = \frac{\text{DV01}_{pos}}{\text{DV01}}",
                    "f is a share of the bankroll, so it multiplies dollars — never DV01.",
                    worked, "sizing"))

    if not lad or not lad.ok:
        st.warning((lad.error if lad else "Sizing unavailable.")
                   + " Kelly only sizes a position with positive expected value.")
        return
    sel = lad.selected
    if sel and not sel.within_daily_limit:
        fits = lad.largest_within_limit
        st.error(f"**{sel.label} Kelly breaches the daily limit.** It risks A${sel.max_loss:,.0f} "
                 f"against A${M.daily_limit:,.0f} ({sel.contracts:,} lots vs a "
                 f"{lad.daily_cap_contracts:,}-lot ceiling)."
                 + (f" Largest rung that fits: **{fits.label}**." if fits else ""))

    g1, g2 = st.columns(2, gap="medium")
    with g1:
        C.sub("Kelly growth curve")
        st.altair_chart(charts.kelly_growth(lad, P), width="stretch", theme=None)
        C.note("Flat approaching the peak, a cliff past it: under-betting is cheap.")
    with g2:
        C.sub("Capital at risk vs Kelly growth")
        st.altair_chart(charts.capital_at_risk(lad, P), width="stretch", theme=None)
        C.note("What the next slice of growth costs. Rungs the daily limit forbids are hollow.")

    rows, colours, marks = [], {}, []
    for i, r in enumerate(lad.rungs):
        if r.is_selected:
            marks.append(i)
        rows.append([r.label, f"{r.f:.1%}", f"A${r.max_loss:,.0f}", f"A${r.position_dv01:,.0f}",
                     f"{r.contracts:,}", f"A${r.expected_pnl:,.0f}", f"A${r.max_gain:,.0f}",
                     f"{r.growth * 100:.2f}%", f"{r.growth_share:.1%}", f"{r.annualised:.1%}",
                     r.status,
                     "—" if r.delta_loss is None else f"+A${r.delta_loss:,.0f}",
                     "—" if r.growth_per_10k is None else f"{r.growth_per_10k * 100:.1f}pp"])
        colours.update({(i, 2): P.negative, (i, 5): P.positive, (i, 6): P.positive,
                        (i, 10): P.good if r.within_daily_limit else P.critical})
    C.sub("The rung ladder")
    C.table(["Kelly", "f", "Max loss", "DV01 risk/bp", "Lots", "Expected A$", "Max gain",
             "g/bet", "% of full", "Annualised", "Daily", "Extra risk", "Growth / A$10k"],
            rows, numeric=(1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12), mark_rows=marks, colours=colours)
    steps = "  ".join(v for v in (sizing.step_up_verdict(a, b)
                                   for a, b in zip(lad.rungs, lad.rungs[1:])) if v)
    C.note(f"▸ is the sidebar's rung. The daily limit allows at most {lad.daily_cap_contracts:,} "
           f"lots. Growth / A$10k falls at every step — the case for sizing below full Kelly. {steps}")
    if lad.zero_growth_f:
        C.note(f"Full Kelly is {lad.f_star:.0%} of the budget and growth hits zero at "
               f"{lad.zero_growth_f:.0%}: past that you carry maximum risk for no expected growth.")
    C.note(f"{M.contract_label} — DV01 A${M.quoted_dv01:,.2f} per lot"
           + (f", corrected to A${M.dv01:,.2f} per bp of the event ({M.capture:.1%} capture)."
              if M.capture < 1 - 1e-9 else "."))
