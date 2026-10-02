"""Moves mode: a contract carrying several undecided meetings is a count of moves.

Ported from the US version with the AU contracts. An IB month past the next
decision, or an IR bill whose 90 days span a meeting, prices more than one
yes/no -- so the view is held in moves and collapsed to an equivalent binary
bet for sizing, while the exit levels stay on the quoted bp.
"""

from datetime import date

from core import model, moves, rba_calendar, sizing
from state import store

AS_OF = date(2026, 10, 2)
MEETINGS = list(rba_calendar.DEFAULT_MEETINGS)
NOV = next(m for m in MEETINGS if m.end == date(2026, 11, 3))


def _state(contract: str, points: float = 10.0, view: float | None = None) -> dict:
    st = store.new_meeting(rba_calendar.meeting_to_dict(NOV), AS_OF)
    st["trade"].update(points=points, size=25.0, side="fade", direction="hike")
    st["sizing"]["contract"] = contract
    if view is not None:
        st["moves"] = {"moves": view, "coherence": "typical cycle"}
    return st


def test_horizons_follow_the_contract():
    universe = {c.key: c for c in sizing.contract_universe(AS_OF)}
    assert [m.end for m in sizing.contract_horizon(universe["ib:2026-11"], MEETINGS, AS_OF)] \
        == [date(2026, 11, 3)]
    two = sizing.contract_horizon(universe["ib:2026-12"], MEETINGS, AS_OF)
    assert [m.end for m in two] == [date(2026, 11, 3), date(2026, 12, 8)]


def test_single_meeting_contract_is_not_moves_mode():
    M = model.build(_state("ib:2026-11"), as_of=AS_OF, all_meetings=MEETINGS)
    assert not M.multi_move
    assert M.level_points == M.points == 10.0


def test_multi_meeting_contract_runs_in_moves():
    M = model.build(_state("ib:2026-12", points=20.0, view=0.5), as_of=AS_OF,
                    all_meetings=MEETINGS)
    assert M.multi_move and M.moves.n == 2
    # Levels stay on the quoted scale; the binary bet is the effective one.
    assert M.level_points == 20.0 and M.level_size == 50.0
    assert M.trade_side == "fade" and M.side == "back"
    assert abs(M.level_q - 0.25) < 1e-12
    # The market prices 0.8 moves; a view of 0.5 fades it, so the edge is positive.
    assert M.moves.edge_bp > 0


def test_market_count_opens_the_view():
    M = model.build(_state("ib:2026-12", points=20.0), as_of=AS_OF, all_meetings=MEETINGS)
    assert abs(M.moves.moves_you - 0.8) < 1e-12
    assert abs(M.moves.edge_bp) < 1e-9


def test_beta_binomial_mean_is_exact():
    pmf = moves.beta_binomial_pmf(3, 1.2, 0.12)
    assert abs(sum(k * p for k, p in enumerate(pmf)) - 1.2) < 1e-9
