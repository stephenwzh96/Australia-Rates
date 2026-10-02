"""Sell-side bank calls: parsing, validation and the summary figures.

The two things that matter here are both about NOT showing something:

1. A call the agent could not source must not appear. In a table, a fluent
   invention and a published house view look identical, so an unsourced row is
   worse than a missing one -- the table would read as the whole street when it
   is only part of it.

2. A call that parsed but cannot be defended must not appear either. A note
   dated in the future, one published before the last decision, or a path no
   house would publish all survive a schema check and then sit there looking
   like a real view.

Rejections are returned with the book rather than discarded, so the table can
say what it dropped.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import bank_calls as bc

TODAY = date(2026, 9, 5)
MEETING = date(2026, 9, 16)
BANKS = ["Goldman Sachs", "Morgan Stanley", "JPMorgan"]


def _book(**over) -> bc.BankCall:
    base = dict(bank="Goldman Sachs", next_call="cut", next_bp=25.0,
                cuts_6m=2, cuts_12m=4, as_of=date(2026, 9, 3),
                source="https://example.com/note")
    base.update(over)
    return bc.BankCall(**base)


def _payload(calls: list[dict]) -> str:
    return json.dumps({"calls": calls})


def _one(**over) -> bc.CallBook:
    """A book holding exactly one call. Note the trailing comma -- `(_book())`
    is a parenthesised BankCall, not a one-tuple."""
    return bc.CallBook(MEETING, (_book(**over),))


def _row(**over) -> dict:
    base = {"bank": "Goldman Sachs", "next_call": "cut", "next_bp": 25,
            "hikes_6m": 0, "cuts_6m": 2, "hikes_12m": 0, "cuts_12m": 4,
            "as_of": "2026-09-03", "source": "https://example.com/note"}
    base.update(over)
    return base


# --------------------------------------------------------------------- parse

def test_a_sourced_call_parses():
    book = bc.parse(_payload([_row()]), BANKS, MEETING, TODAY)
    assert len(book.calls) == 1
    c = book.calls[0]
    assert c.bank == "Goldman Sachs" and c.next_call == "cut"
    assert c.net_6m == -2 and c.net_12m == -4
    assert c.path_6m == "-2" and c.path_12m == "-4"

def test_an_unsourced_call_is_dropped():
    """A call with no URL is an assertion, not a sourced view."""
    book = bc.parse(_payload([_row(source="")]), BANKS, MEETING, TODAY)
    assert book.calls == ()

def test_a_non_url_source_is_dropped():
    book = bc.parse(_payload([_row(source="Reuters, 3 Sep")]), BANKS, MEETING, TODAY)
    assert book.calls == ()

def test_a_bank_off_the_list_is_dropped():
    """Same rule as speeches: an unrecognised name cannot be attributed."""
    book = bc.parse(_payload([_row(bank="Some Boutique")]), BANKS, MEETING, TODAY)
    assert book.calls == ()

def test_an_unknown_call_value_is_dropped():
    book = bc.parse(_payload([_row(next_call="tighten")]), BANKS, MEETING, TODAY)
    assert book.calls == ()

def test_a_duplicate_bank_keeps_one_row():
    book = bc.parse(_payload([_row(), _row(cuts_12m=9)]), BANKS, MEETING, TODAY)
    assert len(book.calls) == 1

def test_no_json_is_an_error_not_a_silence():
    book = bc.parse("I searched but could not find published calls.", BANKS, MEETING, TODAY)
    assert not book.ok and book.error

def test_a_malformed_row_does_not_empty_the_book():
    """One bad row costs that row, not the run."""
    book = bc.parse(_payload([_row(), _row(bank="Morgan Stanley", next_call=None)]),
                    BANKS, MEETING, TODAY)
    assert [c.bank for c in book.calls] == ["Goldman Sachs"]

def test_a_change_is_only_a_change_when_the_previous_call_is_named():
    """'changed' with no 'from' cannot be rendered as a move, so it is not one."""
    book = bc.parse(_payload([_row(changed=True, prev_call="", change_reason="data")]),
                    BANKS, MEETING, TODAY)
    assert book.calls[0].changed is False

def test_a_named_change_is_recorded_with_its_reason():
    book = bc.parse(_payload([_row(changed=True, prev_call="hold",
                                   change_date="2026-09-02",
                                   change_reason="Payrolls revised lower")]),
                    BANKS, MEETING, TODAY)
    c = book.calls[0]
    assert c.changed and c.move == "hold → cut"
    assert c.change_reason == "Payrolls revised lower"
    assert c.change_date == date(2026, 9, 2)
    assert not c.reason_missing


def test_a_change_with_no_reason_is_flagged_rather_than_filled_in():
    book = bc.parse(_payload([_row(changed=True, prev_call="hold", change_reason="")]),
                    BANKS, MEETING, TODAY)
    assert book.calls[0].changed and book.calls[0].reason_missing


def test_a_fractional_count_is_dropped_not_rounded():
    """Half a 25bp move is a parsing artefact."""
    book = bc.parse(_payload([_row(cuts_12m=2.5)]), BANKS, MEETING, TODAY)
    assert book.calls[0].cuts_12m is None


# ------------------------------------------------------------------ validate

def test_a_note_dated_in_the_future_is_rejected():
    book = bc.validate(_one(as_of=date(2026, 10, 1)), TODAY)
    assert book.calls == () and any("future" in r for r in book.rejected)


def test_a_note_older_than_two_months_is_rejected():
    """Published before the last decision, so it cannot be about this one."""
    book = bc.validate(_one(as_of=date(2026, 6, 1)), TODAY)
    assert book.calls == () and any("60 days" in r for r in book.rejected)


def test_an_undated_note_is_rejected():
    book = bc.validate(_one(as_of=None), TODAY)
    assert book.calls == () and any("no date" in r for r in book.rejected)


def test_an_implausible_path_is_rejected():
    book = bc.validate(_one(cuts_12m=99), TODAY)
    assert book.calls == () and any("implausible" in r for r in book.rejected)


def test_a_negative_count_is_rejected():
    book = bc.validate(_one(cuts_12m=-2), TODAY)
    assert book.calls == ()


def test_a_move_with_no_size_is_rejected():
    book = bc.validate(_one(next_call="cut", next_bp=None), TODAY)
    assert book.calls == () and any("no size" in r for r in book.rejected)


def test_a_change_dated_in_the_future_is_rejected():
    book = bc.validate(_one(changed=True, prev_call="hold", change_date=date(2026, 12, 1)), TODAY)
    assert book.calls == ()


def test_a_good_call_survives_validation():
    book = bc.validate(bc.CallBook(MEETING, (_book(),)), TODAY)
    assert len(book.calls) == 1 and not book.rejected


# ------------------------------------------------------------------- summary

def _book_with(*calls) -> bc.CallBook:
    return bc.CallBook(MEETING, tuple(calls))


def test_tally_counts_each_call():
    b = _book_with(_book(next_call="cut", cuts_12m=2),
                   _book(bank="Morgan Stanley", next_call="cut", cuts_12m=3),
                   _book(bank="JPMorgan", next_call="hold", cuts_12m=1))
    assert bc.tally(b) == {"hike": 0, "hold": 1, "cut": 2}


def test_consensus_is_the_modal_call():
    b = _book_with(_book(next_call="cut"),
                   _book(bank="Morgan Stanley", next_call="cut"),
                   _book(bank="JPMorgan", next_call="hold"))
    assert bc.consensus_call(b) == "cut"


def test_a_tied_street_reads_as_split_not_as_a_false_consensus():
    b = _book_with(_book(next_call="cut"),
                   _book(bank="Morgan Stanley", next_call="hold"))
    assert bc.consensus_call(b) == "split"


def test_median_path_uses_an_even_split_correctly():
    b = _book_with(_book(next_call="cut", cuts_12m=2),
                   _book(bank="Morgan Stanley", next_call="cut", cuts_12m=4))
    assert bc.median_path(b, "12m") == -3


def test_dispersion_measures_how_split_the_street_is():
    tight = _book_with(_book(cuts_12m=2), _book(bank="Morgan Stanley", cuts_12m=2))
    wide = _book_with(_book(cuts_12m=1), _book(bank="Morgan Stanley", cuts_12m=5),
                      _book(bank="JPMorgan", next_call="hike", hikes_12m=1, cuts_12m=0))
    assert bc.dispersion(tight, "12m") == 0
    assert bc.dispersion(wide, "12m") == 6      # +1 hike down to -5 cuts


def test_changed_calls_come_most_recent_first():
    b = _book_with(_book(changed=True, prev_call="hold", change_date=date(2026, 9, 1)),
                   _book(bank="Morgan Stanley", changed=True, prev_call="hold",
                         change_date=date(2026, 9, 4)),
                   _book(bank="JPMorgan", next_call="hold"))
    assert [c.bank for c in b.changed_calls] == ["Morgan Stanley", "Goldman Sachs"]


# -------------------------------------------------------------- persistence

def test_a_book_survives_a_dict_round_trip():
    b = _book_with(_book(changed=True, prev_call="hold", change_reason="payrolls",
                         change_date=date(2026, 9, 2), end_rate=3.375,
                         note="sees a pause after"),
                   _book(bank="Morgan Stanley", next_call="hold", next_bp=None))
    out = bc.book_from_dict(bc.book_to_dict(b))
    assert len(out.calls) == 2
    c = out.by_name()["Goldman Sachs"]
    assert c.change_date == date(2026, 9, 2) and c.end_rate == 3.375
    assert c.note == "sees a pause after" and c.change_reason == "payrolls"
    assert out.by_name()["Morgan Stanley"].net_6m == -2


def test_a_saved_run_missing_later_fields_still_loads():
    """A run saved before a field existed must degrade, not vanish."""
    out = bc.book_from_dict({"calls": [{"bank": "JPMorgan", "next_call": "cut",
                                        "source": "https://x.com"}]})
    assert len(out.calls) == 1
    c = out.calls[0]
    assert c.end_rate is None and c.as_of is None and not c.changed


def test_the_prompt_names_the_meeting_and_the_horizons():
    p = bc.prompt(["Goldman Sachs"], MEETING, TODAY)
    assert "16 September 2026" in p and "Goldman Sachs" in p
    assert "hikes_6m" in p and "cuts_12m" in p and "change_reason" in p


def test_batches_cover_every_house_once():
    groups = bc.batches(list(bc.BANKS))
    flat = [b for g in groups for b in g]
    assert sorted(flat) == sorted(bc.BANKS)
    assert all(len(g) <= bc.BATCH_SIZE for g in groups)


# --------------------------------------------------- batch-result assembly

def _answers(*pairs):
    """answers in research_many shape: (raw, err) per group."""
    return list(pairs)


def test_every_batch_failing_is_an_error_not_a_partial():
    groups = [["Goldman Sachs"], ["Morgan Stanley"]]
    answers = _answers(("", "CodeBuddy CLI failed: boom"),
                       ("", "CodeBuddy CLI failed: boom"))
    book = bc.book_from_answers(groups, answers, MEETING, TODAY)
    assert not book.ok and not book.calls and not book.partial


def test_a_silent_throttle_reply_counts_as_a_failed_batch():
    """A 429 or apology arrives as a STRING, not an err. parse() reports it
    via .error on a zero-call book. A caller reading only `err` would call
    that batch a success and save a book that says nothing failed while
    covering a fraction of the houses -- exactly what the first live run
    did. The assembly must count it as failed, and a good batch beside it
    must make the book partial, not silently full."""
    good = json.dumps({"calls": [_row(as_of="2026-09-03")]})
    groups = [["Goldman Sachs"], ["Morgan Stanley"]]
    answers = _answers((good, None),
                       ("429 usage exceeds frequency limit", None))
    book = bc.book_from_answers(groups, answers, MEETING, TODAY)
    assert book.ok and len(book.calls) == 1
    assert book.partial == "1 of 2 searches failed"
    assert book.calls[0].bank == "Goldman Sachs"


def test_a_parsed_empty_calls_array_is_not_a_failure():
    """The agent said 'none of these houses has a current sourced view'.
    That is a thin SEARCH, not a broken batch, and must not raise partial."""
    groups = [["Wells Fargo"]]
    answers = _answers((json.dumps({"calls": []}), None))
    book = bc.book_from_answers(groups, answers, MEETING, TODAY)
    assert book.ok and not book.calls and not book.partial


def test_batch_assembly_validates_before_returning():
    """Rows that parse but fail validation must be dropped here, with the
    rejection carried, so a caller cannot present them by skipping the
    validate step."""
    stale = json.dumps({"calls": [_row(bank="Goldman Sachs",
                                       as_of="2026-06-01")]})
    groups = [["Goldman Sachs"]]
    book = bc.book_from_answers(groups, [(stale, None)], MEETING, TODAY)
    assert not book.calls and "Goldman Sachs" in book.rejected[0]
