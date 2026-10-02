"""How good were the forecasts, measured only on prints they did not see.

Months are one apart and the horizon is one print, so errors do not overlap
and the standard tests apply without long-run variance games.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from nextprint import inputs as I
from nextprint import model as M


def rmse(e: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(e)))) if len(e) else float("nan")


def error_reduction(e_model: np.ndarray, e_bench: np.ndarray) -> float:
    """1 - RMSE(model)/RMSE(benchmark), same months. Positive is better."""
    b = rmse(e_bench)
    return 1 - rmse(e_model) / b if b and math.isfinite(b) else float("nan")


def _hac_upper_p(d: np.ndarray, lags: int = 3) -> float:
    """One-sided p-value that the mean of `d` is not above zero: a t-test with
    a Newey-West (Bartlett, `lags`) variance and the normal tail -- what
    statsmodels' OLS-on-a-constant with HAC errors computes, written out so
    the package needs numpy alone."""
    d = np.asarray(d, float)
    n = len(d)
    e = d - d.mean()
    s = float(e @ e) / n
    for lag in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - lag / (lags + 1)) * float(e[lag:] @ e[:-lag]) / n
    if s <= 0:
        return float("nan")
    t = d.mean() / math.sqrt(s / n)
    return 0.5 * math.erfc(t / math.sqrt(2))


def clark_west(y: np.ndarray, f_bench: np.ndarray, f_model: np.ndarray, lags: int = 3) -> float:
    """One-sided p-value that the model (which nests the benchmark) adds
    nothing. Clark & West (2007): the MSE gap corrected for the noise of
    estimating the extra coefficient; Newey-West standard error."""
    adj = (y - f_bench) ** 2 - ((y - f_model) ** 2 - (f_bench - f_model) ** 2)
    if len(adj) < 24:
        return float("nan")
    return _hac_upper_p(adj, lags)


def loss_test(loss_new: np.ndarray, loss_old: np.ndarray, lags: int = 3) -> float:
    """One-sided p-value that `loss_new` is on average no lower than
    `loss_old` (Diebold & Mariano 1995; Newey-West standard error)."""
    d = np.asarray(loss_old, float) - np.asarray(loss_new, float)
    if len(d) < 12 or not np.any(d):
        return float("nan")
    return _hac_upper_p(d, lags)


def diebold_mariano(e_new: np.ndarray, e_old: np.ndarray, lags: int = 3) -> float:
    """One-sided p-value that `e_new` has no smaller squared error than
    `e_old`. Neither model nests the other, so no Clark-West correction."""
    return loss_test(np.square(e_new), np.square(e_old), lags)


def challenger_verdict(n: int, er: float, p: float) -> str:
    """The rule fixed when the challenger was registered: it replaces the
    average only after CHALLENGER_MIN_PRINTS unseen prints, with a lower error
    AND a one-sided DM p below CHALLENGER_P."""
    if n < I.CHALLENGER_MIN_PRINTS:
        return "collecting"
    return "switch" if er > 0 and p < I.CHALLENGER_P else "keep"


def direction_right(forecast: np.ndarray, published: np.ndarray) -> float:
    """Of the months the published rate moved, the share where the forecast
    had the sign right. A coin flip scores 50%."""
    moved = np.abs(published) > 0.05
    if not moved.any():
        return float("nan")
    return float(np.mean(np.sign(forecast[moved]) == np.sign(published[moved])))


def base_rate(published: np.ndarray) -> float:
    """Of the months the rate moved, the share that went the more common way:
    what "always say that direction" would have scored."""
    moved = published[np.abs(published) > 0.05]
    if not len(moved):
        return float("nan")
    up = float(np.mean(moved > 0))
    return max(up, 1 - up)


def reliability(p_up: np.ndarray, went_up: np.ndarray, groups: int = 5) -> list[tuple[float, float, int]]:
    """(average stated P(up), share that actually went up, months) by quintile
    of the stated probability -- a calibration check a reader can eyeball."""
    if len(p_up) < groups * 10:
        return []
    order = np.argsort(p_up, kind="stable")
    return [(float(p_up[g].mean()), float(went_up[g].mean()), len(g))
            for g in np.array_split(order, groups)]


def brier(probs: list[dict[int, float]], actual: np.ndarray) -> float:
    """Mean multi-bucket Brier score (lower is better)."""
    total = []
    for p, a in zip(probs, actual):
        k = int(np.clip(round(a * 10), M.BUCKETS[0], M.BUCKETS[-1]))
        total.append(sum((p[b] - (1.0 if b == k else 0.0)) ** 2 for b in M.BUCKETS))
    return float(np.mean(total)) if total else float("nan")


def confidence_set(losses: pd.DataFrame, size: float = 0.10) -> set[str]:
    """The inputs the data cannot tell apart from the best: the lowest mean
    loss, plus every input whose loss the best does not beat at `size`
    (one-sided Diebold-Mariano). A pairwise stand-in for the Model Confidence
    Set the US build takes from the arch package."""
    if losses.shape[0] < 36 or losses.shape[1] < 2:
        return set(losses.columns)
    best = losses.mean().idxmin()
    keep = {best}
    for k in losses.columns:
        if k != best:
            p = loss_test(losses[best].to_numpy(), losses[k].to_numpy())
            if not (math.isfinite(p) and p < size):
                keep.add(k)
    return keep


def scored(frame: pd.DataFrame, fc: pd.DataFrame, start: date = I.EVAL_START) -> np.ndarray:
    """The rows every score uses: printed, not masked, from `start`, with a
    benchmark forecast."""
    months = np.array([m >= start for m in frame.index])
    return (months & ~frame["masked"].to_numpy(bool) & ~frame["live"].to_numpy(bool)
            & np.isfinite(frame["y"].to_numpy(float))
            & np.isfinite(fc[f"f_{M.BENCH}"].to_numpy(float)))


def model_scores(frame: pd.DataFrame, fc: pd.DataFrame, key: str, rows: np.ndarray) -> dict:
    """Scores for one model against no change on the same months."""
    f = fc[f"f_{key}"].to_numpy(float)
    use = rows & np.isfinite(f)
    y = frame["y"].to_numpy(float)[use]
    fb = fc[f"f_{M.BENCH}"].to_numpy(float)[use]
    fm = f[use]
    pub = frame["y_pub"].to_numpy(float)[use]
    return {
        "n": int(use.sum()),
        "first": frame.index[use][0] if use.any() else None,
        "er": error_reduction(y - fm, y - fb),
        "p_cw": clark_west(y, fb, fm),
        "dir": direction_right(fm, pub),
        "dir_base": base_rate(pub),
    }


def rolling_error_reduction(frame: pd.DataFrame, fc: pd.DataFrame, rows: np.ndarray,
                            months: int = I.ROLLING_MONTHS) -> list[tuple[date, float]]:
    use = rows & np.isfinite(fc[f"f_{M.COMBO}"].to_numpy(float))
    idx = frame.index[use]
    y = frame["y"].to_numpy(float)[use]
    ec = y - fc[f"f_{M.COMBO}"].to_numpy(float)[use]
    eb = y - fc[f"f_{M.BENCH}"].to_numpy(float)[use]
    return [(idx[i], error_reduction(ec[i - months + 1:i + 1], eb[i - months + 1:i + 1]))
            for i in range(months - 1, len(idx))]
