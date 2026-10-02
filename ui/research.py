"""Research-agent panels and the controls they share.

The agent picker, sign-in handling and retry controls used by every billable
panel: the release consensus search, the speech roundup and the street call
book. Every run happens only on a button."""

from __future__ import annotations

import time
from datetime import date, datetime

import streamlit as st

from core import bank_calls, clock, pricing, speeches
from core import roster as R
from data import agent as claude_agent
from state import store
from ui import components as C
from ui import sources as src
from ui.context import Ctx, mark_dirty

AGENTS = src.RESEARCH_AGENTS
LABELS = src.RESEARCH_AGENT_LABELS
TIMEOUTS = (7, 10, 15, 20, 30)
CACHE_TTL = 3600


def _reset_model() -> None:
    st.session_state["speech_model_override"] = src.RESEARCH_AGENT_MODELS.get(
        st.session_state.get("speech_backend", src.DEFAULT_RESEARCH_BACKEND), "")


def agent_picker(slots=None) -> tuple[str, str]:
    """Backend + model, bound to one pair of session keys shared app-wide.

    `slots` are two columns of the caller's own row, so the two dropdowns
    sit beside the caller's other dropdowns instead of in a row of their own."""
    st.session_state.setdefault("speech_backend", src.DEFAULT_RESEARCH_BACKEND)
    a1, a2 = slots if slots is not None else st.columns([1.2, 1.6])
    # In a caller's bar the two dropdowns carry their own widths.
    w_agent, w_model = (150, 175) if slots is not None else ("stretch", "stretch")
    with a1:
        backend = st.selectbox(
            "Research agent", tuple(AGENTS), format_func=LABELS.get, width=w_agent,
            key="speech_backend", on_change=_reset_model,
            help="One setting for the whole app: the speech roundup, bank calls and "
                 "release consensus all run on it. Answers are cached per agent.")
    st.session_state.setdefault("speech_model_override", src.RESEARCH_AGENT_MODELS.get(backend, ""))
    choices = src.RESEARCH_AGENT_MODEL_CHOICES.get(backend, [src.RESEARCH_AGENT_MODELS.get(backend, "")])
    if st.session_state["speech_model_override"] not in choices:
        st.session_state["speech_model_override"] = choices[0]
    with a2:
        model = st.selectbox("Model", choices, key="speech_model_override", width=w_model,
                             help="Passed to the agent CLI's --model flag.")
    return backend, (model or "").strip()


def needs_sign_in(backend: str, message: str | None) -> bool:
    ask = getattr(AGENTS[backend], "needs_sign_in", None)
    return bool(ask and ask(message))


def agent_blocked(backend: str, why: str) -> None:
    if needs_sign_in(backend, why):
        st.error(why)
        st.code("claude", language="bash")
        C.note("Sign in, then search again — the CLI reports sign-in state when a search runs.")
    else:
        st.info(why)


def _stamp(entry: dict, fallback: str) -> str:
    try:
        return datetime.fromisoformat(entry["stamp"]).strftime("%d %b %Y, %H:%M")
    except (KeyError, ValueError):
        return fallback


def _agent_tail(entry: dict) -> str:
    tail = f" · {entry['meeting']}" if entry.get("meeting") else ""
    tail += f" · {LABELS.get(entry.get('backend', 'claude'), '?')}"
    return tail + (f" · {entry['model']}" if entry.get("model") else "")


def _run_timeout(key: str, width="stretch") -> int:
    st.session_state.setdefault(key, 30)
    return st.selectbox("Search timeout", TIMEOUTS, format_func=lambda m: f"{m} min", key=key,
                        width=width,
                        help="Per-batch budget. Batches run in parallel, so the wait is "
                             "roughly the slowest batch. A timed-out batch can be retried.")


def _retry_controls(backend: str, error: str | None, prefix: str, retry_n: int,
                    key_for) -> bool:
    """Sign-in or retry prompt after a failed/partial run. Returns True to stop."""
    if needs_sign_in(backend, error):
        agent_blocked(backend, claude_agent.SIGN_IN_MESSAGE)
        if st.button("I've signed in — search again", type="primary", key=f"{prefix}_signin"):
            st.session_state[f"{prefix}_retry"] = retry_n + 1
            st.session_state[f"{prefix}_key"] = key_for(retry_n + 1)
            st.rerun()
        return True
    st.warning("Some searches failed or timed out. Retrying re-asks everything; "
               "failed batches are never served from the cache.")
    if st.button("Retry failed searches", key=f"{prefix}_retry_btn", type="primary"):
        st.session_state[f"{prefix}_retry"] = retry_n + 1
        st.session_state[f"{prefix}_key"] = key_for(retry_n + 1)
        st.rerun()
    return False


# ------------------------------------------------------------ speech roundup

@st.cache_resource
def _speech_roundup_cache() -> dict:
    """Hand-rolled cache: the fetch writes into a caller-owned progress bar,
    which st.cache_data cannot replay on a hit."""
    return {}


def fetch_speech_roundup(members: tuple[str, ...], start: date, end: date,
                         scores: tuple[tuple[str, float], ...] = (),
                         _meeting_key: str = "", backend: str = "claude",
                         model: str = "", timeout: int = 900, retry: int = 0,
                         non_voters: tuple[str, ...] = (),
                         blocs: tuple[tuple[str, str], ...] = (),
                         _on_progress=None):
    key = (tuple(members), start, end, tuple(scores), backend, model, timeout, retry,
           tuple(non_voters), tuple(blocs), speeches.PROMPT_VERSION)
    cache = _speech_roundup_cache()
    answered_at, cached = cache.get(key, (0.0, None))
    if cached is not None and time.monotonic() - answered_at < CACHE_TTL:
        if R.roundup_has_blocs(cached):
            return cached
        cache.pop(key, None)

    groups = speeches.batches(list(members))
    answers = AGENTS[backend].research_many(
        [speeches.prompt(g, start, end, dict(scores), set(non_voters), dict(blocs))
         for g in groups],
        speeches.SYSTEM, model=model or None, timeout=timeout, on_progress=_on_progress)
    parts = [speeches.Roundup(start, end, error=err) if err
             else speeches.parse(raw, group, start, end)
             for group, (raw, err) in zip(groups, answers)]
    roundup = speeches.merge(parts, list(members), start, end)
    if roundup.ok:
        store.save_speech_run({
            "stamp": clock.stamp(),
            "meeting": _meeting_key, "backend": backend, "model": model,
            "timeout": timeout, "start": start.isoformat(), "end": end.isoformat(),
            "members": list(members), "scores": dict(scores),
            "non_voters": list(non_voters),
            "roundup": speeches.roundup_to_dict(roundup),
        })
        cache[key] = (time.monotonic(), roundup)
    else:
        cache.pop(key, None)
    return roundup


def _speech_history_label(entry: dict) -> str:
    ru = entry.get("roundup") or {}
    n = len(ru.get("appearances") or [])
    outcome = "search failed" if ru.get("error") else f"{n} appearance{'' if n == 1 else 's'}"
    window = f"{entry.get('start', '?')} to {entry.get('end', '?')}"
    return f"{_stamp(entry, 'unknown time')} · {window} · {outcome}{_agent_tail(entry)}"


def _outing_dates(outings) -> str:
    """"28 Sep, 24 Sep": each distinct day a member spoke, in the order the
    outings are listed (latest first)."""
    days = dict.fromkeys(f"{ap.when.day} {ap.when:%b}" if ap.when else "date unclear"
                         for ap in outings)
    return ", ".join(days)


def render_roundup(ctx: Ctx, roundup, members: list[str], scores: dict[str, float],
                   non_voters: set[str] | None = None,
                   blocs: dict[str, str] | None = None) -> None:
    P = ctx.P
    non_voters = non_voters or set()
    blocs = blocs or {}
    last = R.fomc_label(store.baseline_key(ctx.S, ctx.key))
    if not roundup.ok:
        st.warning(roundup.error)
        return
    if roundup.partial:
        st.warning(roundup.partial + " — members in those batches may show as silent "
                                     "when they were simply not searched.")
    if not roundup.appearances:
        st.info(f"No sourced appearances for the {len(members)} members between "
                f"{roundup.start:%d %b} and {roundup.end:%d %b %Y}.")
        return

    counts = roundup.leans
    C.board([C.Fig("Spoke", f"{len(members) - len(roundup.silent)} of {len(members)}"),
             C.Fig("Hawkish", f"{counts['hawkish']}", colour=P.negative),
             C.Fig("Dovish", f"{counts['dovish']}", colour=P.positive),
             C.Fig("Neutral or unclear", f"{counts['neutral'] + counts['unclear']}")])
    C.legend([("Hawkish", P.negative), ("Dovish", P.positive), ("Neutral or unclear", P.muted)])

    lean_colour = {"hawkish": P.negative, "dovish": P.positive,
                   "neutral": P.muted, "unclear": P.muted}
    move_colour = {"higher": P.negative, "lower": P.positive, "unchanged": P.muted}

    for name in roundup.speakers:
        outings = roundup.by_member(name)
        score = scores.get(name)
        leans = {ap.lean for ap in outings}
        label = (f"{name} · {_outing_dates(outings)}"
                 + (f" · score {score:.1f}" if score is not None else "")
                 + f" · {len(outings)} appearance{'' if len(outings) == 1 else 's'}"
                 + (f" · {next(iter(leans))}" if len(leans) == 1 else " · mixed"))
        with st.expander(label, expanded=False):
            for i, ap in enumerate(outings):
                if i:
                    st.divider()
                st.markdown(
                    f'<div class="fomc-note">{ap.dated} · {ap.kind} · {ap.venue} · '
                    f'<span style="color:{lean_colour[ap.lean]}">{ap.lean}</span> · '
                    f'<a href="{ap.source}" target="_blank">source</a></div>',
                    unsafe_allow_html=True)
                new = ap.new_score(score)
                read = (ap.move if new is None
                        else f"{score:.1f} → {new:.1f}" if ap.direction != "unchanged"
                        else f"unchanged at {new:.1f}")
                bloc_now, current = R.ap_bloc_now(ap), blocs.get(name, "")
                bloc_cell = ("not re-assessed" if not bloc_now
                             else bloc_now if bloc_now == current
                             else f"{current or 'unassigned'} → {bloc_now}")
                C.stat_row([
                    ("Policy now", ap.rate_stance, lean_colour[ap.lean]),
                    ("Bloc", bloc_cell,
                     P.muted if not bloc_now or bloc_now == current else P.warning),
                    (f"Mark vs {last}", read, move_colour[ap.direction])])
                C.reads([("Inflation", ap.inflation), ("Labour market", ap.labour),
                         ("Economy", ap.economy)])
                C.note(R.ap_bloc_note(ap))
                C.note(ap.score_note)
                for q in ap.quotes:
                    st.markdown(f'<div class="fomc-note fomc-quote" style="border-left-color:'
                                f'{lean_colour[ap.lean]}">“{q}”</div>', unsafe_allow_html=True)
                if ap.implication:
                    C.eyebrow("Policy implication")
                    C.note(ap.implication)

    if roundup.silent:
        C.note("No appearance found: " + ", ".join(
            roundup.silent) + ".")
    C.note("Implication and mark are judgements drawn from the sources. The mark is "
           f"measured from Score @ {last}, the same base the table write-back uses.")


def speech_roundup(ctx: Ctx, members: list[str], non_voters: set[str]) -> None:
    C.heading("Latest public appearances", level="sub", help=(
        r"\text{lean} \in \{\text{hawkish},\ \text{dovish},\ \text{neutral},\ \text{unclear}\}",
        "A research agent searches the window for speeches, interviews and testimony by every "
        "Board member — the Governor's media conference, speeches, Senate Estimates — "
        "reports each one's read on inflation, labour, the economy and policy, then gives "
        "a strategist's policy implication and a mark-to-market from Score @ last meeting. "
        "Every row carries a source link and verbatim quotes; an appearance the agent "
        "cannot source is dropped. The externals rarely speak, so silence is normal.",
        "see core.speeches", "speeches"))
    if not members:
        st.info("Add members to the roster to search for their appearances.")
        return

    roster = ctx.S.get("roster", [])
    scores = R.name_map(roster, R.baseline_score)
    blocs = R.name_map(roster, R.active_bloc)
    history = [h for h in store.load_speech_history()
               if not (h.get("roundup") or {}).get("error")]
    # The four dropdowns on one row; the window and the action on the next.
    with C.bar("speech_agent"):
        c_hist, c_agent, c_model, c_time, _ = st.columns([1.35, 1, 1, 0.75, 0.5],
                                                         vertical_alignment="bottom")
    with c_hist:
        view = st.selectbox("History", list(range(len(history) + 1)), width=210,
                            format_func=lambda i: "Live search" if i == 0
                            else _speech_history_label(history[i - 1]),
                            help="The last runs are saved automatically; pick one to replay it.")
    backend, model = agent_picker((c_agent, c_model))
    with c_time:
        timeout_min = _run_timeout("speech_timeout", width=110)

    if view:
        entry = history[view - 1]
        st.info(f"Viewing a saved run from {_stamp(entry, 'an earlier time')}. "
                "Choose “Live search” to run a new one.")
        saved = speeches.roundup_from_dict(entry["roundup"])
        render_roundup(ctx, saved, entry.get("members") or members,
                       scores or entry.get("scores") or {},
                       set(entry.get("non_voters") or ()), blocs)
        if saved.appearances:
            update_main_table(ctx, saved)
        return

    ok, why = AGENTS[backend].available()
    if not ok:
        agent_blocked(backend, why)
        return

    d_start, d_end = speeches.default_window(ctx.as_of)
    with C.bar("speech_window"):
        c1, c2, c3, _ = st.columns([0.8, 0.8, 1.3, 1.2], vertical_alignment="bottom")
    with c1:
        start = st.date_input("From", value=d_start, key="speech_from", width=125)
    with c2:
        end = st.date_input("To", value=d_end, key="speech_to", width=125)
    with c3:
        go = st.button("Get latest speech summary", width="content", type="primary",
                       disabled=start > end)
    if start > end:
        st.warning("The window starts after it ends.")
        return

    timeout_s = int(timeout_min) * 60
    retry_n = int(st.session_state.get("speech_retry", 0))
    silent_seats = tuple(sorted(non_voters))
    score_items = tuple(sorted(scores.items()))
    bloc_items = tuple(sorted(blocs.items()))

    def key_for(rn: int) -> tuple:
        return (tuple(members), start, end, score_items, backend, model, timeout_s, rn,
                silent_seats, bloc_items)

    n_batches = len(speeches.batches(members))
    if go:
        st.session_state["speech_key"] = key_for(retry_n)
    if st.session_state.get("speech_key") != key_for(retry_n):
        C.note(f"Searches {start:%d %b} to {end:%d %b %Y} for {len(members)} members as "
               f"{n_batches} parallel searches on {LABELS[backend]} ({model}), up to "
               f"{timeout_min} min each. Runs only on the button; cached until the "
               "roster, dates, model or budget change.")
        return

    bar, on_progress, t0 = C.progress_clock(n_batches, "batches")
    with st.spinner(f"Searching public appearances ({LABELS[backend]} · {model})"):
        roundup = fetch_speech_roundup(tuple(members), start, end, score_items, ctx.key,
                                       backend, model, timeout=timeout_s, retry=retry_n,
                                       non_voters=silent_seats, blocs=bloc_items,
                                       _on_progress=on_progress)
    bar.progress(1.0, text=f"{n_batches}/{n_batches} batches — done in "
                           f"{C.fmt_secs(time.monotonic() - t0)}")
    render_roundup(ctx, roundup, members, scores, non_voters, blocs)
    if (not roundup.ok or roundup.partial) and _retry_controls(
            backend, roundup.error or roundup.partial, "speech", retry_n, key_for):
        return
    if roundup.appearances:
        update_main_table(ctx, roundup)


def update_main_table(ctx: Ctx, roundup) -> None:
    S = ctx.S
    run_id = f"{roundup.start}|{roundup.end}|{len(roundup.appearances)}"
    multi = [(n, roundup.by_member(n)) for n in roundup.speakers
             if len(roundup.by_member(n)) > 1]
    chosen = {n: int(st.session_state.get(f"mt_outing:{run_id}:{n}", 0) or 0) for n, _ in multi}
    fresh = st.session_state.get(f"mt_fresh:{run_id}", False)
    updates = R.roster_updates(roundup, S.get("roster", []), chosen, include_applied=fresh)

    C.heading("Update main table", level="sub", help=(
        r"\text{score}_\text{now} = \text{score}_\text{last meeting} + \delta_\text{outing}",
        "Writes each member's re-assessed bloc and score into Bloc now / Score now. The "
        "score is the last-meeting mark plus the outing's adjustment, clamped to 0–5, so "
        "re-running the same search offers nothing further. The @ last meeting columns are "
        "never touched. The New columns are editable; untick Write to skip a member.",
        "writes bloc2 / score2", "update-main-table"))

    if multi:
        with st.expander(f"Mark from a different outing · {len(multi)} member"
                         f"{'' if len(multi) == 1 else 's'} spoke more than once"):
            for n, outs in multi:
                st.selectbox(n, list(range(len(outs))), key=f"mt_outing:{run_id}:{n}",
                             format_func=lambda i, o=outs: (
                                 f"{o[i].dated} · {o[i].kind} · {o[i].lean} · {o[i].move}"
                                 + ("   (latest)" if i == 0 else "")))
    st.checkbox("Fresh update — re-offer every searched member", key=f"mt_fresh:{run_id}",
                help="On: list every re-assessed member even if the roster already matches.")

    if not updates:
        C.note("Nothing to update — no member's marks differ from what this search argues.")
        return
    if roundup.partial:
        st.warning("Some batches failed, so some members were never searched and are "
                   "absent from the preview.")

    rows = [{
        "include": True, "name": u["name"], "bloc2": u["bloc2"] or "—",
        "new_bloc2": u["new_bloc2"], "score2": u["score2"], "new_score2": u["new_score2"],
        "new_note": u.get("new_note", ""), "score1": u["score1"], "bloc1": u["bloc1"] or "—",
        "when": ((f"{u['when']:%a %d %b}" if u["when"] else "date unclear")
                 + ("" if u.get("n_outings", 1) < 2
                    else f"  ({u.get('outing_idx', 0) + 1} of {u['n_outings']})")),
    } for u in updates]
    fp = "|".join(f"{r['name']}:{r['new_bloc2']}:{r['new_score2']}:{r['new_note']}:{r['when']}"
                  for r in rows)
    score_col = dict(min_value=0.0, max_value=5.0, format="%.1f")
    last = R.fomc_label(store.baseline_key(S, ctx.key))
    edited = st.data_editor(
        rows, width="stretch", hide_index=True, key=f"mt_preview_{hash(fp) & 0xFFFFFFF:x}",
        column_order=("include", "name", "bloc2", "new_bloc2", "score2", "new_score2",
                      "new_note", "score1", "bloc1", "when"),
        disabled=("name", "bloc2", "score2", "score1", "bloc1", "when"),
        column_config={
            "include": st.column_config.CheckboxColumn("Write", width=58),
            "name": st.column_config.TextColumn("Member", width=150),
            "bloc2": st.column_config.TextColumn("Bloc now", width=120),
            "new_bloc2": st.column_config.TextColumn("New bloc →", width=128),
            "score2": st.column_config.NumberColumn("Score now", width=90, **score_col),
            "new_score2": st.column_config.NumberColumn("New score →", width=100, step=0.1,
                                                        **score_col),
            "new_note": st.column_config.TextColumn("New note →", width="large",
                                                    help="Blank leaves the existing note."),
            "score1": st.column_config.NumberColumn(f"Score @ {last}", width=110, **score_col),
            "bloc1": st.column_config.TextColumn(f"Bloc @ {last}", width=120),
            "when": st.column_config.TextColumn("Marked from", width=140),
        })

    final, problems = R.edits_to_updates(edited, updates)
    if problems:
        st.warning("Not written as edited: " + "; ".join(problems) + ".")
    skipped = len(updates) - len(final) - len(problems)
    if skipped:
        C.note(f"{skipped} row{'' if skipped == 1 else 's'} not counted: unticked, or "
               "edited back to the value already held.")
    if not final:
        C.note("Nothing left to write.")
        return

    C.table(["Member", "Bloc now", "Score now", "Δ after"],
            [[u["name"],
              u["new_bloc2"] if u["new_bloc2"] == u["bloc2"] else f"{u['bloc2'] or '—'} → {u['new_bloc2']}",
              (f"{u['new_score2']:.1f}" if u["score2"] is None or abs(u["score2"] - u["new_score2"]) < 1e-9
               else f"{u['score2']:.1f} → {u['new_score2']:.1f}"),
              "—" if u["score1"] is None else f"{u['new_score2'] - u['score1']:+.1f}"]
             for u in final], numeric=(2, 3))
    C.note(f"Δ after = new Score now − Score @ {last}; positive means moved hawkish.")
    if st.button(f"Update main table — write {len(final)} change{'' if len(final) == 1 else 's'}",
                 type="primary", key="update_main_table_btn"):
        S["roster"] = R.apply_roster_updates(S.get("roster", []), final)
        mark_dirty()
        st.rerun()


# ---------------------------------------------------------------- bank calls

@st.cache_resource
def _call_book_cache() -> dict:
    return {}


def fetch_call_book(banks: tuple[str, ...], meeting_date: date, today: date,
                    backend: str, model: str, timeout: int, retry: int,
                    _meeting_key: str = "", _on_progress=None):
    key = (tuple(banks), meeting_date, today, backend, model, timeout, retry,
           bank_calls.PROMPT_VERSION)
    cache = _call_book_cache()
    answered_at, cached = cache.get(key, (0.0, None))
    if cached is not None and time.monotonic() - answered_at < CACHE_TTL:
        return cached
    groups = bank_calls.batches(list(banks))
    answers = AGENTS[backend].research_many(
        [bank_calls.prompt(g, meeting_date, today) for g in groups],
        bank_calls.SYSTEM, model=model or None, timeout=timeout, on_progress=_on_progress)
    book = bank_calls.book_from_answers(groups, answers, meeting_date, today)
    if book.ok and book.calls:
        store.save_call_run({
            "stamp": clock.stamp(),
            "meeting": _meeting_key, "meeting_date": meeting_date.isoformat(),
            "backend": backend, "model": model, "timeout": timeout,
            "book": bank_calls.book_to_dict(book),
        })
        cache[key] = (time.monotonic(), book)
    else:
        cache.pop(key, None)
    return book


def _call_history_label(entry: dict) -> str:
    book = entry.get("book") or {}
    n = len(book.get("calls") or [])
    outcome = "search failed" if book.get("error") else f"{n} house{'' if n == 1 else 's'}"
    if book.get("partial"):
        outcome += " (partial)"
    return f"{_stamp(entry, 'unknown time')} · {outcome}{_agent_tail(entry)}"


def render_call_book(ctx: Ctx, book) -> None:
    P = ctx.P
    if not book.calls:
        st.info("Nothing sourced came back: the houses have not published for this meeting "
                "yet, or every row failed validation (see below).")
    else:
        _call_summary(ctx, book)
        _call_table(book, P)
    if book.changed_calls:
        _call_changes(book.changed_calls, P)
    if book.rejected:
        C.note("Rejected before presenting: " + "; ".join(book.rejected)
               + ". A thin table is a thin search, not a thin street.")


def _call_colour(call: str, P) -> str:
    return {"hike": P.negative, "cut": P.positive}.get(call, P.muted)


def _call_summary(ctx: Ctx, book) -> None:
    M, P = ctx.M, ctx.P
    t = bank_calls.tally(book)
    n = len(book.calls)
    med6, med12 = bank_calls.median_path(book, "6m"), bank_calls.median_path(book, "12m")
    spread12 = bank_calls.dispersion(book, "12m")
    C.board([
        C.Fig("Houses", f"{n}", f"of {len(bank_calls.BANKS)} searched"),
        C.Fig("Consensus", bank_calls.consensus_call(book).title(),
              f"{t['hike']} hike · {t['hold']} hold · {t['cut']} cut"),
        *[C.Fig(label, "—" if med is None else f"{med * 25:+.0f}bp",
                "" if med is None else f"{med:+g} moves")
          for label, med in (("Median 6m path", med6), ("Median 12m path", med12))],
        C.Fig("Dispersion (12m)", "—" if spread12 is None else f"{spread12} moves",
              "" if spread12 is None else ("unanimous" if spread12 == 0 else "hawk−dove spread")),
    ])
    if M.priced and M.points and M.size and not M.multi_move:
        strip = pricing.implied_probability(M.points, M.size)
        movers = t["hike"] + t["cut"]
        street = movers / n if n else 0.0
        gap = street - strip
        tone = P.good if abs(gap) < 0.15 else P.serious
        st.markdown(
            f'<div class="fomc-note">Street calls a move <b>{street:.0%}</b> ({movers}/{n} '
            f'houses) vs the strip pricing <b>{strip:.0%}</b> — a <span style="color:{tone}">'
            f'{gap:+.0%}</span> gap.</div>', unsafe_allow_html=True)


def _call_table(book, P) -> None:
    rows, colours, bg = [], {}, {}
    for i, c in enumerate(sorted(book.calls, key=lambda x: x.bank)):
        rows.append([
            c.bank + ("  ◆" if c.changed else ""),
            (f"{c.next_call} {c.next_bp:.0f}bp".replace(".0bp", "bp")
             if c.next_call != "hold" and c.next_bp else c.next_call),
            c.path_6m, c.path_12m,
            f"{c.end_rate:.3f}%" if c.end_rate is not None else "—",
            f"{c.as_of:%d %b}" if c.as_of else "—",
        ])
        colours[(i, 1)] = _call_colour(c.next_call, P)
        if c.changed:
            bg[i] = "rgba(250,178,25,0.10)"
    C.table(["Bank", "This meeting", "6m", "12m", "End-27", "As of"], rows,
            numeric=(2, 3, 4), colours=colours, row_bg=bg)
    C.note("6m / 12m are net 25bp moves after this meeting (+ = hikes). End-27 is the cash "
           "rate the house sees at end-2027. ◆ = revised in the last eight weeks, listed with "
           "reasons below. Sourced rows only.")


def _call_changes(changed, P) -> None:
    C.sub("What moved")
    rows, colours = [], {}
    for i, c in enumerate(changed):
        rows.append([c.bank, f"{c.prev_call} → {c.next_call}",
                     f"{c.change_date:%d %b}" if c.change_date else "recent",
                     c.change_reason or ("no reason given in the source" if c.reason_missing else "—")])
        colours[(i, 1)] = _call_colour(c.next_call, P)
    C.table(["Bank", "Move", "When", "Reason"], rows, colours=colours)


def bank_call_book(ctx: Ctx) -> None:
    C.heading("Sell-side calls", f"for {ctx.M.meeting.label}", help=(
        r"\text{street} - \text{strip} = \text{event risk}",
        "A research agent searches each house's latest published RBA call — the big four, "
        "Macquarie, AMP and the global banks' Australian economists: its call for this "
        "meeting and net 25bp moves over the next six and twelve months. Rows need a dated "
        "source and pass validation; revisions are listed with the stated reason.",
        "see core.bank_calls", "bank_calls"))

    history = [h for h in store.load_call_history() if not (h.get("book") or {}).get("error")]
    # The four dropdowns on one row; the action and what it will do on the next.
    with C.bar("call_agent"):
        c_hist, c_agent, c_model, c_time, _ = st.columns([1.35, 1, 1, 0.75, 0.5],
                                                         vertical_alignment="bottom")
    with c_hist:
        view = st.selectbox("History", list(range(len(history) + 1)), key="call_history_view",
                            width=210, format_func=lambda i: "Live search" if i == 0
                            else _call_history_label(history[i - 1]))
    backend, model = agent_picker((c_agent, c_model))
    with c_time:
        timeout_min = _run_timeout("call_timeout", width=110)

    if view:
        entry = history[view - 1]
        st.info(f"Viewing a saved run from {_stamp(entry, 'an earlier time')}.")
        render_call_book(ctx, bank_calls.book_from_dict(entry["book"]))
        return

    ok, why = AGENTS[backend].available()
    if not ok:
        agent_blocked(backend, why)
        return

    meeting_date, today = ctx.M.meeting.end, ctx.M.as_of
    n_batches = len(bank_calls.batches(list(bank_calls.BANKS)))
    with C.bar("call_action"):
        c2, c3 = st.columns([1, 3.1], vertical_alignment="center")
    with c2:
        go = st.button("Get latest bank calls", width="content", type="primary", key="call_go")
    with c3:
        C.note(f"{len(bank_calls.BANKS)} houses as {n_batches} parallel searches on "
               f"{LABELS[backend]} ({model}). Runs only on the button.")

    timeout_s = int(timeout_min) * 60
    retry_n = int(st.session_state.get("call_retry", 0))

    def key_for(rn: int) -> tuple:
        return (tuple(bank_calls.BANKS), meeting_date, today, backend, model, timeout_s, rn)

    if go:
        st.session_state["call_key"] = key_for(retry_n)
    if st.session_state.get("call_key") != key_for(retry_n):
        # Say what the empty panel is waiting for, rather than leave it blank.
        saved = (f" or open one of the {len(history)} saved runs from History"
                 if history else "")
        st.info(f"No calls pulled for {ctx.M.meeting.label} in this session. Run the search"
                f"{saved}. Each run is a billed agent search.",
                icon=":material/travel_explore:")
        return

    bar, on_progress, t0 = C.progress_clock(n_batches, "batches")
    with st.spinner(f"Searching bank calls ({LABELS[backend]} · {model})"):
        book = fetch_call_book(tuple(bank_calls.BANKS), meeting_date, today, backend, model,
                               timeout_s, retry_n, ctx.key, _on_progress=on_progress)
    bar.progress(1.0, text=f"{n_batches}/{n_batches} batches — done in "
                           f"{C.fmt_secs(time.monotonic() - t0)}")
    if (not book.ok or book.partial) and _retry_controls(
            backend, book.error or book.partial, "call", retry_n, key_for):
        return
    render_call_book(ctx, book)
