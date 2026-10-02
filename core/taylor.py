"""Balanced-approach ("Taylor") rule grid for the cash rate.

The US module's rule and grid, unchanged in form, with Australian constants:
inflation is the trimmed mean (the measure the Board targets) against the 2.5%
midpoint of the 2-3% band, and the unemployment gap is measured against the
RBA's NAIRU estimate (around 4.5% in recent Statements). The real neutral rate
is a judgement: the RBA's published model estimates span roughly 0-2% real,
and 1% is the middle of that range. Unlike the US grid, u* is its own input
here -- the RBA publishes a NAIRU estimate separately from any r*."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Sequence

Stance = Literal["cut", "hold", "hike"]

STEP = 0.25


@dataclass(frozen=True)
class RuleParams:
    """The balanced-approach rule's four constants."""
    r_star: float = 1.0            # real neutral rate, the middle of the RBA's model range
    pi_star: float = 2.5           # the midpoint of the 2-3% target band
    u_star: float = 4.5            # the RBA's NAIRU estimate
    inflation_coeff: float = 0.5   # weight on the inflation gap
    unemployment_coeff: float = 2.0  # weight on the unemployment gap

    @property
    def nominal_neutral(self) -> float:
        """r* + pi* -- where the rule sits with both gaps closed."""
        return self.r_star + self.pi_star


DEFAULT_PARAMS = RuleParams()

#: Trimmed mean (rows) and unemployment (columns) the grid spans by default.
CORE_PCE_AXIS: tuple[float, ...] = tuple(round(2.2 + 0.1 * i, 2) for i in range(14))
UNEMPLOYMENT_AXIS: tuple[float, ...] = tuple(round(4.0 + 0.1 * i, 2) for i in range(9))

DEFAULT_CURRENT_RATE = 4.60


def rule_rate(core_pce: float, unemployment: float,
              params: RuleParams = DEFAULT_PARAMS) -> float:
    """The rule's prescribed nominal cash rate, in percent. `core_pce` is the
    inflation input (the trimmed mean here; the name is the US module's)."""
    inflation_gap = core_pce - params.pi_star
    unemployment_gap = params.u_star - unemployment
    return (params.r_star
            + core_pce
            + params.inflation_coeff * inflation_gap
            + params.unemployment_coeff * unemployment_gap)


def display(rate: float, places: int = 2) -> str:
    """Percent to `places` decimals, rounding half AWAY FROM ZERO."""
    quantum = Decimal(1).scaleb(-places)
    d = Decimal(f"{rate:.6f}").quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{d:f}%"


@dataclass(frozen=True)
class HoldBand:
    """Where the rule is close enough to current policy to imply no move."""
    current_rate: float = DEFAULT_CURRENT_RATE
    half_move: float = STEP / 2

    @property
    def low(self) -> float:
        return self.current_rate - self.half_move

    @property
    def high(self) -> float:
        return self.current_rate + self.half_move

    def classify(self, rate: float) -> Stance:
        if rate > self.high + 1e-9:
            return "hike"
        if rate < self.low - 1e-9:
            return "cut"
        return "hold"

    def moves(self, rate: float) -> float:
        """Prescribed distance from current policy, in 25bp moves (signed)."""
        return (rate - self.current_rate) / STEP


@dataclass(frozen=True)
class Cell:
    core_pce: float
    unemployment: float
    rate: float
    stance: Stance
    gap_bp: float        # rule minus current policy, in basis points
    moves: float         # the same gap expressed in 25bp steps

    @property
    def label(self) -> str:
        return display(self.rate)


@dataclass(frozen=True)
class Anchor:
    """A forecast pair worth boxing on the grid -- e.g. an SMP forecast."""
    label: str
    core_pce: float
    unemployment: float
    cell: Cell | None


@dataclass(frozen=True)
class Grid:
    params: RuleParams
    band: HoldBand
    core_pce_axis: tuple[float, ...]
    unemployment_axis: tuple[float, ...]
    rows: tuple[tuple[Cell, ...], ...]
    anchors: tuple[Anchor, ...]

    def cell(self, core_pce: float, unemployment: float) -> Cell | None:
        for row in self.rows:
            for c in row:
                if abs(c.core_pce - core_pce) < 1e-9 and abs(c.unemployment - unemployment) < 1e-9:
                    return c
        return None

    def count(self, stance: Stance) -> int:
        return sum(1 for row in self.rows for c in row if c.stance == stance)

    @property
    def whiplash_bp(self) -> float | None:
        """Gap between the first two anchors, in basis points."""
        placed = [a for a in self.anchors if a.cell is not None]
        if len(placed) < 2:
            return None
        return (placed[1].cell.rate - placed[0].cell.rate) * 100.0


def build(core_pce_axis: Sequence[float] = CORE_PCE_AXIS,
          unemployment_axis: Sequence[float] = UNEMPLOYMENT_AXIS,
          params: RuleParams = DEFAULT_PARAMS,
          band: HoldBand | None = None,
          anchors: Sequence[tuple[str, float, float]] = ()) -> Grid:
    """The whole table: rule rate and stance for every (core PCE, u) pair."""
    b = band or HoldBand()
    rows = tuple(
        tuple(
            Cell(core_pce=round(pi, 2), unemployment=round(u, 2),
                 rate=(r := rule_rate(pi, u, params)),
                 stance=b.classify(r),
                 gap_bp=(r - b.current_rate) * 100.0,
                 moves=b.moves(r))
            for u in unemployment_axis
        )
        for pi in core_pce_axis
    )
    grid = Grid(params=params, band=b,
                core_pce_axis=tuple(round(x, 2) for x in core_pce_axis),
                unemployment_axis=tuple(round(x, 2) for x in unemployment_axis),
                rows=rows, anchors=())
    resolved = tuple(
        Anchor(label, round(pi, 2), round(u, 2), grid.cell(round(pi, 2), round(u, 2)))
        for label, pi, u in anchors
    )
    return Grid(params=params, band=b,
                core_pce_axis=grid.core_pce_axis,
                unemployment_axis=grid.unemployment_axis,
                rows=rows, anchors=resolved)


def implied_u_star(r_star: float, reference: RuleParams = DEFAULT_PARAMS) -> float:
    """The `u*` that keeps the grid unchanged when `r*` moves."""
    constant = reference.r_star + reference.unemployment_coeff * reference.u_star
    return (constant - r_star) / reference.unemployment_coeff
