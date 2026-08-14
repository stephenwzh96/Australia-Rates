"""Step 8 -- vote arithmetic.

The crux: a bloc of dissenters is not a decision. The trade is short the *policy
outcome*, not short the *vote split*. Three hawks voting to hike with nobody
joining is a hold-with-three-dissents, the contract settles at 0, and a fade
pays in full.

So q decomposes into an observable and a decisive part:

    q = P(bloc votes for the move) x P(centre joins | bloc moves)

The bloc's behaviour is broadly readable from speeches. The centre's is not,
and the centre is what settles the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .pricing import Side, breakeven_probability, expected_value


@dataclass(frozen=True)
class Voter:
    name: str
    role: str
    bloc: str
    score: float          # 0-5 hawk-dove, 5 = most hawkish
    is_mover: bool        # a "move candidate" -- counted in P(bloc), regardless of `bloc` field


@dataclass(frozen=True)
class BlocTally:
    bloc: str
    members: list[str]
    count: int
    mean_score: float


@dataclass(frozen=True)
class VoteArithmetic:
    total_voters: int
    votes_needed: int
    mover_count: int
    shortfall: int
    recruitable: int
    blocs: list[BlocTally]
    mover_names: list[str]
    majority_reachable: bool


def votes_needed(total_voters: int) -> int:
    """Simple majority: floor(n/2) + 1. Twelve voters need seven."""
    return total_voters // 2 + 1


def tally(roster: Sequence[Voter]) -> VoteArithmetic:
    total = len(roster)
    needed = votes_needed(total)
    movers = [v for v in roster if v.is_mover]
    shortfall = max(0, needed - len(movers))
    recruitable = total - len(movers)

    order: list[str] = []
    for v in roster:
        if v.bloc not in order:
            order.append(v.bloc)

    blocs: list[BlocTally] = []
    for name in order:
        members = [v for v in roster if v.bloc == name]
        blocs.append(
            BlocTally(
                bloc=name,
                members=[v.name for v in members],
                count=len(members),
                mean_score=sum(v.score for v in members) / len(members) if members else 0.0,
            )
        )

    return VoteArithmetic(
        total_voters=total,
        votes_needed=needed,
        mover_count=len(movers),
        shortfall=shortfall,
        recruitable=recruitable,
        blocs=blocs,
        mover_names=[v.name for v in movers],
        majority_reachable=recruitable >= shortfall,
    )


# --------------------------------------------------------------------------
# The decomposition
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Decomposition:
    p_bloc: float
    p_centre: float
    q: float
    override_q: float | None
    reconciles: bool


def decompose(p_bloc: float, p_centre: float, override_q: float | None = None,
              tol: float = 5e-3) -> Decomposition:
    """q = P(bloc moves) x P(centre joins | bloc moves).

    When an override is set, flag whether the decomposition reconciles with it
    rather than silently preferring one. A mismatch is information: it means the
    number you are sizing on is not the number your vote read supports.
    """
    q = p_bloc * p_centre
    reconciles = override_q is None or abs(q - override_q) <= tol
    return Decomposition(p_bloc, p_centre, q, override_q, reconciles)


@dataclass(frozen=True)
class ConditionalRow:
    p_centre: float
    ev: float
    is_breakeven: bool


def conditional_ev(
    points: float,
    size: float,
    side: Side = "fade",
    centre_grid: Sequence[float] = (0.20, 0.30, 0.40, 0.50),
) -> list[ConditionalRow]:
    """EV *given* the bloc has voted for the move, across P(centre joins).

    Note the breakeven is `points / size` again -- identical to Step 3. That
    invariance is a property of the payoff, not of the scenario, and it means a
    conditional scenario can be judged against the same single number.
    """
    be = breakeven_probability(points, size)
    grid = list(centre_grid)
    if not any(abs(g - be) < 1e-9 for g in grid):
        grid.append(be)
    grid = sorted(set(round(g, 10) for g in grid))
    return [
        ConditionalRow(g, expected_value(points, size, g, side), abs(g - be) < 1e-9)
        for g in grid
    ]


def voters_from_dicts(rows: Sequence[dict]) -> list[Voter]:
    return [
        Voter(
            name=r["name"],
            role=r.get("role", ""),
            bloc=r.get("bloc", "Unassigned"),
            score=float(r.get("score2", r.get("score1", r.get("score", 2.5)))),
            is_mover=bool(r.get("is_mover", False)),
        )
        for r in rows
    ]
