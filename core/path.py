"""Step 6 -- path risk.

EV tells you the terminal payoff. It says nothing about the days between now and
the meeting. Three pieces here:

1. The MTM ladder -- convert any contract level back to a probability before
   judging whether the path is plausible. Same `/ size` as Step 1.
2. The exit map -- the four levels you actually leave the trade at, each in
   probability, basis points, contract price and dollars.
3. The stop-cost decomposition -- when your win rate is 90%, false triggers
   dominate the arithmetic and a stop can cost a third of the edge.

WHY THE EXIT MAP HAS TWO TIERS PER SIDE
---------------------------------------
An event trade has two structurally different kinds of exit, and conflating
them is how a "stop" ends up being quoted at a level the contract can never
reach:

  * A MARKET BOUND is not a choice. Fading the event, the contract runs out of
    room at 0bp priced (nothing priced at all -- your maximum gain) and at
    `size` priced (fully priced -- your maximum loss). Backing it, the two
    swap. These bound the trade whether or not you place an order.
  * A REASSESS level IS a choice: the level at which you would re-examine the
    thesis rather than ride to the bound. It sits strictly inside the bound.

The decomposition below treats the two the same way. The market bounds are
already the terminal outcomes of the "rode to expiry" and "gapped through"
paths, so they need no separate counts. The reassess levels are barriers the
path can touch early, so each carries its own path counts -- and each gets its
effect on EV attributed separately, because a stop and a target do opposite
things to the same distribution.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from .pricing import Direction, Side, expected_value, implied_probability

LADDER_STEPS = 5


# --------------------------------------------------------------------------
# The MTM ladder
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LadderRow:
    level: float
    implied: float
    mtm: float
    is_entry: bool


def mtm_at(points: float, level: float, side: Side = "fade") -> float:
    """Mark-to-market if the contract reprices from `points` to `level`."""
    return (points - level) if side == "fade" else (level - points)


def default_ladder_levels(size: float, steps: int = LADDER_STEPS) -> list[float]:
    """Evenly spaced levels spanning the trade's own full range, from 0 (no
    move priced at all) through `size` (the move fully priced) -- scaled to
    THIS trade rather than a fixed set of bp values that only made sense for
    one specific meeting's size."""
    if size <= 0 or steps <= 0:
        return [0.0]
    step = size / steps
    return [round(step * i, 4) for i in range(steps + 1)]


def ladder(
    points: float,
    size: float,
    levels: Sequence[float] | None = None,
    side: Side = "fade",
) -> list[LadderRow]:
    lv = list(levels) if levels is not None else default_ladder_levels(size)
    if not any(abs(x - points) < 1e-9 for x in lv):
        lv.append(points)
    lv = sorted(set(round(x, 10) for x in lv))
    return [
        LadderRow(x, implied_probability(x, size), mtm_at(points, x, side), abs(x - points) < 1e-9)
        for x in lv
    ]


# --------------------------------------------------------------------------
# The exit map
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitLevel:
    role: str                   # "target" | "entry" | "stop"
    structural: bool            # True = a market bound, not a level you chose
    label: str
    level: float                # bp of the move priced at this exit
    implied: float
    mtm: float                  # bp P&L if you leave here
    price: float | None         # contract price, when a current price is known
    pnl: float | None           # dollars, when the position DV01 is known
    misplaced: bool = False     # sits somewhere it cannot do its job

    @property
    def is_entry(self) -> bool:
        return self.role == "entry"


def _misplaced(role: str, level: float, mtm: float, size: float) -> bool:
    """A reassess level that cannot do the job its name claims.

    Two ways to get this wrong, and cloning a meeting forward causes both: a
    level carried over from a trade with a different entry can end up on the
    WRONG SIDE of this one -- a "stop" that books a profit, a "target" that
    books a loss -- and a level can sit OUTSIDE what the contract can price at
    all. Either way the decomposition downstream keeps computing happily on a
    number that means nothing, so it is flagged at source.
    """
    if role == "entry":
        return False
    if not (0.0 - 1e-9 <= level <= size + 1e-9):
        return True
    return mtm <= 0.0 if role == "target" else mtm >= 0.0


def bound_levels(size: float, side: Side = "fade") -> tuple[float, float]:
    """`(target bound, stop bound)` in bp priced -- see the module docstring."""
    return (0.0, size) if side == "fade" else (size, 0.0)


def reassess_defaults(points: float, size: float,
                      side: Side = "fade") -> tuple[float, float]:
    """Midway between entry and each bound: a starting point to edit, not a
    recommendation. Halfway is the only split that presumes nothing about the
    path -- any other default would be smuggling in a volatility view."""
    target_bound, stop_bound = bound_levels(size, side)
    return (points + target_bound) / 2.0, (points + stop_bound) / 2.0


def exit_map(
    points: float,
    size: float,
    side: Side = "fade",
    direction: Direction = "hike",
    reassess_target: float | None = None,
    reassess_stop: float | None = None,
    position_dv01: float | None = None,
    current_price: float | None = None,
    capture: float = 1.0,
    hard_stop: float | None = None,
    partial_target: float | None = None,
) -> list[ExitLevel]:
    """The four exits plus entry, best outcome first.

    `current_price` is the contract's price NOW, at `points` priced, so every
    other level prices off the mark you can actually see on the screen.
    `position_dv01` is dollars per basis point OF THE EVENT for the size you
    are actually running (`sizing.Rung.position_dv01`), which is what turns
    the bp column into the dollar column.

    `capture` converts event basis points into contract price basis points
    (see `core.contracts`). One bp of the event moves the contract by
    `capture` bp, so the exit price is `current + capture*price_delta/100`.
    At full capture -- the contract you should normally be in -- that is the
    plain `price_delta/100` it has always been; at 47% capture the contract
    simply does not travel as far, and quoting the untouched price would have
    you working an order at a level the contract cannot reach for that
    outcome.

    `price_delta` is NOT `mtm` (`side`'s P&L is direction-agnostic per
    `core.pricing`'s own docstring -- direction only changes the sign of the
    STRIP mapping, never the payoff). A contract is quoted `100 - yield`, so
    more of the event priced (`level` rising toward `size`) pushes the price
    DOWN when the event is a hike and UP when it is a cut, regardless of
    which side of the trade you are on. That is exactly the shape of
    `mtm_at(..., "fade")` for a hike and `mtm_at(..., "back")` for a cut, so
    `price_delta` is computed against `direction`, not `side` -- conflating
    the two priced every fade-a-cut or back-a-hike exit on the wrong side of
    the current market price.

    `hard_stop` (optional) is a non-negotiable RISK floor beyond the reassess
    stop -- the worst case if you hold past the thesis decision or the order
    slips. It is a discipline line, not a market-model barrier: the Monte
    Carlo still exits at the reassess stop. It is flagged as misplaced unless
    it sits strictly beyond the reassess stop toward the stop bound (worse),
    because a "harder" stop that is actually closer to entry does the stop's
    job less well than the reassess level already does.
    """
    target_bound, stop_bound = bound_levels(size, side)
    default_target, default_stop = reassess_defaults(points, size, side)
    rt = default_target if reassess_target is None else reassess_target
    rs = default_stop if reassess_stop is None else reassess_stop
    price_side: Side = "fade" if direction == "hike" else "back"

    def row(role: str, structural: bool, label: str, level: float) -> ExitLevel:
        mtm = mtm_at(points, level, side)
        price_delta = mtm_at(points, level, price_side)
        return ExitLevel(
            role=role, structural=structural, label=label, level=level,
            implied=implied_probability(level, size), mtm=mtm,
            price=(None if current_price is None
                   else current_price + capture * price_delta / 100.0),
            pnl=None if position_dv01 is None else mtm * position_dv01,
            # A bound is defined by the contract, so it is never "misplaced";
            # only the levels someone chose can be.
            misplaced=False if structural else _misplaced(role, level, mtm, size),
        )

    rows = [
        row("target", True, "Target -- market bound", target_bound),
        row("target", False, "Target -- reassess", rt),
    ]
    if partial_target is not None:
        pt = row("target", False, "Target -- partial", partial_target)
        # A partial must sit between entry and the full target on the target
        # side (fade: target < partial < entry; back: entry < partial < target);
        # anywhere else it is a scale-out that never fires before the target.
        between = ((rt > points and points < partial_target < rt)
                   or (rt < points and rt < partial_target < points))
        if not between:
            pt = replace(pt, misplaced=True)
        rows.append(pt)
    rows += [
        row("entry", False, "Entry", points),
        row("stop", False, "Stop -- reassess", rs),
    ]
    if hard_stop is not None:
        hf = row("stop", False, "Stop -- hard floor", hard_stop)
        # A hard floor must be strictly BEYOND the reassess stop, toward the
        # stop bound (fade: higher; back: lower). Anywhere else it is either
        # the same decision twice or a "harder" line that is actually softer.
        beyond = (hard_stop > rs) if side == "fade" else (hard_stop < rs)
        if not beyond:
            hf = replace(hf, misplaced=True)
        rows.append(hf)
    rows.append(row("stop", True, "Stop -- market bound", stop_bound))
    return rows


# --------------------------------------------------------------------------
# The stop-cost decomposition
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StopPath:
    label: str
    count: float
    pnl_each: float

    @property
    def total(self) -> float:
        return self.count * self.pnl_each


@dataclass(frozen=True)
class StopAnalysis:
    stop_level: float
    stop_mtm: float
    paths: list[StopPath]
    trials: float                # the STATED convention -- "out of `trials`"
    counts_total: float          # what the six counts you entered actually sum to
    ev_with_stop: float
    ev_without_stop: float
    cost_of_stop: float          # the WHOLE exit policy, stop and target together
    cost_as_share_of_edge: float
    counts_consistent: bool
    counts_sum_correctly: bool   # counts_total == trials
    expected_no_move_count: float
    expected_move_count: float
    actual_no_move_count: float
    actual_move_count: float
    target_level: float | None = None
    target_mtm: float | None = None
    stop_effect: float = 0.0     # bp of EV attributable to the stop alone
    target_effect: float = 0.0   # bp of EV attributable to the target alone
    partial_target_level: float | None = None
    partial_share: float = 0.5

    @property
    def has_target(self) -> bool:
        return self.target_level is not None

    @property
    def implied_p_move(self) -> float:
        """What your own counts say P(move) is, as a fraction of what you
        ACTUALLY entered -- not `actual_move_count / trials`, which is only
        meaningful once `counts_total` really is `trials`."""
        return self.actual_move_count / self.counts_total if self.counts_total else 0.0


def stop_analysis(
    points: float,
    size: float,
    q: float,
    stop_level: float,
    no_move_never_breached: float,
    no_move_falsely_stopped: float,
    move_stopped_early: float,
    move_gapped_through: float,
    side: Side = "fade",
    trials: float = 100.0,
    target_level: float | None = None,
    no_move_target_hit: float = 0.0,
    move_target_hit: float = 0.0,
    partial_target_level: float | None = None,
    partial_share: float = 0.5,
    partial_reach_full: float = 1.0,
) -> StopAnalysis:
    """Per-`trials` decomposition of running the trade with a stop and,
    optionally, a take-profit as a competing barrier.

    Counts are user-supplied rather than derived, because the split between
    "never breached" and "falsely stopped" is a judgement about path volatility
    that no formula supplies. They are checked against q instead of rescaled --
    a silent rescale would hide a mis-specified path assumption.

    With a target set, each trial is classified by which barrier the path
    reaches FIRST, so the buckets stay mutually exclusive: a trial that takes
    profit never also appears as stopped out. Both target buckets are split by
    the terminal event, because q describes what the Fed did, not where you
    got out -- taking profit at a low implied probability and then watching
    the Fed move anyway is a real path, and folding it into the no-move
    population would quietly overstate how often the target is free.

    `stop_effect` and `target_effect` attribute the EV change to each barrier
    separately: every path that exits early is compared with what that same
    path would have earned riding to expiry. They sum to `cost_of_stop` (the
    whole policy) exactly when the counts reconcile with q.

    A partial take-profit scale-out (`partial_target_level` + `partial_share`)
    blends the target payoff: bank `partial_share` of the position when the
    partial level is touched; the remainder runs to the full target on the
    `partial_reach_full` share of target-touches that got there, or rides to
    the event on the rest. With no partial set the blend collapses to the
    plain full-target payoff, so behaviour is unchanged.
    """
    stop_mtm = mtm_at(points, stop_level, side)
    full_mtm = None if target_level is None else mtm_at(points, target_level, side)
    partial_mtm = (None if partial_target_level is None
                   else mtm_at(points, partial_target_level, side))
    # Terminal payoffs per side. For a FADE the event happening is the loss and
    # the no-move is the win; for a BACK it is the other way round, so a bare
    # `max_loss` would pay a back trade's WINNING "move" bucket a loss. The
    # q=0 / q=1 collapsed-form EVs give the correct signed payoff for both
    # sides.
    no_move_pnl = expected_value(points, size, 0.0, side)   # event does not happen
    move_pnl = expected_value(points, size, 1.0, side)      # event happens, no stop

    # Blended target payoff per stratum when a partial scale-out is set:
    # bank `share` at the partial; the rest runs to the full target (on the
    # share of paths that reached it) or rides to the event.
    no_move_target_mtm = full_mtm
    move_target_mtm = full_mtm
    if partial_mtm is not None and full_mtm is not None:
        banked = partial_share * partial_mtm
        no_move_target_mtm = banked + (1.0 - partial_share) * (
            partial_reach_full * full_mtm + (1.0 - partial_reach_full) * no_move_pnl)
        move_target_mtm = banked + (1.0 - partial_share) * (
            partial_reach_full * full_mtm + (1.0 - partial_reach_full) * move_pnl)

    paths: list[StopPath] = []
    if full_mtm is not None:
        paths.append(StopPath("No move, target hit", no_move_target_hit,
                              no_move_target_mtm))
    paths += [
        # "rode to expiry" rather than "never breached": with two barriers in
        # play, "never breached" no longer says which one.
        StopPath("No move, rode to expiry", no_move_never_breached, no_move_pnl),
        StopPath("No move, falsely stopped out", no_move_falsely_stopped, stop_mtm),
    ]
    if full_mtm is not None:
        paths.append(StopPath("Move, target hit first", move_target_hit,
                              move_target_mtm))
    paths += [
        StopPath("Move, stopped out early", move_stopped_early, stop_mtm),
        StopPath("Move, gapped through", move_gapped_through, move_pnl),
    ]

    # Normalised by what you ACTUALLY entered, not by `trials` -- six counts
    # that sum to 139 are still a valid weighted average of outcomes, just
    # not the "out of 100" the labels promise. Dividing by a fixed 100
    # regardless of the real total silently inflates or deflates EV by
    # however far off that total is; see `counts_sum_correctly` below for
    # the separate, honest flag on the total itself.
    counts_total = sum(p.count for p in paths)
    total = sum(p.total for p in paths)
    ev_with = total / counts_total if counts_total else 0.0
    ev_without = expected_value(points, size, q, side)
    cost = ev_with - ev_without

    # Each early exit against what that same path would have made riding on:
    # a no-move path forgoes `no_move_pnl`, a move path avoids `move_pnl`.
    per_trial = (lambda x: x / counts_total) if counts_total else (lambda x: 0.0)
    stop_effect = per_trial(
        no_move_falsely_stopped * (stop_mtm - no_move_pnl)
        + move_stopped_early * (stop_mtm - move_pnl)
    )
    target_effect = 0.0 if full_mtm is None else per_trial(
        no_move_target_hit * (no_move_target_mtm - no_move_pnl)
        + move_target_hit * (move_target_mtm - move_pnl)
    )

    expected_no_move = trials * (1.0 - q)
    expected_move = trials * q
    actual_no_move = (no_move_never_breached + no_move_falsely_stopped
                      + (no_move_target_hit if full_mtm is not None else 0.0))
    actual_move = (move_stopped_early + move_gapped_through
                   + (move_target_hit if full_mtm is not None else 0.0))
    # Two DIFFERENT ways six counts can go wrong, checked separately rather
    # than folded into one number: they can fail to add up to `trials` at
    # all, or they can add up fine but split no-move/move in a ratio q
    # doesn't predict. Comparing raw counts against `trials`-scaled targets
    # catches both at once, but only because it happens to combine them --
    # `counts_sum_correctly` names the first failure on its own so the
    # warning built from this doesn't blame q for what is really a total
    # that isn't 100.
    sums_correctly = abs(counts_total - trials) < 1e-6
    # Integer counts out of `trials` can only represent q to within half a
    # trial (0.5pp at trials=100), so the q-split check tolerates that
    # quantization. A stricter check would flag a properly rounded 59/41
    # split against q = 59.4% as a mismatch -- which is not what this warning
    # is for; it exists to catch counts that disagree with q by whole trials.
    # `sums_correctly` above stays strict: a total that isn't `trials` is a
    # real failure no matter how close.
    q_tol = 0.5 + 1e-6
    consistent = (
        sums_correctly
        and abs(actual_no_move - expected_no_move) < q_tol
        and abs(actual_move - expected_move) < q_tol
    )

    share = (abs(cost) / abs(ev_without)) if ev_without else 0.0

    return StopAnalysis(
        stop_level=stop_level,
        stop_mtm=stop_mtm,
        paths=paths,
        trials=trials,
        counts_total=counts_total,
        ev_with_stop=ev_with,
        ev_without_stop=ev_without,
        cost_of_stop=cost,
        cost_as_share_of_edge=share,
        counts_consistent=consistent,
        counts_sum_correctly=sums_correctly,
        expected_no_move_count=expected_no_move,
        expected_move_count=expected_move,
        actual_no_move_count=actual_no_move,
        actual_move_count=actual_move,
        target_level=target_level,
        target_mtm=full_mtm,
        stop_effect=stop_effect,
        target_effect=target_effect,
        partial_target_level=partial_target_level,
        partial_share=partial_share,
    )


def default_stop_counts(q: float, trials: float = 100.0, false_stop_share: float = 0.2222,
                        gap_share: float = 0.2, target_share: float = 0.0,
                        move_target_share: float = 0.0) -> dict[str, float]:
    """Counts consistent with q by construction, used to seed a new meeting.

    `false_stop_share` is the fraction of no-move paths that breach the stop
    anyway; `gap_share` the fraction of move paths that gap through it. The
    July defaults (20/90 and 2/10) reproduce the doc's table.

    `target_share` and `move_target_share` carve the take-profit buckets out
    of the same populations, so the totals still reconcile with q. Both
    default to zero: how often a path reaches the target before expiry is a
    volatility judgement, and seeding a non-zero guess would put EV on the
    screen that the user never actually asserted.
    """
    no_move = trials * (1.0 - q)
    move = trials * q
    falsely = round(no_move * false_stop_share)
    gapped = round(move * gap_share)
    target_no_move = round(no_move * target_share)
    target_move = round(move * move_target_share)
    return {
        "no_move_target_hit": target_no_move,
        "no_move_never_breached": no_move - falsely - target_no_move,
        "no_move_falsely_stopped": falsely,
        "move_target_hit": target_move,
        "move_stopped_early": move - gapped - target_move,
        "move_gapped_through": gapped,
    }
