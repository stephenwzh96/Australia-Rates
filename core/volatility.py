"""Per-release volatility multipliers -- how much louder is an NFP day?

`montecarlo` used to run one flat sigma across every day between now and the
decision. A Thursday carrying CPI is not a quiet Monday, and for a barrier
problem the density of loud days inside the forward window is what sets the
total variance. This estimates, for each release, how much variance its own
publication days carry relative to a quiet day.

TWO WINDOWS, ON PURPOSE
-----------------------
The level and the shape are estimated over DIFFERENT lookbacks, because they
have different stability properties. Measured on the ZQ contract this app
actually sizes in:

    sigma_quiet    2.14bp (90d)   3.11bp (1y)   4.97bp (2y)
    NFP variance ratio            2.24x         5.13x

The level is strongly regime-dependent -- 2024-25 was a far livelier rate path,
and pulling it into today's estimate would import a stale regime. The ratio
needs the long window to be estimable at all: at one year its 95% interval is
[0.87, 5.80], which does not even exclude "no effect".

So: LEVEL from a short window (recent regime), SHAPE from a long one (enough
release observations to measure). Each parameter takes the window that suits
it rather than one compromise window suiting neither.

EACH RELEASE USES ONLY ITS OWN HISTORY
--------------------------------------
No pooling across release types: a CPI multiplier is estimated from CPI days
alone. That keeps every number attributable to the series it describes, at the
cost of small samples -- roughly 24 observations over two years. Small samples
are handled honestly rather than hidden:

  * the estimate carries its own sample size and confidence interval, and
  * it is SHRUNK toward 1.0 (no effect) with weight n/(n + PRIOR_STRENGTH).

Shrinkage toward the null is not borrowing from other releases; it is refusing
to let eight noisy observations assert a 5x multiplier outright. With n=24 and
a prior strength of 12, a raw 5x becomes 3.7x; by n=100 the prior is nearly
irrelevant, which is the behaviour you want as history accumulates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

# Observations of a release's own days needed before its raw ratio is taken at
# close to face value. Set to roughly one year of a monthly release, so a
# two-year sample lands near two-thirds weight on the data.
PRIOR_STRENGTH = 12.0

# Below this many observations, a ratio is reported but never applied: three
# prints cannot distinguish a loud release from a quiet one.
MIN_OBSERVATIONS = 6

# A single day's variance can only be scaled this far. Guards against a lone
# outlier -- a fat-finger print, a bad close -- producing an absurd schedule.
MAX_MULTIPLIER = 12.0


@dataclass(frozen=True)
class Multiplier:
    """One release's variance ratio against quiet days."""
    name: str
    n_event: int
    n_quiet: int
    raw: float                  # var(event days) / var(quiet days)
    shrunk: float               # what actually gets applied
    ci_low: float
    ci_high: float

    @property
    def usable(self) -> bool:
        return self.n_event >= MIN_OBSERVATIONS and self.n_quiet >= MIN_OBSERVATIONS

    @property
    def applied(self) -> float:
        """What the schedule actually uses -- never below 1.0.

        Some releases genuinely measure quieter than an average quiet day:
        PCE lands around 0.8x on real ZQ data, because CPI and PPI have
        already told the market most of what core PCE will say. That is a
        real effect, and it is still not one to size on. The quiet bucket is
        a mixture that includes unlabelled events -- refunding announcements,
        Fed speakers, geopolitics -- so "quieter than the mixture" is not the
        same as "safe", and letting a SCHEDULED release reduce estimated risk
        below baseline is the one direction a risk tool should not be
        aggressive in. The raw figure is still reported, so the measurement
        is visible even though it is not acted on.
        """
        if not self.usable:
            return 1.0
        return max(1.0, self.shrunk)

    @property
    def significant(self) -> bool:
        """Interval excludes 'no effect'."""
        return self.usable and self.ci_low > 1.0


def _shrink(raw: float, n: int, prior_strength: float = PRIOR_STRENGTH) -> float:
    w = n / (n + prior_strength)
    return 1.0 + w * (raw - 1.0)


def daily_changes(history: list[tuple[date, float]],
                  scale: float = 100.0) -> dict[date, float]:
    """`{date: change}` in bp, keyed by the LATER date of each pair -- the day
    the move happened, which is the day whose label applies to it."""
    rows = sorted(history)
    return {d1: (c1 - c0) * scale for (_, c0), (d1, c1) in zip(rows, rows[1:])}


def estimate(changes: dict[date, float], event_dates: set[date],
             quiet_dates: set[date], name: str) -> Multiplier:
    """Variance ratio of `event_dates` against `quiet_dates`.

    `quiet_dates` must already exclude every OTHER release and the decision
    days themselves -- a contaminated denominator is the fastest way to
    understate a multiplier, and it is the caller that knows the full calendar.
    """
    ev = np.array([changes[d] for d in event_dates if d in changes], dtype=float)
    qt = np.array([changes[d] for d in quiet_dates if d in changes], dtype=float)
    n_e, n_q = ev.size, qt.size
    if n_e < 2 or n_q < 2:
        return Multiplier(name, n_e, n_q, 1.0, 1.0, 1.0, 1.0)

    # Variance about zero, not about the sample mean: a daily price change has
    # no drift worth estimating over 30 days, and removing a spuriously fitted
    # mean would understate the dispersion the simulation needs.
    v_e = float(np.mean(ev ** 2))
    v_q = float(np.mean(qt ** 2))
    if v_q <= 0:
        return Multiplier(name, n_e, n_q, 1.0, 1.0, 1.0, 1.0)

    raw = min(v_e / v_q, MAX_MULTIPLIER)
    # Log-ratio standard error for two variance estimates, the usual
    # sqrt(2/n_e + 2/n_q) approximation.
    se = math.sqrt(2.0 / n_e + 2.0 / n_q)
    return Multiplier(
        name=name, n_event=n_e, n_quiet=n_q, raw=raw,
        shrunk=min(_shrink(raw, n_e), MAX_MULTIPLIER),
        ci_low=raw * math.exp(-1.96 * se), ci_high=raw * math.exp(1.96 * se),
    )


"""How many recent calendar days the LEVEL is estimated over. The shape uses
whatever history it is given, which should be years."""
LEVEL_WINDOW_DAYS = 90


def estimate_from_history(
    history: list[tuple[date, float]],
    events: list[tuple[date, str]],
    exclude_dates: set[date] | None = None,
    level_window_days: int = LEVEL_WINDOW_DAYS,
) -> tuple[float | None, dict[str, Multiplier], int]:
    """`(quiet-day sigma, {series_key: Multiplier}, n_quiet_observations)`.

    The third element is the number of quiet-day DIFF observations the level
    was estimated from -- the honest sample size for the vol-uncertainty
    chi-square in `montecarlo` (a variance estimate from n diffs has n-1
    degrees of freedom), replacing a close-count approximation.

    `events` is `(date, series_key)` for every release landing in the history
    window -- generate it from `econ_calendar.recurring_events` over the same
    window so the labels match the ones the forward schedule will use.

    TWO WINDOWS, per the module docstring: multipliers use the whole history
    (they need the release count), the quiet sigma uses only the last
    `level_window_days` (it needs the current regime). Measured on ZQ, the
    level roughly doubles between a 90-day and a two-year window while the
    ratios do not -- estimating both on the long window would silently import
    2024's volatility into a 2026 trade.

    The quiet bucket excludes EVERY labelled release and every excluded date
    (decision days), not just the release being measured. A quiet bucket that
    still contains CPI days understates every multiplier, because the thing
    each one is measured against is itself inflated.
    """
    changes = daily_changes(history)
    if len(changes) < 4:
        return None, {}, 0

    by_key: dict[str, set[date]] = {}
    for d, key in events:
        if key:
            by_key.setdefault(key, set()).add(d)

    loud = set().union(*by_key.values()) if by_key else set()
    excluded = exclude_dates or set()
    quiet_all = set(changes) - loud - excluded
    if len(quiet_all) < 2:
        return None, {}, 0

    cutoff = max(changes) - timedelta(days=level_window_days)
    quiet_recent = {d for d in quiet_all if d >= cutoff}
    # Fall back to the full quiet set only if the recent window is too thin to
    # measure -- better a stale level than no simulation at all.
    level_pool = quiet_recent if len(quiet_recent) >= MIN_OBSERVATIONS else quiet_all

    quiet_sigma = float(np.sqrt(np.mean([changes[d] ** 2 for d in level_pool])))
    if quiet_sigma <= 0:
        return None, {}, 0

    mults = {k: estimate(changes, dates, quiet_all, k) for k, dates in by_key.items()}
    return quiet_sigma, mults, len(level_pool)


@dataclass(frozen=True)
class VolSchedule:
    """The forward per-day sigma the simulation walks, plus why it looks so."""
    sigmas: list[float]                 # one per trading day, in bp of the EVENT
    days: list[date]
    quiet_sigma: float
    multipliers: dict[str, Multiplier]
    loud_days: int

    @property
    def total_vol(self) -> float:
        return float(np.sqrt(np.sum(np.square(self.sigmas)))) if self.sigmas else 0.0

    @property
    def flat_equivalent(self) -> float:
        """The single sigma that would produce the same total variance --
        what the old flat model would have had to use to agree."""
        n = len(self.sigmas)
        return self.total_vol / math.sqrt(n) if n else 0.0


def build_schedule(days: list[date], quiet_sigma: float,
                   day_events: dict[date, list[str]],
                   multipliers: dict[str, Multiplier]) -> VolSchedule:
    """Per-day sigma across `days`.

    Same-day releases compose by ADDING EXCESS VARIANCE, not by multiplying:
    a day carrying both CPI and claims has variance
    `(1 + (k_cpi - 1) + (k_claims - 1)) * quiet_var`. Multiplying would treat
    two independent shocks as compounding, which overstates a day that happens
    to be busy -- and busy days are common (NFP and ISM Services share a date
    whenever the month opens on a Friday).
    """
    sigmas: list[float] = []
    loud = 0
    for d in days:
        excess = 0.0
        for name in day_events.get(d, ()):
            m = multipliers.get(name)
            if m is not None:
                excess += m.applied - 1.0
        k = min(1.0 + max(0.0, excess), MAX_MULTIPLIER)
        if k > 1.0:
            loud += 1
        sigmas.append(quiet_sigma * math.sqrt(k))
    return VolSchedule(sigmas=sigmas, days=list(days), quiet_sigma=quiet_sigma,
                       multipliers=dict(multipliers), loud_days=loud)
