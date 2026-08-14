"""Meeting calendar, blackout window, SMP flag.

Every 2026 and 2027 date below is the RBA's own published schedule
(rba.gov.au/schedules-events/board-meeting-schedules.html, fetched 2026-08-14),
not this app's guess -- `verified=True` throughout both years. 2028+ isn't
listed because the RBA hasn't published it yet; add it here, from the same
source, once it is. Never interpolate the eight-meeting pattern forward and
call it verified, which is exactly the mistake this flag exists to prevent.

WHAT DIFFERS FROM THE FOMC
--------------------------
The two calendars look alike -- eight scheduled meetings a year, two days each,
a decision on the second day -- but three things do not carry over, and each one
changes a number in this app:

  * THE PROJECTIONS MONTH. The Fed publishes an SEP in Mar/Jun/Sep/Dec. The RBA
    publishes its Statement on Monetary Policy alongside the Feb/May/Aug/Nov
    decisions. So `SMP_MONTHS` is {2, 5, 8, 11}, not {3, 6, 9, 12}, and a
    no-SMP meeting is the one where the tradeable event shifts from the
    forecast round onto the statement and the Governor's press conference.

  * THE BLACKOUT. The Fed's window is long and rule-bound: it opens the second
    Saturday before the meeting and closes the Thursday after. The RBA's is
    much shorter and differently anchored -- it opens at the internal Policy
    Discussion meeting, usually 2:00pm on the WEDNESDAY BEFORE the Board
    meeting, and closes when the decision is announced. Source: RBA,
    "Arrangements for Public Speaking Engagements"
    (rba.gov.au/media/arrangements-speeches.html). Five days, not seventeen.

  * THE VOTE. The RBA publishes an unattributed count ("decided by six votes
    to three"); no member is ever named. See `meetings/_roster.json`.

The decision lands at 2:30pm Sydney time on the second day, and the new cash
rate target takes effect the FOLLOWING day -- so `effective_date` is
`meeting_end + 1`, the same shape as the Fed's, which is what lets
`core.strip` invert the IB month blend unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# The Statement on Monetary Policy accompanies the February, May, August and
# November decisions -- the RBA's analogue of the SEP, and the meetings that
# carry a full forecast round.
SMP_MONTHS = frozenset({2, 5, 8, 11})

_WEDNESDAY = 2


@dataclass(frozen=True)
class Meeting:
    start: date
    end: date
    verified: bool = False

    @property
    def key(self) -> str:
        return self.end.isoformat()

    @property
    def label(self) -> str:
        if self.start.month == self.end.month:
            return f"{self.start.strftime('%b')} {self.start.day}-{self.end.day}, {self.end.year}"
        return f"{self.start.strftime('%b %d')}-{self.end.strftime('%b %d')}, {self.end.year}"

    @property
    def has_smp(self) -> bool:
        """A full forecast round, published with the decision.

        No SMP means no revised forecasts to anchor or disrupt the strip, which
        pushes the tradeable event from the numbers onto the statement's wording
        and the Governor's press conference.
        """
        return self.end.month in SMP_MONTHS


def blackout_window(meeting: Meeting) -> tuple[date, date]:
    """`(opens, closes)` inclusive.

    Opens at the internal Policy Discussion, the Wednesday before the meeting
    starts; closes on the decision day itself. A meeting that somehow began on
    a Wednesday would take the Wednesday a full week earlier -- the `or 7`
    guard -- rather than opening the same morning.
    """
    back = (meeting.start.weekday() - _WEDNESDAY) % 7 or 7
    return meeting.start - timedelta(days=back), meeting.end


def in_blackout(meeting: Meeting, today: date) -> bool:
    opens, closes = blackout_window(meeting)
    return opens <= today <= closes


def days_until(meeting: Meeting, today: date) -> int:
    return (meeting.end - today).days


# Source: rba.gov.au/schedules-events/board-meeting-schedules.html, fetched
# 2026-08-14. Monetary Policy Board only -- the Payments System Board sits on
# its own schedule and decides nothing this app prices.
DEFAULT_MEETINGS: tuple[Meeting, ...] = (
    Meeting(date(2026, 2, 2), date(2026, 2, 3), verified=True),
    Meeting(date(2026, 3, 16), date(2026, 3, 17), verified=True),
    Meeting(date(2026, 5, 4), date(2026, 5, 5), verified=True),
    Meeting(date(2026, 6, 15), date(2026, 6, 16), verified=True),
    Meeting(date(2026, 8, 10), date(2026, 8, 11), verified=True),
    Meeting(date(2026, 9, 28), date(2026, 9, 29), verified=True),
    Meeting(date(2026, 11, 2), date(2026, 11, 3), verified=True),
    Meeting(date(2026, 12, 7), date(2026, 12, 8), verified=True),
    Meeting(date(2027, 2, 8), date(2027, 2, 9), verified=True),
    Meeting(date(2027, 3, 22), date(2027, 3, 23), verified=True),
    Meeting(date(2027, 5, 3), date(2027, 5, 4), verified=True),
    Meeting(date(2027, 6, 21), date(2027, 6, 22), verified=True),
    Meeting(date(2027, 8, 9), date(2027, 8, 10), verified=True),
    Meeting(date(2027, 9, 27), date(2027, 9, 28), verified=True),
    Meeting(date(2027, 11, 1), date(2027, 11, 2), verified=True),
    Meeting(date(2027, 12, 13), date(2027, 12, 14), verified=True),
)


def meeting_from_dict(d: dict) -> Meeting:
    return Meeting(
        start=date.fromisoformat(d["start"]),
        end=date.fromisoformat(d["end"]),
        verified=bool(d.get("verified", False)),
    )


def meeting_to_dict(m: Meeting) -> dict:
    return {"start": m.start.isoformat(), "end": m.end.isoformat(), "verified": m.verified}


def next_meeting_after(meetings: list[Meeting], m: Meeting) -> Meeting | None:
    later = sorted((x for x in meetings if x.end > m.end), key=lambda x: x.end)
    return later[0] if later else None


def previous_meeting_before(meetings: list[Meeting], m: Meeting) -> Meeting | None:
    earlier = sorted((x for x in meetings if x.end < m.end), key=lambda x: x.end)
    return earlier[-1] if earlier else None


def upcoming(meetings: list[Meeting], today: date) -> Meeting | None:
    later = sorted((x for x in meetings if x.end >= today), key=lambda x: x.end)
    return later[0] if later else None
