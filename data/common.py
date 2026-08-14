"""Shared value types across data/ adapters.

Every adapter in this package (`fred`, `ism`, and anything added later)
degrades to an error string rather than raising, and boils its result down to
a list of dated points -- this is that one shared shape, so a future adapter
reuses it instead of redefining an identical (date, value) pair again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Observation:
    date: date
    value: float
