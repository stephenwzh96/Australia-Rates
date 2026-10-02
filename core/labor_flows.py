"""The monthly change in the unemployment rate, split into the gross flows.

The FOMC version's identity, unchanged:

    u_t - u_{t-1} = (EU + NU - UE - UN) / L_t  +  U_{t-1} (1/L_t - 1/L_{t-1})  +  residual

* Job Finding   -UE / L   unemployed who found work
* Separations   +EU / L   employed who lost or left a job and are looking
* Drop Out (UN) -UN / L   unemployed who stopped looking
* Enter (NU)    +NU / L   people outside the labour force who started looking
* Participation  U_{t-1} (1/L_t - 1/L_{t-1})
* Residual      the rate change less all of the above

What differs is how the flows are built. The BLS publishes seasonally adjusted
flows that are benchmarked to the stocks. The ABS publishes its gross flows
(LMS1) unadjusted and for the MATCHED sample only -- the seven-eighths of
respondents interviewed both months -- so the raw counts are neither seasonal-
ly adjusted nor on the population scale. Each flow is therefore rebuilt as

    flow_t = rate_t x stock_{t-1}

a matched-sample transition rate (with stable seasonals removed) times last
month's seasonally adjusted stock from the published series. That is the
standard construction (Shimer 2012) and it keeps every flow on the scale of
the unemployment rate it explains. The residual then carries the rotation
groups, the seasonal adjustment of the stocks, and each population rebenchmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SMOOTH_MONTHS = 3
BP = 10_000.0

COMPONENTS: tuple[tuple[str, str], ...] = (
    ("job_finding", "Job Finding"),
    ("separations", "Separations"),
    ("drop_out", "Drop Out (UN)"),
    ("enter", "Enter (NU)"),
    ("participation", "Participation"),
    ("residual", "Residual"),
)

#: The transition rates the decomposition reads (fraction per month, by origin).
RATES = ("UE", "EU", "UN", "NU")
#: The seasonally adjusted stocks ('000): labour force, unemployed, employed,
#: and not in the labour force.
STOCKS = ("L", "U", "E", "N")

Series = list[tuple[date, float]]


def _index(d: date) -> int:
    return d.year * 12 + d.month - 1


def _prior(d: date) -> date:
    m = _index(d) - 1
    return date(m // 12, m % 12 + 1, 1)


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    monthly: list[tuple[date, float]]
    smoothed: list[tuple[date, float]]

    @property
    def ok(self) -> bool:
        return bool(self.monthly)


@dataclass(frozen=True)
class Decomposition:
    components: list[Component]
    change: list[tuple[date, float]]        # the rate change itself, bp

    @property
    def ok(self) -> bool:
        return any(c.ok for c in self.components)

    @property
    def latest(self) -> date | None:
        return self.change[-1][0] if self.change else None


def _smooth(monthly: list[tuple[date, float]], window: int) -> list[tuple[date, float]]:
    """Trailing mean over `window` CONSECUTIVE months, dated on the last; a
    missing month breaks the run rather than shortening the average."""
    out = []
    for i in range(window - 1, len(monthly)):
        run = monthly[i - window + 1:i + 1]
        if _index(run[-1][0]) - _index(run[0][0]) == window - 1:
            out.append((run[-1][0], sum(v for _, v in run) / window))
    return out


def decompose(rates: dict[str, Series], stocks: dict[str, Series]) -> Decomposition:
    """Each month's contributions, in bp of the unemployment rate.

    `rates` are monthly transition rates (fractions) by origin -> destination,
    `stocks` the seasonally adjusted levels, both dated on the first of the
    month. A month needs every rate, and the stocks for it and the month before."""
    r = {k: dict(rates.get(k, [])) for k in RATES}
    s = {k: dict(stocks.get(k, [])) for k in STOCKS}
    rows: dict[str, list[tuple[date, float]]] = {key: [] for key, _ in COMPONENTS}
    change: list[tuple[date, float]] = []
    for d in sorted(s["L"]):
        p = _prior(d)
        need = (s["L"].get(p), s["U"].get(p), s["E"].get(p), s["N"].get(p), s["U"].get(d))
        if any(x is None for x in need) or any(r[k].get(d) is None for k in RATES):
            continue
        L, Lp, U, Up, Ep, Np = s["L"][d], s["L"][p], s["U"][d], s["U"][p], s["E"][p], s["N"][p]
        if not (L and Lp):
            continue
        ue, un = r["UE"][d] * Up, r["UN"][d] * Up
        eu, nu = r["EU"][d] * Ep, r["NU"][d] * Np
        du = (U / L - Up / Lp) * BP
        parts = {"job_finding": -ue / L * BP, "separations": eu / L * BP,
                 "drop_out": -un / L * BP, "enter": nu / L * BP,
                 "participation": Up * (1 / L - 1 / Lp) * BP}
        parts["residual"] = du - sum(parts.values())
        for key, value in parts.items():
            rows[key].append((d, value))
        change.append((d, du))
    return Decomposition(
        [Component(key, label, rows[key], _smooth(rows[key], SMOOTH_MONTHS))
         for key, label in COMPONENTS],
        change)
