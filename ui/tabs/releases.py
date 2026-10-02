"""Releases: the pre-decision economic calendar and the release-prep reaction card."""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from core import econ_calendar
from ui import components as C
from ui.context import Ctx
from ui.theme import FAMILY_NAME, FAMILY_SOLID, FAMILY_TINT, RELEASE_FAMILIES, event_family


def render(ctx: Ctx) -> None:
    calendar(ctx)
    st.divider()
    from ui import release_card
    release_card.prep_card(ctx)


# ------------------------------------------------------------------ calendar

def calendar(ctx: Ctx) -> None:
    M, P = ctx.M, ctx.P
    window_start = M.as_of - timedelta(days=econ_calendar.LOOKBACK_DAYS)
    C.heading(f"Around {M.meeting.label}",
              f"{window_start:%d %b} → {M.meeting.end:%d %b %Y} · past four months plus "
              f"{M.days_to_meeting}d to the decision" if M.days_to_meeting >= 0
              else "this meeting has already happened",
              help=(r"\text{shown} = \{\text{Labour Force, CPI, WPI, GDP, RBA}\}",
                    "Rule-dated releases: Labour Force on the Thursday of the third week, CPI on "
                    "the last Wednesday of the month (the quarterly print in Jan/Apr/Jul/Oct, "
                    "the monthly otherwise), the Wage Price Index on the third Wednesday of the "
                    "middle month of a quarter, the National Accounts on the first Wednesday "
                    "of Mar/Jun/Sep/Dec, the decisions themselves and the minutes a fortnight "
                    "after. Actual reads the latest ABS/RBA vintage for the period the report "
                    "covers.",
                    f"window: {window_start.isoformat()} to {M.meeting.end.isoformat()}",
                    "econcal"))
    if M.days_to_meeting < 0:
        return
    if not M.calendar_events:
        st.info("No rule-based release falls in this window.")
        return

    from ui import release_actuals
    actuals = release_actuals.for_events(M.calendar_events, M.as_of)
    headers = ["Date", "Out", "Release", "Tier", "Actual", "Source"]

    def table(events, muted: bool) -> None:
        rows, colours, marks, bg = [], {}, [], {}
        for i, e in enumerate(events):
            if e.when == M.meeting.end:
                marks.append(i)
            rows.append([e.when.strftime("%a %d %b"), f"{(e.when - M.as_of).days}d", e.label,
                         e.weight, actuals.get((e.when, e.release), "—" if e.when >= M.as_of
                                               else ""),
                         "confirmed" if e.source == "manual" else "rule"])
            family = event_family(e.label)
            if family:
                bg[i] = FAMILY_TINT[family]
            if muted:
                colours.update({(i, c): P.muted for c in range(len(headers))})
            else:
                colours[(i, 3)] = {1: P.critical, 2: P.serious}.get(e.weight, P.muted)
        C.table(headers, rows, numeric=(1, 3), mark_rows=marks, colours=colours, row_bg=bg)

    past = [e for e in M.calendar_events if e.when < M.as_of]
    future = [e for e in M.calendar_events if e.when >= M.as_of]
    tier1 = sorted((e for e in future if e.weight == 1), key=lambda e: e.when)
    C.eyebrow(f"Before the meeting · {len(tier1)} tier-1 print{'' if len(tier1) == 1 else 's'} "
              "to come")
    if tier1:
        counts: dict[str, int] = {}
        for e in tier1:
            counts[e.label] = counts.get(e.label, 0) + 1
        C.chips([(f"{n}× {label}", FAMILY_SOLID.get(event_family(label) or "", None))
                 for label, n in counts.items()])
    if future:
        table(future, muted=False)
    else:
        C.note("Nothing left on the calendar before the meeting.")
    if past:
        with st.expander(f"Past four months — {len(past)} realised releases"):
            table(past, muted=True)

    C.legend([(FAMILY_NAME[f], FAMILY_SOLID[f]) for f in RELEASE_FAMILIES])
    C.note("Tier 1 = market-moving, 2 = notable. Actual is the reference period at the latest "
           "vintage. \"rule\" is generated from a fixed release convention, not read off a "
           "calendar page, so a public holiday that shifts an ABS release is not accounted "
           "for; \"confirmed\" marks a calendar entry typed for this meeting.")
