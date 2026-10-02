"""CPI inflation dispersion -- how BROAD a quarter's inflation is, not just how big.

The FOMC version's scatter, on the Australian CPI. Each point is one period:
the headline change against the share of the basket (by CPI weight) whose own
price rose faster. The US plots months of PCE; the Australian expenditure-class
indexes are quarterly (ABS appendix 1a, seasonally adjusted, back to the
1980s), so a point here is a QUARTER and every step is three months. The fit,
the eras and the balanced-share read are the US module's, unchanged.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

REGIMES: tuple[tuple[str, date, date], ...] = (
    ("Mar 1993 - Dec 2019", date(1993, 1, 1), date(2019, 12, 31)),
    ("Mar 2021 - Dec 2023", date(2021, 1, 1), date(2023, 12, 31)),
    ("Mar 2025 - present", date(2025, 1, 1), date(2100, 1, 1)),
)

#: Months between two observations. Quarterly here; the US scatter is monthly.
STEP_MONTHS = 3
PERIODS_PER_YEAR = 12 // STEP_MONTHS

BALANCED_SHARE = 50.0

# Coverage is two-sided on purpose. A partition can fail by missing categories
# OR by double-counting them, and the second is the one that actually happened:
# the BEA API returns line descriptions with the indentation stripped, so a
# depth-based leaf rule kept every subtotal and analytical aggregate as well and
# the weights summed to nearly fourteen times total PCE. A floor alone reads
# 1385% as healthy.
MIN_COVERAGE = 90.0
MAX_COVERAGE = 110.0

#: Quarterly: twelve months are four points, so the US floor of twelve
#: observations would leave the recent eras unfitted. Six quarters is the floor.
MIN_FIT_POINTS = 6

# The trend is fitted in LOG-ODDS of the share, not in the share itself.
#
# The x axis is a proportion, bounded at 0 and 100, and the relationship has to
# steepen at both ends: a month whose headline is carried by 5% of the basket
# needs enormous increases in that 5%, and a month where 95% of the basket beats
# the average needs a handful of collapses to drag the mean below it. A straight
# line cannot do that -- it has one slope everywhere and runs off the ends of a
# bounded axis. Log-odds gives dy/dshare = b / (p(1-p)): flattest in the middle,
# steepening towards either edge, which is the curvature the published chart
# shows. It costs no extra parameter and beats the straight line on adjusted R2
# in every era (0.762 vs 0.750, 0.616 vs 0.607, 0.764 vs 0.734). A cubic scores
# higher still and is overfitting -- on the 19 months of the current era it
# swings to -0.17% at the 80% share where the data say -0.06%.
LOGIT_CLAMP = 0.001

# How much bend the data is allowed to buy.
#
# The published chart's lines steepen much harder at the ends than a single
# log-odds term can bend, and with the fit measured over the range each era is
# actually DRAWN across -- not extrapolated past its own data -- a cubic is
# well behaved for all three:
#
#   1995-2020  n=299  -5.0 / -0.6 / -4.6 bp per share point   adjR2 0.759 -> 0.792
#   2021-2022  n=24   -3.7 / -0.5 / -3.9                      adjR2 0.652 -> 0.682
#   2025-      n=19   -3.8 / -0.3 / -3.0                      adjR2 0.764 -> 0.853
#
# Two guards do the real work, and both are on the drawn range: a higher order
# is taken only if it earns its extra term on ADJUSTED R2, and only if the
# curve still falls monotonically from one end of the era to the other. A trend
# line that turns back up is an overfit wiggle however well it scores, and that
# test already refuses the quadratic for both short eras.
#
# The months-per-term floor is now only a guard against fitting a curve with
# barely more months than it has parameters -- at six, a cubic needs eighteen
# months, which the shortest era on the chart has. It is not doing the judging.
MAX_FIT_ORDER = 3
MONTHS_PER_TERM = 6

# The published PCE price index is a chained Fisher index, so a spend-weighted
# arithmetic mean of category changes never reproduces it exactly. The gap is
# substitution bias, and it scales with how far prices moved -- which makes a
# FLAT basis-point tolerance a dispersion filter wearing a data-quality badge.
# Measured over the months this chart plots, a flat 10bp dropped 3 of the 11
# months with |m/m| above 0.60% and only 5 of the other 368: a twentyfold
# higher rejection rate for exactly the months the chart exists to show. Two of
# the casualties -- Sep 2005 and Mar 2022, both around +0.9% m/m on a fifth of
# the basket -- are plotted in the published version.
#
# So the tolerance is relative, with an absolute floor for quiet months. A
# residual worth 12-14% of the move is ordinary chaining; the three months this
# still rejects (Sep and Oct 2001, Nov 2008) miss by 28% to 177% of theirs.
#
# None of this touches the point's coordinates. The y value is the PUBLISHED
# change, and the x value is a share of spend computed from category changes and
# weights; the residual only says an arithmetic re-aggregation disagrees with a
# Fisher index, which it always does. The real integrity check is that the
# partition covers the basket exactly once, and that is enforced separately.
RECONCILE_FLOOR_BP = 15.0
RECONCILE_SHARE = 0.25


@dataclass(frozen=True)
class MonthPoint:
    month: date
    mom_pct: float          # overall PCE price index, % m/m
    share: float            # 0-100, expenditure share beating that rate
    n_categories: int
    coverage: float         # 0-100, share of nominal PCE the categories cover
    residual_bp: float      # weighted category mean less the published rate


def logit_share(share: float) -> float:
    """A 0-100 share as log-odds, clamped so the endpoints stay on the scale."""
    p = min(max(share / 100.0, LOGIT_CLAMP), 1.0 - LOGIT_CLAMP)
    return math.log(p / (1.0 - p))


@dataclass(frozen=True)
class Fit:
    """A polynomial in the LOG-ODDS of the share, ascending powers."""
    coefficients: tuple[float, ...]
    r2: float
    n: int

    @property
    def order(self) -> int:
        return len(self.coefficients) - 1

    @property
    def intercept(self) -> float:
        return self.coefficients[0] if self.coefficients else 0.0

    @property
    def slope(self) -> float:
        """The first-order term: the trend's direction at the balanced share."""
        return self.coefficients[1] if len(self.coefficients) > 1 else 0.0

    def at(self, share: float) -> float:
        z = logit_share(share)
        return sum(c * z ** k for k, c in enumerate(self.coefficients))

    @property
    def balanced_mom(self) -> float:
        """Fitted m/m percent change where half the basket beats the average.

        The log-odds of a half is zero, so this is just the intercept -- the
        era's central pace, read off with the mechanical breadth slope removed."""
        return self.at(BALANCED_SHARE)

    @property
    def balanced_annualised(self) -> float:
        """That pace compounded to a yearly rate."""
        return ((1.0 + self.balanced_mom / 100.0) ** PERIODS_PER_YEAR - 1.0) * 100.0


@dataclass(frozen=True)
class Regime:
    label: str
    start: date
    end: date
    points: tuple[MonthPoint, ...]
    fit: Fit | None

    @property
    def ok(self) -> bool:
        return bool(self.points)


@dataclass(frozen=True)
class Dispersion:
    regimes: tuple[Regime, ...]
    latest: MonthPoint | None
    dropped: int            # months excluded by coverage or reconciliation

    @property
    def ok(self) -> bool:
        return any(r.ok for r in self.regimes)

    def regime(self, label: str) -> Regime | None:
        return next((r for r in self.regimes if r.label == label), None)


def reconcile_tolerance(mom_pct: float, floor_bp: float = RECONCILE_FLOOR_BP,
                        share: float = RECONCILE_SHARE) -> float:
    """How far the re-aggregated mean may sit from the published change, in bp."""
    return max(floor_bp, share * abs(mom_pct) * 100.0)


def _back(d: date, months: int = STEP_MONTHS) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, 1)


def mom_percent(series: list[tuple[date, float]]) -> dict[date, float]:
    """Period-on-period percent change, matched by DATE not by row position --
    a quarter against the quarter three months before it."""
    by_month = {date(d.year, d.month, 1): v for d, v in series}
    out: dict[date, float] = {}
    for d, v in by_month.items():
        p = by_month.get(_back(d))
        if p:
            out[d] = (v / p - 1.0) * 100.0
    return out


def month_points(prices: dict[str, list[tuple[date, float]]],
                 nominal: dict[str, list[tuple[date, float]]],
                 overall: list[tuple[date, float]],
                 min_coverage: float = MIN_COVERAGE,
                 tolerance_bp: float = RECONCILE_FLOOR_BP,
                 max_coverage: float = MAX_COVERAGE,
                 tolerance_share: float = RECONCILE_SHARE) -> tuple[list[MonthPoint], int]:
    """One point per month: the overall m/m change, and the breadth behind it."""
    overall_mom = mom_percent(overall)
    cat_mom = {c: mom_percent(rows) for c, rows in prices.items()}
    cat_nom = {c: {date(d.year, d.month, 1): v for d, v in rows} for c, rows in nominal.items()}
    shared = sorted(set(cat_mom) & set(cat_nom))

    points: list[MonthPoint] = []
    dropped = 0
    for month in sorted(overall_mom):
        rate = overall_mom[month]
        weights: list[tuple[float, float]] = []      # (weight, category m/m)
        for code in shared:
            w = cat_nom[code].get(month)
            pi = cat_mom[code].get(month)
            if w is None or pi is None or w <= 0:
                continue
            weights.append((w, pi))
        if not weights:
            dropped += 1
            continue

        total = sum(w for w, _ in weights)
        basket = {date(d.year, d.month, 1): v for d, v in nominal.get("", [])}.get(month)
        coverage = 100.0 * total / basket if basket else 100.0
        if not min_coverage <= coverage <= max_coverage:
            dropped += 1
            continue

        implied = sum(w * pi for w, pi in weights) / total
        residual_bp = (implied - rate) * 100.0
        if abs(residual_bp) > reconcile_tolerance(rate, tolerance_bp, tolerance_share):
            dropped += 1
            continue

        above = sum(w for w, pi in weights if pi > rate)
        points.append(MonthPoint(
            month=month,
            mom_pct=rate,
            share=100.0 * above / total,
            n_categories=len(weights),
            coverage=coverage,
            residual_bp=residual_bp,
        ))
    return points, dropped


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        out[r] = (m[r][n] - sum(m[r][c] * out[c] for c in range(r + 1, n))) / m[r][r]
    return out


def _polyfit(xs: list[float], ys: list[float], order: int) -> tuple[list[float], float] | None:
    """Least-squares polynomial of `order` in xs, with its R^2 on the y scale."""
    terms = order + 1
    if len(xs) <= terms:
        return None
    powers = [[x ** k for k in range(terms)] for x in xs]
    a = [[sum(row[i] * row[j] for row in powers) for j in range(terms)] for i in range(terms)]
    b = [sum(row[i] * y for row, y in zip(powers, ys)) for i in range(terms)]
    coef = _solve(a, b)
    if coef is None:
        return None
    my = statistics.fmean(ys)
    syy = sum((y - my) ** 2 for y in ys)
    resid = sum((y - sum(c * x ** k for k, c in enumerate(coef))) ** 2
                for x, y in zip(xs, ys))
    return coef, (1.0 - resid / syy if syy else 0.0)


def _adjusted(r2: float, n: int, order: int) -> float:
    return 1.0 - (1.0 - r2) * (n - 1) / (n - order - 1) if n - order - 1 > 0 else -1.0


def _falls_throughout(coef: list[float], lo: float, hi: float, steps: int = 40) -> bool:
    """Whether the fitted curve decreases all the way across the era's range.
    A trend line that turns back up is an overfit wiggle, not a trend."""
    step = (hi - lo) / steps if hi > lo else 0.0
    prev = None
    for k in range(steps + 1):
        share = lo + step * k
        z = logit_share(share)
        here = sum(c * z ** j for j, c in enumerate(coef))
        if prev is not None and here > prev + 1e-12:
            return False
        prev = here
    return True


def fit(points: list[MonthPoint], min_points: int = MIN_FIT_POINTS,
        max_order: int = MAX_FIT_ORDER,
        months_per_term: int = MONTHS_PER_TERM) -> Fit | None:
    """Least-squares fit of m/m change on the LOG-ODDS of breadth.

    Linear in log-odds already curves -- steepest at either edge of a bounded
    share, flattest in the middle. An era with enough months to pay for them
    takes higher terms as well, which is what lets the ends bend as hard as the
    published chart's do. None if too few months."""
    if len(points) < min_points:
        return None
    xs = [logit_share(p.share) for p in points]
    ys = [p.mom_pct for p in points]
    lo = min(p.share for p in points)
    hi = max(p.share for p in points)

    cap = max(1, min(max_order, len(points) // months_per_term))
    best: tuple[float, list[float], float] | None = None
    for order in range(1, cap + 1):
        got = _polyfit(xs, ys, order)
        if got is None:
            continue
        coef, r2 = got
        if order > 1 and not _falls_throughout(coef, lo, hi):
            continue
        score = _adjusted(r2, len(points), order)
        if best is None or score > best[0]:
            best = (score, coef, r2)
    if best is None:
        return None
    _, coef, r2 = best
    return Fit(coefficients=tuple(coef), r2=r2, n=len(points))


def build(prices: dict[str, list[tuple[date, float]]],
          nominal: dict[str, list[tuple[date, float]]],
          overall: list[tuple[date, float]],
          regimes: tuple[tuple[str, date, date], ...] = REGIMES,
          as_of: date | None = None) -> Dispersion:
    """The whole scatter: every month placed, grouped into regimes and fitted."""
    points, dropped = month_points(prices, nominal, overall)
    if as_of is not None:
        points = [p for p in points if p.month <= as_of]
    built = []
    for label, start, end in regimes:
        inside = tuple(p for p in points if start <= p.month <= end)
        built.append(Regime(label=label, start=start, end=end,
                            points=inside, fit=fit(list(inside))))
    return Dispersion(regimes=tuple(built),
                      latest=points[-1] if points else None,
                      dropped=dropped)


def from_classes(classes: dict[str, list[tuple[date, float]]],
                 weights: dict[str, list[tuple[date, float]]],
                 regimes: tuple[tuple[str, date, date], ...] = REGIMES,
                 as_of: date | None = None) -> Dispersion:
    """The scatter on the Australian expenditure classes.

    `classes` are the quarterly SA class indexes and `weights` each class's
    index-point contribution. Each class is weighted by its contribution in the
    PREVIOUS quarter (a Laspeyres mean), and the y value is the basket's own
    re-aggregated change on those weights rather than the published headline.

    That is a deliberate departure from the US module, which plots the published
    PCE change and drops months the partition cannot reproduce. Here the class
    weights are today's basket held back through history (the ABS reweights
    annually and the cache keeps the latest), and the published CPI carried
    mortgage interest until 1998 and the GST step in 2000 -- so against the
    headline, half the history would fail reconciliation for reasons that say
    nothing about breadth. On its own weights the basket reconciles exactly, so
    both axes measure the same thing and the balanced-share read stands."""
    if not classes or not weights:
        return Dispersion(regimes=(), latest=None, dropped=0)

    def lagged(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
        return [(d1, v0) for (_, v0), (d1, _) in zip(rows, rows[1:])]

    nominal = {k: lagged(v) for k, v in weights.items() if k in classes}
    total: dict[date, float] = {}
    for rows in nominal.values():
        for d, v in rows:
            total[d] = total.get(d, 0.0) + v
    nominal[""] = sorted(total.items())
    cat = {k: mom_percent(v) for k, v in classes.items()}
    by_date = {k: dict(rows) for k, rows in nominal.items() if k}
    # The basket's own change each quarter, chained into an index for `build`.
    index, level = [], 100.0
    for d, tot in sorted(total.items()):
        moves = [(by_date[k][d], cat[k][d]) for k in by_date
                 if d in by_date[k] and d in cat.get(k, {})]
        live = sum(w for w, _ in moves)
        if not live:
            continue
        if not index:
            index.append((_back(d), level))
        level *= 1.0 + sum(w * pi for w, pi in moves) / live / 100.0
        index.append((d, level))
    return build(classes, nominal, index, regimes, as_of)
