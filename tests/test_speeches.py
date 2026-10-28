"""Public-appearance roundup.

The parser is the guard rail, not a convenience. A research agent asked who
spoke last week can produce a fluent, sourced-looking, entirely invented
answer, and nothing about it reads differently from a real one -- so these
tests pin the drops: no source, not a voter, duplicate, malformed.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import speeches

MEMBERS = ["Jerome Powell", "Michelle Bowman", "John Williams"]
START, END = date(2026, 7, 27), date(2026, 8, 3)


def payload(**over) -> dict:
    row = {
        "member": "Jerome Powell", "date": "2026-07-29",
        "venue": "Jackson Hole", "kind": "speech",
        "inflation": "Still above target.", "labour": "Cooling but solid.",
        "economy": "Growing near trend.", "rate_stance": "restrictive",
        "lean": "hawkish", "quotes": ["We are not yet done."],
        "source": "https://federalreserve.gov/speech.htm",
    }
    row.update(over)
    return {"appearances": [row], "silent": []}


def parse(obj, members=None) -> speeches.Roundup:
    return speeches.parse(json.dumps(obj), members or MEMBERS, START, END)


# ------------------------------------------------------------ happy path

def test_a_well_formed_appearance_survives():
    r = parse(payload())
    assert r.ok and len(r.appearances) == 1
    a = r.appearances[0]
    assert a.member == "Jerome Powell"
    assert a.when == date(2026, 7, 29)
    assert a.lean == "hawkish" and a.rate_stance == "restrictive"
    assert a.quotes == ("We are not yet done.",)


def test_silent_is_derived_from_who_actually_has_a_row():
    """Never trusted from the payload: a member dropped for a missing source
    must not also be reported as having said nothing."""
    obj = payload()
    obj["silent"] = ["Jerome Powell"]          # contradicts the appearance
    r = parse(obj)
    assert "Jerome Powell" not in r.silent
    assert set(r.silent) == {"Michelle Bowman", "John Williams"}


# ------------------------------------------------------------- the drops

def test_an_unsourced_appearance_is_dropped():
    assert parse(payload(source="")).appearances == ()


def test_a_non_url_source_is_dropped():
    """'Reuters' is not a receipt."""
    assert parse(payload(source="Reuters, last Tuesday")).appearances == ()


def test_an_appearance_by_someone_off_the_roster_is_dropped():
    """The specific shape of a hallucinated attribution."""
    assert parse(payload(member="Christopher Waller")).appearances == ()


def test_the_same_speech_reported_twice_counts_once():
    obj = payload()
    obj["appearances"].append(dict(obj["appearances"][0], venue="Wyoming"))
    assert len(parse(obj).appearances) == 1


def test_a_malformed_record_costs_only_itself():
    obj = payload()
    obj["appearances"].insert(0, {"member": "Michelle Bowman"})   # no source
    obj["appearances"].append("not even a dict")
    r = parse(obj)
    assert len(r.appearances) == 1 and r.appearances[0].member == "Jerome Powell"


def test_member_matching_ignores_case_but_not_identity():
    assert parse(payload(member="jerome POWELL")).appearances[0].member == "Jerome Powell"


# --------------------------------------------------------- normalisation

def test_an_unknown_lean_reads_as_unclear_not_as_a_real_position():
    assert parse(payload(lean="mildly spicy")).appearances[0].lean == "unclear"


def test_an_unknown_stance_reads_as_unclear():
    assert parse(payload(rate_stance="fairly tight-ish")).appearances[0].rate_stance == "unclear"


def test_a_date_outside_the_window_is_blanked_not_kept():
    a = parse(payload(date="2026-01-05")).appearances[0]
    assert a.when is None and a.dated == "date unclear"


def test_missing_views_read_as_not_addressed():
    a = parse(payload(inflation="", labour=None)).appearances[0]
    assert a.inflation == "not addressed" and a.labour == "not addressed"


def test_quotes_are_stripped_of_wrapping_marks_and_blanks():
    a = parse(payload(quotes=['"quoted"', "", "  ", "plain"])).appearances[0]
    assert a.quotes == ("quoted", "plain")


def test_non_list_quotes_degrade_to_empty():
    assert parse(payload(quotes="a single string")).appearances[0].quotes == ()


# ------------------------------------------------------- JSON extraction

def test_json_is_found_inside_a_fenced_block_with_preamble():
    raw = ('I searched the Fed site and the wires.\n\n```json\n'
           + json.dumps(payload()) + '\n```\n\nLet me know if you want more.')
    r = speeches.parse(raw, MEMBERS, START, END)
    assert len(r.appearances) == 1


def test_braces_inside_strings_do_not_end_the_scan():
    obj = payload(venue="The {Brookings} Institution")
    r = speeches.parse("chatter " + json.dumps(obj), MEMBERS, START, END)
    assert r.appearances[0].venue == "The {Brookings} Institution"


def test_a_leading_brace_that_is_not_the_payload_is_skipped():
    """The scan tries the next opening brace rather than giving up."""
    raw = "Note: {this is not json} then " + json.dumps(payload())
    assert len(speeches.parse(raw, MEMBERS, START, END).appearances) == 1


def test_a_reply_with_no_json_is_an_error_not_an_empty_roundup():
    r = speeches.parse("I could not find anything.", MEMBERS, START, END)
    assert not r.ok and "JSON" in r.error


def test_empty_reply_degrades_rather_than_raising():
    assert not speeches.parse("", MEMBERS, START, END).ok


# ---------------------------------------------------------------- window

def test_default_window_is_a_week_back_from_the_as_of_not_from_today():
    start, end = speeches.default_window(date(2026, 4, 15))
    assert (start, end) == (date(2026, 4, 8), date(2026, 4, 15))


def test_prompt_carries_every_member_and_both_dates():
    p = speeches.prompt(MEMBERS, START, END)
    for m in MEMBERS:
        assert m in p
    assert "27 July 2026" in p and "03 August 2026" in p
    assert "VERBATIM" in p and "source URL" in p


# ------------------------------------------------------ the score read

def test_a_hawkish_shift_reads_higher_and_lands_on_the_new_score():
    a = parse(payload(direction="higher", delta=0.9)).appearances[0]
    assert (a.direction, a.delta) == ("higher", 0.9)
    assert a.new_score(1.8) == pytest.approx(2.7)
    assert a.move == "higher +0.9"


def test_a_dovish_shift_reads_lower():
    a = parse(payload(direction="lower", delta=-0.6)).appearances[0]
    assert a.direction == "lower"
    assert a.new_score(3.0) == pytest.approx(2.4)


def test_the_sign_of_the_delta_wins_when_it_contradicts_the_word():
    """A table showing 'higher' while the arithmetic subtracts would be wrong
    in the most expensive direction, so the two are reconciled on the number."""
    a = parse(payload(direction="higher", delta=-0.5)).appearances[0]
    assert a.direction == "lower" and a.delta == pytest.approx(-0.5)


def test_a_direction_word_with_no_size_does_not_invent_one():
    a = parse(payload(direction="higher", delta=0)).appearances[0]
    assert a.direction == "unchanged" and a.delta == 0.0


def test_an_oversized_delta_is_clamped_not_taken_at_face_value():
    a = parse(payload(direction="higher", delta=4.0)).appearances[0]
    assert a.delta == pytest.approx(speeches.MAX_DELTA)


def test_a_non_numeric_delta_degrades_to_unchanged():
    a = parse(payload(delta="quite a lot")).appearances[0]
    assert a.direction == "unchanged" and a.delta == 0.0


def test_the_new_score_is_clamped_to_the_roster_scale():
    a = parse(payload(direction="higher", delta=1.5)).appearances[0]
    assert a.new_score(4.8) == pytest.approx(speeches.SCORE_MAX)
    b = parse(payload(direction="lower", delta=-1.5)).appearances[0]
    assert b.new_score(0.4) == pytest.approx(speeches.SCORE_MIN)


def test_new_score_without_a_current_score_is_none_not_a_guess():
    assert parse(payload(direction="higher", delta=0.5)).appearances[0].new_score(None) is None


def test_the_brief_carries_peer_context_so_the_read_is_relative():
    p = speeches.prompt(MEMBERS, START, END,
                        {"Jerome Powell": 3.0, "Michelle Bowman": 1.8,
                         "John Williams": 2.7})
    assert "Board mean 2.5" in p
    assert "Michelle Bowman: 1.8" in p
    assert "RELATIVE" in p


def test_the_mean_is_the_committee_not_the_batch_being_searched():
    """The roster is batched so the searches finish. If the mean followed the
    batch, a member would re-rate differently depending on who they happened
    to be grouped with -- which is not a property of anything real."""
    committee = {"Jerome Powell": 3.0, "Michelle Bowman": 1.8,
                 "John Williams": 2.7, "Lorie Logan": 4.4, "Beth Hammack": 4.5}
    # Search only the two hawks; their own mean would be 4.45.
    p = speeches.prompt(["Lorie Logan", "Beth Hammack"], START, END, committee)
    assert "Board mean 3.3" in p
    assert "4.4" not in p.split("Board mean")[1][:20]
    # Everyone is listed for context, even though only two are searched.
    for m in committee:
        assert m in p


def test_the_brief_is_still_valid_without_any_scores():
    p = speeches.prompt(MEMBERS, START, END)
    assert "THE DESK'S CURRENT SCORES" not in p and "Jerome Powell" in p


def test_peer_context_ranks_members_so_a_move_is_judged_against_neighbours():
    """The mean alone says "above average"; the rank says who they passed.
    The brief asks for the score note to name the nearest colleagues, so the
    ordering has to be in front of the model, not left for it to sort."""
    p = speeches.prompt(["Jerome Powell"], START, END,
                        {"Jerome Powell": 3.0, "Michelle Bowman": 1.8,
                         "Beth Hammack": 4.5})
    assert "rank 1 of 3 most hawkish" in p       # Hammack, the top score
    assert "rank 3 of 3 most hawkish" in p       # Bowman, the bottom
    assert "above the mean" in p and "below the mean" in p


def test_the_brief_marks_against_the_last_fomc_baseline():
    """The mark to market is measured FROM Score @ last FOMC, and the brief has
    to say so, because that is the base `_roster_updates` applies the delta to.

    It used to hand the model Score NOW and say "mark against your current
    standing marks", while the write anchored on Score @ last FOMC -- two
    bases for one number, which is how a card came to read "unchanged at 4.3"
    against a table showing 3.3.
    """
    p = speeches.prompt(MEMBERS, START, END, {"Jerome Powell": 3.0})
    assert "AS AT THE LAST MEETING" in p
    assert "BASELINE" in p
    assert "FROM THE LAST-MEETING MARK" in p
    assert "MARK TO MARKET" in p
    # and it must not still be claiming the base is the latest read
    assert "LATEST standing assessment" not in p
    assert "RIGHT NOW" not in p


def test_the_brief_says_the_latest_outing_carries_the_whole_cycle():
    """Two outings in one window, both measured from the same baseline, and
    only the latest is ever written -- so that one has to carry the full shift
    rather than just its own increment, or the earlier move is silently lost."""
    p = speeches.prompt(MEMBERS, START, END, {"Jerome Powell": 3.0})
    assert "spoke twice" in p and "LATEST outing" in p


def test_the_brief_demands_a_strategist_read_of_the_policy_implication():
    p = speeches.prompt(MEMBERS, START, END)
    assert "THE STRATEGIST'S READ" in p
    assert '"implication"' in p
    for expected in ("POLICY PATH", "REACTION FUNCTION", "counter-argument"):
        assert expected in p


def test_the_brief_pushes_the_stance_to_be_inferred_rather_than_left_unclear():
    """A column of "unclear" carries none of what the speech contained. The
    brief has to say so explicitly, and has to reconcile that with the rule
    forbidding inference from reputation -- the two are different questions."""
    p = speeches.prompt(MEMBERS, START, END)
    assert "Reserve \"unclear\" for a speech that genuinely gives nothing" in p
    # The stance IS inferred from the remarks; it is still never inferred from
    # who the speaker is. Both halves have to be in the brief.
    assert "inference from the SPEECH, which rule 6 asks for" in p
    assert "which rule 6 forbids" in p
    assert "Do not infer a view from their general reputation." in p


def test_the_system_prompt_separates_the_record_from_the_judgement():
    assert "experienced Australian rates strategist" in speeches.SYSTEM
    assert "quotes and view fields are the record" in speeches.SYSTEM
    # The anti-fabrication discipline survives the reframing.
    assert "only what you can source" in speeches.SYSTEM
    assert "evidence of a CHANGE" in speeches.SYSTEM


# ------------------------------------------------------- strategist read

def test_the_implication_is_parsed_and_kept_whole():
    a = parse(payload(implication="Consistent with a hold in September, but "
                                  "the bar for cutting is lower than his "
                                  "December remarks implied.")).appearances[0]
    assert a.implication.startswith("Consistent with a hold")
    assert "December remarks implied." in a.implication


def test_a_missing_implication_is_blank_not_invented():
    a = parse(payload()).appearances[0]
    assert a.implication == ""


def test_a_non_string_implication_degrades_to_blank():
    a = parse(payload(implication={"path": "hold"})).appearances[0]
    assert a.implication == ""


def test_the_implication_survives_the_round_trip():
    a = parse(payload(implication="Reaction function unchanged; the bar "
                                  "for a cut is two soft CPI prints.")).appearances[0]
    got = speeches.appearance_from_dict(speeches.appearance_to_dict(a))
    assert got == a


def test_a_history_entry_predating_the_implication_still_loads():
    """Runs saved before the field existed must replay, not crash -- the same
    degrade rule the parser follows, applied to our own disk format."""
    d = speeches.appearance_to_dict(parse(payload()).appearances[0])
    del d["implication"]
    assert speeches.appearance_from_dict(d).implication == ""


# ------------------------------------------------------------- ordering

def _dated(member, day, source):
    return dict(member=member, date=f"2026-07-{day:02d}", source=source)


def test_appearances_come_back_latest_first():
    obj = {"appearances": [
        {**payload()["appearances"][0], **_dated("Jerome Powell", 28, "https://a")},
        {**payload()["appearances"][0], **_dated("John Williams", 31, "https://b")},
        {**payload()["appearances"][0], **_dated("Michelle Bowman", 29, "https://c")},
    ], "silent": []}
    got = [a.when.day for a in parse(obj).appearances]
    assert got == [31, 29, 28]


def test_speakers_are_ordered_by_recency_not_roster_order():
    obj = {"appearances": [
        {**payload()["appearances"][0], **_dated("Jerome Powell", 28, "https://a")},
        {**payload()["appearances"][0], **_dated("John Williams", 31, "https://b")},
    ], "silent": []}
    assert parse(obj).speakers == ("John Williams", "Jerome Powell")


def test_an_undated_appearance_sorts_last_not_first():
    """date.min would push the row we are least sure about to the top."""
    obj = {"appearances": [
        {**payload()["appearances"][0], "member": "Jerome Powell",
         "date": "", "source": "https://a"},
        {**payload()["appearances"][0], **_dated("John Williams", 28, "https://b")},
    ], "silent": []}
    got = parse(obj).appearances
    assert got[0].member == "John Williams" and got[-1].when is None


# ------------------------------------------------------- (de)serialisation

def test_an_appearance_round_trips_through_dicts():
    a = parse(payload(direction="higher", delta=0.9)).appearances[0]
    got = speeches.appearance_from_dict(speeches.appearance_to_dict(a))
    assert got == a


def test_a_roundup_round_trips_through_dicts():
    obj = payload()
    obj["appearances"].append(dict(
        obj["appearances"][0], member="John Williams", lean="dovish",
        source="https://newyorkfed.org/x"))
    r = parse(obj)
    got = speeches.roundup_from_dict(speeches.roundup_to_dict(r))
    assert got == r


def test_an_undated_appearance_survives_the_round_trip():
    """`when=None` is the one field that cannot just be `.isoformat()`d."""
    a = parse(payload(date="")).appearances[0]
    assert a.when is None
    got = speeches.appearance_from_dict(speeches.appearance_to_dict(a))
    assert got.when is None


def test_an_error_roundup_round_trips_too():
    r = speeches.Roundup(START, END, error="boom")
    got = speeches.roundup_from_dict(speeches.roundup_to_dict(r))
    assert got.error == "boom" and got.appearances == ()


def test_from_dict_tolerates_a_missing_optional_field():
    """A hand-edited or older-schema file should degrade, not crash --
    the same rule `parse` follows for agent output, applied to our own disk
    format for the same reason: a corrupt history entry should cost that
    entry, not every entry."""
    d = speeches.appearance_to_dict(parse(payload()).appearances[0])
    del d["score_note"]
    got = speeches.appearance_from_dict(d)
    assert got.score_note == ""


def test_from_dict_rejects_an_unknown_lean_the_same_way_parse_does():
    d = speeches.appearance_to_dict(parse(payload()).appearances[0])
    d["lean"] = "some future value"
    assert speeches.appearance_from_dict(d).lean == "unclear"


# ----------------------------------------------------------- the summary

def test_leans_tally_covers_every_row():
    obj = payload()
    obj["appearances"].append(dict(
        obj["appearances"][0], member="John Williams", lean="dovish",
        source="https://newyorkfed.org/x"))
    r = parse(obj)
    assert sum(r.leans.values()) == len(r.appearances) == 2
    assert r.leans["hawkish"] == 1 and r.leans["dovish"] == 1


# ----------------------------------------------------- adapter progress

def _fake_run(result="ok"):
    """Patchable stand-in for the adapters' `_run` that completes at its own
    pace -- each fake call sleeps a little so the progress loop sees batches
    land over time rather than all at once."""
    from time import sleep
    def run(*_a, **_k):
        sleep(0.15)
        return result
    return run


def test_wb_agent_reports_progress_on_the_caller_thread(monkeypatch):
    """`on_progress` fires from the caller's thread as each batch lands --
    never from a pool worker -- and ends at (total, total)."""
    import threading
    import time
    from data import wb_agent

    monkeypatch.setattr(wb_agent, "_run", _fake_run())
    seen, caller = [], threading.get_ident()
    t0 = time.monotonic()

    def on_progress(done, total):
        assert threading.get_ident() == caller
        seen.append((done, total))

    answers = wb_agent.research_many(
        ["a", "b", "c", "d"], model="custom-local:deepseek-v4-flash",
        timeout=30, concurrency=4, on_progress=on_progress)
    assert [a[1] for a in answers] == [None] * 4       # all succeeded
    assert seen[-1] == (4, 4)                          # reports completion
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)   # monotonic
    assert all(t == 4 for _, t in seen)                # total never wobbles
    assert time.monotonic() - t0 < 2.0                 # ran concurrently


def test_agent_reports_progress_from_the_event_loop_thread(monkeypatch):
    """Claude adapter's `on_progress` fires on the caller's thread too (the
    event loop runs there via asyncio.run)."""
    import asyncio
    import threading
    from data import agent as research_agent

    async def fake_run(*_a, **_k):
        await asyncio.sleep(0.15)
        return "ok"

    monkeypatch.setattr(research_agent, "_run", fake_run)
    seen, caller = [], threading.get_ident()

    def on_progress(done, total):
        assert threading.get_ident() == caller
        seen.append((done, total))

    answers = research_agent.research_many(
        ["a", "b", "c"], model="opus", timeout=30, concurrency=3,
        on_progress=on_progress)
    assert [a[1] for a in answers] == [None] * 3
    assert seen[-1] == (3, 3)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_agent_timeout_is_forwarded_and_reported(monkeypatch):
    """A batch that outlives its budget comes back as that slot's error --
    with a short budget this must not hang the batch."""
    import asyncio
    from data import agent as research_agent

    async def slow_run(*_a, **_k):
        await asyncio.sleep(5.0)
        return "late"

    monkeypatch.setattr(research_agent, "_run", slow_run)
    answers = research_agent.research_many(
        ["a"], model="opus", timeout=1, concurrency=1)
    assert answers[0][0] == ""
    assert "Timed out after 1s." in answers[0][1]


# ------------------------------------------------------ sign-in reporting

# Sign-in state is NOT inferred from disk. That was tried: `.credentials.json`
# on Windows can carry every non-secret field (scopes, subscription, rate-limit
# tier) with both token strings blank while the CLI is perfectly signed in, so
# reading it and concluding "logged out" locked a signed-in user out of the
# button. The CLI is the only thing that knows, and it says so in-band; these
# pin that the UI can recognise that answer however it arrives.


def test_available_does_not_guess_at_the_session(monkeypatch):
    """`available()` gates on the SDK import and the CLI's version alone. A
    credentials file saying nothing useful must not be turned into a refusal
    to run."""
    from data import agent
    monkeypatch.setattr(agent, "cli_choice", lambda: ("claude.exe", agent.MIN_VERSION))
    assert agent.available() == (True, "")
    assert not hasattr(agent, "signed_in")


def test_the_in_band_failure_is_the_one_the_ui_recognises():
    """The UI keys off the message to decide whether to show the sign-in
    prompt, so the translation and the marker have to be the same string."""
    from data import agent
    assert agent.FAILURES["authentication_failed"] == agent.SIGN_IN_MESSAGE
    assert agent.FAILURES["oauth_token_expired"] == agent.SIGN_IN_MESSAGE
    assert agent.needs_sign_in(agent.SIGN_IN_MESSAGE)
    assert "`claude`" in agent.SIGN_IN_MESSAGE


def test_the_prompt_is_recognised_through_merges_partial_wrapper():
    """It reaches the UI wrapped in "2 of 4 searches failed: ...", not bare."""
    from data import agent
    wrapped = f"2 of 4 searches failed: {agent.SIGN_IN_MESSAGE}"
    assert agent.needs_sign_in(wrapped)


def test_needs_sign_in_ignores_failures_a_sign_in_would_not_fix():
    from data import agent
    for other in (None, "", "Timed out after 900s.",
                  "CLINotFoundError: claude not on PATH"):
        assert agent.needs_sign_in(other) is False


def test_an_auth_failure_reaches_the_caller_as_that_message(monkeypatch):
    """End to end through the adapter: the CLI's in-band code comes back as
    the sentence the UI matches on, not as the SDK's generic wrapper."""
    import asyncio
    from data import agent

    async def boom(*_a, **_k):
        raise agent.AgentFailure(agent.FAILURES["authentication_failed"])

    monkeypatch.setattr(agent, "_run", boom)
    answers = agent.research_many(["a", "b"], model="opus", concurrency=2)
    assert [a[0] for a in answers] == ["", ""]
    assert all(agent.needs_sign_in(a[1]) for a in answers)


def test_the_workbuddy_adapter_answers_the_same_question():
    """Both adapters carry `needs_sign_in` so the UI can ask whichever backend
    is selected without knowing which one it got."""
    from data import wb_agent
    assert wb_agent.needs_sign_in("anything at all") is False


# ------------------------------------------------ the cache's failure contract
#
# app.fetch_speech_roundup keeps its own dict cache and decides whether to
# persist a result from `roundup.ok`. The rule it depends on: a run where every
# batch failed for an external reason (expired CLI login, network) is `ok is
# False` and must NOT be cached, so re-clicking re-runs rather than replaying
# the failure for an hour. A partial -- some real appearances came back -- is
# `ok is True` and IS worth caching.

def _ap(member="Jerome Powell", src="https://federalreserve.gov/x"):
    return parse(payload(member=member, source=src)).appearances[0]


def test_all_batches_failed_is_not_ok_so_the_cache_will_not_keep_it():
    parts = [speeches.Roundup(START, END, error="auth expired"),
             speeches.Roundup(START, END, error="auth expired")]
    r = speeches.merge(parts, MEMBERS, START, END)
    assert r.ok is False
    assert r.error == "auth expired"


def test_a_partial_failure_is_ok_so_the_cache_keeps_the_real_appearances():
    good = speeches.Roundup(START, END, appearances=(_ap(),))
    bad = speeches.Roundup(START, END, error="one batch timed out")
    r = speeches.merge(parts=[good, bad], members=MEMBERS, start=START, end=END)
    assert r.ok is True                    # error is only set when NOTHING came back
    assert r.appearances and r.partial     # but the failure is still surfaced


def test_a_batch_that_merely_found_nothing_is_ok_and_cacheable():
    """Silence is a finding, not a failure -- it should cache like a hit."""
    r = speeches.merge([speeches.Roundup(START, END)], MEMBERS, START, END)
    assert r.ok is True and not r.partial and not r.error
