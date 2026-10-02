"""One call from data to everything the tab shows: `run(as_of)`."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from nextprint import evaluate as E
from nextprint import fetch
from nextprint import inputs as I
from nextprint import model as M
from nextprint import panel as P


@dataclass
class Result:
    as_of: date
    ok: bool
    message: str = ""
    live: dict = field(default_factory=dict)
    drivers: list[dict] = field(default_factory=list)
    track: dict = field(default_factory=dict)
    rolling: list[tuple[date, float]] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    table: list[dict] = field(default_factory=list)
    robustness: list[dict] = field(default_factory=list)
    challenger: dict = field(default_factory=dict)


def _probs_summary(probs: dict[int, float] | None, prior_print: float) -> dict:
    if not probs:
        return {}
    likely = max(probs, key=probs.get)
    return {
        "buckets": [(prior_print + b / 10, b, probs[b]) for b in M.BUCKETS],
        "likely_print": prior_print + likely / 10,
        "likely_p": probs[likely],
        "p_up": sum(v for b, v in probs.items() if b > 0),
        "p_down": sum(v for b, v in probs.items() if b < 0),
        "p_flat": probs.get(0, 0.0),
    }


def _band_fields(band: dict[float, tuple[float, float]] | None) -> dict:
    """{0.7: (lo, hi), ...} -> {"lo70": lo, "hi70": hi, ...}; NaN without one."""
    out = {}
    for c in M.BANDS:
        lo, hi = band[c] if band else (float("nan"), float("nan"))
        out[f"lo{round(100 * c)}"], out[f"hi{round(100 * c)}"] = lo, hi
    return out


def coverage(frame: pd.DataFrame, rows: np.ndarray,
             bands: list[dict[float, tuple[float, float]] | None]) -> dict[float, tuple[float, int]]:
    """Share of scored prints whose unrounded rate fell inside each band, and
    how many were scored. A well-built 70% band holds about 70%."""
    new_u = frame["new_u"].to_numpy(float)
    use = [i for i in np.flatnonzero(rows) if bands[i] and math.isfinite(new_u[i])]
    return {c: (float(np.mean([bands[i][c][0] <= new_u[i] <= bands[i][c][1] for i in use]))
                if use else float("nan"), len(use)) for c in M.BANDS}


def history(frame: pd.DataFrame, fc: pd.DataFrame, rows: np.ndarray,
            probs: list[dict[int, float] | None],
            bands: list[dict[float, tuple[float, float]] | None] | None = None) -> list[dict]:
    """Forecast against print, month by month: every scored month plus the live
    one (which has no print yet unless the as-of date is past its release)."""
    f = fc[f"f_{M.COMBO}"].to_numpy(float)
    live = frame["live"].to_numpy(bool) & ~frame["masked"].to_numpy(bool)
    out = []
    for i in np.flatnonzero((rows | live) & np.isfinite(f)):
        row, p = frame.iloc[i], probs[i]
        out.append({
            "month": frame.index[i], "live": bool(live[i]),
            "forecast": float(f[i]), "actual": float(row["y_pub"]), "actual_u": float(row["y"]),
            "prior_print": float(row["prior_print"]), "new_print": float(row["new_print"]),
            "new_u": float(row["new_u"]),
            # The forecast as a level: the unrounded last rate plus the change.
            "fc_level": M._anchor(float(row["prior_u"]), float(row["prior_print"])) + float(f[i]),
            **_band_fields(bands[i] if bands else None),
            # Odds rather than the likeliest bucket: the end buckets are open
            # tails, so the likeliest is often "0.2 or more", not a print.
            **({"p_up": sum(v for b, v in p.items() if b > 0),
                "p_flat": p.get(0, 0.0),
                "p_down": sum(v for b, v in p.items() if b < 0),
                "probs": dict(p)} if p else {}),
            "release": row["release"],
        })
    return out


def challenger(frame: pd.DataFrame, fc: pd.DataFrame, rows: np.ndarray) -> dict:
    """The ridge challenger against the average: over the backtest (seen when
    it was chosen, so context only) and over the prints released after it was
    registered (unseen -- the only ones the switching rule counts)."""
    y = frame["y"].to_numpy(float)
    fr, fcomb = fc[f"f_{M.RIDGE}"].to_numpy(float), fc[f"f_{M.COMBO}"].to_numpy(float)
    both = rows & np.isfinite(fr) & np.isfinite(fcomb)
    after = np.array([isinstance(r, date) and r > I.CHALLENGER_REGISTERED
                      for r in frame["release"]])
    new = both & after

    def vs(use: np.ndarray) -> dict:
        n = int(use.sum())
        if not n:
            return {"n": 0, "er": float("nan"), "er_bench": float("nan"), "p": float("nan")}
        er, eo = y[use] - fr[use], y[use] - fcomb[use]
        return {"n": n, "er": E.error_reduction(er, eo), "er_bench": E.error_reduction(er, y[use]),
                "p": E.diebold_mariano(er, eo)}

    unseen = vs(new)
    return {"registered": I.CHALLENGER_REGISTERED, "backtest": vs(both), "unseen": unseen,
            "verdict": E.challenger_verdict(unseen["n"], unseen["er"], unseen["p"]),
            "min_prints": I.CHALLENGER_MIN_PRINTS, "p_needed": I.CHALLENGER_P}


def evaluate_all(frame: pd.DataFrame, fc: pd.DataFrame, fc_expanding: pd.DataFrame) -> dict:
    """Track record, the input table and robustness, all out of sample."""
    rows = E.scored(frame, fc)
    y = frame["y"].to_numpy(float)
    pub = frame["y_pub"].to_numpy(float)

    combo = E.model_scores(frame, fc, M.COMBO, rows)
    # Probability skill, combination vs benchmark, on months both can price.
    pc = M.probability_track(frame, fc, M.COMBO)
    pb = M.probability_track(frame, fc, M.BENCH)
    both = [i for i in np.flatnonzero(rows) if pc[i] and pb[i]]
    bs_c = E.brier([pc[i] for i in both], pub[both])
    bs_b = E.brier([pb[i] for i in both], pub[both])
    p_up = np.array([sum(v for b, v in pc[i].items() if b > 0) for i in both])
    bands = M.interval_track(frame, fc, M.COMBO)
    track = {**combo, "brier_skill": 1 - bs_c / bs_b if bs_b else float("nan"),
             "brier_n": len(both),
             "reliability": E.reliability(p_up, pub[both] > 0.05),
             "coverage": coverage(frame, rows, bands)}

    common = E.scored(frame, fc, I.COMMON_START)
    common &= np.all([np.isfinite(fc[f"f_{k}"].to_numpy(float)) for k in M.KEYS], axis=0)
    losses = pd.DataFrame({k: (y - fc[f"f_{k}"].to_numpy(float))[common] ** 2 for k in M.KEYS})
    tied = E.confidence_set(losses)

    table = []
    for inp in I.INPUTS:
        own = E.model_scores(frame, fc, inp.key, rows)
        com = E.model_scores(frame, fc, inp.key, common)
        coef = fc[f"c_{inp.key}"].to_numpy(float)[rows]
        coef = coef[np.isfinite(coef)]
        long_sample = own["first"] is not None and own["first"] <= date(1995, 12, 1)
        table.append({
            "key": inp.key, "label": inp.label, "channel": inp.channel,
            "er_long": own["er"] if long_sample else float("nan"),
            "er_common": com["er"],
            "dir": own["dir"], "dir_base": own["dir_base"],
            "p_cw": own["p_cw"], "tied": inp.key in tied,
            "first": own["first"], "n": own["n"],
            "sign_ok": float(np.mean(np.sign(coef) == inp.sign)) if len(coef) else float("nan"),
        })
    table.sort(key=lambda r: -(r["er_common"] if math.isfinite(r["er_common"]) else -9))

    # Robustness of the combination's edge.
    def er_on(mask: np.ndarray, f: pd.DataFrame = fc) -> float:
        use = mask & np.isfinite(f[f"f_{M.COMBO}"].to_numpy(float))
        return E.error_reduction(y[use] - f[f"f_{M.COMBO}"].to_numpy(float)[use],
                                 y[use] - f[f"f_{M.BENCH}"].to_numpy(float)[use])

    months = np.array(frame.index)
    idx = np.flatnonzero(rows)
    half = np.zeros(len(frame), bool)
    if len(idx):
        half[idx[: len(idx) // 2]] = True
    gfc = np.array([date(2008, 7, 1) <= m <= date(2009, 12, 1) for m in months])
    exp_rows = E.scored(frame, fc_expanding)
    robustness = [
        {"label": f"Headline (rolling {I.WINDOW_YEARS}y window)", "er": er_on(rows)},
        {"label": "Expanding window", "er": er_on(exp_rows, fc_expanding)},
        {"label": "First half of the sample", "er": er_on(rows & half)},
        {"label": "Second half of the sample", "er": er_on(rows & ~half)},
        {"label": "Excluding the GFC (2008-09)", "er": er_on(rows & ~gfc)},
        {"label": f"Since {I.COMMON_START.year} (all inputs live)", "er": er_on(common)},
    ]
    return {"track": track, "table": table, "robustness": robustness,
            "rolling": E.rolling_error_reduction(frame, fc, rows),
            "history": history(frame, fc, rows, pc, bands),
            "challenger": challenger(frame, fc, rows),
            "probs_combo": pc}


def run(as_of: date, data: dict | None = None, next_release: date | None = None) -> Result:
    data = data if data is not None else fetch.load_all()
    if not data["lf"].get(I.UNEMPLOYED):
        return Result(as_of, False, "The Labour Force history could not be loaded (network).")
    panel = P.build(data, as_of, next_release=next_release)
    frame = panel.frame
    fc = M.forecasts(frame)
    fc_exp = M.forecasts(frame, window_years=None)
    ev = evaluate_all(frame, fc, fc_exp)

    live = {}
    drivers: list[dict] = []
    if panel.live_month is not None:
        i = frame.index.get_loc(panel.live_month)
        row, f = frame.iloc[i], fc.iloc[i]
        fb, fcomb = f[f"f_{M.BENCH}"], f[f"f_{M.COMBO}"]
        live = {
            "month": panel.live_month, "release": panel.live_release,
            "prior_print": row["prior_print"], "prior_u": row["prior_u"],
            "forecast": fcomb, "bench": fb, "n_inputs": f["n_inputs"],
            "challenger": f[f"f_{M.RIDGE}"],
            "masked": bool(row["masked"]),
            "actual": panel.live_actual,
            **_probs_summary(ev["probs_combo"][i], row["prior_print"]),
        }
        for inp in I.INPUTS:
            fk = f[f"f_{inp.key}"]
            meta = panel.timing.get(inp.key) or {}
            drivers.append({
                "key": inp.key, "label": inp.label, "unit": inp.unit,
                "driver": fk, "x": row[f"x_{inp.key}"], "normal": f[f"m_{inp.key}"],
                **meta,
            })
        drivers.sort(key=lambda d: -abs(d["driver"]) if math.isfinite(d["driver"]) else 0)

    return Result(as_of, True, live=live, drivers=drivers, track=ev["track"],
                  rolling=ev["rolling"], history=ev["history"], table=ev["table"],
                  robustness=ev["robustness"], challenger=ev["challenger"])
