"""What each 90 day bank bill future settles at under a given policy path.

The rates desk's scenario grid, as the US version builds it for SR3: rows are
policy paths ("Nov, Feb" = a move at each), columns are IR contracts, each
cell the price the contract would settle at if that path were delivered.

The settlement mechanics are the IR contract's, not SR3's. An IR contract
settles at 100 minus the 3-month BBSW published on its last trading day -- one
fix, on one morning -- and 3-month BBSW is the 90-day bank bill rate: the cash
rate expected over the bill's own 90 days, plus a bank credit spread. So a cell
is

    price = 100 - ( mean cash rate over [fix, fix + 90d) under the path + basis )

with the mean a simple day-weighted average (a bill is a simple-yield discount
instrument and `core.bbsw` averages the IB path the same way), and the basis --
BBSW over the cash path -- held flat at the value the caller supplies. A move
counts from the day after its decision (`contracts.effective_date`), so a
meeting inside a bill's 90 days moves the fix only for the days after it, and
one after the fix not at all for that contract.

Pure: no Streamlit, no I/O. `ui.scenarios` supplies the quotes and the paths;
the path vocabulary (`Path`, `clean_move`, `path_label`, `default_paths`) is
the US module's, so a row reads identically on both desks.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Mapping, Sequence

from .contracts import IR_TENOR_DAYS, effective_date
from .rba_calendar import Meeting

#: A move larger than this at one meeting is a typo, not a scenario.
MAX_MOVE_BP = 200.0


@dataclass(frozen=True)
class Contract:
    """One IR contract: its code, its fix, the 90 days the bill delivered on
    that fix spans, and today's price."""

    code: str
    fix: date
    start: date
    end: date
    market: float | None = None      # settlement price today; None when not quoted

    @classmethod
    def from_fix(cls, code: str, fix: date, market: float | None = None) -> "Contract":
        return cls(code, fix, fix, fix + timedelta(days=IR_TENOR_DAYS), market)


@dataclass(frozen=True)
class Path:
    """A scenario: bp moved at each meeting (meeting key -> bp; 0 = no move)."""

    moves: Mapping[str, float] = field(default_factory=dict)
    label: str = ""

    def effective(self, meetings: Sequence[Meeting]) -> list[tuple[date, float]]:
        """(effective date, move in percentage points), in date order."""
        by_key = {m.key: m for m in meetings}
        return sorted((effective_date(by_key[k].end), bp / 100.0)
                      for k, bp in self.moves.items() if bp and k in by_key)


@dataclass(frozen=True)
class Cell:
    price: float | None
    rate: float | None

    def vs(self, market: float | None) -> float | None:
        """Price above the market, in bp (positive = the contract rallies)."""
        if self.price is None or market is None:
            return None
        return (self.price - market) * 100.0


def clean_move(value) -> float:
    """A typed move as bp: blanks, text and NaN are no move; huge values are
    clamped to +/-MAX_MOVE_BP rather than silently pricing a 2,500bp hike."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return max(-MAX_MOVE_BP, min(MAX_MOVE_BP, v))


def path_label(moves: Mapping[str, float], meetings: Sequence[Meeting], size: float) -> str:
    """"Nov, Feb" when every move is `size` the same way; otherwise each move
    carries its size: "Nov +25, Feb +50". "No change" for an empty path. A
    meeting after the first year of `meetings` carries its year."""
    order = {m.key: m for m in meetings}
    live = sorted(((order[k], bp) for k, bp in moves.items() if bp and k in order),
                  key=lambda t: t[0].end)
    if not live:
        return "No change"
    uniform = len({bp for _, bp in live}) == 1 and abs(live[0][1]) == abs(size)
    first_year = min(m.end.year for m in meetings)

    def name(m: Meeting) -> str:
        return m.end.strftime("%b") if m.end.year == first_year else m.end.strftime("%b %y")

    if uniform:
        return ", ".join(name(m) for m, _ in live)
    return ", ".join(f"{name(m)} {bp:+g}" for m, bp in live)


def default_paths(meetings: Sequence[Meeting], size: float, sign: int,
                  depth: int = 4) -> list[dict[str, float]]:
    """The desk's standard ladder: a move at the first meeting, then at the
    first two, the first three ... up to `depth` meetings."""
    step = abs(size) * (1 if sign >= 0 else -1)
    keys = [m.key for m in list(meetings)[:depth]]
    return [{k: (step if i < n else 0.0) for i, k in enumerate(keys)}
            for n in range(1, len(keys) + 1)]


def mean_cash(start: date, end: date, base: float,
              moves: Sequence[tuple[date, float]]) -> float | None:
    """Day-weighted mean of the cash rate over [start, end) when `base` holds
    today and each (effective date, pp) move applies from its date on."""
    days = (end - start).days
    if days <= 0:
        return None
    eff = [d for d, _ in moves]
    cum, running = [], 0.0
    for _, pp in moves:
        running += pp
        cum.append(running)
    total, day = 0.0, start
    while day < end:
        n = bisect_right(eff, day)
        total += base + (cum[n - 1] if n else 0.0)
        day += timedelta(days=1)
    return total / days


def price(contract: Contract, path: Path, meetings: Sequence[Meeting], cash: float,
          basis_bp: float, as_of: date) -> Cell:
    """The settlement price of `contract` if `path` is delivered.

    `cash` is today's cash rate (percent) and `basis_bp` the BBSW-over-cash
    spread held to the fix. Moves already in force by the as-of are in `cash`."""
    moves = [(eff, pp) for eff, pp in path.effective(meetings) if eff > as_of]
    avg = mean_cash(contract.start, contract.end, cash, moves)
    if avg is None:
        return Cell(None, None)
    rate = avg + basis_bp / 100.0
    return Cell(round(100.0 - rate, 6), rate)


@dataclass(frozen=True)
class Row:
    label: str
    cells: list[Cell]


@dataclass(frozen=True)
class Grid:
    contracts: list[Contract]
    rows: list[Row]
    cash: float
    basis_bp: float
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.contracts and self.rows)


def build(contracts: Sequence[Contract], paths: Sequence[Path], meetings: Sequence[Meeting],
          cash: float, basis_bp: float, as_of: date) -> Grid:
    """The grid: one row per path, one column per contract still to fix.

    A contract that has already fixed has nothing left to price and is dropped;
    so is a meeting already in force (its move is in `cash`)."""
    live = [c for c in contracts if c.fix > as_of]
    if not live:
        return Grid([], [], cash, basis_bp, "no IR contract is still to fix")
    future = [m for m in meetings if effective_date(m.end) > as_of]
    rows = [Row(p.label or path_label(p.moves, future, 25.0),
                [price(c, p, future, cash, basis_bp, as_of) for c in live])
            for p in paths]
    return Grid(live, rows, cash, basis_bp)
