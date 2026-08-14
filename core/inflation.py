"""CPI breadth, composition and cycle-sensitivity.

Five readings of the same quarterly CPI, each answering a question the headline
number cannot:

  * BREADTH -- what share of the basket is running hot? A 3% headline made of
    two extreme items is a different problem from one where two-thirds of the
    basket is above target, and only the second is a monetary problem.
  * COMPOSITION -- which parts of the basket are doing the work.
  * UNDERLYING -- trimmed mean and ex-volatiles, the measures the RBA targets.
  * CYCLE SENSITIVITY -- splitting the basket into the categories that respond
    to domestic slack and the ones that do not.

FINDING THE EXPENDITURE CLASSES WITHOUT A HARDCODED LIST
----------------------------------------------------------
The ABS publishes the CPI as a hierarchy -- All groups, then 11 groups, then
sub-groups, then ~80 expenditure classes -- and its workbooks flatten all four
levels into one ordered list with nothing marking the depth. Counting every
series would double-count: "Bread" would be counted once on its own and again
inside "Bread and cereal products".

The obvious fix is a hardcoded list of group and sub-group names to exclude.
That list would be wrong at the next ABS restructure, and the April 2026 Labour
Force renaming is a live reminder that they do restructure.

So the hierarchy is recovered from the data instead. Table 18 publishes a
`Contribution to Total CPI` for every series at every level, and a parent's
contribution is exactly the sum of its children's -- verified on the published
figures: Bread and cereal products 0.0336 = Bread 0.0154 + Cakes and biscuits
0.0115 + Breakfast cereals 0.0042 + Other cereal products 0.0025. Walking the
ordered list and marking any series that equals the sum of the run following it
recovers the tree with no external list at all, and it self-corrects when the
ABS adds or moves a class.

WHERE JUDGEMENT ENTERS, AND WHERE IT DOES NOT
-----------------------------------------------
Breadth and the underlying measures are arithmetic: a threshold, a count, a
weight. Nothing to argue with.

The composition buckets ("administered prices", "domestic market services") and
the cyclical/non-cyclical split are NOT published series -- they are analytical
classifications, and different houses draw them differently. They live in
`meetings/_cpi_classification.json` as editable data rather than in this file
as code, every chart that uses them says so on screen, and any class the file
does not mention falls into a residual bucket rather than being silently
dropped. A classification you cannot see is a classification you cannot check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

Series = Sequence[tuple[date, float]]

# Quarterly CPI: four periods a year.
PERIODS_PER_YEAR = 4

# The two thresholds the published breadth charts use. 3% is above the top of
# the RBA's 2-3% band; 2.5% is its midpoint, so the second answers "how much of
# the basket is above target" rather than "above the band".
ABOVE_BAND = 3.0
ABOVE_MIDPOINT = 2.5

# How far a parent may sit from the sum of its children before the two are
# treated as unrelated. Two terms, because two things push them apart.
#
# ABS contributions are published to two decimals, so each of a parent's n
# children can be off by half a cent and the parent by another: 0.005*(n+1).
# That bound alone is too tight, because the ABS does not compute a parent by
# adding its children -- it prices each level from weights independently. Across
# the published quarters the residual reaches 0.030 on Holiday travel and
# accommodation, twice what rounding can explain, and it scales with the
# parent. Hence the relative term, sized at roughly ten times the worst
# observed 0.5% so a reweighting quarter does not start dropping real parents.
_ROUNDING = 0.005
_RELATIVE = 0.004
_TREE_SLACK = 1e-9

# A parent needs at least two children. On the June 2026 basket every series
# whose immediate successor carries the same contribution -- Pork/Lamb 0.27,
# Cheese/Ice cream 0.33, Glassware/Tools 0.34, AV equipment/AV media 0.97 -- is
# a leaf sitting next to another leaf, and every genuine parent has two or more.
# The rule is not just empirical: a one-child aggregate is arithmetically
# indistinguishable from a leaf, since its contribution IS its child's. Nothing
# is lost by refusing to infer one, and accepting one lets a leaf swallow its
# sibling and shift every boundary after it.
_MIN_CHILDREN = 2


def _tol(n_children: int, target: float, extra: float = 0.0) -> float:
    return (_ROUNDING * (n_children + 1) + _RELATIVE * abs(target)
            + extra + _TREE_SLACK)


# --------------------------------------------------------------------------
# Hierarchy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Tree:
    """The CPI hierarchy recovered from additive contributions."""
    order: list[str]
    children: dict[str, list[str]]

    @property
    def leaves(self) -> list[str]:
        """Expenditure classes -- every series with no children of its own."""
        return [n for n in self.order if not self.children.get(n)]

    @property
    def aggregates(self) -> list[str]:
        return [n for n in self.order if self.children.get(n)]

    def depth_of(self, name: str) -> int:
        parent = {c: p for p, cs in self.children.items() for c in cs}
        d = 0
        while name in parent:
            name, d = parent[name], d + 1
        return d


def build_tree(order: Sequence[str], contributions: dict[str, Series],
               extra_tol: float = 0.0) -> Tree:
    """Recover the CPI hierarchy from published contributions.

    `order` is the series in workbook order, and the ABS lists the hierarchy
    DEPTH-FIRST: a parent, then that parent's entire subtree, then its next
    sibling. That ordering is what makes the tree recoverable, and it is also
    why a flat forward scan does not work -- summing forward from a parent picks
    up its grandchildren as well as its children and overshoots. An earlier
    version of this function did exactly that and reported Pork as an aggregate.

    Greedy forward descent does not work either, and the reason is worth
    stating because it is what the rules below exist to fix. Deciding each node
    by probing ahead lets Pork (0.27) claim Lamb (0.27) as its only child; the
    claim balances, so a local test accepts it, and Lamb then disappears from
    Meat and seafoods, whose remaining children no longer reach it, so it in
    turn is misread as a leaf. One coincidence four levels down corrupts every
    boundary above it.

    Reading the list BACKWARDS removes the guesswork. Keep a stack of subtree
    roots not yet claimed by a parent; at each series, its children can only be
    a prefix of that stack, because depth-first order puts a node's whole
    subtree immediately after it. Take the shortest prefix that sums to the
    node, require at least two, and pop them. Nothing is provisional -- by the
    time a node is reached everything below it is already resolved -- so a
    coincidence cannot propagate. Pork is offered only Lamb, one child, which
    the minimum forbids, and it stays a leaf where it belongs.

    The last defence is to test EVERY published quarter rather than the latest.
    A structure is a property of the classification, so a real parent balances
    in all of them, while a coincidence is a fact about one quarter's prices.
    March 2026 is the case that forced this: Automotive fuel came to 3.46 and
    Maintenance and repair 2.12 plus Other services 1.34 came to 3.46 as well,
    so a single-quarter parse promoted a leaf and lost Private motoring,
    Transport and All groups CPI above it. In December (3.29 against 3.43) and
    June (3.39 against 3.51) the two are nowhere near each other.
    """
    names = [n for n in order if contributions.get(n)]
    if not names:
        return Tree([], {})
    obs = {n: dict(contributions[n]) for n in names}

    children: dict[str, list[str]] = {}
    stack: list[str] = []                 # unclaimed roots, nearest first

    for name in reversed(names):
        parent = obs[name]
        take = 0
        acc: dict[date, float] = {}
        shared: set[date] = set(parent)
        for k in range(1, len(stack) + 1):
            kid = obs[stack[k - 1]]
            for d, v in kid.items():
                acc[d] = acc.get(d, 0.0) + v
            shared &= set(kid)
            if not shared:
                break                     # no quarter can compare them
            fits = [abs(acc[d] - parent[d]) <= _tol(k, parent[d], extra_tol)
                    for d in shared]
            if any(acc[d] > parent[d] + _tol(k, parent[d], extra_tol) for d in shared):
                break                     # overshot; no longer prefix can undo it
            if k >= _MIN_CHILDREN and all(fits):
                take = k
                break
        children[name] = stack[:take]
        stack = [name] + stack[take:]

    return Tree(names, children)


def latest_common(series: dict[str, Series]) -> date | None:
    """The most recent date every one of these series reaches."""
    live = [s for s in series.values() if s]
    if not live:
        return None
    return min(max(d for d, _ in s) for s in live)


def index_point_contributions(class_indexes: dict[str, Series],
                              contribution: dict[str, float], at: date,
                              ) -> dict[str, list[tuple[date, float]]]:
    """Extend three quarters of published contributions over the full history.

    The ABS publishes an index-point contribution only for the last three
    quarters, and the breadth charts need a weight for every quarter back to
    1990. The two are one step apart. A contribution is `weight * index`, and
    the All groups contribution equals the All groups index exactly -- checked,
    102.31 against 102.31 at June 2026 -- so dividing a class's contribution by
    its own index recovers the expenditure weight itself. Multiplying that
    weight back through the class's index gives its contribution at any date.

    What this is NOT is the contribution the ABS would have published in 2013.
    Expenditure weights are re-based every year and this holds the latest
    basket fixed, so the reconstruction is a fixed-weight Laspeyres: the shares
    still move through history, because a class whose price rose faster takes a
    larger share of the index, but they move only for that reason. That is the
    standard approximation and it is stated on the chart rather than buried
    here. The alternative -- dropping the weighted line, or drawing it over
    three quarters -- says less.
    """
    out: dict[str, list[tuple[date, float]]] = {}
    for name, obs in class_indexes.items():
        c = contribution.get(name)
        if c is None or not obs:
            continue
        rows = sorted(obs, key=lambda p: p[0])
        base = dict(rows).get(at)
        if not base or base <= 0:
            continue                      # `at` must be a quarter this class has
        weight = c / base
        out[name] = [(d, weight * v) for d, v in rows]
    return out


def reconciles(tree: Tree, contribution: dict[str, float]) -> float | None:
    """Leaf contributions minus the root's, in index points.

    The one number that says whether the hierarchy parsed. Every leaf is counted
    once and no aggregate is counted at all, so on a correct tree this is zero
    to within the ABS's own rounding. A non-zero residual means classes are
    being double-counted or dropped, and every chart built on the leaves is
    wrong by that much.
    """
    if not tree.order:
        return None
    root = tree.order[0]
    if root not in contribution:
        return None
    return sum(contribution[n] for n in tree.leaves) - contribution[root]


# --------------------------------------------------------------------------
# Rates of change
# --------------------------------------------------------------------------

def annualised(index: Series, periods: int = 1) -> list[tuple[date, float]]:
    """Quarterly change, annualised: `((I_t / I_t-n) ** (4/n) - 1) * 100`.

    Compounded rather than multiplied by four. Over one quarter at ordinary
    rates the two differ by only a few hundredths, but the breadth charts count
    items against a fixed threshold, so a systematic bias of the wrong sign
    would move items across the line and change the count.
    """
    rows = sorted(index, key=lambda p: p[0])
    out = []
    for a, b in zip(rows, rows[periods:]):
        if a[1] > 0:
            out.append((b[0], ((b[1] / a[1]) ** (PERIODS_PER_YEAR / periods) - 1) * 100.0))
    return out


def year_ended(index: Series) -> list[tuple[date, float]]:
    """Four-quarter change, in per cent."""
    rows = sorted(index, key=lambda p: p[0])
    return [(b[0], (b[1] / a[1] - 1) * 100.0)
            for a, b in zip(rows, rows[PERIODS_PER_YEAR:]) if a[1] > 0]


# --------------------------------------------------------------------------
# Breadth
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BreadthPoint:
    when: date
    share_by_count: float
    share_by_weight: float
    n_above: int
    n_total: int


def breadth(class_indexes: dict[str, Series], weights: dict[str, Series],
            threshold: float = ABOVE_BAND,
            periods: int = 1) -> list[BreadthPoint]:
    """Share of the basket inflating faster than `threshold`, two ways.

    By COUNT every class carries equal say, which is the honest measure of how
    widespread a price rise is. By WEIGHT the shares are the classes' own CPI
    weights, which is the honest measure of how much of what people actually
    buy is rising. They answer different questions and routinely disagree --
    a handful of heavily weighted classes can carry the weighted line well
    above the counted one.

    Weights come from the ABS's own `Contribution to Total CPI`, taken at each
    date rather than fixed, so the annual reweighting is picked up instead of
    today's basket being projected back over history.
    """
    rates = {n: dict(annualised(s, periods)) for n, s in class_indexes.items()}
    wts = {n: dict(s) for n, s in weights.items()}

    dates: set[date] = set()
    for r in rates.values():
        dates |= set(r)

    out: list[BreadthPoint] = []
    for when in sorted(dates):
        live = [(n, r[when]) for n, r in rates.items() if when in r]
        if not live:
            continue
        above = [n for n, v in live if v > threshold]
        total_w = sum(abs(wts.get(n, {}).get(when, 0.0)) for n, _ in live)
        above_w = sum(abs(wts.get(n, {}).get(when, 0.0)) for n in above)
        out.append(BreadthPoint(
            when=when,
            share_by_count=len(above) / len(live) * 100.0,
            share_by_weight=(above_w / total_w * 100.0) if total_w else float("nan"),
            n_above=len(above), n_total=len(live)))
    return out


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

RESIDUAL = "Other"


@dataclass(frozen=True)
class Bucket:
    name: str
    members: list[str]


def total_points(contributions: dict[str, Series]) -> list[tuple[date, float]]:
    """The reconstructed All groups index: every class's points, added up.

    This is the denominator `contribution_to_change` needs, and it has to be
    this rather than the published index because the two are on DIFFERENT
    BASES. The ABS re-based the CPI to 100 at the September 2025 quarter when
    it went monthly, so contributions run at ~102 while the quarterly seasonally
    adjusted index published alongside them is still on the old base at ~147.
    Mixing the two scales every bar down by a factor of 1.44 -- which looks
    plausible on screen, because the bars keep their shape and only the stack
    total goes quietly wrong.
    """
    acc: dict[date, float] = {}
    for obs in contributions.values():
        for d, v in obs:
            acc[d] = acc.get(d, 0.0) + v
    return sorted(acc.items())


def contribution_to_change(contributions: dict[str, Series], total: Series,
                           periods: int = 1) -> dict[str, list[tuple[date, float]]]:
    """Index-point contributions -> percentage points of the total's change.

    A bucket's bar is how much of the quarter's CPI change it accounts for, so
    the numerator is the change in its index points and the denominator is the
    PREVIOUS total. Dividing by the current total instead is the classic
    off-by-one here: it is a different quantity, close enough to look right and
    wrong by roughly the quarter's own inflation rate.
    """
    base = dict(total)
    out: dict[str, list[tuple[date, float]]] = {}
    for name, obs in contributions.items():
        rows = sorted(obs, key=lambda p: p[0])
        pts = []
        for (d0, v0), (d1, v1) in zip(rows, rows[periods:]):
            prev = base.get(d0)
            if prev:
                pts.append((d1, (v1 - v0) / prev * 100.0))
        out[name] = pts
    return out


def load_classification(path: str) -> tuple[list[Bucket], list[str]]:
    """The composition buckets and the cyclical list, from editable JSON.

    Keys starting `_` are prose for whoever opens the file and are skipped. A
    missing or damaged file yields empty lists rather than an exception: the
    charts that need it then say the classification is missing, which is a more
    useful failure than a stack trace over an unrelated tab.
    """
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return [], []
    comp = raw.get("composition", {})
    buckets = [Bucket(k, [m for m in v if isinstance(m, str)])
               for k, v in comp.items()
               if not k.startswith("_") and isinstance(v, list)]
    cyc = raw.get("cyclical", {}).get("members", [])
    return buckets, [c for c in cyc if isinstance(c, str)]


def bucket_contributions(contribution: dict[str, Series],
                         buckets: Sequence[Bucket],
                         universe: Iterable[str]) -> dict[str, list[tuple[date, float]]]:
    """Sum published contributions into analytical buckets.

    Anything in `universe` that no bucket claims lands in `Other` rather than
    vanishing, so the stack always reconciles to the published total and a
    classification with a hole in it is visible as a fat residual instead of a
    quietly short bar.
    """
    claimed = {m for b in buckets for m in b.members}
    named = [Bucket(b.name, [m for m in b.members if m in contribution])
             for b in buckets]
    residual = [n for n in universe if n not in claimed and n in contribution]
    named.append(Bucket(RESIDUAL, residual))

    out: dict[str, list[tuple[date, float]]] = {}
    for b in named:
        acc: dict[date, float] = {}
        for m in b.members:
            for d, v in contribution[m]:
                acc[d] = acc.get(d, 0.0) + v
        out[b.name] = sorted(acc.items())
    return out


# --------------------------------------------------------------------------
# Cycle sensitivity
# --------------------------------------------------------------------------

def sub_index(points: dict[str, Series], members: Sequence[str]
              ) -> list[tuple[date, float]]:
    """A sub-index for a subset of classes: their index points, added up.

    Adding levels rather than chaining growth rates, and the difference is not
    cosmetic. An earlier version built the level by compounding each quarter's
    weighted mean of member growth, which meant every member's quarterly ratio
    was raised to the fourth power to annualise it. Child care survived that
    for nine years and then broke it: the index fell about 95% in June 2020
    when child care went free and came back the quarter after, so the ratio was
    roughly 20, the fourth power roughly 160,000, and the aggregate reached
    15,000 per cent. A ratio of small numbers has no business inside an index.

    Summing points has no such failure mode -- a class going to zero removes
    its own weight and nothing else -- and it is the same fixed-weight
    construction the rest of the tab uses, so the pieces stay comparable.
    """
    live = [dict(points[m]) for m in members if m in points]
    if not live:
        return []
    # Only the quarters EVERY member reaches. A level index has to be summed
    # over a constant membership or it steps whenever a class enters: the ABS
    # starts Deposit and loan facilities in 2011 and Tertiary education in
    # 2000, and letting either arrive mid-series put a discontinuity into the
    # level that the year-ended change then read as 900 per cent inflation.
    # (A SHARE is a ratio and has no such problem, which is why `breadth`
    # deliberately counts whatever is live in each quarter instead.)
    shared = set(live[0]).intersection(*live[1:]) if len(live) > 1 else set(live[0])
    return [(d, sum(m[d] for m in live)) for d in sorted(shared)]


@dataclass(frozen=True)
class CycleSplit:
    cyclical: list[tuple[date, float]] = field(default_factory=list)
    non_cyclical: list[tuple[date, float]] = field(default_factory=list)
    n_cyclical: int = 0
    n_non_cyclical: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.cyclical) and bool(self.non_cyclical)


def cycle_split(points: dict[str, Series], cyclical: Sequence[str],
                leaves: Sequence[str]) -> CycleSplit:
    """Year-ended inflation for the cycle-sensitive basket and its complement.

    `cyclical` is a classification, not a measurement -- which categories
    respond to domestic slack is an empirical judgement that different houses
    make differently, and it is why this takes the list as an argument rather
    than owning one.
    """
    cyc = [c for c in cyclical if c in points]
    non = [c for c in leaves if c not in set(cyc) and c in points]
    return CycleSplit(
        cyclical=year_ended(sub_index(points, cyc)),
        non_cyclical=year_ended(sub_index(points, non)),
        n_cyclical=len(cyc), n_non_cyclical=len(non))


def mean_over(series: Series, start: date, end: date) -> float | None:
    """Average of a series across a window -- the reference lines the breadth
    charts carry."""
    vals = [v for d, v in series if start <= d <= end]
    return sum(vals) / len(vals) if vals else None
