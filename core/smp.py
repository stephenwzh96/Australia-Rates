"""The Statement on Monetary Policy forecast table, with its revisions -- the
Australian twin of the US tab's SEP median table.

Rows are the forecasts a rates desk reads (growth, the labour market, wages,
inflation and the cash-rate assumption); columns the Statement's half-yearly
horizons; each value carries its change against the previous round on the
same horizon. Colours follow the US table: a revision that implies a tighter
outlook (higher inflation, growth or cash rate, lower unemployment) is the
hawkish colour.

The cash-rate assumption gets its own view. The RBA publishes no dots: its
forecasts assume the cash rate follows MARKET pricing on the day they are
finalised. So the useful read is not "where does the Board see rates" but how
far the market has moved since the Statement was written -- the assumption,
the previous round's, and the IB strip today on the same horizons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Metric:
    match: str               # prefix of the Statement's row label
    label: str
    higher_is_tighter: int   # +1: a higher number is the hawkish revision


METRICS: tuple[Metric, ...] = (
    Metric("Gross domestic product", "GDP growth", +1),
    Metric("Household consumption", "Household consumption", +1),
    Metric("Unemployment rate", "Unemployment rate", -1),
    Metric("Hours-based underutilisation", "Underutilisation (hours)", -1),
    Metric("Wage Price Index", "Wage Price Index", +1),
    Metric("Consumer Price Index", "CPI inflation", +1),
    Metric("Trimmed mean", "Trimmed mean inflation", +1),
    Metric("Cash rate", "Cash rate assumption", +1),
)

CASH_RATE = "Cash rate assumption"


@dataclass(frozen=True)
class Cell:
    value: float | None
    change: float | None


@dataclass(frozen=True)
class Row:
    metric: Metric
    cells: list[Cell]


@dataclass(frozen=True)
class Table:
    columns: list[str]
    rows: list[Row]
    latest: str = ""
    prior: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.columns and self.rows)

    def value(self, label: str, column: str) -> float | None:
        row = next((r for r in self.rows if r.metric.label == label), None)
        if row is None or column not in self.columns:
            return None
        return row.cells[self.columns.index(column)].value


def _find(rows: dict[str, list[float | None]], match: str) -> list[float | None] | None:
    return next((v for k, v in rows.items() if k.startswith(match)), None)


def build(latest, prior=None) -> Table:
    """The table from two `data.rba_smp.Round`s (prior may be None)."""
    if latest is None:
        return Table([], [])
    out = []
    for m in METRICS:
        vals = _find(latest.rows, m.match)
        if vals is None:
            continue
        before = dict(zip(prior.horizons, _find(prior.rows, m.match) or [])) if prior else {}
        out.append(Row(m, [Cell(v, None if v is None or before.get(h) is None
                                else round(v - before[h], 2))
                           for h, v in zip(latest.horizons, vals)]))
    return Table(list(latest.horizons), out, latest.label, prior.label if prior else "")


_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                                        "Sep", "Oct", "Nov", "Dec"), start=1)}


def horizon_end(label: str) -> date | None:
    """"Dec 2026" -> 2026-12-01, the quarter's last month."""
    try:
        mon, year = label.split()
        return date(int(year), _MONTHS[mon], 1)
    except (ValueError, KeyError):
        return None


@dataclass(frozen=True)
class PathPoint:
    horizon: str
    latest: float | None
    prior: float | None
    market: float | None      # the IB strip today, averaged over the horizon's quarter


def cash_path(latest, prior, ib_by_month: dict[date, float], as_of: date) -> list[PathPoint]:
    """The cash-rate assumption by horizon, beside the previous round's and the
    strip's average over each horizon's quarter (where the strip reaches)."""
    if latest is None:
        return []
    now = dict(zip(latest.horizons, _find(latest.rows, "Cash rate") or []))
    before = dict(zip(prior.horizons, _find(prior.rows, "Cash rate") or [])) if prior else {}
    out = []
    for h in latest.horizons:
        end = horizon_end(h)
        market = None
        if end is not None and end >= date(as_of.year, as_of.month, 1):
            months = [date(end.year + (end.month - k - 1) // 12, (end.month - k - 1) % 12 + 1, 1)
                      for k in range(3)]
            got = [ib_by_month[m] for m in months if m in ib_by_month]
            market = round(sum(got) / len(got), 3) if got else None
        out.append(PathPoint(h, now.get(h), before.get(h), market))
    return out
