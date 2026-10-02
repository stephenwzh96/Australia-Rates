"""The Board's membership, its published votes, and the roster that reads them."""

from core import roster as R
from data import rba_board, rba_decisions
from state import store

STATEMENT = """<p>Number 2026-08</p><p>Date 17&nbsp;March 2026</p>
<p>At its meeting today, the Board decided to increase the cash rate target by 25 basis
points to 4.10 per cent. While inflation has fallen ...</p>
<p>Today’s policy decision was made by majority: five members voted to increase the cash
rate target by 25 basis points to 4.10 per cent; four members voted to leave the cash rate
target unchanged at 3.85 per cent.</p><p>More on the March 2026 monetary policy decision</p>"""

EARLY = """<p>Date 8 July 2025</p><p>At its meeting today, the Board decided to leave the cash
rate target unchanged at 3.85 per cent.</p><p>Today's policy decision was made by majority:
6 in favour, 3 against.</p>"""

BOARD = """<div class="item"><img src="/assets/images/people/michele-bullock.jpg?v=1" />
<h3 class="name">Chair: <a href="/about-rba/people/gov.html">Michele Bullock</a></h3></div>
<div class="item" id="x"><img src="/assets/images/people/board/a-b.jpg?v=2" />
<h3 class="name">Non-executive Member: <a href="/m/a-b.html">Ann Bee <abbr title="x">AO</abbr></a></h3>
<p>Member since 1&nbsp;March 2026</p></div>
<div class="item"><h3 class="name">Ex officio Member: <a href="/m/t.html">Tess Ury</a></h3></div>
<h2>Communications</h2>"""


def test_statement_vote_split_and_dissent_side():
    d = rba_decisions.parse(STATEMENT)
    assert (d.day, d.action, d.change_bp, d.level) == ("2026-03-17", "hike", 25, 4.10)
    assert d.votes == {"hike": 5, "hold": 4} and d.split == "5–4"
    assert d.dissent_side == "dovish" and d.unanimous is False


def test_first_published_vote_reads_the_short_form():
    d = rba_decisions.parse(EARLY)
    assert d.action == "hold" and d.votes == {"hold": 6, "cut": 3}
    assert d.dissent_side == "dovish"


def test_board_page_parse_orders_seats_and_drops_honours():
    ms = rba_board.parse(BOARD)
    assert [m.role for m in ms] == ["Governor, Chair", "Secretary to the Treasury",
                                    "External member"]
    assert ms[2].name == "Ann Bee" and ms[2].since == "1 March 2026"
    assert ms[0].photo.endswith("/michele-bullock.jpg")


def test_seat_board_fills_blank_seats_and_drops_leavers():
    rows = [{"name": "Michele Bullock", "role": "Governor, Chair", "score": 3.0},
            {"name": "Old Member", "role": "External member", "score": 4.0},
            {"name": "", "role": "External member", "score": 2.0},
            {"name": "", "role": "Secretary to the Treasury", "score": 2.5}]
    board = rba_board.parse(BOARD)
    out, joined, left = R.seat_board(rows, board)
    assert [r["name"] for r in out] == ["Michele Bullock", "Tess Ury", "Ann Bee"]
    assert joined == ["Tess Ury", "Ann Bee"] and left == ["Old Member"]
    assert out[2]["score2"] == 2.0                # took the blank seat's mark
    assert R.seat_board(rows, []) == (rows, [], [])


def test_clone_rolls_scores_forward():
    src = {"roster": [{"name": "A", "role": "External member", "score1": 2.0, "score2": 3.5,
                       "bloc1": "External", "bloc2": "Hawks"}]}
    state = {"roster": [dict(src["roster"][0])]}
    out = store.roll_roster(state, "2026-09-29", src)
    row = out["roster"][0]
    assert row["score1"] == 3.5 and row["bloc1"] == "Hawks" and row["score2"] == 3.5
    assert out["score_baseline"] == "2026-09-29"


def test_seat_board_keeps_a_score_typed_without_the_accent():
    from data.rba_board import Member
    board = [Member("Renée Fry-McKibbin", "External member", "", "", "")]
    rows = [{"name": "Renee Fry-McKibbin AO", "role": "External member", "score": 3.8}]
    out, joined, left = R.seat_board(rows, board)
    assert out[0]["name"] == "Renée Fry-McKibbin" and out[0]["score2"] == 3.8
    assert joined == [] and left == []

