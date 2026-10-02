"""Your named read of the Board -- one dot per member, per horizon.

The FOMC publishes an anonymised dot plot and the US tab lets you put a name on
each dot. The RBA publishes no dots at all, and votes unattributed, so here
both the name and the level are your judgement -- a structured way to write
down where you think each of the nine members would put the cash rate at a
horizon, read against the two things that ARE published: the Statement's
cash-rate assumption and the last vote split.

Every member of the Monetary Policy Board votes at every meeting (there is no
rotation), so the groups are seats -- executive, ex officio, external -- and
the voters' median is everyone's median. Dots snap to 5bp, the grid the cash
rate itself moves on (4.10, 4.35, 4.60).
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median as _median
from typing import Mapping, Sequence

DOT_STEP = 0.05

EXECUTIVE, EX_OFFICIO, EXTERNAL = "Executive", "Ex officio", "External"
GROUPS: tuple[str, str, str] = (EXECUTIVE, EX_OFFICIO, EXTERNAL)


def default_labels(year: int) -> tuple[str, ...]:
    """Year-end horizons: this year and the next two."""
    return tuple(f"Dec {year + i}" for i in range(3))


def snap(value) -> float | None:
    """`value` on the 5bp grid, or None when it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return round(round(v / DOT_STEP) * DOT_STEP, 2)


def fmt(rate: float | None) -> str:
    return "—" if rate is None else f"{float(rate):.2f}"


def seat_name(row: Mapping, index: int) -> str:
    """A member's name, or their seat when the roster leaves it blank."""
    name = str(row.get("name") or "").strip()
    if name:
        return name
    role = str(row.get("role") or "Member").replace("External member", "External")
    return f"{role} {index + 1}"


def label_for(name: str) -> str:
    parts = str(name or "").split()
    return parts[-1] if parts and not parts[-1].isdigit() else name


def group_of(row: Mapping) -> str:
    bloc = str(row.get("bloc2") or row.get("bloc1") or row.get("bloc") or "").strip()
    if bloc in GROUPS:
        return bloc
    role = str(row.get("role") or "").lower()
    if "governor" in role:
        return EXECUTIVE
    return EX_OFFICIO if "treasury" in role or "secretary" in role else EXTERNAL


@dataclass(frozen=True)
class Dot:
    name: str
    label: str
    role: str
    group: str
    rate: float
    score: float | None
    votes: bool = True


@dataclass(frozen=True)
class Level:
    rate: float
    dots: list[Dot]


@dataclass(frozen=True)
class Panel:
    label: str
    levels: list[Level]
    median: float | None

    @property
    def ok(self) -> bool:
        return bool(self.levels)

    @property
    def dots(self) -> list[Dot]:
        return [d for lv in self.levels for d in lv.dots]

    @property
    def count(self) -> int:
        return len(self.dots)

    @property
    def voter_count(self) -> int:
        return self.count

    @property
    def voter_median(self) -> float | None:
        return self.median

    @property
    def span(self) -> tuple[float, float]:
        rates = [lv.rate for lv in self.levels]
        return (min(rates), max(rates)) if rates else (0.0, 0.0)

    @property
    def widest(self) -> int:
        return max((len(lv.dots) for lv in self.levels), default=1)


def build(roster: Sequence[Mapping], label: str, values: Mapping[str, float | None]) -> Panel:
    """One horizon: every member with a dot, grouped by level, most hawkish
    first within a level."""
    by_rate: dict[float, list[Dot]] = {}
    for i, row in enumerate(roster):
        name = seat_name(row, i)
        rate = snap(values.get(name))
        if rate is None:
            continue
        score = next((row[k] for k in ("score2", "score1", "score")
                      if isinstance(row.get(k), (int, float))), None)
        by_rate.setdefault(rate, []).append(Dot(
            name, label_for(name), str(row.get("role") or ""), group_of(row), rate,
            float(score) if isinstance(score, (int, float)) else None))
    levels = [Level(r, sorted(ds, key=lambda d: -(d.score or 0.0)))
              for r, ds in sorted(by_rate.items(), reverse=True)]
    rates = [d.rate for lv in levels for d in lv.dots]
    return Panel(label, levels, _median(rates) if rates else None)


def reconcile_split(panel: Panel, current: float, size: float = 0.25) -> tuple[int, int, int]:
    """(above, at, below) the current cash rate at this horizon: how the named
    read splits, for the Board-vote read beside the published split."""
    above = sum(1 for d in panel.dots if d.rate > current + 1e-9)
    below = sum(1 for d in panel.dots if d.rate < current - 1e-9)
    return above, panel.count - above - below, below
