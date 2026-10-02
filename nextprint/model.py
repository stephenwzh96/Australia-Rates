"""Forecasts, one row at a time, with nothing but the past.

For every panel row (every print, plus the live one):
  * benchmark:  no change -- the rate stays where it printed. It is the
    trader's null, and on this data the strongest simple rule: it beats a
    drift, an AR(1) and an AR(2) on every period tested.
  * input k:    dU = c_k * (x_k - normal_k)
    where normal_k is x_k's average over the estimation window. An input
    moves the forecast only when it is away from its own normal, and there
    is no drift: the unemployment rate has no long-run trend.
  * combination = the plain average of the input forecasts.
  * challenger (tracked, not used): a ridge regression on all inputs at
    once -- see `ridge`.
c_k is OLS on the prints released before the row's cutoff, over the last
WINDOW_YEARS (rolling), excluding masked months.

Print probabilities add the combination's own past out-of-sample errors
(de-meaned, rescaled to today's level of unemployment) to the unrounded last
rate plus the forecast, round each to 0.1 as the ABS does, and count outcomes.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from nextprint import inputs as I

BENCH = "bench"
COMBO = "combo"
RIDGE = "ridge"
KEYS = [i.key for i in I.INPUTS]
#: Published-change buckets, in tenths of a point: <=-0.2, -0.1, 0, +0.1, >=+0.2.
BUCKETS = (-2, -1, 0, 1, 2)


def _years_back(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:                       # 29 Feb
        return d.replace(year=d.year - years, day=28)


def _release_ordinals(frame: pd.DataFrame) -> np.ndarray:
    return np.array([d.toordinal() if isinstance(d, date) else np.inf for d in frame["release"]])


def forecasts(frame: pd.DataFrame, window_years: int | None = I.WINDOW_YEARS,
              min_train: int = I.MIN_TRAIN) -> pd.DataFrame:
    """f_bench (0), f_<key>, c_<key> (slope), m_<key> (the input's normal),
    f_combo, f_ridge and n_inputs for every row. `window_years=None` is
    expanding."""
    rel = _release_ordinals(frame)
    y = frame["y"].to_numpy(float)
    ok = (~frame["masked"].to_numpy(bool)) & np.isfinite(y)
    X = {k: frame[f"x_{k}"].to_numpy(float) for k in KEYS}

    cols = ([f"f_{BENCH}", f"f_{COMBO}", f"f_{RIDGE}", "n_inputs"] + [f"f_{k}" for k in KEYS]
            + [f"c_{k}" for k in KEYS] + [f"m_{k}" for k in KEYS])
    out = {c: np.full(len(frame), np.nan) for c in cols}
    out[f"f_{BENCH}"][:] = 0.0
    for i, cutoff in enumerate(frame["cutoff"]):
        c = cutoff.toordinal()
        train = ok & (rel < c)
        if window_years is not None:
            train &= rel >= _years_back(cutoff, window_years).toordinal()
        forecasts_i = []
        for k in KEYS:
            xk = X[k]
            if not math.isfinite(xk[i]):
                continue
            t = train & np.isfinite(xk)
            if t.sum() < min_train:
                continue
            dev = xk[t] - xk[t].mean()
            ss = float(dev @ dev)
            if ss <= 0:
                continue
            slope = float(dev @ y[t]) / ss
            fk = slope * (xk[i] - xk[t].mean())
            out[f"f_{k}"][i], out[f"c_{k}"][i], out[f"m_{k}"][i] = fk, slope, xk[t].mean()
            forecasts_i.append(fk)
        if forecasts_i:
            out[f"f_{COMBO}"][i] = float(np.mean(forecasts_i))
        out["n_inputs"][i] = len(forecasts_i)
        out[f"f_{RIDGE}"][i] = ridge(X, y, train, i, min_train)
    return pd.DataFrame(out, index=frame.index)


def ridge(X: dict[str, np.ndarray], y: np.ndarray, train: np.ndarray, i: int,
          min_train: int = I.MIN_TRAIN) -> float:
    """The challenger's forecast for row i: every input released for it,
    standardised over the training rows, in one ridge regression with no
    intercept (no change stays nested) and penalty = the number of training
    rows -- strong shrinkage, fixed in advance, never tuned. The latest-starting
    input is dropped until the rows with every input number `min_train`."""
    keys = [k for k in KEYS if math.isfinite(X[k][i])]
    while keys:
        t = train & np.all([np.isfinite(X[k]) for k in keys], axis=0)
        if t.sum() >= min_train:
            break
        keys.remove(max(keys, key=lambda k: int(np.argmax(np.isfinite(X[k])))))
    else:
        return float("nan")
    Z = np.column_stack([X[k][t] for k in keys])
    mu, sd = Z.mean(0), Z.std(0)
    sd[sd == 0] = 1.0
    Z = (Z - mu) / sd
    z = (np.array([X[k][i] for k in keys]) - mu) / sd
    beta = np.linalg.solve(Z.T @ Z + len(Z) * np.eye(len(keys)), Z.T @ y[t])
    return float(z @ beta)


def abs_round(x):
    """Round half up to one decimal, as the published rate is."""
    return np.floor(np.asarray(x, dtype=float) * 10 + 0.5 + 1e-9) / 10


def _anchor(prior_u: float, prior_print: float) -> float:
    """The unrounded last rate, held inside its printed bucket (a guard: with
    the ABS's unrounded levels the two always agree)."""
    if not math.isfinite(prior_u):
        return prior_print
    return min(max(prior_u, prior_print - 0.05), prior_print + 0.0499)


def print_probabilities(prior_u: float, prior_print: float, forecast: float,
                        errors: np.ndarray) -> dict[int, float] | None:
    """P(published change = k tenths) for k in BUCKETS (ends open), from the
    empirical distribution of past errors. None with too few errors."""
    errors = errors[np.isfinite(errors)]
    if len(errors) < I.MIN_ERRORS or not math.isfinite(forecast):
        return None
    # De-meaned: past errors give the SPREAD, the forecast gives the centre.
    # Keeping their mean would re-add an old bias (e.g. the steady 2010s
    # decline) on top of the forecast and contradict the expected change.
    errors = errors - errors.mean()
    sims = abs_round(_anchor(prior_u, prior_print) + forecast + errors)
    k = np.clip(np.rint((sims - prior_print) * 10), BUCKETS[0], BUCKETS[-1])
    return {b: float(np.mean(k == b)) for b in BUCKETS}


def noise_scale(u_now: float, u_then: np.ndarray) -> np.ndarray:
    """How much bigger a miss made at rate u_then would be at rate u_now (%).

    Most of a month's miss is Labour Force sampling error, and the standard
    error of an estimated share u runs with sqrt(u(1-u)). A 15-year window can
    hold both 6% and 3.5% unemployment, so its raw errors would price a 3.5%
    month with a 6% month's noise. No parameters: it is the sampling formula."""
    a, b = u_now / 100, np.asarray(u_then, float) / 100
    return np.sqrt(a * (1 - a) / (b * (1 - b)))


def error_track(frame: pd.DataFrame, fc: pd.DataFrame, model: str,
                window_years: int = I.WINDOW_YEARS) -> list[np.ndarray]:
    """For every row, `model`'s out-of-sample errors on the prints released
    before its cutoff (last `window_years`), rescaled to the row's level of
    unemployment (`noise_scale`)."""
    rel = _release_ordinals(frame)
    err = np.array(frame["y"] - fc[f"f_{model}"], dtype=float)     # a copy: written below
    err[frame["masked"].to_numpy(bool)] = np.nan
    level = frame["prior_u"].to_numpy(float)
    level = np.where(np.isfinite(level), level, frame["prior_print"].to_numpy(float))
    out = []
    for i, cutoff in enumerate(frame["cutoff"]):
        c = cutoff.toordinal()
        use = (rel < c) & (rel >= _years_back(cutoff, window_years).toordinal())
        e = err[use] * noise_scale(level[i], level[use])
        out.append(e[np.isfinite(e)])
    return out


def probability_track(frame: pd.DataFrame, fc: pd.DataFrame, model: str,
                      window_years: int = I.WINDOW_YEARS) -> list[dict[int, float] | None]:
    """Print probabilities for every row of `model`'s forecasts (`error_track`)."""
    return [print_probabilities(frame["prior_u"].iat[i], frame["prior_print"].iat[i],
                                fc[f"f_{model}"].iat[i], e)
            for i, e in enumerate(error_track(frame, fc, model, window_years))]


#: Central coverage of the forecast bands: 70% (the RBA's own forecast-
#: uncertainty fans start at 70%, also from historical errors); 90% shows the tails.
BANDS = (0.70, 0.90)


def interval_track(frame: pd.DataFrame, fc: pd.DataFrame, model: str,
                   window_years: int = I.WINDOW_YEARS) -> list[dict[float, tuple[float, float]] | None]:
    """Equal-tailed bands for the unrounded rate each print will show: the
    unrounded last rate plus the forecast plus quantiles of the same
    de-meaned, rescaled past errors that price the print probabilities.
    Empirical, so fat tails and skew in past misses carry through; None with
    too few errors."""
    out = []
    for i, e in enumerate(error_track(frame, fc, model, window_years)):
        f = fc[f"f_{model}"].iat[i]
        if len(e) < I.MIN_ERRORS or not math.isfinite(f):
            out.append(None)
            continue
        centre = _anchor(frame["prior_u"].iat[i], frame["prior_print"].iat[i]) + f
        e = e - e.mean()
        out.append({c: (centre + float(np.quantile(e, (1 - c) / 2)),
                        centre + float(np.quantile(e, (1 + c) / 2))) for c in BANDS})
    return out
