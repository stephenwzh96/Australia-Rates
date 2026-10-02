"""Multi-meeting trades, counted in 25bp moves instead of one probability."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .pricing import Side

COHERENCE: dict[str, float] = {
    "independent": 0.0,
    "typical cycle": 0.12,
    "all-or-nothing": 0.60,
}
DEFAULT_COHERENCE = "typical cycle"

MAX_RHO = 0.95

_EPS = 1e-12


def rho_for(coherence: str) -> float:
    """The correlation a coherence label stands for. Unknown -> the default."""
    return COHERENCE.get(str(coherence).strip().lower(),
                         COHERENCE[DEFAULT_COHERENCE])


def beta_binomial_pmf(n: int, mean: float, rho: float) -> tuple[float, ...]:
    """P(N = k) for k in 0..n, with E[N] == `mean` exactly."""
    if n <= 0:
        return (1.0,)
    mean = min(max(float(mean), 0.0), float(n))
    rho = min(max(float(rho), 0.0), MAX_RHO)
    pi = mean / n

    if pi <= _EPS:
        return tuple(1.0 if k == 0 else 0.0 for k in range(n + 1))
    if pi >= 1.0 - _EPS:
        return tuple(1.0 if k == n else 0.0 for k in range(n + 1))

    if rho <= 0.0:
        raw = [math.comb(n, k) * pi ** k * (1.0 - pi) ** (n - k)
               for k in range(n + 1)]
    else:
        kappa = (1.0 - rho) / rho
        a, b = pi * kappa, (1.0 - pi) * kappa
        base = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        raw = [math.exp(math.log(math.comb(n, k))
                        + math.lgamma(k + a) + math.lgamma(n - k + b)
                        - math.lgamma(n + a + b) + base)
               for k in range(n + 1)]
    total = sum(raw)
    return tuple(p / total for p in raw) if total else tuple(raw)


@dataclass(frozen=True)
class MovesSummary:
    """One multi-meeting trade, in moves."""
    n: int                        # meetings in the horizon
    size: float                   # bp per move
    points: float                 # bp priced by the horizon meeting
    side: Side
    coherence: str
    rho: float

    moves_priced: float           # points / size -- the market's count
    moves_you: float              # your count; E[N] of the distribution
    pmf: tuple[float, ...]        # index = number of moves

    edge_bp: float                # signed for your side; + is in your favour
    margin_moves: float           # signed; how far your count clears breakeven
    best_bp: float                # every meeting goes your way
    worst_bp: float               # every meeting goes against you (negative)

    p_win: float
    p_scratch: float              # lands exactly on breakeven -- neither
    gain_bp: float                # E[gain | win]
    loss_bp: float                # E[loss | loss], as a positive magnitude

    @property
    def breakeven_moves(self) -> float:
        """The count you have to beat. Same number as `moves_priced` -- there
        is no structural edge in the payoff, only a difference of view."""
        return self.moves_priced

    @property
    def moves_needed(self) -> int:
        """The first whole count that actually pays, for the headline line
        "1.98 priced -- you need 2"."""
        if self.side == "back":
            return math.floor(self.moves_priced) + 1
        return math.ceil(self.moves_priced) - 1

    @property
    def effective(self) -> tuple[float, float, float]:
        """`(points, size, q)` for an equivalent BINARY bet, to be read on the
        "back" side."""
        return self.loss_bp, self.loss_bp + self.gain_bp, self.p_win

    def p_at_least(self, k: int) -> float:
        return sum(self.pmf[min(max(k, 0), self.n):])

    def interval(self, mass: float = 0.80) -> tuple[int, int]:
        """The narrowest central range of counts holding `mass`, as (lo, hi)."""
        tail = (1.0 - mass) / 2.0
        cum, lo, hi = 0.0, 0, self.n
        for k, p in enumerate(self.pmf):
            cum += p
            if cum >= tail:
                lo = k
                break
        cum = 0.0
        for k, p in enumerate(self.pmf):
            cum += p
            if cum >= 1.0 - tail:
                hi = k
                break
        return lo, max(lo, hi)


def summarise(points: float, size: float, n: int, moves: float,
              side: Side = "fade",
              coherence: str = DEFAULT_COHERENCE) -> MovesSummary:
    """Price a multi-meeting trade in moves. Never raises on ordinary input."""
    if size <= 0:
        raise ValueError("size must be positive")
    n = max(0, int(n))
    moves = min(max(float(moves), 0.0), float(n))
    rho = rho_for(coherence)
    pmf = beta_binomial_pmf(n, moves, rho)
    moves_priced = points / size

    def pnl(k: int) -> float:
        """Settlement is `size * k`; you are long that if you backed it."""
        return (size * k - points) if side == "back" else (points - size * k)

    p_win = sum(p for k, p in enumerate(pmf) if pnl(k) > 0)
    p_scratch = sum(p for k, p in enumerate(pmf) if pnl(k) == 0)
    gain = sum(p * pnl(k) for k, p in enumerate(pmf) if pnl(k) > 0)
    loss = sum(p * -pnl(k) for k, p in enumerate(pmf) if pnl(k) < 0)
    p_lose = 1.0 - p_win

    sign = 1.0 if side == "back" else -1.0
    return MovesSummary(
        n=n, size=size, points=points, side=side, coherence=coherence, rho=rho,
        moves_priced=moves_priced, moves_you=moves, pmf=pmf,
        edge_bp=sign * size * (moves - moves_priced),
        margin_moves=sign * (moves - moves_priced),
        best_bp=pnl(n) if side == "back" else pnl(0),
        worst_bp=pnl(0) if side == "back" else pnl(n),
        p_win=p_win, p_scratch=p_scratch,
        gain_bp=(gain / p_win) if p_win > 0 else 0.0,
        loss_bp=(loss / p_lose) if p_lose > 0 else 0.0,
    )
