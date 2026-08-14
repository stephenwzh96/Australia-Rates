"""Step 5 -- from a Kelly fraction to a lot count, via the position's DV01.

Reconciled cell-for-cell against the desk spreadsheet (FFV6 / Sep 2026: receive
18 on a 25bp event at q = 50%, $250k max drawdown, $50k daily limit, ZQ DV01).

TWO RISK NUMBERS, NOT ONE
-------------------------
This is the thing an earlier version got wrong. Kelly is a fraction of a
*bankroll*, and for a rates book the bankroll is the **drawdown you can survive**
-- not the daily loss limit. Those are different quantities doing different jobs:

  * `max_drawdown`  the Kelly bankroll. Sets the position.
  * `daily_limit`   a hard cap. Does not size anything; it VETOES.

Sizing off the daily limit understates the position by the ratio between them
(5x in the reference sheet) and hides the case that actually matters: full Kelly
sizing to $76k of risk against a $50k daily limit is a **breach**, and the tool
has to say so rather than quietly producing a smaller number that looks safe.

THE CHAIN
---------
    f          = f* x fraction                    share of bankroll
    max loss   = f x max_drawdown                 dollars risked
    DV01 risk  = max loss / loss_bp               dollars per bp of POSITION
    lots       = DV01 risk / contract DV01        position

The third line is the step a rates trader actually thinks in, and it is why
`f` multiplies dollars rather than DV01: `f` is dimensionless, so it scales the
bankroll, and DV01 only converts the resulting dollars-per-bp into lots.

WHY FRACTIONAL KELLY
--------------------
    g(f) = p ln(1 + f b) + (1 - p) ln(1 - f)

is flat approaching its peak and falls off a cliff past it, reaching zero growth
-- the same as not trading -- while carrying the most risk on the curve.
`zero_growth_fraction` solves for that point rather than assuming the textbook
"2 x f*", which only holds in the continuous limit.

No Streamlit imports: every number here is callable from a test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Sequence

from .pricing import (Side, expected_value, kelly_fraction, max_gain, max_loss,
                      net_odds)

# --------------------------------------------------------------------------
# Contract specifications
# --------------------------------------------------------------------------

FED_FUNDS_DV01 = 5_000_000 * 0.0001 * 30 / 360      # ZQ  -> 41.667
SOFR_3M_DV01 = 1_000_000 * 0.0001 * 90 / 360        # SR3 -> 25.00

DEFAULT_MAX_DRAWDOWN = 250_000.0
DEFAULT_DAILY_LIMIT = 50_000.0
DEFAULT_BETS_PER_YEAR = 8                            # eight scheduled FOMC meetings

# At exactly breakeven f* solves to floating-point residue (~5e-17) rather than
# a clean zero, which is enough to slip past a `<= 0` guard and produce a ladder
# of absurd numbers. Anything under this is no edge.
EDGE_EPSILON = 1e-9

# Ascending in risk, so reading down the table is "stepping up a rung".
DEFAULT_FRACTIONS: tuple[float, ...] = (0.25, 1.0 / 3.0, 0.5, 1.0)


FF_STRIP_MONTHS = 18            # matches data.asx.STRIP_MONTHS
SR3_STRIP_QUARTERS = 10         # matches data.asx.SOFR_QUARTERS
QUARTERLY_MONTHS = (3, 6, 9, 12)


@dataclass(frozen=True)
class Contract:
    key: str
    label: str
    dv01: float
    note: str
    kind: str = "ff"            # "ff" | "sr3" | "custom"
    month: date | None = None


CUSTOM_CONTRACT = Contract("custom", "Custom DV01", 0.0,
                           "Type the DV01 per lot.", kind="custom")

# Legacy keys from before the dropdown listed real contracts.
LEGACY_CONTRACTS: dict[str, Contract] = {
    "zq": Contract("zq", "Fed funds futures (ZQ)", FED_FUNDS_DV01,
                   "$5,000,000 notional, 30/360.", kind="ff"),
    "sr3": Contract("sr3", "3M SOFR futures (SR3)", SOFR_3M_DV01,
                    "$1,000,000 notional, 90/360.", kind="sr3"),
    "custom": CUSTOM_CONTRACT,
}


def _ff_contract(month: date) -> Contract:
    from .contracts import month_code
    code = f"FF{month_code(month)}{month.year % 10}"
    return Contract(
        key=f"ff:{month:%Y-%m}", label=f"{code} — {month:%b %Y}",
        dv01=FED_FUNDS_DV01,
        note="$5,000,000 notional, 30/360 -- $41.67 per bp per lot. "
             "1-month SOFR (SR1) carries the same DV01.",
        kind="ff", month=month,
    )


def _sr3_contract(month: date) -> Contract:
    from .contracts import month_code
    code = f"SR3{month_code(month)}{month.year % 10}"
    return Contract(
        key=f"sr3:{month:%Y-%m}", label=f"{code} — {month:%b %Y} IMM",
        dv01=SOFR_3M_DV01,
        note="$1,000,000 notional, 90/360 -- $25.00 per bp per lot.",
        kind="sr3", month=month,
    )


def contract_universe(anchor: date,
                      ff_months: int = FF_STRIP_MONTHS,
                      sr3_quarters: int = SR3_STRIP_QUARTERS) -> list[Contract]:
    """Every contract the Curve tab quotes, nearest delivery first.

    The same 18 fed funds months and 10 SOFR quarters the strip is built from,
    so the sizing dropdown cannot offer an instrument the curve has no price
    for. Codes are generated from the calendar rather than the live quotes:
    a dropdown should not need the network to populate, and it must render
    identically whether or not the strip fetch succeeded.

    Sorted by delivery month ascending -- nearest to `anchor` first -- with the
    fed funds contract ahead of the SOFR one when both land in the same month.
    """
    from .contracts import add_month

    first = date(anchor.year, anchor.month, 1)
    out: list[Contract] = []

    m = first
    for _ in range(max(0, ff_months)):
        out.append(_ff_contract(m))
        m = add_month(m)

    q = first
    while q.month not in QUARTERLY_MONTHS:
        q = add_month(q)
    for _ in range(max(0, sr3_quarters)):
        out.append(_sr3_contract(q))
        q = add_month(add_month(add_month(q)))

    out.sort(key=lambda c: (c.month, 0 if c.kind == "ff" else 1))
    return out


def contract_for(key: str, anchor: date | None = None) -> Contract:
    """Resolve a saved key to its spec, tolerating legacy and stale keys."""
    if key in LEGACY_CONTRACTS:
        return LEGACY_CONTRACTS[key]
    if anchor is not None:
        match = next((c for c in contract_universe(anchor) if c.key == key), None)
        if match:
            return match
    # A contract that has rolled off the front of the strip still has to render.
    if ":" in key:
        kind, _, stamp = key.partition(":")
        try:
            month = date.fromisoformat(f"{stamp}-01")
        except ValueError:
            return CUSTOM_CONTRACT
        return _sr3_contract(month) if kind == "sr3" else _ff_contract(month)
    return CUSTOM_CONTRACT


def dv01_for(key: str, custom: float | None = None) -> float:
    """DV01 per lot. Keyed off the contract family, so a rolled-off month still
    prices correctly rather than silently falling back to fed funds."""
    if key == "custom":
        return float(custom or 0.0)
    if key.startswith("sr3"):
        return SOFR_3M_DV01
    if key.startswith("ff") or key == "zq":
        return FED_FUNDS_DV01
    return FED_FUNDS_DV01


def fraction_label(multiple: float) -> str:
    """1.0 -> "Full", 0.5 -> "Half", 1/3 -> "Third", 0.25 -> "Quarter"."""
    named = {1.0: "Full", 0.5: "Half", 1.0 / 3.0: "Third", 0.25: "Quarter"}
    for value, name in named.items():
        if abs(multiple - value) < 1e-6:
            return name
    inverse = 1.0 / multiple if multiple else 0.0
    nearest = round(inverse)
    if nearest and abs(inverse - nearest) < 1e-6:
        return f"1/{nearest}"
    return f"{multiple:.3g}x"


def snap_fraction(value: float | None,
                  options: Sequence[float] = DEFAULT_FRACTIONS) -> float:
    """Nearest offered rung. Saved meetings predate this option set."""
    if value is None:
        return 0.25
    return min(options, key=lambda o: abs(o - float(value)))


# --------------------------------------------------------------------------
# The Kelly growth curve
# --------------------------------------------------------------------------

def growth_rate(f: float, p_win: float, b: float) -> float:
    """Expected log growth per bet at bankroll fraction `f`."""
    if f <= 0.0:
        return 0.0
    if f >= 1.0:
        return float("-inf")
    if b == float("inf"):
        return p_win * math.log(1.0 + f * 1e9) + (1.0 - p_win) * math.log(1.0 - f)
    return p_win * math.log(1.0 + f * b) + (1.0 - p_win) * math.log(1.0 - f)


def annualised_growth(g: float, bets_per_year: int = DEFAULT_BETS_PER_YEAR) -> float:
    """Compound `bets_per_year` bets of log growth `g` into an annual return.

    g is a LOG growth rate, so the year's wealth multiple is exp(n g) and the
    annual return is that less one. Simply multiplying by n would report the
    annual log growth instead, which is a smaller and different number.
    """
    if bets_per_year <= 0 or not math.isfinite(g):
        return 0.0
    return math.exp(bets_per_year * g) - 1.0


def zero_growth_fraction(f_star: float, p_win: float, b: float,
                         tol: float = 1e-9) -> float | None:
    """The over-betting point: the f > f* where expected growth returns to zero."""
    if f_star <= 0 or not 0 < p_win < 1:
        return None
    lo, hi = f_star, 1.0 - 1e-12
    if growth_rate(hi, p_win, b) > 0:
        return None
    while hi - lo > tol:
        mid = (lo + hi) / 2.0
        if growth_rate(mid, p_win, b) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------
# The rung ladder
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rung:
    multiple: float
    label: str
    f: float                        # share of the bankroll staked
    max_loss: float                 # f x max_drawdown -- positive dollars risked
    position_dv01: float            # max_loss / loss_bp -- $ per bp of position
    contracts: int
    max_gain: float
    expected_pnl: float
    growth: float                   # g(f), log growth per bet
    growth_share: float             # g(f) / g(f*)
    annualised: float
    within_daily_limit: bool
    delta_loss: float | None        # extra risk over the next-lighter rung
    delta_growth_share: float | None
    growth_per_10k: float | None    # growth-share bought per $10k more at risk
    is_selected: bool = False

    @property
    def status(self) -> str:
        return "pass" if self.within_daily_limit else "breach"

    @property
    def tradeable(self) -> bool:
        return self.contracts > 0


@dataclass(frozen=True)
class SizingLadder:
    f_star: float
    p_win: float
    b: float
    dv01: float
    max_drawdown: float
    daily_limit: float
    bets_per_year: int
    loss_bp: float
    gain_bp: float
    ev_bp: float
    loss_per_contract: float        # one lot, in the bad state
    peak_growth: float
    zero_growth_f: float | None
    daily_cap_contracts: int        # most lots the daily limit allows
    rungs: list[Rung] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.rungs)

    def rung_for(self, multiple: float) -> Rung | None:
        return next((r for r in self.rungs if abs(r.multiple - multiple) < 1e-6), None)

    @property
    def selected(self) -> Rung | None:
        return next((r for r in self.rungs if r.is_selected), None)

    @property
    def breaches(self) -> list[Rung]:
        return [r for r in self.rungs if not r.within_daily_limit]

    @property
    def largest_within_limit(self) -> Rung | None:
        ok = [r for r in self.rungs if r.within_daily_limit]
        return ok[-1] if ok else None


def build_ladder(points: float, size: float, q: float, side: Side = "fade",
                 max_drawdown: float = DEFAULT_MAX_DRAWDOWN,
                 dv01: float = FED_FUNDS_DV01,
                 daily_limit: float = DEFAULT_DAILY_LIMIT,
                 bets_per_year: int = DEFAULT_BETS_PER_YEAR,
                 fractions: Sequence[float] = DEFAULT_FRACTIONS,
                 selected: float | None = None) -> SizingLadder:
    """Every Kelly rung costed out in DV01, lots and dollars."""
    p_win = (1.0 - q) if side == "fade" else q
    b = net_odds(points, size, side)
    f_star = kelly_fraction(points, size, q, side)

    loss_bp = abs(max_loss(points, size, side))
    gain_bp = max_gain(points, size, side)
    ev_bp = expected_value(points, size, q, side)
    loss_per_contract = loss_bp * dv01

    base = SizingLadder(
        f_star=f_star, p_win=p_win, b=b, dv01=dv01, max_drawdown=max_drawdown,
        daily_limit=daily_limit, bets_per_year=bets_per_year,
        loss_bp=loss_bp, gain_bp=gain_bp, ev_bp=ev_bp,
        loss_per_contract=loss_per_contract, peak_growth=0.0,
        zero_growth_f=None, daily_cap_contracts=0,
    )
    if f_star <= EDGE_EPSILON:
        return replace(base, error="No edge at this probability -- Kelly says do not "
                                   "put the trade on at any size.")
    if loss_bp <= 0 or max_drawdown <= 0 or dv01 <= 0:
        return replace(base, error="Need a positive DV01, drawdown budget and downside "
                                   "to size a position.")

    peak = growth_rate(f_star, p_win, b)
    zero_f = zero_growth_fraction(f_star, p_win, b)
    daily_cap = int(math.floor(daily_limit / loss_per_contract)) if loss_per_contract else 0

    rungs: list[Rung] = []
    prev_loss: float | None = None
    prev_share: float | None = None
    for mult in sorted(fractions):
        f = f_star * mult
        risked = f * max_drawdown
        position_dv01 = risked / loss_bp
        # Rounded, not floored: the daily limit -- not lot rounding -- is what
        # enforces the cap, and it is checked explicitly below.
        contracts = int(round(position_dv01 / dv01))
        g = growth_rate(f, p_win, b)
        share = (g / peak) if peak > 0 else 0.0

        d_loss = None if prev_loss is None else risked - prev_loss
        d_share = None if prev_share is None else share - prev_share
        per_10k = (d_share / (d_loss / 10_000.0)
                   if d_loss and d_loss > 0 and d_share is not None else None)

        rungs.append(Rung(
            multiple=mult, label=fraction_label(mult), f=f,
            max_loss=risked, position_dv01=position_dv01, contracts=contracts,
            max_gain=gain_bp * position_dv01,
            expected_pnl=ev_bp * position_dv01,
            growth=g, growth_share=share,
            annualised=annualised_growth(g, bets_per_year),
            within_daily_limit=risked <= daily_limit,
            delta_loss=d_loss, delta_growth_share=d_share, growth_per_10k=per_10k,
            is_selected=selected is not None and abs(mult - selected) < 1e-6,
        ))
        prev_loss, prev_share = risked, share

    return replace(base, peak_growth=peak, zero_growth_f=zero_f,
                   daily_cap_contracts=daily_cap, rungs=rungs)


def growth_curve(f_star: float, p_win: float, b: float,
                 points_n: int = 160) -> list[tuple[float, float]]:
    """(f, growth) samples out to the over-betting zone, for plotting."""
    if f_star <= 0:
        return []
    far = min(0.99, max(f_star * 2.4, f_star + 0.05))
    return [((i / points_n) * far, growth_rate((i / points_n) * far, p_win, b))
            for i in range(points_n + 1)]


def step_up_verdict(frm: Rung, to: Rung) -> str:
    """One sentence on whether stepping up a rung is worth it."""
    extra = to.max_loss - frm.max_loss
    gained = (to.growth_share - frm.growth_share) * 100.0
    if extra <= 0:
        return ""
    flag = "" if to.within_daily_limit else "  — but that breaches the daily limit."
    return (f"{frm.label} to {to.label}: ${extra:,.0f} more at risk buys "
            f"{gained:+.1f}pp of growth.{flag}")
