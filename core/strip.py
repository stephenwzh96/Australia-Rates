"""Step 1a -- the market's own path, backed out of the IB strip.

Step 1 takes `points priced` as a given and asks whether your probability beats
it. This module produces that number from the market instead of from memory, for
every meeting on the calendar at once.

WHY THIS PORTS FROM THE US VERSION UNCHANGED
---------------------------------------------
Not an assumption -- checked against the contract specification. ASX settles a
30 Day Interbank Cash Rate future at 100 minus "the monthly average of the
Interbank Overnight Cash Rate for that contract month", averaged over every
calendar day, with weekends and public holidays carrying the previous business
day's fix. That is the same settlement mechanic as CME's ZQ, word for word, so
the blend inversion below is the same algebra with a different rate in it.

One trap worth naming, because it nearly cost the denominator: the ASX price
feed reports a `dateExpiry` two calendar days before the real last trading day
on every contract. The averaging window is the CALENDAR MONTH, not anything
derived from that field -- see `data.asx`, which parses the delivery month from
the contract symbol for exactly this reason.

An IB contract settles on the simple average of the daily cash rate across its
delivery month, so any month containing a policy change prices a BLEND of the
old rate and the new one, weighted by days:

    r_month * N = r_before * n1 + r_after * n2

Everything except `r_after` is known -- N and the split come from the calendar,
`r_month` from the contract, `r_before` from the previous meeting's solved rate.
Walk it forward and you recover the whole priced path. The per-meeting STEP is
r_after - r_before, the CUMULATIVE is r_after - spot, and the implied
probability is the step as a fraction of one full move -- the same points/size
arithmetic as `core.pricing`, run off the market's number rather than yours.

WHICH CONTRACT ANSWERS THE QUESTION
-----------------------------------
Inverting the blend divides by n2, so a meeting late in its month is read
through a very small denominator -- and the RBA calendar puts several meetings
exactly there. The 28-29 September 2026 decision is effective the 30th, giving
n2 = 1 of 30 days: a 1bp error in the assumed starting rate comes out as 30bp
of "priced move", more than a full rate move conjured from a rounding
difference. That is the same trap `core.contracts` warns about for putting the
POSITION in the near contract, and it applies just as brutally to reading a
probability out of it.

So the meeting-month contract is used only when it actually carries the move.
Below `CAPTURE_THRESHOLD` the following month is used instead, read directly:
a month wholly after the effective date, with no meeting of its own, prices the
new rate outright and needs no inversion at all. The `source` field on every
step records which of the two answered, because a STIR trader needs to know
whether a number is a clean read or a leveraged one.

Two limitations, surfaced rather than hidden:

  * `spot` is charged for the part-elapsed days of the front month. That is
    exact only while the cash rate has been flat month-to-date, which it
    normally is between meetings -- and rather more reliably here than in the
    US, since AONIA tracks the target almost exactly under the RBA's
    ample-reserves framework. `implied_spot` derives it from the strip itself.
  * A month containing two effective dates cannot be split by one equation. The
    solved change is attributed to the LAST meeting in the month and the others
    are flagged `ambiguous`, rather than silently spread.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from .contracts import add_month, effective_date
from .rba_calendar import Meeting

# Below this share of the delivery month at the new rate, inverting the
# meeting-month blend is too leveraged to trust -- read the next month instead.
CAPTURE_THRESHOLD = 0.20

# A month with no meeting whose contract disagrees with the carried rate by more
# than this is worth a look: an unscheduled move being priced, year-end funding
# pressure in the cash market, or a meeting calendar that has drifted from the
# market's. In Australia this flag is a sharper signal than its US counterpart
# -- there is no IORB technical adjustment or reserve-scarcity drift here to
# explain a gap away, so a clean month that misprices is usually saying
# something real.
DRIFT_FLAG_BP = 1.5


@dataclass(frozen=True)
class MeetingStep:
    """What the strip has priced for one meeting."""
    meeting: Meeting
    month: date                  # delivery month the change lands in
    code: str                    # contract actually used to solve it
    source: str                  # "next" (clean read) | "blend" (inverted)
    days_in_month: int
    days_before: int
    days_after: int
    implied_month_rate: float    # the meeting month's own contract, for display
    r_before: float
    r_after: float
    step_bp: float
    cum_bp: float
    prob: float
    ambiguous: bool = False
    stale: bool = False

    @property
    def reliable(self) -> bool:
        """Solved from a contract that is actually printing, and unambiguous."""
        return not self.stale and not self.ambiguous

    @property
    def label(self) -> str:
        return self.meeting.label

    @property
    def key(self) -> str:
        return self.meeting.key

    @property
    def capture_share(self) -> float:
        """Fraction of the delivery month sitting at the new rate."""
        return self.days_after / self.days_in_month if self.days_in_month else 0.0

    @property
    def is_clean(self) -> bool:
        return self.source == "next"


@dataclass(frozen=True)
class Reconciliation:
    """A month with a contract but no meeting: implied should equal carried."""
    month: date
    code: str
    implied: float
    expected: float
    diff_bp: float

    @property
    def flagged(self) -> bool:
        return abs(self.diff_bp) > DRIFT_FLAG_BP


@dataclass(frozen=True)
class StripPath:
    spot: float
    size: float
    steps: list[MeetingStep] = field(default_factory=list)
    checks: list[Reconciliation] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.steps)

    @property
    def reliable_steps(self) -> list[MeetingStep]:
        """Steps a headline number is allowed to be built from.

        A stale contract still produces a step, and that step still belongs in
        the ladder so the gap is visible -- but it must not set the terminal
        rate or the peak. A quote that has not traded for a month deciding what
        the market has priced is exactly the failure this tool exists to catch.
        """
        return [s for s in self.steps if s.reliable]

    @property
    def excluded_count(self) -> int:
        return len(self.steps) - len(self.reliable_steps)

    @property
    def terminal(self) -> float | None:
        rel = self.reliable_steps
        return rel[-1].r_after if rel else None

    @property
    def total_bp(self) -> float:
        rel = self.reliable_steps
        return rel[-1].cum_bp if rel else 0.0

    @property
    def peak(self) -> MeetingStep | None:
        rel = self.reliable_steps
        return max(rel, key=lambda s: s.cum_bp) if rel else None

    @property
    def trough(self) -> MeetingStep | None:
        rel = self.reliable_steps
        return min(rel, key=lambda s: s.cum_bp) if rel else None

    def step_for(self, key: str) -> MeetingStep | None:
        """The priced step for a meeting, addressed by its `Meeting.key`."""
        return next((s for s in self.steps if s.key == key), None)

    def upcoming(self, today: date) -> list[MeetingStep]:
        return [s for s in self.steps if s.meeting.end >= today]


def _effective_months(meetings: list[Meeting]) -> dict[date, list[tuple[Meeting, date]]]:
    """Meetings keyed by the month their change would take EFFECT.

    That is the month whose contract prices them -- not the month they are held
    in. A meeting ending on the 29th lands in the same month; one ending on the
    last day of the month lands in the next.
    """
    out: dict[date, list[tuple[Meeting, date]]] = {}
    for m in meetings:
        eff = effective_date(m.end)
        out.setdefault(date(eff.year, eff.month, 1), []).append((m, eff))
    for rows in out.values():
        rows.sort(key=lambda t: t[1])
    return out


def implied_spot(quotes, meetings: list[Meeting],
                 as_of: date | None = None) -> float | None:
    """Back today's prevailing EFFR out of the strip, or admit it cannot.

    Two routes, most reliable first:

      1. A CLEAN MONTH. The first quoted month with no meeting effective inside
         it, and no meeting taking effect between today and the start of it. Its
         contract is then a constant rate, and that rate is the one in force
         right now -- no inversion, no leverage.

      2. INVERT THE FRONT. If the front month holds exactly one meeting that has
         NOT yet taken effect, and the month after it is clean, run the blend
         backwards off that clean month.

    Returns None when neither applies, and the caller must NOT invent a number.
    Once a meeting has already taken effect mid-month with another one landing
    before the next clean contract, the strip has one more unknown than it has
    equations and spot genuinely is not recoverable from futures alone. It needs
    an outside anchor -- EFFR itself, or the target range.
    """
    usable = sorted((q for q in quotes if q.ok), key=lambda q: q.month)
    if not usable:
        return None
    by_month = {q.month: q for q in usable}
    eff = _effective_months(meetings)
    all_effs = sorted(effective_date(m.end) for m in meetings)
    anchor = as_of or usable[0].month

    # Route 1 -- a month whose contract is simply the rate in force today.
    for q in usable:
        if q.month in eff:
            continue
        if any(anchor < e < q.month for e in all_effs):
            continue                      # the rate changes before we get there
        return q.implied_rate

    # Route 2 -- invert the front month against the next clean contract.
    front = usable[0]
    pending = [e for _, e in eff.get(front.month, []) if e > anchor]
    if len(pending) != 1:
        return None

    dim = calendar.monthrange(front.month.year, front.month.month)[1]
    n1 = pending[0].day - 1
    n2 = dim - n1
    if n1 <= 0 or n2 <= 0:
        return None

    nxt = add_month(front.month)
    if nxt not in by_month or nxt in eff:
        return None
    return (front.implied_rate * dim - by_month[nxt].implied_rate * n2) / n1


def decompose(meetings: list[Meeting], quotes, spot: float, size: float = 25.0,
              capture_threshold: float = CAPTURE_THRESHOLD,
              stale_codes: set[str] | None = None,
              as_of: date | None = None) -> StripPath:
    """Invert the strip into a per-meeting priced path.

    `quotes` is any iterable of `data.asx.Quote`; unusable ones are skipped
    rather than fatal, so one dead contract costs a meeting, not the path.
    `stale_codes` marks steps solved off a contract that is not printing: they
    stay in the ladder but are kept out of the headline numbers.

    `as_of` drops meetings whose change has ALREADY taken effect, and passing it
    matters more than it looks. `spot` is the rate in force today, so the walk
    forward must start from today -- but the front contract's delivery month
    usually still contains a decision from earlier in the month, and inverting
    that blend "solves" a meeting whose outcome is already known and already in
    `spot`. Doing so on the live August 2026 strip produced a phantom -0.8bp
    step and shifted the starting point of every meeting behind it. There is
    nothing to infer about a decision that has happened; it is history, and
    `spot` already contains it.
    """
    if size <= 0:
        return StripPath(spot, size, error="move size must be positive")
    if as_of is not None:
        meetings = [m for m in meetings if effective_date(m.end) > as_of]

    by_month = {q.month: q for q in quotes if q.ok}
    if not by_month:
        return StripPath(spot, size, error="no usable quotes")

    eff = _effective_months(meetings)
    stale = stale_codes or set()
    steps: list[MeetingStep] = []
    consumed: set[date] = set()
    rate = float(spot)

    for meeting in sorted(meetings, key=lambda m: m.end):
        eff_date = effective_date(meeting.end)
        month = date(eff_date.year, eff_date.month, 1)
        quote = by_month.get(month)
        if quote is None or quote.implied_rate is None:
            continue

        here = eff[month]
        dim = calendar.monthrange(month.year, month.month)[1]
        n1 = eff_date.day - 1
        n2 = dim - n1
        if n2 <= 0:
            continue

        implied = quote.implied_rate
        ambiguous = len(here) > 1

        # One equation cannot split two changes in a month: the last meeting
        # carries the solved move, the rest are recorded flat and flagged.
        if ambiguous and meeting is not here[-1][0]:
            steps.append(MeetingStep(
                meeting=meeting, month=month, code=quote.code, source="blend",
                days_in_month=dim, days_before=n1, days_after=n2,
                implied_month_rate=implied, r_before=rate, r_after=rate,
                step_bp=0.0, cum_bp=(rate - spot) * 100.0, prob=0.0,
                ambiguous=True, stale=quote.code in stale,
            ))
            continue

        nxt = add_month(month)
        nxt_quote = by_month.get(nxt)
        nxt_is_clean = nxt_quote is not None and nxt not in eff

        if n2 / dim < capture_threshold and nxt_is_clean:
            # The following month sits wholly at the new rate and prices it
            # outright -- no inversion, no leverage on the starting rate.
            r_after = nxt_quote.implied_rate
            code, source = nxt_quote.code, "next"
            consumed.add(nxt)
        else:
            r_after = (implied * dim - rate * n1) / n2
            code, source = quote.code, "blend"

        consumed.add(month)
        step_bp = (r_after - rate) * 100.0
        steps.append(MeetingStep(
            meeting=meeting, month=month, code=code, source=source,
            days_in_month=dim, days_before=n1, days_after=n2,
            implied_month_rate=implied, r_before=rate, r_after=r_after,
            step_bp=step_bp, cum_bp=(r_after - spot) * 100.0,
            prob=step_bp / size, ambiguous=ambiguous,
            stale=code in stale,
        ))
        rate = r_after

    # Months with no meeting and no role as an anchor should simply reprint the
    # carried rate. Where they do not, something outside the calendar is moving.
    checks: list[Reconciliation] = []
    carried = float(spot)
    for month in sorted(by_month):
        if month in eff:
            nxt_step = next((s for s in steps if s.month == month and not s.ambiguous), None)
            if nxt_step:
                carried = nxt_step.r_after
            continue
        if month in consumed:
            carried = by_month[month].implied_rate
            continue
        implied = by_month[month].implied_rate
        checks.append(Reconciliation(
            month=month, code=by_month[month].code, implied=implied,
            expected=carried, diff_bp=(implied - carried) * 100.0,
        ))

    if not steps:
        return StripPath(spot, size, checks=checks,
                         error="no meeting falls inside the quoted strip")
    return StripPath(spot, size, steps=steps, checks=checks)
