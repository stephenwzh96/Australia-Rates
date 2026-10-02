"""One clock for the whole app: Sydney, Australia.

Every "what day is it" question -- days to a decision, whether a release has
printed, whether a cached series is stale, what to stamp a save with -- is
answered here. Nothing calls `date.today()` or `datetime.now()` directly.

`date.today()` answers in whatever the host machine is set to, which is right on
a Sydney laptop and wrong the moment the app runs anywhere else. The RBA, the
ABS and the ASX all publish on Sydney/Canberra time, so that is the only
timezone in which "has this printed yet" has a single answer.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ_NAME = "Australia/Sydney"
TZ = ZoneInfo(TZ_NAME)


def now() -> datetime:
    """Sydney wall-clock time, timezone-aware. Use for elapsed-time arithmetic."""
    return datetime.now(TZ)


def today() -> date:
    """The current calendar date in Sydney. The app's definition of "today"."""
    return now().date()


def stamp(timespec: str = "seconds") -> str:
    """Sydney wall-clock time as a NAIVE ISO string, for records written to disk."""
    return now().replace(tzinfo=None).isoformat(timespec=timespec)


def year() -> int:
    return today().year
