"""The panel: one row per Labour Force release, holding only what was public
the day before it.

Row m (the reference month of a print) carries:
  * the target -- the change in the unemployment rate that print reported,
    unrounded (unemployed / labour force, seasonally adjusted) and as
    published (the new rounded rate minus the last rounded rate);
  * the unrounded and printed last rate (the anchor for the probabilities);
  * one column per input, transformed from observations released strictly
    before the cutoff (`releases.known`).

History rows use cutoff R_m. The live row, for the next print after the
as-of date, uses the as-of date itself.

Values are today's vintage throughout: the ABS archives past releases only
from December 2019, too short for a 15-year window, so there is no ALFRED
equivalent to read them as they stood. The surveys (NAB, Westpac-MI) revise
only with seasonal factors; the Labour Force levels revise by small amounts
as its seasonal factors update. Release DATES are exact or conservative,
which is the guarantee against look-ahead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from nextprint import inputs as I
from nextprint import releases as R

#: An input older than this many months at the cutoff is treated as missing.
#: Long enough to carry a quarterly vacancies print to the next one.
MAX_STALENESS_MONTHS = 5


@dataclass
class Series:
    """One input's observations with their release dates, as numpy arrays."""
    obs: np.ndarray        # observation-date ordinals, ascending
    val: np.ndarray
    rel: np.ndarray        # release-date ordinals

    @classmethod
    def build(cls, points: list[tuple[date, float]], releases: list[date]) -> "Series":
        keep = sorted((d, v, r) for (d, v), r in zip(points, releases)
                      if v is not None and not math.isnan(v))
        return cls(np.array([d.toordinal() for d, _, _ in keep], dtype=float),
                   np.array([v for _, v, _ in keep], dtype=float),
                   np.array([r.toordinal() for _, _, r in keep], dtype=float))

    def known(self, cutoff: date) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        m = self.rel < cutoff.toordinal()
        return self.obs[m], self.val[m], self.rel[m]


def _months_apart(a: float, b: float) -> int:
    da, db = date.fromordinal(int(a)), date.fromordinal(int(b))
    return (db.year - da.year) * 12 + db.month - da.month


def transform(kind: str, s: Series, cutoff: date, month: date) -> tuple[float, dict]:
    """The input's value for the print of `month`, from what was public before
    `cutoff`, plus what it was built from (for the timing table)."""
    obs, val, rel = s.known(cutoff)
    nan = float("nan"), {}
    if len(obs) == 0:
        return nan
    last = date.fromordinal(int(obs[-1]))
    meta = {"obs": last, "released": date.fromordinal(int(rel[-1])), "raw": float(val[-1])}
    if _months_apart(obs[-1], month.toordinal()) > MAX_STALENESS_MONTHS:
        return nan
    if kind == I.LEVEL:
        return float(val[-1]), meta
    if kind == I.DIFF1:
        if len(obs) < 2 or _months_apart(obs[-2], obs[-1]) != 1:
            return nan
        return float(val[-1] - val[-2]), meta
    if kind == I.DIFF3:
        if len(obs) < 4 or _months_apart(obs[-4], obs[-1]) != 3:
            return nan
        return float(val[-1] - val[-4]), meta
    if kind == I.GROWTH3:
        if len(obs) < 4 or _months_apart(obs[-4], obs[-1]) != 3 or val[-4] <= 0:
            return nan
        return 100 * math.log(val[-1] / val[-4]) / 3, meta
    if kind == I.QGROWTH:
        # The quarter before, not the observation before: the 2008-09
        # suspension must not read as one quarter's growth.
        if len(obs) < 2 or _months_apart(obs[-2], obs[-1]) != 3 or val[-2] <= 0:
            return nan
        return 100 * math.log(val[-1] / val[-2]), meta
    raise ValueError(kind)


@dataclass
class Panel:
    frame: pd.DataFrame                        # index: reference month
    live_month: date | None
    live_release: date | None                  # date of the next print (rule when not out)
    timing: dict[str, dict] = field(default_factory=dict)   # key -> what the live row used
    #: When the as-of date is in the past, what the live print turned out to be.
    #: Display only -- it never enters the live forecast.
    live_actual: dict | None = None


def round_rate(x: float) -> float:
    """The published rate: one decimal, half up."""
    return math.floor(x * 10 + 0.5 + 1e-9) / 10


def masked(month: date) -> bool:
    return I.COVID[0] <= month <= I.COVID[1]


def build(data: dict, as_of: date, next_release: date | None = None) -> Panel:
    """The panel from `fetch.load_all()` data, as seen on `as_of`.

    History rows are the prints released strictly before `as_of`; the live
    row is the next print after them, cut off at `as_of`."""
    lf, exact = data["lf"], data["lf_releases"]
    un, lab = dict(lf[I.UNEMPLOYED]), dict(lf[I.LABOUR_FORCE_LEVEL])
    rate = {m: 100 * un[m] / lab[m] for m in un if lab.get(m)}

    series: dict[str, Series] = {}
    for inp in I.INPUTS:
        points = sorted(lf[inp.code] if inp.source == "lf" else data["rba"][inp.code])
        series[inp.key] = Series.build(points, R.release_dates(
            [d for d, _ in points], inp.timing, exact))

    def row_for(month: date, cutoff: date, printed: bool) -> tuple[dict, dict]:
        prev = R.add_months(month, -1)
        prior_u = rate.get(prev, float("nan"))
        row = {"cutoff": cutoff, "prior_u": prior_u,
               "prior_print": round_rate(prior_u) if math.isfinite(prior_u) else float("nan"),
               "y": float("nan"), "y_pub": float("nan"), "new_print": float("nan"),
               "new_u": float("nan")}
        if printed and month in rate and math.isfinite(prior_u):
            row["new_u"] = rate[month]
            row["y"] = rate[month] - prior_u
            row["new_print"] = round_rate(rate[month])
            row["y_pub"] = round(row["new_print"] - row["prior_print"], 1)
        meta = {}
        for key, s in series.items():
            row[f"x_{key}"], meta[key] = transform(I.BY_KEY[key].transform, s, cutoff, month)
        return row, meta

    months = sorted(rate)
    rows, index = [], []
    for month in months[1:]:
        day = R.lf_release(month, exact)
        if day >= as_of:
            continue
        row, _ = row_for(month, day, True)
        row["release"], row["masked"], row["live"] = day, masked(month), False
        rows.append(row)
        index.append(month)

    printed = max(index) if index else None
    live_month = R.add_months(printed, 1) if printed else None
    live_release = None
    timing: dict[str, dict] = {}
    actual = None
    if live_month is not None:
        live_release = exact.get(live_month) or next_release or R.lf_rule(live_month)
        row, timing = row_for(live_month, as_of, False)
        row["release"], row["masked"], row["live"] = live_release, masked(live_month), True
        rows.append(row)
        index.append(live_month)
        if live_month in rate and live_month in exact:
            done, _ = row_for(live_month, exact[live_month], True)
            actual = {k: done[k] for k in ("new_print", "new_u", "y_pub", "y", "prior_print")}

    frame = pd.DataFrame(rows, index=pd.Index(index, name="month"))
    return Panel(frame, live_month, live_release, timing, actual)
