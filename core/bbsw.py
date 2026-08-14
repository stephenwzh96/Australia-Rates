"""Step 1b -- the same question asked of 3-month SOFR, and where it stops working.

SR3 settles on daily SOFR COMPOUNDED across an IMM-to-IMM reference quarter:
third Wednesday of the contract month to third Wednesday of the next quarterly
month, roughly 91 days. That single difference breaks the `core.strip` method,
and it is worth being blunt about why.

ZQ gives one equation per calendar month and there is at most one meeting in a
month, so the system is exactly determined. An IMM quarter is three months long
and the Fed meets eight times a year, so a SOFR quarter usually contains TWO
meetings and sometimes three -- one equation, two or three unknowns. No amount
of algebra recovers a per-meeting probability from that. A tool that printed one
anyway would be inventing the split and calling it market data.

So this module does three honest things instead:

  1. SPANS. For every contract, which meetings fall inside its window and how
     many days sit at each rate. That is what the position is actually exposed
     to, and it is exact.

  2. BASIS. Take the meeting path that ZQ *does* determine, compound it across
     the same window, and compare. The difference is the SOFR/EFFR basis plus
     futures convexity -- which is the real reason to look at SR3 next to ZQ,
     and the number that decides which of the two you should be trading.

  3. A SOLVED STEP, but only where the window contains exactly one meeting and
     the answer is therefore determined. Those are flagged, and they are still
     in SOFR terms: the basis is not stripped out of them.

Compounding is done properly rather than as a simple average. At 4% over a
quarter the two differ by about 2bp, which is most of a meeting's worth of
probability and far too big to wave through on a tool built to catch exactly
that kind of slippage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .contracts import effective_date
from .rba_calendar import Meeting
from .strip import StripPath

QUARTERLY_MONTHS = (3, 6, 9, 12)
DAY_COUNT = 360.0

# SOFR/EFFR basis wider than this is not a quiet technical spread any more --
# it is funding stress, and it changes which instrument expresses the view.
BASIS_FLAG_BP = 8.0

_WEDNESDAY = 2


def imm_date(year: int, month: int) -> date:
    """Third Wednesday -- the IMM roll."""
    first = date(year, month, 1)
    return first + timedelta(days=(_WEDNESDAY - first.weekday()) % 7 + 14)


def next_quarterly(month: date) -> date:
    for q in QUARTERLY_MONTHS:
        if q > month.month:
            return date(month.year, q, 1)
    return date(month.year + 1, QUARTERLY_MONTHS[0], 1)


def reference_window(month: date) -> tuple[date, date]:
    """[start, end) of the contract's accrual period. End is the next IMM."""
    nxt = next_quarterly(month)
    return imm_date(month.year, month.month), imm_date(nxt.year, nxt.month)


def rate_on(path: StripPath, when: date) -> float:
    """The ZQ-implied policy rate in force on a given day."""
    rate = path.spot
    for step in path.steps:
        if effective_date(step.meeting.end) <= when:
            rate = step.r_after
        else:
            break
    return rate


def segments(path: StripPath, start: date, end: date) -> list[tuple[float, int]]:
    """The path across [start, end) as (rate, days) pieces."""
    out: list[tuple[float, int]] = []
    cursor, rate = start, rate_on(path, start)
    for step in path.steps:
        eff = effective_date(step.meeting.end)
        if start < eff < end:
            out.append((rate, (eff - cursor).days))
            cursor, rate = eff, step.r_after
    out.append((rate, (end - cursor).days))
    return [(r, n) for r, n in out if n > 0]


def compounded(pieces: list[tuple[float, int]]) -> float | None:
    """Annualised compounded rate (percent) for a piecewise-constant path.

    Each calendar day accrues rate/360, which is exactly how SOFR carries a
    weekend: Friday's fixing applies until Monday, and a constant-rate stretch
    reproduces that without needing a business-day calendar.
    """
    acc, total = 1.0, 0
    for rate, days in pieces:
        acc *= (1.0 + rate / 100.0 / DAY_COUNT) ** days
        total += days
    if not total:
        return None
    return (acc - 1.0) * DAY_COUNT / total * 100.0


def solve_single(implied: float, r_before: float, n1: int, n2: int) -> float | None:
    """Invert the compounding for a window containing exactly one meeting."""
    if n2 <= 0:
        return None
    total = n1 + n2
    target = 1.0 + implied / 100.0 * total / DAY_COUNT
    before = (1.0 + r_before / 100.0 / DAY_COUNT) ** n1
    if before <= 0 or target / before <= 0:
        return None
    return ((target / before) ** (1.0 / n2) - 1.0) * DAY_COUNT * 100.0


@dataclass(frozen=True)
class SofrPeriod:
    code: str
    month: date
    start: date
    end: date
    days: int
    price: float
    implied: float                       # compounded SOFR the market prices
    meetings: list[Meeting]
    expected: float | None = None        # ZQ path compounded over the same window
    basis_bp: float | None = None        # implied - expected
    policy_bp: float | None = None       # policy priced inside the window (ZQ)
    r_entry: float | None = None
    solved_r_after: float | None = None   # only when exactly one meeting
    solved_step_bp: float | None = None
    solved_prob: float | None = None
    change_bp: float | None = None
    stale: bool = False
    beyond_horizon: bool = False

    @property
    def n_meetings(self) -> int:
        return len(self.meetings)

    @property
    def comparable(self) -> bool:
        """True when the ZQ path genuinely covers this window."""
        return self.basis_bp is not None and not self.beyond_horizon

    @property
    def determinate(self) -> bool:
        return self.n_meetings == 1

    @property
    def window_label(self) -> str:
        return f"{self.start:%d %b} – {self.end:%d %b %y}"

    @property
    def meetings_label(self) -> str:
        if not self.meetings:
            return "none"
        return ", ".join(m.end.strftime("%b") for m in self.meetings)

    @property
    def basis_flagged(self) -> bool:
        return self.basis_bp is not None and abs(self.basis_bp) > BASIS_FLAG_BP


@dataclass(frozen=True)
class Spread:
    """A calendar spread -- how a STIR desk actually holds a meeting view on SR3."""
    near: SofrPeriod
    far: SofrPeriod

    @property
    def label(self) -> str:
        return f"{self.near.code}/{self.far.code}"

    @property
    def market_bp(self) -> float:
        return (self.far.implied - self.near.implied) * 100.0

    @property
    def path_bp(self) -> float | None:
        if self.near.expected is None or self.far.expected is None:
            return None
        return (self.far.expected - self.near.expected) * 100.0

    @property
    def diff_bp(self) -> float | None:
        p = self.path_bp
        return None if p is None else self.market_bp - p

    @property
    def meetings_between(self) -> int:
        return self.far.n_meetings


@dataclass(frozen=True)
class SofrCurve:
    periods: list[SofrPeriod] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.periods)

    @property
    def spreads(self) -> list[Spread]:
        return [Spread(a, b) for a, b in zip(self.periods, self.periods[1:])]

    @property
    def mean_basis_bp(self) -> float | None:
        vals = [p.basis_bp for p in self.periods if p.comparable and not p.stale]
        return sum(vals) / len(vals) if vals else None

    @property
    def comparable_periods(self) -> list[SofrPeriod]:
        return [p for p in self.periods if p.comparable]

    @property
    def front(self) -> SofrPeriod | None:
        return self.periods[0] if self.periods else None


def analyse(quotes, path: StripPath, meetings: list[Meeting],
            size: float = 25.0, stale_codes: set[str] | None = None,
            horizon: date | None = None) -> SofrCurve:
    """Map the SR3 strip onto the meeting calendar and the ZQ-implied path.

    `horizon` is the last day the ZQ strip can speak for. SR3 quotes run years
    past the fed funds contracts that are actually liquid, and comparing a live
    2029 SOFR price against a fed funds path that ran out in 2027 produces a
    twenty-basis-point "basis" that is nothing of the sort -- it is the ZQ side
    flat-lining. Those windows are marked `beyond_horizon` and left uncompared.
    """
    live = sorted((q for q in quotes if q.ok), key=lambda q: q.month)
    if not live:
        return SofrCurve(error="no usable SOFR quotes")

    stale = stale_codes or set()
    effs = sorted(((m, effective_date(m.end)) for m in meetings), key=lambda t: t[1])
    out: list[SofrPeriod] = []

    for q in live:
        start, end = reference_window(q.month)
        days = (end - start).days
        inside = [m for m, e in effs if start < e < end]
        beyond = horizon is not None and end > horizon

        expected = basis = policy = entry = None
        solved_r = solved_step = solved_prob = None

        if path.ok and not beyond:
            entry = rate_on(path, start)
            expected = compounded(segments(path, start, end))
            if expected is not None:
                basis = (q.implied_rate - expected) * 100.0
            policy = (rate_on(path, end - timedelta(days=1)) - entry) * 100.0

            # One meeting in the window is the only case the algebra closes on.
            if len(inside) == 1:
                eff = effective_date(inside[0].end)
                n1, n2 = (eff - start).days, (end - eff).days
                solved_r = solve_single(q.implied_rate, entry, n1, n2)
                if solved_r is not None:
                    solved_step = (solved_r - entry) * 100.0
                    solved_prob = solved_step / size if size else None

        out.append(SofrPeriod(
            code=q.code, month=q.month, start=start, end=end, days=days,
            price=q.price, implied=q.implied_rate, meetings=inside,
            expected=expected, basis_bp=basis, policy_bp=policy, r_entry=entry,
            solved_r_after=solved_r, solved_step_bp=solved_step,
            solved_prob=solved_prob, change_bp=q.change_bp,
            stale=q.code in stale, beyond_horizon=beyond,
        ))

    return SofrCurve(periods=out)
