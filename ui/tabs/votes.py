"""Vote count: the arithmetic, the roster, and the EV once the bloc has voted."""

from __future__ import annotations

import streamlit as st

from state import store
from ui import components as C
from ui.context import Ctx, mark_dirty
from ui.theme import pnl_colour


def render(ctx: Ctx) -> None:
    M, S, P = ctx.M, ctx.S, ctx.P
    a = M.arithmetic
    C.heading("Vote arithmetic", "a bloc of dissenters is not a decision", help=(
        r"\text{need} = \lfloor n/2 \rfloor + 1",
        "The Monetary Policy Board decides by majority of the members present, the "
        "Governor holding a casting vote. Since the 2025 reforms the vote is published "
        "unattributed — \"decided by six votes to three\" — and no member is named, so the "
        "roster below is your read, never scored against a published tally.",
        f"{a.total_voters} members · {a.votes_needed} to carry", "votes"))
    C.board([
        C.Fig("Board size", f"{a.total_voters}"),
        C.Fig("Votes needed", f"{a.votes_needed}"),
        C.Fig("Movers identified", f"{a.mover_count}", f"of {a.total_voters}",
              P.accent if a.mover_count else None),
        *[C.Fig(b.bloc, f"{b.count}", "members") for b in a.blocs[:3]],
    ])
    st.warning("**The RBA never attributes a vote.** The Board publishes an unattributed "
               "count and no member is ever named, so unlike the FOMC version this roster "
               "can never be checked against a published tally. It encodes *your* read, and "
               "that read is doing all the work.")

    roster_rows = store.roster_for(S)
    if not any(r.get("name") for r in roster_rows[2:]):
        st.info("The roster is seeded with the nine statutory seats but only the two executive "
                "names are filled, and every score is a neutral 2.5 placeholder. Fill the "
                "names from rba.gov.au and set the scores from each member's own speeches.")

    C.section("Roster", f"{len(roster_rows)} members · {a.mover_count} move candidates · "
                        "everything above is computed from this table")
    # Namespaced by meeting: a bare key would carry the grid across a meeting
    # switch and zip the new meeting's rows against the old one's edits.
    key = f"roster_editor_{S.get('meeting')}"
    edited = st.data_editor(
        [{"Member": r.get("name", ""), "Role": r.get("role", ""),
          "Bloc": r.get("bloc", ""), "Hawk-dove": float(r.get("score", 2.5)),
          "Mover": bool(r.get("is_mover"))} for r in roster_rows],
        column_config={
            "Hawk-dove": st.column_config.NumberColumn(
                min_value=0.0, max_value=5.0, step=0.1,
                help="5 = most hawkish. Your read, from the member's own speeches."),
            "Mover": st.column_config.CheckboxColumn(
                help="Counted in P(bloc moves), whatever their bloc."),
        },
        hide_index=True, width="stretch", key=key)
    snap = f"_roster_snapshot_{S.get('meeting')}"
    if edited != st.session_state.get(snap):
        st.session_state[snap] = edited
        S["roster"] = [{**old, "name": row["Member"], "role": row["Role"], "bloc": row["Bloc"],
                        "score": float(row["Hawk-dove"]), "is_mover": bool(row["Mover"])}
                       for old, row in zip(roster_rows, edited)]
        mark_dirty()
        ctx.rebuild()
    C.note("Changes land in this meeting's own roster on Save, so one meeting's read never "
           "silently rewrites another's.")

    M = ctx.M
    if M.priced:
        C.section("EV given the bloc has already voted for the move")
        C.table(["P(centre joins)", "EV", ""],
                [[f"{r.p_centre:.0%}", f"{r.ev:+.2f}bp", "breakeven" if r.is_breakeven else ""]
                 for r in M.conditional], numeric=(0, 1),
                colours={(i, 1): pnl_colour(r.ev, P) for i, r in enumerate(M.conditional)})
        C.note("The breakeven is points / size again — identical to the unconditional one. "
               "That invariance is a property of the payoff, not of the scenario.")
