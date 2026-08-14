"""Golden values -- the numbers that prove the port is still the framework.

Two families here, and they are load-bearing in different ways.

THE PAYOFF ALGEBRA IS SHARED WITH THE US VERSION, DELIBERATELY
---------------------------------------------------------------
`core.pricing`, `core.votes` and `core.sizing`'s Kelly chain contain nothing
jurisdictional -- they are the same arithmetic in any currency -- so the worked
example from the original document is reused verbatim as the regression:
8bp priced on a 25bp event at q = 10% gives 32% implied, +5.50bp EV, 32%
breakeven and 68.75% / 17.2% Kelly. Reproducing the US numbers exactly is the
POINT of these tests: a divergence means the port has damaged the engine, not
that Australia is different.

THE CONTRACT MECHANICS ARE NOT SHARED, AND THAT IS WHERE THE RISK IS
----------------------------------------------------------------------
Everything below the pricing block pins something that had to be re-derived
from the ASX specifications: ACT/365, the IB averaging window, IR's single fix,
the level-dependent bank bill DV01, and the two vendor-feed traps. Those are
the tests that would have caught the real mistakes made while porting.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import bbsw, contracts, holidays, pricing, rba_calendar, sizing, strip, votes

POINTS, SIZE, Q = 8.0, 25.0, 0.10


# --------------------------------------------------------------- Steps 1-5

def test_implied_probability_is_points_over_size():
    """8bp of a 25bp event is 32%, not 8%. The single most important line."""
    assert pricing.implied_probability(POINTS, SIZE) == pytest.approx(0.32)


def test_expected_value():
    assert pricing.expected_value(POINTS, SIZE, Q) == pytest.approx(5.50)


def test_breakeven_equals_implied():
    """Not a coincidence -- it is the definition of fair value."""
    assert (pricing.breakeven_probability(POINTS, SIZE)
            == pytest.approx(pricing.implied_probability(POINTS, SIZE)))


def test_ev_routes_agree_everywhere():
    """`points - size*q` must equal `(implied - q) * size` on both sides.

    The check that catches a points-vs-probability mix-up, and it is free.
    """
    for side in ("fade", "back"):
        for q in (0.0, 0.05, 0.32, 0.5, 1.0):
            assert pricing.ev_routes_agree(POINTS, SIZE, q, side)


def test_kelly():
    assert pricing.kelly_fraction(POINTS, SIZE, Q) == pytest.approx(0.6875)
    assert pricing.fractional_kelly(POINTS, SIZE, Q) == pytest.approx(0.171875)


def test_risk_reward():
    assert pricing.risk_reward(POINTS, SIZE) == pytest.approx(17.0 / 8.0)


# ----------------------------------------------------------------- Step 8

def test_nine_member_board_needs_five():
    """The RBA board is 9, not the FOMC's 12. Majority moves with it."""
    assert votes.votes_needed(9) == 5
    assert votes.votes_needed(12) == 7


def test_decomposition():
    d = votes.decompose(0.25, 0.40)
    assert d.q == pytest.approx(0.10)


def test_conditional_breakeven_is_the_same_number():
    """Invariance is a property of the payoff, not of the scenario."""
    rows = votes.conditional_ev(POINTS, SIZE)
    be = [r for r in rows if r.is_breakeven]
    assert len(be) == 1
    assert be[0].p_centre == pytest.approx(0.32)


# ------------------------------------------------- Australian conventions

def test_ib_dv01_matches_the_asx_quoted_figure():
    """ASX quotes A$24.66. A 360 basis would give A$25.00 -- a 1.4% sizing
    error on every position, which is why ACT/365 is not cosmetic."""
    assert contracts.ib_dv01() == pytest.approx(24.66, abs=0.005)


def test_ir_dv01_is_level_dependent():
    """A bank bill is priced by discounting, so unlike the flat $25.00 SOFR
    contract this replaces, its bp value falls as yields rise."""
    assert contracts.ir_dv01(3.0) == pytest.approx(24.30, abs=0.01)
    assert contracts.ir_dv01(6.0) == pytest.approx(23.94, abs=0.01)
    assert contracts.ir_dv01(3.0) > contracts.ir_dv01(6.0)


def test_day_count_is_365():
    assert contracts.DAY_COUNT_BASIS == 365.0


# ------------------------------------------------------- IB capture window

def test_ib_capture_is_the_calendar_month_proportion():
    """IB settles on the simple average over every calendar day of the month,
    so capture is exactly days-at-new-rate over days-in-month."""
    eff = contracts.effective_date(date(2026, 9, 29))
    assert eff == date(2026, 9, 30)
    days, dim, share = contracts.ib_capture_share(date(2026, 9, 1), eff)
    assert (days, dim) == (1, 30)
    assert share == pytest.approx(1 / 30)


def test_ib_capture_is_binary_at_the_edges():
    eff = contracts.effective_date(date(2026, 9, 29))
    assert contracts.ib_capture_share(date(2026, 10, 1), eff)[2] == 1.0
    assert contracts.ib_capture_share(date(2026, 8, 1), eff)[2] == 0.0


def test_late_month_meeting_makes_the_near_contract_a_trap():
    caps = contracts.capture(date(2026, 9, 29), SIZE)
    assert contracts.near_contract_is_a_trap(caps)
    near = next(c for c in caps if c.is_near)
    far = next(c for c in caps if not c.is_near)
    assert near.share == pytest.approx(1 / 30)
    assert far.share == 1.0


def test_holidays_do_not_change_ib_capture():
    """A public holiday still carries a rate -- the previous business day's --
    so it counts in numerator and denominator alike and cancels out. This is
    why the Sydney holiday calendar does NOT feed the settlement arithmetic."""
    eff = date(2026, 4, 20)            # April carries Easter and Anzac Day
    _, dim, share = contracts.ib_capture_share(date(2026, 4, 1), eff)
    assert dim == 30
    assert share == pytest.approx(11 / 30)


# ----------------------------------------------------------- IR mechanics

def test_ir_capture_is_binary_because_there_is_one_fix():
    """No averaging window means no partial capture. A fractional answer here
    would be inventing a settlement mechanic that does not exist."""
    month = date(2026, 12, 1)
    fix = contracts.ir_last_trading_day(month)
    assert contracts.ir_capture_share(month, fix) == 1.0
    assert contracts.ir_capture_share(month, fix + timedelta(days=1)) == 0.0


def test_ir_last_trading_day_is_the_business_day_before_the_second_friday():
    """Verified against all 18 live IR contracts: the ASX feed's `dateExpiry`
    sits exactly two calendar days before this date on every one."""
    assert contracts.ir_last_trading_day(date(2026, 9, 1)) == date(2026, 9, 10)
    assert contracts.ir_last_trading_day(date(2026, 12, 1)) == date(2026, 12, 10)
    assert contracts.ir_last_trading_day(date(2027, 3, 1)) == date(2027, 3, 11)


# -------------------------------------------------------- Meeting calendar

def test_effective_date_is_the_day_after_the_decision():
    assert contracts.effective_date(date(2026, 9, 29)) == date(2026, 9, 30)


def test_smp_months_are_feb_may_aug_nov():
    """The RBA's forecast round, not the Fed's Mar/Jun/Sep/Dec SEP."""
    assert rba_calendar.SMP_MONTHS == frozenset({2, 5, 8, 11})
    by_month = {m.end.month: m for m in rba_calendar.DEFAULT_MEETINGS}
    assert by_month[8].has_smp
    assert not by_month[9].has_smp


def test_blackout_opens_the_wednesday_before():
    """RBA practice, per its own public-speaking arrangements: the internal
    Policy Discussion, not the Fed's second-Saturday rule."""
    m = next(m for m in rba_calendar.DEFAULT_MEETINGS if m.end == date(2026, 9, 29))
    opens, closes = rba_calendar.blackout_window(m)
    assert opens == date(2026, 9, 23)
    assert opens.weekday() == 2
    assert closes == m.end
    assert rba_calendar.in_blackout(m, date(2026, 9, 24))
    assert not rba_calendar.in_blackout(m, date(2026, 9, 22))


def test_eight_meetings_a_year_both_years():
    for year in (2026, 2027):
        n = sum(1 for m in rba_calendar.DEFAULT_MEETINGS if m.end.year == year)
        assert n == 8
    assert all(m.verified for m in rba_calendar.DEFAULT_MEETINGS)


def test_bets_per_year_matches_the_calendar():
    assert sizing.DEFAULT_BETS_PER_YEAR == 8


# ----------------------------------------------------- Sydney business days

def test_anzac_day_is_never_substituted_in_nsw():
    """25 April 2026 is a Saturday. NSW does NOT create a Monday holiday for
    it, unlike New Year's Day or Australia Day."""
    assert date(2026, 4, 25).weekday() == 5
    assert date(2026, 4, 25) in holidays.holidays(2026)
    assert date(2026, 4, 27) not in holidays.holidays(2026)


def test_fixed_holidays_substitute_forward():
    """NSW moves a weekend holiday FORWARD to Monday, where the US federal
    rule moves a Saturday one back to Friday."""
    assert date(2027, 1, 1).weekday() == 4          # Friday, no shift
    assert date(2027, 1, 1) in holidays.holidays(2027)
    # 26 Jan 2025 was a Sunday -> observed Monday the 27th.
    assert date(2025, 1, 27) in holidays.holidays(2025)


def test_christmas_and_boxing_day_do_not_collapse():
    """25 Dec 2027 is a Saturday and 26th a Sunday; both substitute, and they
    must land on different days rather than merging into one holiday."""
    h = holidays.holidays(2027)
    assert date(2027, 12, 27) in h
    assert date(2027, 12, 28) in h


def test_last_business_day_of_month_is_the_ib_last_trading_day():
    assert holidays.last_business_day_of_month(2026, 8) == date(2026, 8, 31)
    assert holidays.last_business_day_of_month(2026, 9) == date(2026, 9, 30)


# ------------------------------------------------------ Strip decomposition

class _Q:
    """Minimal stand-in for `data.asx.Quote`.

    Deliberately hand-rolled rather than built by calling the live feed: these
    tests must pin the arithmetic with the network unplugged.
    """

    def __init__(self, month: date, rate: float, code: str = "X"):
        self.month, self._rate, self.code = month, rate, code
        self.ok = True
        self.change_bp = None

    @property
    def implied_rate(self) -> float:
        return self._rate

    @property
    def price(self) -> float:
        return 100.0 - self._rate


def test_strip_round_trips_the_blend():
    """Decompose then recompose must reprint the contract exactly.

    The invariant the whole Curve tab rests on: `r_month * N = r_before * n1 +
    r_after * n2`. Uses the November 2026 meeting, which sits mid-month and so
    is solved by inversion rather than read off the next contract.
    """
    meetings = [m for m in rba_calendar.DEFAULT_MEETINGS if m.end == date(2026, 11, 3)]
    quotes = [_Q(date(2026, 11, 1), 4.46, "IBX6")]
    path = strip.decompose(meetings, quotes, spot=4.35, size=25.0,
                           as_of=date(2026, 10, 1))
    assert path.ok
    s = path.steps[0]
    assert s.source == "blend"
    recomposed = (s.r_before * s.days_before + s.r_after * s.days_after) / s.days_in_month
    assert recomposed == pytest.approx(s.implied_month_rate)


def test_strip_reads_a_late_month_meeting_off_the_following_contract():
    """September's decision covers 1 of 30 days, far below the capture
    threshold, so the October contract answers instead -- a clean read."""
    meetings = [m for m in rba_calendar.DEFAULT_MEETINGS if m.end == date(2026, 9, 29)]
    quotes = [_Q(date(2026, 9, 1), 4.35, "IBU6"), _Q(date(2026, 10, 1), 4.38, "IBV6")]
    path = strip.decompose(meetings, quotes, spot=4.35, size=25.0,
                           as_of=date(2026, 8, 14))
    s = path.steps[0]
    assert s.source == "next" and s.is_clean
    assert s.code == "IBV6"
    assert s.step_bp == pytest.approx(3.0)
    assert s.prob == pytest.approx(0.12)


def test_strip_skips_meetings_already_in_effect():
    """`spot` already contains a decision that has happened. Re-solving it off
    the front contract invents a phantom step and shifts every meeting behind
    it -- observed as -0.8bp on the live August 2026 strip."""
    meetings = list(rba_calendar.DEFAULT_MEETINGS)
    quotes = [_Q(date(2026, 8, 1), 4.345, "IBQ6"), _Q(date(2026, 9, 1), 4.35, "IBU6"),
              _Q(date(2026, 10, 1), 4.38, "IBV6")]
    path = strip.decompose(meetings, quotes, spot=4.35, size=25.0,
                           as_of=date(2026, 8, 14))
    assert all(s.meeting.end > date(2026, 8, 14) for s in path.steps)
    assert path.steps[0].r_before == pytest.approx(4.35)


def test_strip_refuses_to_invent_spot():
    """Once a meeting has taken effect mid-month with another landing before
    the next clean contract, spot genuinely is not recoverable. Returning None
    is the correct answer; the caller anchors on the published cash rate."""
    meetings = list(rba_calendar.DEFAULT_MEETINGS)
    quotes = [_Q(date(2026, 8, 1), 4.345), _Q(date(2026, 9, 1), 4.35)]
    assert strip.implied_spot(quotes, meetings, as_of=date(2026, 8, 14)) is None


# ------------------------------------------------------------ Bank bills

def test_bill_basis_is_not_used_to_solve_the_forward():
    """Guards against a bug this codebase actually had.

    `basis_bp` is defined as implied minus the IB-path average, so subtracting
    it from the implied rate returns the IB path exactly and learns nothing
    from the bill price. The cash-equivalent column must therefore move with
    the INDEPENDENT spot spread, not with the basis.
    """
    meetings = [m for m in rba_calendar.DEFAULT_MEETINGS if m.end == date(2026, 11, 3)]
    ib = [_Q(date(2026, 11, 1), 4.46, "IBX6")]
    path = strip.decompose(meetings, ib, spot=4.35, size=25.0, as_of=date(2026, 10, 1))

    bills = [_Q(date(2026, 12, 1), 4.60, "IRZ6")]
    a = bbsw.analyse(bills, path, meetings, as_of=date(2026, 10, 1), spot_basis_bp=16.0)
    b = bbsw.analyse(bills, path, meetings, as_of=date(2026, 10, 1), spot_basis_bp=30.0)
    pa, pb = a.periods[0], b.periods[0]
    assert pa.basis_bp == pytest.approx(pb.basis_bp)          # basis unchanged
    assert pa.cash_equivalent == pytest.approx(4.60 - 0.16)
    assert pb.cash_equivalent == pytest.approx(4.60 - 0.30)
    assert pa.cash_equivalent != pytest.approx(pb.cash_equivalent)


def test_bill_curve_marks_windows_the_ib_strip_cannot_reach():
    """IR quotes run to 2030 and IB to early 2028. Comparing them past the IB
    horizon produces a 'basis' that is really the IB side flat-lining."""
    meetings = list(rba_calendar.DEFAULT_MEETINGS)
    ib = [_Q(date(2026, 11, 1), 4.46, "IBX6")]
    path = strip.decompose(meetings, ib, spot=4.35, size=25.0, as_of=date(2026, 10, 1))
    far = [_Q(date(2029, 9, 1), 4.50, "IRU9")]
    curve = bbsw.analyse(far, path, meetings, horizon=date(2027, 5, 31),
                         as_of=date(2026, 10, 1))
    p = curve.periods[0]
    assert p.beyond_horizon
    assert p.basis_bp is None and not p.comparable


def test_bill_averaging_is_simple_not_compounded():
    """Both instruments are simple-yield, so compounding here would introduce
    a couple of basis points belonging to neither."""
    assert bbsw.averaged([(4.0, 30), (5.0, 60)]) == pytest.approx((4.0 * 30 + 5.0 * 60) / 90)


# ---------------------------------------------------------------- Sizing

def test_lot_count_chain():
    """f x bankroll / loss_bp / contract DV01, in A$ on the IB contract."""
    lad = sizing.build_ladder(POINTS, SIZE, Q, "fade",
                              max_drawdown=250_000.0, dv01=sizing.IB_DV01,
                              daily_limit=50_000.0, bets_per_year=8,
                              selected=0.25)
    assert lad.ok
    r = lad.selected
    assert r.f == pytest.approx(0.171875)
    assert r.max_loss == pytest.approx(0.171875 * 250_000.0)
    assert r.position_dv01 == pytest.approx(r.max_loss / 17.0)
    # Lots are whole contracts -- you cannot trade 102.5 of them -- so the
    # chain is checked against the rounded figure the ladder actually shows.
    assert r.contracts == round(r.position_dv01 / sizing.IB_DV01)


def test_daily_limit_vetoes_rather_than_sizes():
    """Full Kelly here risks far more than the daily limit. The tool has to
    call that a breach, not quietly produce a smaller number that looks safe."""
    lad = sizing.build_ladder(POINTS, SIZE, Q, "fade", 250_000.0, sizing.IB_DV01,
                              50_000.0, 8, selected=0.25)
    assert lad.rung_for(0.25).within_daily_limit
    assert not lad.rung_for(1.0).within_daily_limit
