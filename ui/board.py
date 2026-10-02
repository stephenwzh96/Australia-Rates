"""The Monetary Policy Board as the meeting's roster: seated once per meeting
per session from rba.gov.au, before any tab reads it.

Every tab that reads the roster -- the Vote count, the SMP tab's named dots,
the Trade recs brief, the model behind the tape -- should see the nine people
who actually vote. Seating here, at the top of the run, rather than inside the
Vote count tab means a meeting opened straight onto another tab is not read
with blank seats. The seating itself is `core.roster.seat_board`.
"""

from __future__ import annotations

import streamlit as st

from core import roster as R
from state import store
from ui import sources as src


def seat(S: dict, key: str) -> tuple[list[str], list[str]]:
    """Seat this meeting's roster once per session; (joined, left) from the
    first seating, for the Vote count tab to report."""
    flag, changes = f"_roster_seated_{key}", f"_board_changes_{key}"
    if not st.session_state.get(flag):
        rows = R.normalize_roster(S.get("roster") or store.load_roster())
        rows, joined, left = R.seat_board(rows, src.fetch_board())
        S["roster"] = rows
        st.session_state[flag] = True
        st.session_state[changes] = (joined, left)
        if joined or left:
            from ui.context import mark_dirty
            mark_dirty()
    return st.session_state.get(changes, ([], []))
