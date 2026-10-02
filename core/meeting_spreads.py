"""The meeting-spread matrix -- every forward rate against every other."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .bbsw import BillCurve
from .strip import StripPath

SPOT_KEY = "__spot__"


@dataclass(frozen=True)
class Node:
    """One point on both axes: a forward rate with something to call it."""
    key: str
    label: str            # the axis label -- "03 Nov 26", "IRZ6"
    tag: str              # the contract behind it -- "IBX6", "Spot"
    rate: float           # percent
    when: date | None
    is_spot: bool = False
    reliable: bool = True


@dataclass(frozen=True)
class SpreadMatrix:
    nodes: list[Node]
    axis: str             # "meeting" | "contract"
    spot: float | None = None

    @property
    def ok(self) -> bool:
        return len(self.nodes) >= 2

    @property
    def priced(self) -> list[Node]:
        """Everything except the spot node -- the forwards themselves."""
        return [n for n in self.nodes if not n.is_spot]

    def bp(self, row: int, col: int) -> float:
        """The cell: column rate less row rate, in basis points."""
        return round((self.nodes[col].rate - self.nodes[row].rate) * 100.0, 4)

    @property
    def grid(self) -> list[list[float]]:
        n = range(len(self.nodes))
        return [[self.bp(i, j) for j in n] for i in n]

    @property
    def extreme_bp(self) -> float:
        """Largest absolute spread anywhere -- what a heatmap scales against."""
        if not self.ok:
            return 0.0
        rates = [n.rate for n in self.nodes]
        return round((max(rates) - min(rates)) * 100.0, 4)

    @property
    def first_spread(self) -> float | None:
        """Spot to the nearest forward -- what is priced for the next one."""
        rest = self.priced
        return None if not rest or self.spot is None else round(
            (rest[0].rate - self.spot) * 100.0, 4)

    @property
    def end_move(self) -> float | None:
        """Spot to the last RELIABLE forward: the whole priced cycle."""
        rel = [n for n in self.priced if n.reliable]
        return None if not rel or self.spot is None else round(
            (rel[-1].rate - self.spot) * 100.0, 4)

    def head(self, count: int | None) -> "SpreadMatrix":
        """The spot node plus the nearest `count` forwards."""
        rest = self.priced
        if count is None or count >= len(rest):
            return self
        spot = [n for n in self.nodes if n.is_spot][:1]
        return SpreadMatrix(spot + rest[:count], self.axis, self.spot)

    @property
    def peak(self) -> Node | None:
        rel = [n for n in self.priced if n.reliable]
        return max(rel, key=lambda n: n.rate) if rel else None

    @property
    def trough(self) -> Node | None:
        rel = [n for n in self.priced if n.reliable]
        return min(rel, key=lambda n: n.rate) if rel else None


def _spot_node(spot: float) -> Node:
    return Node(SPOT_KEY, "Cash rate", "Spot", spot, None, is_spot=True)


def from_strip(path: StripPath, limit: int | None = None) -> SpreadMatrix:
    """The meeting matrix, off the IB strip."""
    if not path.ok:
        return SpreadMatrix([], "meeting", path.spot)
    steps = path.steps[:limit] if limit else path.steps
    nodes = [_spot_node(path.spot)] + [
        Node(key=s.key, label=f"{s.meeting.end:%d %b %y}", tag=s.code,
             rate=s.r_after, when=s.meeting.end, reliable=s.reliable)
        for s in steps
    ]
    return SpreadMatrix(nodes, "meeting", path.spot)


def from_bills(curve: BillCurve, spot: float,
               limit: int | None = None) -> SpreadMatrix:
    """The contract matrix, off the IR strip: one node per bill contract, at the
    3-month BBSW it prices for its fix. A bill spans 90 days, so a node is a
    contract, not a meeting -- and BBSW carries the bank credit spread the cash
    rate does not, which is why the spot-to-front cell includes the basis."""
    if not curve.ok:
        return SpreadMatrix([], "contract", spot)
    periods = curve.periods[:limit] if limit else curve.periods
    nodes = [_spot_node(spot)] + [
        Node(key=p.code, label=p.code,
             tag=f"{p.covers_start:%b}–{p.covers_end:%b %y}",
             rate=p.implied, when=p.fix_date, reliable=not p.stale)
        for p in periods
    ]
    return SpreadMatrix(nodes, "contract", spot)
