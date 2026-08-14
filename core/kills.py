"""Step 7 -- kill criteria.

Pre-commit to what invalidates the thesis, before you're in the position and
rationalising. The doc's threshold is explicit: *any two of these together and
the estimate is stale*. That rule is enforced here rather than left to memory,
because the moment it matters is the moment you are least inclined to apply it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Comparator = Literal["above", "below", "manual"]

STALE_THRESHOLD = 2


@dataclass(frozen=True)
class KillCriterion:
    label: str
    comparator: Comparator
    threshold: float | None = None
    series: str | None = None
    current: float | None = None
    manual_flag: bool = False
    note: str = ""
    unit: str = ""

    @property
    def is_auto(self) -> bool:
        return self.comparator != "manual" and self.series is not None

    @property
    def triggered(self) -> bool:
        if not self.is_auto or self.current is None or self.threshold is None:
            return self.manual_flag
        if self.comparator == "above":
            return self.current > self.threshold
        if self.comparator == "below":
            return self.current < self.threshold
        return self.manual_flag

    @property
    def status(self) -> str:
        if self.triggered:
            return "triggered"
        if self.is_auto and self.current is None:
            return "unknown"
        return "clear"


@dataclass(frozen=True)
class KillAssessment:
    criteria: list[KillCriterion]
    triggered: list[str]
    unknown: list[str]
    count: int
    is_stale: bool


def assess(criteria: list[KillCriterion], threshold: int = STALE_THRESHOLD) -> KillAssessment:
    fired = [c.label for c in criteria if c.triggered]
    unknown = [c.label for c in criteria if c.status == "unknown"]
    return KillAssessment(
        criteria=list(criteria),
        triggered=fired,
        unknown=unknown,
        count=len(fired),
        is_stale=len(fired) >= threshold,
    )


def from_dicts(rows, current_values: dict[str, float | None] | None = None,
               units: dict[str, str] | None = None) -> list[KillCriterion]:
    values = current_values or {}
    unit_map = units or {}
    out: list[KillCriterion] = []
    for r in rows:
        series = r.get("series")
        out.append(
            KillCriterion(
                label=r.get("label", ""),
                comparator=r.get("comparator", "manual"),
                threshold=r.get("threshold"),
                series=series,
                current=values.get(series) if series else None,
                manual_flag=bool(r.get("triggered", False)),
                note=r.get("note", ""),
                unit=unit_map.get(series, "") if series else "",
            )
        )
    return out
