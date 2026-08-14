"""Steps 1-5 of the event-pricing framework.

The event is "the FOMC moves `size` bp in `direction` at this meeting". You either
FADE it (sell the event -- the July 2026 receive) or BACK it (buy the event).

Every number below collapses to one identity:

    EV = points - size * q          (fade)
    EV = size * q - points          (back)

and the breakeven probability is `points / size` on both sides. Direction only
changes labels and the sign of the strip mapping, never the payoff algebra.

No Streamlit imports here -- everything is callable from a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

Side = Literal["fade", "back"]
Direction = Literal["hike", "cut"]

DEFAULT_KELLY_FRACTION = 0.25


# --------------------------------------------------------------------------
# Step 1 -- points priced to implied probability
# --------------------------------------------------------------------------

def implied_probability(points: float, size: float) -> float:
    """Points priced / size of the move.

    The single most important line in the framework. 8bp of a 25bp event is a
    32% implied probability, not 8%.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    return points / size


def fair_value_points(probability: float, size: float) -> float:
    """Inverse of `implied_probability` -- the arithmetic check in Step 1."""
    return probability * size


# --------------------------------------------------------------------------
# Step 2 -- the two terminal outcomes
# --------------------------------------------------------------------------

def max_gain(points: float, size: float, side: Side = "fade") -> float:
    """Payoff if the event does NOT happen (fade) / DOES happen (back)."""
    return points if side == "fade" else size - points


def max_loss(points: float, size: float, side: Side = "fade") -> float:
    """Signed (negative) worst case. Capped because the move size is capped."""
    return -(size - points) if side == "fade" else -points


def risk_reward(points: float, size: float, side: Side = "fade") -> float:
    """Loss per unit of gain, e.g. 2.1 for the July +8 / -17 profile."""
    gain = max_gain(points, size, side)
    if gain == 0:
        return float("inf")
    return abs(max_loss(points, size, side)) / gain


@dataclass(frozen=True)
class Outcome:
    label: str
    settles_at: float
    pnl: float


def terminal_outcomes(
    points: float,
    size: float,
    side: Side = "fade",
    direction: Direction = "hike",
) -> list[Outcome]:
    """Step 2's two-row table."""
    move_label = f"{size:g}bp {direction}"
    return [
        Outcome("Hold", 0.0, max_gain(points, size, side) if side == "fade" else max_loss(points, size, side)),
        Outcome(move_label, size, max_loss(points, size, side) if side == "fade" else max_gain(points, size, side)),
    ]


# --------------------------------------------------------------------------
# Step 3 -- expected value, breakeven, edge
# --------------------------------------------------------------------------

def expected_value(points: float, size: float, q: float, side: Side = "fade") -> float:
    """EV via the collapsed form: points - size*q (fade).

    Derivation for the fade side, with q = P(event):
        EV = (1-q)*points - q*(size-points) = points - size*q
    """
    return points - size * q if side == "fade" else size * q - points


def edge(points: float, size: float, q: float, side: Side = "fade") -> float:
    """EV via the second route: (market probability - your probability) * size.

    Must agree with `expected_value` to floating point. The framework uses the
    agreement of these two routes as its arithmetic check -- it is exactly the
    check that catches a points-vs-probability mix-up.
    """
    gap = implied_probability(points, size) - q
    return gap * size if side == "fade" else -gap * size


def ev_routes_agree(points: float, size: float, q: float, side: Side = "fade", tol: float = 1e-9) -> bool:
    return abs(expected_value(points, size, q, side) - edge(points, size, q, side)) < tol


def breakeven_probability(points: float, size: float) -> float:
    """Solve EV = 0 for q. Equals the implied probability, on both sides.

    Not a coincidence -- it is the definition of fair value. There is no
    structural edge in the payoff shape; the only edge is q differing from
    the market's number.
    """
    return implied_probability(points, size)


def margin_of_safety(points: float, size: float, q: float, side: Side = "fade") -> float:
    """Percentage points of room between your q and breakeven, signed so that
    positive always means "in your favour"."""
    be = breakeven_probability(points, size)
    return (be - q) if side == "fade" else (q - be)


# --------------------------------------------------------------------------
# Step 4 -- sensitivity to q
# --------------------------------------------------------------------------

DEFAULT_Q_GRID: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20, 0.25, 0.32, 0.40)


@dataclass(frozen=True)
class SensitivityRow:
    q: float
    ev: float
    is_breakeven: bool


def sensitivity(
    points: float,
    size: float,
    side: Side = "fade",
    q_grid: Sequence[float] | None = None,
    include_breakeven: bool = True,
) -> list[SensitivityRow]:
    """EV across a grid of q, with the breakeven row always present."""
    grid = list(q_grid) if q_grid is not None else list(DEFAULT_Q_GRID)
    be = breakeven_probability(points, size)
    if include_breakeven and not any(abs(g - be) < 1e-9 for g in grid):
        grid.append(be)
    grid = sorted(set(round(g, 10) for g in grid))
    return [
        SensitivityRow(g, expected_value(points, size, g, side), abs(g - be) < 1e-9)
        for g in grid
    ]


# --------------------------------------------------------------------------
# Step 5 -- Kelly sizing
# --------------------------------------------------------------------------

def net_odds(points: float, size: float, side: Side = "fade") -> float:
    """b = W / L."""
    loss = abs(max_loss(points, size, side))
    if loss == 0:
        return float("inf")
    return max_gain(points, size, side) / loss


def kelly_fraction(points: float, size: float, q: float, side: Side = "fade") -> float:
    """f* = (1-q) - q*(L/W) for the fade side; p is the win probability.

    Equivalent to the standard (p*b - q)/b. Can go negative when you are on the
    wrong side of breakeven -- that is meaningful (it says do not put it on),
    so it is returned unclipped and the UI clamps for display.
    """
    p_win = (1.0 - q) if side == "fade" else q
    p_lose = 1.0 - p_win
    b = net_odds(points, size, side)
    if b == float("inf"):      # nothing to lose
        return p_win
    if b == 0.0:               # nothing to win -- there is no bet here
        return 0.0
    return p_win - p_lose / b


def fractional_kelly(
    points: float,
    size: float,
    q: float,
    side: Side = "fade",
    fraction: float = DEFAULT_KELLY_FRACTION,
) -> float:
    """The column to actually look at.

    Full Kelly assumes log utility, a repeatable independent bet and no
    mark-to-market constraint -- none of which describe a rates book facing a
    single event with a risk limit and a P&L stop.
    """
    return kelly_fraction(points, size, q, side) * fraction


@dataclass(frozen=True)
class KellyRow:
    q: float
    full: float
    fractional: float


def kelly_table(
    points: float,
    size: float,
    side: Side = "fade",
    q_grid: Sequence[float] = (0.10, 0.15, 0.20, 0.25),
    fraction: float = DEFAULT_KELLY_FRACTION,
) -> list[KellyRow]:
    return [
        KellyRow(q, kelly_fraction(points, size, q, side), fractional_kelly(points, size, q, side, fraction))
        for q in q_grid
    ]


# --------------------------------------------------------------------------
# Roll-up used by the Verdict card
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PricingSummary:
    points: float
    size: float
    q: float
    side: Side
    direction: Direction
    implied: float
    ev: float
    ev_via_edge: float
    breakeven: float
    margin: float
    gain: float
    loss: float
    rr: float
    kelly_full: float
    kelly_fractional: float
    kelly_fraction_used: float


def summarise(
    points: float,
    size: float,
    q: float,
    side: Side = "fade",
    direction: Direction = "hike",
    fraction: float = DEFAULT_KELLY_FRACTION,
) -> PricingSummary:
    return PricingSummary(
        points=points,
        size=size,
        q=q,
        side=side,
        direction=direction,
        implied=implied_probability(points, size),
        ev=expected_value(points, size, q, side),
        ev_via_edge=edge(points, size, q, side),
        breakeven=breakeven_probability(points, size),
        margin=margin_of_safety(points, size, q, side),
        gain=max_gain(points, size, side),
        loss=max_loss(points, size, side),
        rr=risk_reward(points, size, side),
        kelly_full=kelly_fraction(points, size, q, side),
        kelly_fractional=fractional_kelly(points, size, q, side, fraction),
        kelly_fraction_used=fraction,
    )
