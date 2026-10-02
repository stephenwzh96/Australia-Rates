"""Vote count: the arithmetic, where the Board sits, bloc means, how the Board
has actually voted, the roster, and the speech roundup that updates it.

The US tab, on the Monetary Policy Board. Two differences carry through:
every member votes at every meeting (no rotation, so no voter/non-voter
split), and the RBA publishes each decision's vote split but never who cast
which vote -- so the published band below is a count to read your roster
against, never a tally to score it by name."""

from __future__ import annotations

import streamlit as st

from core import roster as R
from core import rotation
from state import store
from ui import bloc_icons
from ui import charts
from ui import components as C
from ui import research
from ui import sources as src
from ui.context import Ctx, mark_dirty
from ui.theme import pnl_colour

ACTION_WORD = {"hike": "Hike", "hold": "Hold", "cut": "Cut"}


def render(ctx: Ctx) -> None:
    S = ctx.S
    year = ctx.M.meeting.end.year
    rows = R.normalize_roster(S.setdefault("roster", store.load_roster()))
    # Once per meeting per session: the seating is session state, never written
    # into the meeting file itself.
    seated = f"_roster_seated_{ctx.key}"
    if not st.session_state.get(seated):
        rows, joined, left = R.seat_board(rows, src.fetch_board())
        st.session_state[seated] = True
        st.session_state[f"_board_changes_{ctx.key}"] = (joined, left)
        if joined or left:
            mark_dirty()
    S["roster"] = rows

    count_band, standing_band, voted_band, roster_band, roundup_band = (
        st.container() for _ in range(5))
    with roster_band:
        st.divider()
        _roster_editor(ctx, year)
    M = ctx.rebuild()
    with count_band:
        _count(ctx)
    with standing_band:
        st.divider()
        _standing(ctx, year)
    with voted_band:
        st.divider()
        _published(ctx)
    with roundup_band:
        st.divider()
        names = [str(r.get("name", "")).strip() for r in S["roster"] if str(r.get("name", "")).strip()]
        research.speech_roundup(ctx, names, set(M.arithmetic.non_voters))


def _roster_editor(ctx: Ctx, year: int) -> None:
    S = ctx.S
    rows = S["roster"]
    n_movers = sum(1 for r in rows if r.get("is_mover"))
    head, sort_col = st.columns([3, 1], vertical_alignment="bottom")
    with head:
        C.section("Roster", f"{len(rows)} members · all vote · {n_movers} move candidates · "
                            "everything above is computed from this table")

    def hawk_key(r: dict) -> tuple:
        sc = R.active_score(r)
        return (0 if sc is not None else 1, -(sc or 0.0), str(r.get("name", "")).lower())

    ordered = sorted(rows, key=hawk_key)
    with sort_col:
        if st.button("Sort by hawkishness", width="stretch", disabled=rows == ordered,
                     help="Strongest hawk on top. One-off; the table does not re-sort as "
                          "you edit."):
            S["roster"] = ordered
            mark_dirty()
            st.rerun()

    joined, left = st.session_state.get(f"_board_changes_{ctx.key}", ([], []))
    if joined or left:
        C.note("Seated from rba.gov.au: "
               + "; ".join(([f"joined {', '.join(joined)}"] if joined else [])
                           + ([f"left {', '.join(left)}"] if left else []))
               + ". Save to keep it on this meeting.")
    if rows and all(R.active_score(r) == 2.5 for r in rows):
        st.info("Every score is the neutral 2.5 placeholder. Set them from each member's own "
                "speeches and testimony — or run the speech roundup below and write its marks "
                "back — before the standing chart and bloc means mean anything.")

    score = dict(min_value=0.0, max_value=5.0, step=0.1, format="%.1f")
    last = R.fomc_label(store.baseline_key(S, ctx.key))
    history = S.get("score_history") or []
    with st.expander("Edit members, blocs and scores", expanded=False):
        shown: list[str] = []
        if history:
            shown = st.pills(
                "Earlier meetings", [h["fomc"] for h in history if h.get("fomc")],
                selection_mode="multi",
                format_func=lambda k: R.fomc_label(k, "%b %Y"), key="roster_history_cols",
                help="Scores archived each time a meeting rolled forward. Read-only; shown "
                     "left of Score @ " + last + ".") or []
        past = {R.history_col(k): st.column_config.NumberColumn(
                    "@ " + R.fomc_label(k, "%b %y").removesuffix(" RBA"), width=72,
                    format="%.1f", help=f"Score @ {R.fomc_label(k, '%b %Y')}, archived.")
                for k in sorted(shown)}
        edited = st.data_editor(
            R.with_vote_flag(rows, year, history, tuple(shown)), num_rows="dynamic",
            width="stretch", key=f"roster_editor_{ctx.key}",
            height=min(38 + 35 * (len(rows) + 1), 720),
            disabled=["votes", "trend", *past],
            column_order=("name", "role", "bloc1", "bloc2", *past, "score1", "trend",
                          "score2", "is_mover", "note"),
            column_config={
                "name": st.column_config.TextColumn("Member", width=160),
                "role": st.column_config.TextColumn("Seat", width=165,
                                                    help=rotation.rotation_note(year)),
                "bloc1": st.column_config.TextColumn(f"Bloc @ {last}", width=130),
                "bloc2": st.column_config.TextColumn(
                    "Bloc now", width=120,
                    help="The read the tally and bloc means use. Seeded with the seat group "
                         "(Executive, Ex officio, External); relabel as Hawks, Doves, Centre "
                         "or Data dependent once you have a read."),
                **past,
                "score1": st.column_config.NumberColumn(
                    f"Score @ {last}", width=110, **score,
                    help="Carried from that meeting's Score now when this one was opened. "
                         "Speech marks are measured from here."),
                "trend": st.column_config.TextColumn("vs last", width=64),
                "score2": st.column_config.NumberColumn(
                    "Score now", width=90, **score,
                    help="0 = most dovish, 5 = most hawkish. Drives the vote model and dot "
                         "order. Rolls into Score @ last meeting when the next one opens."),
                "is_mover": st.column_config.CheckboxColumn("Move candidate", width=115),
                "note": st.column_config.TextColumn("Note", width="large"),
            },
            on_change=mark_dirty)
    S["roster"] = R.without_vote_flag(edited)


def _count(ctx: Ctx) -> None:
    M, P = ctx.M, ctx.P
    a, d = M.arithmetic, M.decomposition
    C.section(f"Scenario: the bloc votes for the {M.direction}",
              "a bloc of dissenters is not a decision")
    C.board([C.Fig("Voters", f"{a.total_voters}", "every member votes"),
             C.Fig("Majority needed", f"{a.votes_needed}", "Governor holds a casting vote"),
             C.Fig("Move candidates", f"{a.mover_count}"),
             C.Fig("Must recruit", f"{a.shortfall}", f"from {a.recruitable}")])

    left, right = st.columns([1, 1.25], gap="large")
    with left:
        C.heading("Decomposition", level="sub", help=(
            r"q = P(\text{bloc moves}) \times P(\text{centre joins} \mid \text{bloc moves})",
            "Separates the observable (the bloc, readable from speeches) from the decisive "
            "(the centre, which settles the contract).",
            f"{d.p_bloc:.0%} x {d.p_centre:.0%} = {d.q:.1%}", "decomp"))
        C.table(["Component", "Value"],
                [["P(bloc votes for the move)", f"{d.p_bloc:.0%}"],
                 ["P(centre joins | bloc moves)", f"{d.p_centre:.0%}"],
                 ["q (decomposed)", f"{d.q:.1%}"],
                 ["q (recorded)", "—" if d.override_q is None else f"{d.override_q:.1%}"]],
                numeric=(1,), mark_rows=[2])
        if d.override_q is not None:
            if d.reconciles:
                C.good("✓ Reconciles.", P)
            else:
                st.warning("The number you are sizing on is not the number your vote read supports.")
    with right:
        if not M.priced:
            C.sub("Conditional EV", "not priced")
            C.note("Enter the points priced in the sidebar to see EV across P(centre joins).")
            return
        C.sub("Conditional EV", "given the bloc already voted yes")
        marked = [i for i, r in enumerate(M.conditional)
                  if r.is_breakeven or abs(r.p_centre - d.p_centre) < 1e-9]
        C.table(["P(centre joins)", "EV", ""],
                [[f"{r.p_centre:.0%}", f"{r.ev:+.2f}bp",
                  "breakeven" if r.is_breakeven else ("assumed" if abs(r.p_centre - d.p_centre) < 1e-9 else "")]
                 for r in M.conditional],
                numeric=(1,), mark_rows=marked,
                colours={(i, 1): pnl_colour(r.ev, P) for i, r in enumerate(M.conditional)})
        C.note(f"Breakeven is again {M.summary.breakeven:.0%} — a property of the payoff, "
               "invariant to the scenario.")


def _standing(ctx: Ctx, year: int) -> None:
    S, P = ctx.S, ctx.P
    last = R.fomc_label(store.baseline_key(S, ctx.key))
    records = []
    for r in S["roster"]:
        name = str(r.get("name", "")).strip()
        s2 = R.active_score(r)
        if not name or s2 is None:
            continue
        records.append({"name": name, "role": str(r.get("role", "") or ""), "voter": True,
                        "s1": R.num_or_none(r.get("score1")), "s2": s2,
                        "mover": bool(r.get("is_mover")), "bloc": R.active_bloc(r)})
    records.sort(key=lambda x: (-x["s2"], x["name"].lower()))
    C.section("Where the Board sits",
              f"{len(records)} member{'' if len(records) == 1 else 's'} · strongest hawk on top")

    chart_col, bloc_col = st.columns([1.35, 1], gap="large")
    with chart_col:
        if records:
            photos = src.fetch_board_avatars(tuple(sorted(x["name"] for x in records)))
            for x in records:
                x["avatar"] = photos.get(x["name"]) or C.monogram_uri(x["name"], x["s2"], P)
            st.altair_chart(charts.hawk_dove_transition(records, P), width="stretch", theme=None)
            C.legend([("move candidate", P.accent, "✓"),
                      (f"more hawkish vs {last}", P.negative),
                      (f"more dovish vs {last}", P.positive)])
            # One entry per bloc as its icon reads it: "Hawks" and "hawks " are one bloc.
            blocs: dict[str, str] = {}
            for x in records:
                if x["bloc"].strip():
                    blocs.setdefault(x["bloc"].strip().lower(), x["bloc"].strip())
            C.legend([(b, P.ink_secondary, icon) for b in blocs.values()
                      if (icon := bloc_icons.icon_uri(b, P.ink_secondary))])
            C.note(f"Hollow dot = Score @ {last}, filled = Score now. Portraits from rba.gov.au.")
        else:
            C.note("No scored members yet.")
    with bloc_col:
        rows, notes = R.bloc_summary(S["roster"], year, voters_only=False)
        C.sub("Bloc means", "all members")
        if not rows:
            C.note("No scored members yet.")
            return
        C.table(["Bloc", "n", f"@ {last}", "Now", "Δ", "Spread"], rows,
                numeric=(1, 2, 3, 4), mark_rows=[len(rows) - 1])
        C.note("Spread is the range, not an SD. 'All members' averages members, not bloc means.")
        for note in notes:
            C.note(note)


def _published(ctx: Ctx) -> None:
    """The Board's own record: every decision since it first sat, with the vote
    split the statement published. The FOMC names its dissenters; the RBA only
    counts them, so this is the one hard number a roster read answers to."""
    P, M = ctx.P, ctx.M
    decisions = [d for d in src.fetch_board_votes(ctx.as_of) if d.when <= ctx.as_of]
    C.heading("How the Board has voted", "published, unattributed · since March 2025", help=(
        r"\text{split} = n_\text{for} \text{–} n_\text{against}",
        "Each decision statement ends with the vote: \"unanimous\", or \"made by majority: "
        "five members voted to increase …; four members voted to leave … unchanged\". No "
        "member is ever named. Read your roster against the count — a 5–4 the roster calls "
        "8–1 is a read that has missed a camp — but it can never be checked name by name.",
        "rba.gov.au decision statements", "published-votes"))
    voted = [d for d in decisions if d.votes]
    if not voted:
        C.note("No published vote could be read from rba.gov.au.")
        return
    latest = voted[-1]
    split_votes = [d for d in voted if d.dissents]
    last_split = split_votes[-1] if split_votes else None
    C.board([
        C.Fig("Last decision", f"{ACTION_WORD[latest.action]} · {latest.level:.2f}%",
              f"{latest.when:%d %b %Y}"),
        C.Fig("Vote", latest.split, "unanimous" if latest.unanimous else "by majority",
              None if latest.unanimous else P.warning),
        C.Fig("Split decisions", f"{len(split_votes)} of {len(voted)}", "meetings with a vote"),
        C.Fig("Last dissent",
              "—" if last_split is None else f"{last_split.when:%b %y} · {last_split.split}",
              "" if last_split is None else f"{last_split.dissent_side or 'direction not given'}",
              None if last_split is None else (P.negative if last_split.dissent_side == "hawkish"
                                               else P.positive))])
    chart_col, table_col = st.columns([1.35, 1], gap="large")
    with chart_col:
        steps, squares = charts.published_votes(decisions, P)
        st.altair_chart(steps, width="stretch", theme=None)
        st.altair_chart(squares, width="stretch", theme=None)
        C.legend([("hike", P.negative), ("hold", P.muted), ("cut", P.positive),
                  ("dissent (outlined)", P.ink)])
    with table_col:
        rows, colours = [], {}
        for i, d in enumerate(reversed(decisions)):
            move = ("—" if d.action == "hold" else f"{d.change_bp:+d}bp")
            dissent = ("" if not d.dissents else
                       f"{d.dissents} {d.dissent_side}".strip())
            rows.append([f"{d.when:%d %b %Y}", ACTION_WORD[d.action], move, f"{d.level:.2f}",
                         d.split if d.votes else "not published", dissent or "—"])
            if d.dissent_side:
                colours[(i, 5)] = P.negative if d.dissent_side == "hawkish" else P.positive
        C.table(["Meeting", "Decision", "Move", "Cash", "Vote", "Dissent"], rows,
                numeric=(2, 3), colours=colours)
    a = M.arithmetic
    C.note(f"Your roster: {a.mover_count} move candidate{'' if a.mover_count == 1 else 's'} "
           f"for a {M.direction}, {a.votes_needed} needed. The Board last split "
           + ("never" if last_split is None else
              f"{last_split.split} in {last_split.when:%B %Y} ({last_split.votes.get(last_split.action, 0)} "
              f"for the {last_split.action}, {last_split.dissents} {last_split.dissent_side or 'against'})")
           + ". The vote has been published since July 2025; earlier Board decisions show "
             "\"not published\".")
