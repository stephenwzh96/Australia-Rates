"""Step 6 -- Monte Carlo path simulation for the exit-count decomposition.

`path.default_stop_counts` seeds the six-bucket split with two fixed ratios
lifted from the July worked example (20/90 falsely stopped, 2/10 gapped
through). That is a starting point to edit, not a calibration -- it has
nothing to do with THIS trade's volatility, THIS barrier's distance from
entry, or how many trading days are actually left. This module replaces the
guess with a simulation, calibrated to the contract's own realised price
behaviour wherever that data is available.

THE MODEL
---------
Two pieces, matching the trade's actual structure:

  1. PRE-ANNOUNCEMENT DIFFUSION. Between now and the decision, the priced
     level (bp) does a driftless random walk -- daily steps drawn from
     N(0, daily_vol_bp). Driftless is the load-bearing assumption: if the
     market expected to walk toward one outcome, that expectation would
     already be priced into `points` today (no free lunch from a forecastable
     drift). Data surprises are what actually move the level day to day, and
     under this assumption they average out to noise rather than a signal
     about which way the decision breaks -- a simplification, stated plainly
     rather than hidden in a black box.
  2. THE DECISION JUMP. At the meeting, the outcome resolves discontinuously:
     with probability q, the level jumps straight to the "fully priced" bound;
     with probability 1-q, it jumps to "nothing priced". This is what actually
     produces a GAP through the stop rather than a walk into it -- most FOMC
     surprises are not a slow drift, they are a single 2pm repricing.

Because the diffusion phase carries no information about which way the jump
will go (that is the whole content of "driftless"), the probability of
touching a barrier during the diffusion phase does not depend on the eventual
outcome. That lets the two phases be simulated separately and combined
EXACTLY: draw one large, low-noise Monte Carlo batch of diffusion-only paths
to estimate the touch probabilities, then apply those same probabilities to a
100-trial population split deterministically by q (`round(100*(1-q))` no-move,
the rest move) -- so the six counts always reconcile with q exactly, the same
guarantee `default_stop_counts` gives, but now the SPLITS within each stratum
come from simulation, not a hardcoded ratio.

CALIBRATION
-----------
`daily_vol_bp` should come from `realized_daily_vol_bp` on the actual sizing
contract's own recent daily closes (`data.asx.fetch_history`) -- this module
never fetches data itself, matching how `core/` stays decoupled from `data/`
everywhere else in this app (see `core.econ_calendar`'s docstring for the same
rule). No history, no default: the caller must supply a value or skip this
entirely and fall back to the ratio-based seed.

CONVENTIONS (stated plainly, because they shape the numbers)
-----------------------------------------------------------
* q is held FIXED while the level wanders. A path drifting toward the stop
  bound is implicitly repricing q (level = size*q), yet the terminal jump is
  still drawn from the initial q. The touch probabilities are exact
  conditional on that initial q -- a standard static approximation, not a
  claim that q cannot move.
* Reassess levels act only during the diffusion phase. The decision jump is
  assumed NOT to fill a resting order: a move path "gaps through" the stop
  and eats the full max loss, and a no-move path rides past its target to
  the full max gain. If the trader rests orders through the event, both
  numbers change materially (a filled stop halves the loss; a filled target
  cuts the gain).
* `counts_from_shares` returns CONTINUOUS expected counts; the "out of 100"
  display layers round. EV derived from the counts is therefore the exact
  continuous EV, not a quantized integer approximation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from . import holidays

from . import path as pathmod
from .pricing import Side

TRIALS = 100                 # display convention -- matches default_stop_counts
DEFAULT_MC_TRIALS = 20_000   # diffusion-only batch size for the touch-probability estimate
DEFAULT_SAMPLE_PATHS = 120   # subset kept in full for the fan chart

_MONDAY, _SATURDAY = 0, 5


def trading_days_between(start: date, end: date) -> int:
    """Business days strictly between `start` and `end`, exclusive of both --
    the number of pre-announcement diffusion steps available.

    Holidays are excluded, not just weekends: a closed market cannot produce a
    day's move, and counting Labor Day as a diffusion step both inflates the
    total variance and slides every later day of the schedule one slot out of
    alignment with the calendar it was built from."""
    return len(trading_day_list(start, end))


def trading_day_list(start: date, end: date) -> list[date]:
    """The dates counted by `trading_days_between`, in order -- what the
    per-day volatility schedule is indexed against, so the calendar and the
    simulation cannot drift apart."""
    if end <= start:
        return []
    out, d = [], start + timedelta(days=1)
    while d < end:
        if holidays.is_business_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def realized_daily_vol_bp(history: list[tuple[date, float]],
                          exclude_dates: set[date] | None = None) -> float | None:
    """Sample stdev of day-over-day close changes, in bp (price x 100 -- one
    bp of rate is 0.01 of price on both ZQ and SR3, same convention as
    `path.exit_map`'s price column). None with fewer than 3 usable closes;
    there is no vol estimate to make from one or two points.

    `exclude_dates` drops any day-over-day diff LANDING on one of these dates
    -- meant for FOMC decision dates (and the following session, to catch a
    settlement lag). A 90-day lookback window almost always contains at least
    one other live decision, and its jump belongs to the JUMP phase this
    module simulates separately, not the pre-announcement drift being
    calibrated here: leaving it in can roughly triple the estimated diffusion
    vol from a single two-session move (confirmed against this contract's own
    June and July 2026 decision days, both ~9bp one/two-day moves against a
    ~2-3bp/day baseline in between).
    """
    closes = sorted(history)
    if len(closes) < 3:
        return None
    ex = exclude_dates or set()
    diffs = [(c1 - c0) * 100.0 for (_, c0), (d1, c1) in zip(closes, closes[1:])
             if d1 not in ex]
    if len(diffs) < 2:
        return None
    vol = float(np.std(np.array(diffs), ddof=1))
    return vol if vol > 0 else None


@dataclass(frozen=True)
class VolEstimate:
    daily_vol_bp: float            # the QUIET-day level, in bp of the event
    source: str                    # "historical" | "override"
    symbol: str | None = None
    observations: int = 0
    capture: float = 1.0           # contract capture used to rescale price->event bp
    schedule: tuple[float, ...] | None = None   # per-day sigma; None = flat

    @property
    def is_flat(self) -> bool:
        return not self.schedule

    def sigmas(self, trading_days: int) -> np.ndarray:
        """Per-day sigma vector of exactly `trading_days` length.

        A schedule shorter or longer than the window is padded with, or
        truncated to, the quiet level rather than being allowed to silently
        mis-align the calendar against the simulation.
        """
        n = max(0, trading_days)
        if self.is_flat:
            return np.full(n, self.daily_vol_bp, dtype=float)
        s = np.asarray(self.schedule, dtype=float)
        if s.size == n:
            return s
        out = np.full(n, self.daily_vol_bp, dtype=float)
        out[:min(n, s.size)] = s[:min(n, s.size)]
        return out


def _simulate_diffusion(points: float, vol: "VolEstimate | float",
                        trading_days: int, mc_trials: int, seed: int,
                        vol_uncertainty: bool = False) -> np.ndarray:
    """Shared driftless random walk -- independent of any barrier and of the
    eventual outcome, so it is simulated once and reused both for the
    touch-probability estimate and for a stop/target sensitivity sweep rather
    than redrawn per candidate level.

    Volatility may vary day to day (see `core.volatility`): the step for day
    `t` is drawn at that day's own sigma. A bare float is still accepted and
    means a flat schedule, which is what every caller predating the event
    calendar passes.

    `vol_uncertainty` is OFF by default. When on, each path additionally draws
    its own scale from the sampling distribution of the variance estimate --
    (n-1)s^2/sigma^2 ~ chi-squared(n-1) -- which turns "vol IS 2.36" into "vol
    is 2.36 give or take", fattening the tails of total variance. It is
    deliberately opt-in: at n~500 the effect is about 3%, and a default that
    quietly randomises results would cost more in reproducibility than it buys
    in realism.
    """
    rng = np.random.default_rng(seed)
    days = max(0, trading_days)
    est = vol if isinstance(vol, VolEstimate) else VolEstimate(float(vol), "override")
    sigma = est.sigmas(days)

    steps = rng.standard_normal(size=(mc_trials, days)) * sigma
    if vol_uncertainty and est.observations > 2 and days:
        dof = est.observations - 1
        scale = np.sqrt(dof / rng.chisquare(dof, size=(mc_trials, 1)))
        steps *= scale

    path = points + np.cumsum(steps, axis=1)
    return np.concatenate([np.full((mc_trials, 1), points), path], axis=1)


def _first_touch(path: np.ndarray, entry: float, level: float) -> np.ndarray:
    """First column index (0 = entry) each row reaches `level`, approaching
    from `entry`'s side. `path.shape[1]` (one past the last real column) is
    the sentinel for "never, within this window"."""
    hit = path >= level if level >= entry else path <= level
    reached = hit.any(axis=1)
    return np.where(reached, hit.argmax(axis=1), path.shape[1])


def touch_shares(path: np.ndarray, points: float, target_level: float,
                 stop_level: float,
                 partial_target: float | None = None,
                 ) -> tuple[float, float, float, float]:
    """(p_target_first, p_stop_first, p_neither, frac_reached_full) over a
    shared diffusion batch.

    A same-day double-crossing (both barriers reached within one discretised
    step) is broken toward whichever level sits closer to entry -- the barrier
    a smaller move would have reached first. Rare unless volatility is large
    relative to the distance between the two levels, and never material to
    the rounded, out-of-100 counts this feeds.

    `partial_target` (optional) moves the target barrier IN to the scale-out
    level: every path that reaches the full target must first pass the partial
    one, so the target barrier is the partial. It only counts when it sits
    strictly between entry and the full target on the target side (a partial
    beyond the target is meaningless and is ignored). `frac_reached_full` is
    the share of target-first paths that ALSO reached the full target (they
    bank the remainder there); the rest ride the remainder to the event. 1.0
    when no usable partial is set.
    """
    n = path.shape[0]
    sentinel = path.shape[1]
    # A partial target only makes sense between entry and the full target on
    # the target side: back (target above entry) -> entry < partial < target;
    # fade (target below entry) -> target < partial < entry.
    usable = (
        partial_target is not None
        and target_level != points
        and ((target_level > points and points < partial_target < target_level)
             or (target_level < points and target_level < partial_target < points))
    )
    barrier = partial_target if usable else target_level
    t_first = _first_touch(path, points, barrier)
    s_first = _first_touch(path, points, stop_level)
    tied = (t_first == s_first) & (t_first != sentinel)
    target_wins_tie = abs(barrier - points) <= abs(stop_level - points)
    target_first = (t_first < s_first) | (tied & target_wins_tie)
    stop_first = (s_first < t_first) | (tied & (not target_wins_tie))
    p_target = float(target_first.sum()) / n
    p_stop = float(stop_first.sum()) / n

    frac_full = 1.0
    if usable:
        if target_level >= points:
            reached = path.max(axis=1) >= target_level
        else:
            reached = path.min(axis=1) <= target_level
        hit = int(target_first.sum())
        frac_full = float(reached[target_first].sum()) / max(1, hit)
    return p_target, p_stop, max(0.0, 1.0 - p_target - p_stop), frac_full


def counts_from_shares(q: float, p_target: float, p_stop: float,
                       trials: int = TRIALS) -> dict[str, float]:
    """Expected six-bucket counts from the continuous touch shares.

    Returns CONTINUOUS expected counts, not rounded integers. The q-split and
    each stratum's sub-split are exact products, so EV computed from these
    counts equals the continuous EV -- the integer rounding that used to bias
    EV by up to one trial's P&L (and, with the reassess levels at the market
    bounds, report a spurious "cost of stops") is gone. Display layers round
    for the "out of 100" convention; `p_target + p_stop > 1` (defensive, an
    invalid input `touch_shares` never produces) is clamped so no bucket can
    go negative.
    """
    no_move = trials * (1.0 - q)
    move = trials - no_move
    no_move_target = no_move * p_target
    no_move_stop = no_move * p_stop
    move_target = move * p_target
    move_stop = move * p_stop
    return {
        "no_move_target_hit": no_move_target,
        "no_move_falsely_stopped": no_move_stop,
        "no_move_never_breached": max(0.0, no_move - no_move_target - no_move_stop),
        "move_target_hit": move_target,
        "move_stopped_early": move_stop,
        "move_gapped_through": max(0.0, move - move_target - move_stop),
    }


@dataclass(frozen=True)
class SamplePath:
    """One illustrative trial, full trajectory, for the fan chart."""
    levels: list[float]      # entry through exit (or the full window)
    exit_reason: str         # "target" | "stop" | "rode_out"
    terminal_move: bool
    terminal_level: float    # where the path actually ends, incl. the jump


@dataclass(frozen=True)
class MonteCarloResult:
    vol: VolEstimate
    trading_days: int
    mc_trials: int
    target_level: float
    stop_level: float
    p_target_first: float
    p_stop_first: float
    p_rode_out: float
    counts: dict[str, float]
    sample_paths: list[SamplePath] = field(default_factory=list)
    # The actual dates the diffusion steps land on, so a chart can put the
    # calendar on its x-axis instead of an index nobody can act on.
    days: tuple[date, ...] = ()
    # Partial take-profit scale-out (optional): bank `partial_share` when the
    # level hits `partial_target_level`; the rest runs to the full target.
    partial_target_level: float | None = None
    partial_share: float = 0.5
    frac_reached_full: float = 1.0   # share of target-touches that also reached the full target


def simulate_exit_paths(
    points: float, size: float, q: float,
    target_level: float, stop_level: float, vol: VolEstimate,
    trading_days: int, mc_trials: int = DEFAULT_MC_TRIALS,
    n_sample_paths: int = DEFAULT_SAMPLE_PATHS, seed: int = 0,
    vol_uncertainty: bool = False, days: list[date] | None = None,
    partial_target_level: float | None = None, partial_share: float = 0.5,
) -> MonteCarloResult:
    """`size` only sets where the terminal jump lands (0 or `size`) for the
    sample paths drawn for the fan chart -- the touch-probability estimate
    that drives `counts` needs only `points` and the two barrier levels.
    "Side" does not enter: level is how much of the event the market has
    priced in, a property of the market, not of which side is being traded.

    `partial_target_level` (optional) enables the scale-out: the level is
    banked at `partial_share` of the position when the partial target is
    touched; the remainder runs to the full target (or the event). The target
    barrier in the simulation moves in to the partial level.
    """
    # 0 is "blank", not a level -- see core.model.build. A scale-out at 0bp
    # marks the entry (zero MTM), so a stored or typed 0 must not move the
    # target barrier down to entry.
    partial_target_level = partial_target_level or None
    path = _simulate_diffusion(points, vol, trading_days, mc_trials, seed,
                               vol_uncertainty)
    p_target, p_stop, p_neither, frac_full = touch_shares(
        path, points, target_level, stop_level, partial_target_level)
    counts = counts_from_shares(q, p_target, p_stop)

    rng = np.random.default_rng(seed + 1)
    n_sample = min(n_sample_paths, mc_trials)
    idx = rng.choice(mc_trials, size=n_sample, replace=False)
    terminal_moves = rng.random(n_sample) < q

    t_first = _first_touch(path[idx], points,
                           partial_target_level if partial_target_level is not None
                           else target_level)
    s_first = _first_touch(path[idx], points, stop_level)
    sentinel = path.shape[1]
    # Same rule as `touch_shares`: a same-step double-crossing goes to the
    # barrier closer to entry, so the fan chart and the counts tell the same
    # story. The target barrier moves in to the partial level when set.
    barrier = partial_target_level if partial_target_level is not None else target_level
    target_wins_tie = abs(barrier - points) <= abs(stop_level - points)

    samples: list[SamplePath] = []
    for i in range(n_sample):
        row = path[idx[i]]
        tf, sf = int(t_first[i]), int(s_first[i])
        if tf == sentinel and sf == sentinel:
            exit_day, reason = len(row) - 1, "rode_out"
        elif tf < sf or (tf == sf and target_wins_tie):
            exit_day, reason = tf, "target"
        else:
            exit_day, reason = sf, "stop"
        levels = list(row[:exit_day + 1])
        move = bool(terminal_moves[i])
        if reason == "rode_out":
            # "Level" is how much of the event is priced in, a property of
            # the market, not the trader's side -- it converges to `size`
            # (fully priced) if the move happens and to 0 (nothing priced)
            # if it doesn't, regardless of which side is being traded.
            terminal_level = size if move else 0.0
        else:
            terminal_level = levels[-1]
        samples.append(SamplePath(levels=levels, exit_reason=reason,
                                  terminal_move=move, terminal_level=terminal_level))

    return MonteCarloResult(
        vol=vol, trading_days=trading_days, mc_trials=mc_trials,
        target_level=target_level, stop_level=stop_level,
        p_target_first=p_target, p_stop_first=p_stop, p_rode_out=p_neither,
        counts=counts, sample_paths=samples, days=tuple(days or ()),
        partial_target_level=partial_target_level, partial_share=partial_share,
        frac_reached_full=frac_full,
    )


@dataclass(frozen=True)
class SweepPoint:
    level: float               # the candidate level being swept
    p_hit_first: float         # probability THIS level is touched before the other
    counts: dict[str, float]
    ev_with_exits: float
    cost_of_exits: float


DEFAULT_SWEEP_SEEDS = 5      # independent batches averaged per sweep chart
DEFAULT_SWEEP_LEVELS = 30    # candidate levels plotted per sweep chart

def _sweep_once(
    points: float, size: float, side: Side, q: float, vol: VolEstimate,
    trading_days: int, fixed_target: float | None, fixed_stop: float | None,
    sweep_levels: list[float], mc_trials: int, seed: int,
    partial_target: float | None = None, partial_share: float = 0.5,
) -> list[SweepPoint]:
    """One diffusion batch, classified at every candidate level -- exactly
    one of `fixed_target` / `fixed_stop` is None, and that is the axis being
    swept. Cheap regardless of how many levels are requested: the diffusion
    does not depend on where either barrier sits, only the classification of
    already-simulated paths does."""
    diffusion = _simulate_diffusion(points, vol, trading_days, mc_trials, seed)
    out = []
    for lvl in sweep_levels:
        target = lvl if fixed_target is None else fixed_target
        stop = lvl if fixed_stop is None else fixed_stop
        p_target, p_stop, _, frac = touch_shares(
            diffusion, points, target, stop, partial_target)
        counts = counts_from_shares(q, p_target, p_stop)
        sa = pathmod.stop_analysis(
            points, size, q, stop,
            counts["no_move_never_breached"], counts["no_move_falsely_stopped"],
            counts["move_stopped_early"], counts["move_gapped_through"], side,
            target_level=target, no_move_target_hit=counts["no_move_target_hit"],
            move_target_hit=counts["move_target_hit"],
            partial_target_level=partial_target, partial_share=partial_share,
            partial_reach_full=frac,
        )
        out.append(SweepPoint(
            level=lvl, p_hit_first=(p_target if fixed_target is None else p_stop),
            counts=counts, ev_with_exits=sa.ev_with_stop, cost_of_exits=sa.cost_of_stop,
        ))
    return out


def _sweep(
    points: float, size: float, side: Side, q: float, vol: VolEstimate,
    trading_days: int, fixed_target: float | None, fixed_stop: float | None,
    sweep_levels: list[float], mc_trials: int, seed: int, n_seeds: int = 1,
    partial_target: float | None = None, partial_share: float = 0.5,
) -> list[SweepPoint]:
    """`_sweep_once`, averaged over `n_seeds` independent batches.

    `counts_from_shares` now returns continuous expected counts, so EV is a
    smooth function of the candidate level -- the old integer rounding that
    made it a staircase is gone. What remains is Monte Carlo sampling noise:
    each candidate level's touch shares come from ONE shared diffusion batch,
    so neighbouring levels carry correlated but nonzero sampling error that
    can still create a false local wiggle in the EV curve. Averaging several
    independent batches (different seeds, same levels) cancels that noise;
    what survives the average is the real trend.

    `n_seeds=1` (the default) is exactly `_sweep_once` -- unchanged, so a
    single-batch call remains reproducible from its own seed alone.
    """
    if n_seeds <= 1:
        return _sweep_once(points, size, side, q, vol, trading_days,
                           fixed_target, fixed_stop, sweep_levels, mc_trials, seed,
                           partial_target, partial_share)

    runs = [_sweep_once(points, size, side, q, vol, trading_days, fixed_target,
                        fixed_stop, sweep_levels, mc_trials, seed + i,
                        partial_target, partial_share)
            for i in range(n_seeds)]
    out = []
    for j, lvl in enumerate(sweep_levels):
        pts = [run[j] for run in runs]
        avg_counts = {k: sum(p.counts[k] for p in pts) / len(pts) for k in pts[0].counts}
        out.append(SweepPoint(
            level=lvl,
            p_hit_first=sum(p.p_hit_first for p in pts) / len(pts),
            counts=avg_counts,
            ev_with_exits=sum(p.ev_with_exits for p in pts) / len(pts),
            cost_of_exits=sum(p.cost_of_exits for p in pts) / len(pts),
        ))
    return out


def stop_sweep(
    points: float, size: float, side: Side, q: float, target_level: float,
    stop_levels: list[float], vol: VolEstimate, trading_days: int,
    mc_trials: int = DEFAULT_MC_TRIALS, seed: int = 0, n_seeds: int = 1,
    partial_target_level: float | None = None, partial_share: float = 0.5,
) -> list[SweepPoint]:
    """EV as the stop moves, target held fixed at its current level."""
    return _sweep(points, size, side, q, vol, trading_days, target_level, None,
                 stop_levels, mc_trials, seed, n_seeds,
                 partial_target_level, partial_share)


def suggested_sweep_levels(entry: float, bound: float, n: int = 12) -> list[float]:
    """`n` evenly spaced candidate levels between `entry` (exclusive) and the
    market bound (inclusive) -- a sensible default range for the sensitivity
    chart. Excludes entry itself: zero distance from entry is not a
    meaningful barrier to sweep."""
    if n <= 0 or bound == entry:
        return [bound]
    step = (bound - entry) / n
    return [round(entry + step * i, 4) for i in range(1, n + 1)]


def target_sweep(
    points: float, size: float, side: Side, q: float, stop_level: float,
    target_levels: list[float], vol: VolEstimate, trading_days: int,
    mc_trials: int = DEFAULT_MC_TRIALS, seed: int = 0, n_seeds: int = 1,
    partial_target_level: float | None = None, partial_share: float = 0.5,
) -> list[SweepPoint]:
    """EV as the target moves, stop held fixed at its current level.

    With a partial scale-out set, the swept level is the FULL target; the
    partial stays fixed (and is ignored by the classification whenever a
    swept level sits at or inside it, since a partial beyond the target is
    meaningless)."""
    return _sweep(points, size, side, q, vol, trading_days, None, stop_level,
                 target_levels, mc_trials, seed, n_seeds,
                 partial_target_level, partial_share)
