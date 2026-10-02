"""RBA event-pricing dashboard.

Sections live in `ui/tabs/`, every data source in `ui/sources.py`, and all
calculation in `core/`. This file wires them together -- the same shape as the
FOMC version it is benchmarked against.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from core import clock, rba_calendar
from state import store
from ui import components as C
from ui import sidebar, verdict
from ui.context import Ctx, Market
from ui.tabs import curve, data, inflation, labour, macro, path, releases, sizing, votes
from ui.theme import active as active_palette

st.set_page_config(page_title="RBA event pricing", page_icon="◷", layout="wide")
C.inject_css(active_palette())

SECTIONS = {
    "Curve": curve.render,
    "Sizing": sizing.render,
    "Path": path.render,
    "Releases": releases.render,
    "Vote count": votes.render,
    "Macro": macro.render,
    "Labour": labour.render,
    "Inflation": inflation.render,
    "Data": data.render,
}
#: History windows reset to their default whenever the reader navigates into
#: the tab: widget state otherwise persists across tab switches, so a window
#: set an hour ago would still be in force with nothing saying why.
RESET_ON_ENTRY = {"Labour": ("hist_window_labour",)}
#: Where the app opens: the strip.
DEFAULT_SECTION = "Curve"


def main() -> None:
    today = clock.today()
    saved = store.load_calendar()
    meetings = ([rba_calendar.meeting_from_dict(m) for m in saved] if saved
                else list(rba_calendar.DEFAULT_MEETINGS))

    key, S, as_of = sidebar.render(meetings, today)
    ctx = Ctx(key=key, S=S, as_of=as_of, today=today, P=active_palette(), meetings=meetings,
              market=Market.fetch())
    ctx.rebuild()

    names = list(SECTIONS)
    if not st.session_state.get("section"):
        wanted = st.query_params.get("section")
        st.session_state["section"] = wanted if wanted in SECTIONS else DEFAULT_SECTION
    # The tape and the section bar stick to the top of the page together
    # (.st-key-topbar in ui/style.css), so the trade's numbers and the way to
    # the next tab stay in reach however far down a long tab is scrolled.
    with st.container(key="topbar"):
        header = st.empty()
        # Drawn now from the state as the run starts, then redrawn in place once
        # the tab has run: the tape is never blank while a slow tab computes.
        with header:
            verdict.header(ctx)
        section = C.choice("Section", names, key="section", fallback=DEFAULT_SECTION,
                           label_visibility="collapsed")
    if st.query_params.get("section") != section:
        st.query_params["section"] = section
    if st.session_state.get("_prev_section") != section:
        for k in RESET_ON_ENTRY.get(section, ()):
            st.session_state.pop(k, None)
        st.session_state["_prev_section"] = section

    left, right = st.columns([2.7, 1], gap="large")
    with left:
        SECTIONS[section](ctx)

    ctx.rebuild()
    with right:
        verdict.render(ctx, ROOT)
    with header:
        verdict.header(ctx)


main()
