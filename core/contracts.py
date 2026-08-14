"""Contract mechanics for the two ASX short-end instruments.

IB (30 Day Interbank Cash Rate Futures) settles on the simple average of the
RBA's Interbank Overnight Cash Rate across the delivery month. A late-month
meeting is therefore expressed in the FOLLOWING contract: a decision announced
on 29 September is effective the 30th, so IBU6 captures 1 of 30 days --
roughly 3% of the intended risk. Putting the position in the wrong month is the
cheapest way to be right about the RBA and flat on the P&L.

CAPTURE IS A DV01 SCALER, NOT JUST A WARNING
---------------------------------------------
"Captures 47% of the move" is not a caveat to read and move on from -- it is a
multiplier on the contract's sensitivity, and it belongs in the arithmetic:

    DV01 per bp OF THE EVENT  =  contract DV01  x  capture

Sizing, the exit map's price column, and the volatility estimate all convert
between "bp of the event" and "bp of contract price", so all three need it.
Omitting it under-sizes a partial-capture position by exactly 1/capture.

THE TWO INSTRUMENTS ARE NOT TWO FLAVOURS OF THE SAME THING
-----------------------------------------------------------
This is the difference that does not survive translation from the US version,
where fed funds and SOFR futures are both averaging instruments and differ only
in whether the average compounds:

  * IB settles on the SIMPLE AVERAGE of the cash rate over every CALENDAR day
    of the delivery month -- weekends and public holidays carry the previous
    business day's fix (ASX contract spec). Sensitivity to a rate applying from
    day `e` onward is therefore exactly `days_at_new_rate / days_in_month`: a
    proportion, exact by construction, no compounding and no holiday calendar
    involved. This is the same arithmetic as CME's ZQ.

  * IR settles on a SINGLE 3-month BBSW fix taken on its last trading day.
    There is no averaging window at all, so there is no partial capture to
    compute: either the decision lands before the fix and the contract sees the
    new rate in full, or it lands after and the contract never sees it. Capture
    is BINARY. Anything that looked like a fractional IR capture would be an
    expectations effect -- the market pricing a move it has not had yet -- and
    not a settlement mechanic.

ACT/365
-------
Australian money market convention, and the reason every day-count here reads
365 where the US original reads 360. It is not cosmetic: it sets IB's value per
basis point at A$24.66 rather than the A$25.00 the 360 basis would give.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

# Month codes, shared with the CME convention the ASX also uses.
MONTH_CODES = "FGHJKMNQUVXZ"

# Below this share of the intended move, the near contract is not expressing
# the view.
CAPTURE_WARNING_THRESHOLD = 0.20

# Australian money market basis. ACT/365, not the ACT/360 of USD.
DAY_COUNT_BASIS = 365.0

# Contract sizes, from the ASX specifications.
IB_NOTIONAL = 3_000_000.0        # A$, 30-day
IB_TENOR_DAYS = 30
IR_FACE_VALUE = 1_000_000.0      # A$, 90-day bank accepted bills
IR_TENOR_DAYS = 90


def month_code(d: date) -> str:
    return MONTH_CODES[d.month - 1]


def ib_contract_code(d: date) -> str:
    """e.g. IBU6 for September 2026 -- the short form for display."""
    return f"IB{month_code(d)}{d.year % 10}"


def ir_contract_code(d: date) -> str:
    """e.g. IRZ6 for the December 2026 bank bill contract."""
    return f"IR{month_code(d)}{d.year % 10}"


def add_month(d: date) -> date:
    return date(d.year + (d.month // 12), d.month % 12 + 1, 1)


def effective_date(meeting_end: date) -> date:
    """A cash rate change takes effect the day after the decision.

    The RBA announces at 2:30pm Sydney on the second day of the meeting and the
    new target applies from the following day, which is the same shape as the
    Fed's rule and is what lets `core.strip` invert the IB month blend
    unchanged.
    """
    return meeting_end + timedelta(days=1)


def ib_capture_share(month_start: date, effective: date) -> tuple[int, int, float]:
    """`(days_at_new_rate, days_in_month, share)` for the IB contract
    delivering in `month_start`'s month.

    Exact. IB settles on the simple average across every calendar day, so the
    sensitivity to a rate applying from `effective` is the plain proportion of
    days it covers. Holidays do not enter: a public holiday still carries a
    rate (the previous business day's), so it still counts as a day in both the
    numerator and the denominator.
    """
    dim = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = date(month_start.year, month_start.month, dim)
    if effective > month_end:
        days = 0
    elif effective <= month_start:
        days = dim
    else:
        days = (month_end - effective).days + 1
    return days, dim, days / dim


def ir_last_trading_day(month_start: date) -> date:
    """The IR contract's BBSW fix date.

    ASX sets it as the business day immediately prior to settlement day, with
    settlement on the second Friday of the delivery month. Derived rather than
    read off the price feed, whose `dateExpiry` field is a vendor artifact --
    see `data.asx`.
    """
    from . import holidays as hol
    second_friday = _nth_weekday(month_start.year, month_start.month, 4, 2)
    return hol.previous_business_day(second_friday)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def ir_capture_share(month_start: date, effective: date) -> float:
    """Binary: 1.0 if the rate change is in force by the contract's BBSW fix,
    else 0.0.

    A single fix cannot see a fraction of a move. Returning 0.5 here for a
    meeting halfway through the contract's life would be inventing a
    settlement mechanic that does not exist.
    """
    return 1.0 if effective <= ir_last_trading_day(month_start) else 0.0


def contract_capture_share(kind: str, month_start: date | None, meeting_end: date) -> float:
    """Capture for any contract, dispatched on its kind.

    Returns 1.0 for an instrument with no month structure ("custom", or an
    RBA-dated OIS leg): those are already quoted on the event itself, so no
    conversion is needed.
    """
    if kind not in ("ib", "ir") or month_start is None:
        return 1.0
    eff = effective_date(meeting_end)
    if kind == "ib":
        return ib_capture_share(month_start, eff)[2]
    return ir_capture_share(month_start, eff)


# --------------------------------------------------------------------------
# Value per basis point
# --------------------------------------------------------------------------

def ib_dv01() -> float:
    """A$24.66 per basis point per lot -- fixed, and quoted as such by ASX.

    A$3,000,000 notional over a 30-day month on an ACT/365 basis:
        3,000,000 x 0.0001 x 30/365 = 24.657...
    """
    return IB_NOTIONAL * 0.0001 * IB_TENOR_DAYS / DAY_COUNT_BASIS


def ir_price(yield_pct: float) -> float:
    """Dollar value of one IR contract at a given yield, per the ASX formula.

        P = 1,000,000 / (1 + i x 90/365)

    Discount pricing, not the linear `100 - yield` of an averaging contract.
    """
    i = yield_pct / 100.0
    return IR_FACE_VALUE / (1.0 + i * IR_TENOR_DAYS / DAY_COUNT_BASIS)


def ir_dv01(yield_pct: float) -> float:
    """Value of one basis point on an IR contract AT THE CURRENT YIELD.

    Roughly A$24 but NOT a constant, which is the trap: the US original's SR3
    is a flat $25.00/bp because it is quoted on a linear rate, whereas a bank
    bill is priced by discounting, so its bp value falls as yields rise. At
    3% it is A$24.30; at 6% it is A$23.94 -- 1.5% apart, which on a thousand
    lots is real money. The live yield is always passed in rather than
    defaulted.
    """
    return ir_price(yield_pct) - ir_price(yield_pct + 0.01)


@dataclass(frozen=True)
class ContractCapture:
    code: str
    month_label: str
    days_in_month: int
    days_at_new_rate: int
    share: float
    captured_bp: float
    is_near: bool


def capture(meeting_end: date, size: float) -> list[ContractCapture]:
    """Effective bp captured by the meeting-month and following-month IB
    contracts -- the near/far comparison that decides which month to trade."""
    eff = effective_date(meeting_end)
    near = date(meeting_end.year, meeting_end.month, 1)
    far = add_month(near)

    out: list[ContractCapture] = []
    for i, m in enumerate((near, far)):
        days, dim, share = ib_capture_share(m, eff)
        out.append(
            ContractCapture(
                code=ib_contract_code(m),
                month_label=m.strftime("%B %Y"),
                days_in_month=dim,
                days_at_new_rate=days,
                share=share,
                captured_bp=share * size,
                is_near=i == 0,
            )
        )
    return out


def near_contract_is_a_trap(captures: list[ContractCapture],
                            threshold: float = CAPTURE_WARNING_THRESHOLD) -> bool:
    near = next((c for c in captures if c.is_near), None)
    return near is not None and near.share < threshold


@dataclass(frozen=True)
class Instrument:
    name: str
    rank: int
    verdict: str
    caveat: str


INSTRUMENT_RANKING: tuple[Instrument, ...] = (
    Instrument(
        "RBA-dated OIS", 1, "Cleanest",
        "Exact event isolation, no calendar contamination, no basis. Traded OTC, "
        "so the price is a dealer quote rather than a screen.",
    ),
    Instrument(
        "IB, following month", 2, "Clean, and screen-priced",
        "You own the whole month's average cash rate. In Australia that is a "
        "cleaner exposure than its US counterpart: AONIA tracks the target "
        "closely under the RBA's ample-reserves framework, so there is little "
        "of the reserve-scarcity drift a fed funds contract carries.",
    ),
    Instrument(
        "IR, 90 day bank bill", 3, "Avoid as the primary leg",
        "Takes BBSW/OIS basis -- bank credit and term premium -- which you have "
        "no view on, and settles on a single fix rather than an average, so a "
        "quiet day at the fix decides the whole contract. Deep and liquid, "
        "which is the argument for it, but it is not an event instrument.",
    ),
)
