"""The sidebar: meeting picker, trade inputs, sizing, your view, save/recall."""

from __future__ import annotations

from datetime import date

import streamlit as st

from core import rba_calendar, sizing
from state import store
from ui import components as C
from ui import sources as src
from ui.context import mark_dirty


def _or(value, default):
    """`value` unless it is missing. `value or default` read a saved 0% as unset
    and reopened the slider on the default."""
    return default if value is None else value


#: Widget keys that belong to one meeting's analysis and must not survive a
#: switch to another (the tabs namespace most keys by meeting; these are not).
PER_MEETING_WIDGET_KEYS = ("smp_read_editor",)


def _switch_meeting(key: str) -> None:
    st.session_state["key"] = key
    st.session_state.pop("state", None)
    st.session_state["dirty"] = False
    for k in PER_MEETING_WIDGET_KEYS:
        st.session_state.pop(k, None)


def render(meetings: list[rba_calendar.Meeting], today: date) -> tuple[str, dict, date]:
    with st.sidebar:
        if st.button("↻  Refresh all data", width="stretch",
                     help="Re-fetch both ASX strips and every RBA and ABS source."):
            src.refresh_curve_quotes()
            src.refresh_all_sources()
            st.rerun()

        key, S = _meeting(meetings, today)
        as_of = _as_of(S, today)
        t = _trade(S)
        _sizing(S, t, as_of)
        _view(S)
        st.divider()
        _save(key, S)
        _recall(key)
    return key, S, as_of


def _meeting(meetings, today: date) -> tuple[str, dict]:
    C.sub("Meeting")
    saved_keys = store.list_saved()
    options = sorted({m.key for m in meetings} | set(saved_keys))
    default = next((k for k in options if k >= today.isoformat()), options[-1])
    if "key" not in st.session_state:
        # ?meeting=YYYY-MM-DD wins, so a meeting can be bookmarked or linked.
        wanted = st.query_params.get("meeting")
        st.session_state["key"] = wanted if wanted in options else default
    labels = {m.key: m.label for m in meetings}

    key = st.selectbox(
        "Meeting", options, index=options.index(st.session_state["key"]),
        format_func=lambda k: labels.get(k, k) + ("  · saved" if k in saved_keys else "  (new)"),
        label_visibility="collapsed")
    if key != st.session_state.get("key"):
        _switch_meeting(key)
    if st.query_params.get("meeting") != key:
        st.query_params["meeting"] = key

    if "state" not in st.session_state:
        st.session_state["state"] = store.load_or_seed(
            key, [rba_calendar.meeting_to_dict(m) for m in meetings], today)
        # Every meeting opens on today, saved or not: a saved meeting is a live
        # analysis you keep reopening, and the as-of scopes every tab, so
        # restoring a stale one would silently reprice the page days back.
        st.session_state["state"]["as_of"] = today.isoformat()
        st.session_state["dirty"] = False

    S = st.session_state["state"]
    if S.pop("_recovered", False):
        st.warning("The saved file for this meeting was unreadable and has been replaced with "
                   "a fresh one. Nothing is overwritten on disk until you press Save.")
    if S.get("cloned_from"):
        st.caption(f"Started from {S['cloned_from']}. Market and path inputs cleared.")
    elif key not in saved_keys:
        st.caption("Unsaved. Roster seeded from the standing Board.")
    return key, S


def _as_of(S: dict, today: date) -> date:
    """The as-of date, and a way back to today when it has fallen behind."""
    if st.session_state.pop("_as_of_today", False):
        S["as_of"] = today.isoformat()
    as_of = st.date_input("As of", value=date.fromisoformat(S.get("as_of") or today.isoformat()),
                          on_change=mark_dirty)
    S["as_of"] = as_of.isoformat()
    behind = rba_calendar.as_of_note(as_of, today)
    if behind:
        c1, c2 = st.columns([3, 2], vertical_alignment="center")
        c1.caption(f"**{behind}** — every tab is scoped to this date: no reference period "
                   "after it is shown. Series keep their latest vintage, so the numbers are "
                   "not restated back to it.")
        if c2.button("Today", width="stretch",
                     help=f"Move the as-of date to {today:%d %b %Y} and reprice."):
            st.session_state["_as_of_today"] = True
            mark_dirty()
            st.rerun()
    return as_of


def _trade(S: dict) -> dict:
    C.sub("Trade")
    t = S.setdefault("trade", {})
    c1, c2 = st.columns(2)
    with c1:
        t["direction"] = st.selectbox("Event", ["hike", "cut"],
                                      index=0 if t.get("direction", "hike") == "hike" else 1,
                                      on_change=mark_dirty)
    with c2:
        sides = ["fade", "back"]
        t["side"] = st.selectbox("Side", sides, index=sides.index(t.get("side", "fade")),
                                 help="Fade = sell the event (receive). Back = buy it.",
                                 on_change=mark_dirty)
    c3, c4 = st.columns(2)
    with c3:
        # Left empty rather than 0 -- "not priced yet" is a different statement
        # from "priced at zero".
        t["points"] = st.number_input(
            "Points priced",
            value=float(t["points"]) if t.get("points") is not None else None,
            step=0.5, format="%.2f", placeholder="not priced", on_change=mark_dirty)
    with c4:
        t["size"] = st.number_input("Move size (bp)", value=float(t.get("size") or 25.0),
                                    step=5.0, format="%.0f", min_value=1.0, on_change=mark_dirty)
    return t


def _sizing(S: dict, t: dict, as_of: date) -> None:
    C.sub("Sizing")
    sz = S.setdefault("sizing", {})
    z1, z2 = st.columns(2)
    with z1:
        sz["max_drawdown"] = st.number_input(
            "Max DD (A$)", value=float(sz.get("max_drawdown") or sizing.DEFAULT_MAX_DRAWDOWN),
            min_value=0.0, step=25_000.0, format="%.0f", on_change=mark_dirty,
            help="The Kelly bankroll: the drawdown you can survive. Sets the position.")
    with z2:
        sz["daily_limit"] = st.number_input(
            "Daily cap (A$)", value=float(sz.get("daily_limit") or sizing.DEFAULT_DAILY_LIMIT),
            min_value=0.0, step=10_000.0, format="%.0f", on_change=mark_dirty,
            help="A hard cap, not a sizing input: rungs risking more are flagged as a breach.")

    # The strip's own contracts, nearest delivery first, anchored on the as-of:
    # which contract is the front month is a question about a date.
    universe = sizing.contract_universe(as_of) + [sizing.CUSTOM_CONTRACT]
    by_key = {c.key: c for c in universe}
    prior = sz.get("contract")
    if prior not in by_key:
        # A legacy "ib"/"ir" family key, or a month that has rolled off: keep
        # the family and roll to its nearest live contract. Never default to
        # Custom -- Custom means "I typed a DV01", not "nothing was saved".
        kind = sizing.contract_for(prior, as_of).kind if prior else "ib"
        kind = "ib" if kind == "custom" else kind
        prior = next((c.key for c in universe if c.kind == kind), universe[0].key)
    keys = list(by_key)
    z3, z4 = st.columns([2.5, 1.1])
    with z3:
        sz["contract"] = st.selectbox(
            "Contract", keys, index=keys.index(prior), format_func=lambda k: by_key[k].label,
            on_change=mark_dirty,
            help="Sets the DV01: IB A$24.66/bp; IR moves with the yield because a bank bill "
                 "is discount-priced.")
    with z4:
        sz["bets_per_year"] = st.number_input(
            "Bets/yr", value=int(sz.get("bets_per_year") or sizing.DEFAULT_BETS_PER_YEAR),
            min_value=1, max_value=60, step=1, on_change=mark_dirty,
            help="Eight scheduled RBA meetings. Only annualises the per-bet growth.")
    if sz["contract"] == "custom":
        sz["custom_dv01"] = st.number_input(
            "DV01 per lot (A$/bp)", value=float(sz.get("custom_dv01") or 24.66),
            min_value=0.0, step=1.0, format="%.2f", on_change=mark_dirty)
    live_yield = None
    if sz["contract"].startswith("ir:"):
        month = sz["contract"].partition(":")[2]
        live_yield = next((q.implied_rate for q in src.fetch_ir()
                           if q.ok and f"{q.month:%Y-%m}" == month), None)
    dv01 = sizing.dv01_for(sz["contract"], sz.get("custom_dv01"), live_yield)
    st.caption(f"**A${dv01:,.2f}** per bp per lot")

    t["kelly_fraction"] = st.select_slider(
        "Kelly fraction", options=list(sizing.DEFAULT_FRACTIONS),
        value=sizing.snap_fraction(t.get("kelly_fraction")),
        format_func=sizing.fraction_label, on_change=mark_dirty)
    st.divider()


def _view(S: dict) -> None:
    pr = S.setdefault("probability", {})
    C.sub("Your probability")
    mode = st.radio("Mode", ["decomposition", "manual"],
                    index=0 if pr.get("mode", "decomposition") == "decomposition" else 1,
                    horizontal=True, on_change=mark_dirty,
                    help="Decomposition: q = P(bloc) × P(centre). Manual: type q directly.")
    pr["mode"] = mode
    if mode == "decomposition":
        pr["p_bloc"] = st.slider(
            "P(bloc votes for the move)", 0, 100, int(round(_or(pr.get("p_bloc"), 0.25) * 100)),
            1, format="%d%%", on_change=mark_dirty,
            help="How likely the move candidates actually vote for it.") / 100
        pr["p_centre"] = st.slider(
            "P(centre joins | bloc moves)", 0, 100,
            int(round(_or(pr.get("p_centre"), 0.40) * 100)), 1, format="%d%%",
            on_change=mark_dirty,
            help="Given the candidates vote yes, how likely enough of the rest of the Board "
                 "joins them.") / 100
        derived = pr["p_bloc"] * pr["p_centre"]
        st.caption(f"Decomposition: {pr['p_bloc']:.0%} × {pr['p_centre']:.0%} = **{derived:.1%}**")
        pr["override_q"] = derived
    else:
        derived = _or(pr.get("p_bloc"), 0.25) * _or(pr.get("p_centre"), 0.40)
        start = pr.get("override_q") if pr.get("override_q") is not None else derived
        pr["override_q"] = st.number_input(
            "Recorded estimate q (%)", value=float(round(start * 100, 2)),
            min_value=0.0, max_value=100.0, step=1.0, format="%.2f", on_change=mark_dirty) / 100


def _save(key: str, S: dict) -> None:
    dirty = st.session_state.get("dirty", False)
    if st.button("Save trade" + (" ●" if dirty else ""), type="primary" if dirty else "secondary",
                 width="stretch", help=f"Persist the whole analysis to meetings/{key}.json."):
        store.save(key, S)
        st.session_state["dirty"] = False
        st.success(f"Trade saved → meetings/{key}.json")


def _recall(key: str) -> None:
    rows = store.saved_trade_rows()
    if not rows:
        return
    dirty = st.session_state.get("dirty", False)
    arms = st.session_state.setdefault("_del_arms", {})
    st.divider()
    with st.expander(f"Recall a saved trade ({len(rows)})"):
        for row in rows:
            k = row["key"]
            is_open = k == key
            code = f" · {row['code']}" if row.get("code") else ""
            extras = [row.get("q"), row.get("instrument"),
                      row.get("as_of") and f"as of {row['as_of']}",
                      row.get("saved_at") and f"saved {str(row['saved_at'])[:16].replace('T', ' ')}"]
            tip = " · ".join([f"{row['title']} — {row['text']}{code}",
                              "Reload the saved copy, discarding edits" if is_open
                              else "Open; unsaved edits to the current trade are discarded",
                              *[e for e in extras if e]])
            c1, c2, c3, c4 = st.columns([6, 1, 1, 1], vertical_alignment="center")
            with c1:
                st.caption(f"**{row['title']}** — {row['text']}{code}"
                           + (" · *unsaved edits*" if is_open and dirty else ""))
            with c2:
                if st.button("", key=f"recall:{k}", help=tip,
                             icon=":material/refresh:" if is_open else ":material/open_in_new:"):
                    _switch_meeting(k)
                    st.rerun()
            if arms.get(k):
                with c3:
                    if st.button("", key=f"delok:{k}", icon=":material/delete_forever:",
                                 type="primary", help=f"Confirm: delete meetings/{k}.json"):
                        store.delete_trade(k)
                        arms.pop(k, None)
                        st.session_state["dirty"] = False
                        st.toast(f"Deleted meetings/{k}.json")
                        st.rerun()
                with c4:
                    if st.button("", key=f"delno:{k}", icon=":material/close:", help="Keep it"):
                        arms.pop(k, None)
                        st.rerun()
            else:
                with c3:
                    if st.button("", key=f"del:{k}", icon=":material/delete_outline:",
                                 help=f"Delete meetings/{k}.json (asks to confirm)"):
                        arms[k] = True
                        st.rerun()
