"""Step 1b -- the same question asked of 90 day bank bills, and where it stops
working.

An IR contract settles at 100 minus the 3-month BBSW rate published on its last
trading day. That single sentence breaks the `core.strip` method, and it is
worth being blunt about why -- it breaks it in a DIFFERENT way from how SOFR
futures break it in the US version, and the difference matters.

  * SR3 fails because its window is too LONG: an IMM quarter contains two or
    three FOMC meetings, so one equation has several unknowns.
  * IR fails because it has no window AT ALL. There is one fix, on one morning.
    A contract expiring in December tells you what the market thinks 3-month
    BBSW will be on 10 December, and nothing whatever about how the path got
    there. Nothing to invert; no algebra to be clever with.

And IR carries something ZQ and SR3 do not have between them: the rate it
settles on is not a policy rate. BBSW is a BANK CREDIT rate -- what the major
banks pay to issue paper -- so an IR price is the expected policy path PLUS a
credit and term spread. Reading a rate expectation straight off an IR price
means quietly assuming that spread is zero.

So this module does three honest things instead:

  1. SPANS. For every contract, where its fix sits relative to the meeting
     calendar: which decisions land before it (and are therefore in the fix),
     and which land inside the 90 days the bill actually covers. The second
     group is what a holder is exposed to through the bill's own tenor, and
     it is exact.

  2. BASIS. Take the meeting path the IB strip DOES determine, average it
     across the 90 days the bill spans, and compare. The difference is the
     BBSW/OIS spread -- bank credit plus term premium -- which is the real
     reason to look at IR next to IB, and the number that decides which of the
     two you should be trading.

     The US original reads its OIS leg off a published series. That route is
     shut here: the RBA retired its OIS series on 1 December 2022 (see
     `data.rba`). Deriving the leg from the IB strip is better anyway -- it is
     live and tradeable rather than surveyed, and it is the same construction
     the US module uses for its expected leg.

  3. A CASH-EQUIVALENT READING, under an assumption that is named on screen
     rather than buried. Strip today's OBSERVED spot spread -- 3-month BBSW
     minus the cash rate target, both from RBA table F1 -- off the bill's
     implied rate, and what remains is the cash path the bill is consistent
     with IF that credit spread holds to the fix. It usually does not, exactly,
     which is why this is reported as an assumption and not as a solve.

     There is a wrong version of this that is very easy to write, and the first
     draft of this module contained it: "solve" the forward by subtracting
     `basis_bp` from the implied rate. That is circular. `basis_bp` is DEFINED
     as implied minus the IB-path average, so subtracting it returns the IB
     path exactly and learns nothing from the bill price at all -- an
     impressive-looking number derived entirely from the other instrument.
     The spot spread from RBA data is a genuinely independent anchor; the
     contemporaneous basis is not.

Averaging, not compounding. An IB contract settles on a simple average and a
bank bill is a discount instrument quoted on a simple yield, so there is no
compounding anywhere in this chain -- which is the one respect in which the
Australian arithmetic is easier than the American.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .contracts import (IR_TENOR_DAYS, effective_date, ir_last_trading_day,
                        ir_settlement_day)
from .rba_calendar import Meeting
from .strip import StripPath

# A BBSW/OIS spread wider than this is not a quiet technical wedge any more --
# it is bank funding stress, and it changes which instrument expresses the view.
# Anchored on history rather than taste: the published BBSW-OIS spread averaged
# about 20bp over 2011-2022 (see `data.rba.historical_bbsw_ois_basis`), so 45bp
# is roughly where a reading stops being ordinary term premium.
BASIS_FLAG_BP = 45.0


def rate_on(path: StripPath, when: date) -> float:
    """The IB-implied cash rate in force on a given day."""
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


def averaged(pieces: list[tuple[float, int]]) -> float | None:
    """Simple day-weighted average of a piecewise-constant path.

    Simple, not compounded, and that is a deliberate match to the instruments
    rather than an approximation: a bank bill is a discount security quoted on
    a simple ACT/365 yield, and the IB contract this path came from settles on
    a simple average too. Compounding here would introduce a couple of basis
    points of error that belong to neither contract.
    """
    total = sum(n for _, n in pieces)
    if not total:
        return None
    return sum(r * n for r, n in pieces) / total


@dataclass(frozen=True)
class BillPeriod:
    code: str
    month: date
    fix_date: date                       # last trading day: the BBSW print it settles on
    covers_start: date                   # the 90 days the bill itself spans
    covers_end: date
    price: float
    implied: float                       # BBSW the market prices for the fix
    meetings_before: list[Meeting]       # decisions in force by the fix
    meetings_covered: list[Meeting]      # decisions inside the bill's own tenor
    expected: float | None = None        # IB path averaged over the same 90 days
    basis_bp: float | None = None        # implied - expected
    policy_bp: float | None = None       # policy priced across the covered window
    r_entry: float | None = None
    # Cash path the bill is consistent with IF today's observed BBSW/cash
    # spread holds to the fix. An assumption, named as one -- see the module
    # docstring on why the contemporaneous basis must NOT be used here.
    spot_basis_bp: float | None = None
    cash_equivalent: float | None = None
    cash_equivalent_step_bp: float | None = None
    cash_equivalent_prob: float | None = None
    change_bp: float | None = None
    stale: bool = False
    beyond_horizon: bool = False

    @property
    def n_covered(self) -> int:
        return len(self.meetings_covered)

    @property
    def comparable(self) -> bool:
        """True when the IB path genuinely reaches across this bill's tenor."""
        return self.basis_bp is not None and not self.beyond_horizon

    @property
    def determinate(self) -> bool:
        """Exactly one undecided meeting before the fix.

        The cash-equivalent reading is only interpretable as a view on ONE
        decision in this case. With two or more before the fix it is a
        statement about their sum, which is still useful but is not a
        per-meeting probability -- `cash_equivalent_prob` is left None.
        """
        return len(self.meetings_before) == 1

    @property
    def window_label(self) -> str:
        return f"{self.covers_start:%d %b} – {self.covers_end:%d %b %y}"

    @property
    def covered_label(self) -> str:
        if not self.meetings_covered:
            return "none"
        return ", ".join(m.end.strftime("%b") for m in self.meetings_covered)

    @property
    def basis_flagged(self) -> bool:
        return self.basis_bp is not None and abs(self.basis_bp) > BASIS_FLAG_BP


@dataclass(frozen=True)
class Spread:
    """A calendar spread -- how a desk actually holds a meeting view on bills."""
    near: BillPeriod
    far: BillPeriod

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
        """Decisions falling between the two fixes -- what the spread is a bet
        on, and the reason a desk trades the spread rather than the outright:
        the credit spread is common to both legs and largely cancels."""
        return len([m for m in self.far.meetings_before
                    if m.end > self.near.fix_date])


@dataclass(frozen=True)
class BillCurve:
    periods: list[BillPeriod] = field(default_factory=list)
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
    def comparable_periods(self) -> list[BillPeriod]:
        return [p for p in self.periods if p.comparable]

    @property
    def front(self) -> BillPeriod | None:
        return self.periods[0] if self.periods else None


def analyse(quotes, path: StripPath, meetings: list[Meeting],
            size: float = 25.0, stale_codes: set[str] | None = None,
            horizon: date | None = None, as_of: date | None = None,
            spot_basis_bp: float | None = None) -> BillCurve:
    """Map the IR strip onto the meeting calendar and the IB-implied path.

    `horizon` is the last day the IB strip can speak for. IR quotes run years
    past the IB contracts that are actually liquid -- the live strips reach
    2030 and early 2028 respectively -- and comparing a 2029 bill price against
    a cash path that ran out in 2027 produces a forty-basis-point "basis" that
    is nothing of the sort: it is the IB side flat-lining. Those windows are
    marked `beyond_horizon` and left uncompared.

    `spot_basis_bp` is today's observed 3-month BBSW minus the cash rate
    target, from RBA table F1 -- the independent anchor for the cash-equivalent
    column. Omit it and that column is simply absent, which is the right
    behaviour offline: better a missing number than one resting on a spread
    nobody measured.
    """
    live = sorted((q for q in quotes if q.ok), key=lambda q: q.month)
    if not live:
        return BillCurve(error="no usable bank bill quotes")

    stale = stale_codes or set()
    today = as_of or date.today()
    effs = sorted(((m, effective_date(m.end)) for m in meetings), key=lambda t: t[1])
    out: list[BillPeriod] = []

    for q in live:
        # The fix is the rate the contract settles on; the bill it stands for
        # accrues from SETTLEMENT DAY, one business day later. A day out of 90
        # is small, but this module claims its spans are exact, so it uses the
        # right boundary rather than the convenient one.
        fix = ir_last_trading_day(q.month)
        covers_start = ir_settlement_day(q.month)
        covers_end = covers_start + timedelta(days=IR_TENOR_DAYS)
        # In force BY the fix, and still undecided as of today: a decision
        # already taken is in `path.spot`, not something the contract is
        # pricing.
        before = [m for m, e in effs if today < e <= fix]
        covered = [m for m, e in effs if covers_start < e < covers_end]
        beyond = horizon is not None and covers_end > horizon

        expected = basis = policy = entry = None
        cash_eq = cash_step = cash_prob = None

        if path.ok and not beyond:
            entry = rate_on(path, covers_start)
            expected = averaged(segments(path, covers_start, covers_end))
            if expected is not None:
                basis = (q.implied_rate - expected) * 100.0
            policy = (rate_on(path, covers_end - timedelta(days=1)) - entry) * 100.0

        # The cash-equivalent reading needs no IB path at all -- that is the
        # point of it. It works past the horizon and on its own terms.
        if spot_basis_bp is not None:
            r_before = rate_on(path, today) if path.ok else None
            cash_eq = q.implied_rate - spot_basis_bp / 100.0
            if r_before is not None:
                cash_step = (cash_eq - r_before) * 100.0
                if before and size:
                    cash_prob = cash_step / size if len(before) == 1 else None

        out.append(BillPeriod(
            code=q.code, month=q.month, fix_date=fix,
            covers_start=covers_start, covers_end=covers_end,
            price=q.price, implied=q.implied_rate,
            meetings_before=before, meetings_covered=covered,
            expected=expected, basis_bp=basis, policy_bp=policy, r_entry=entry,
            spot_basis_bp=spot_basis_bp, cash_equivalent=cash_eq,
            cash_equivalent_step_bp=cash_step, cash_equivalent_prob=cash_prob,
            change_bp=q.change_bp,
            stale=q.code in stale, beyond_horizon=beyond,
        ))

    return BillCurve(periods=out)
