"""A Taylor-rule cash-rate prescription heatmap: five r* against five u*.

The US tab reads the Atlanta Fed's Taylor Rule Utility, which publishes thirty
prescriptions (five natural-rate estimates against six measures of slack) and
the inputs behind them. Nothing like it is published for Australia, so this
grid is COMPUTED, and says so: the same question asked twenty-five ways --
"what does a balanced-approach rule say the cash rate should be?" -- across the
two contestable inputs, the real neutral rate and the NAIRU, each over the
range the RBA's own published estimates span.

    rule = r* + pi + 0.5 (pi - pi*) + 2 (u* - u)

pi is the latest year-ended trimmed mean, u the latest unemployment rate, pi*
the 2.5% band midpoint. When the whole grid sits on one side of the cash
rate, the gap between the rule and the Board does not hang on one input.
Shading uses the Atlanta Fed's own seven bands, so the two read alike.
"""

from __future__ import annotations

from dataclasses import dataclass

INFLATION_GAP_COEFF = 0.5
UNEMPLOYMENT_GAP_COEFF = 2.0
PI_STAR = 2.5

BAND_EDGES: tuple[float, ...] = (1.25, 0.75, 0.25, -0.25, -0.75, -1.25)
BAND_COUNT = len(BAND_EDGES) + 1
NEUTRAL_BAND = 3
BAND_LABELS: tuple[str, ...] = (
    "more than 125bp below the rule", "75–125bp below the rule", "25–75bp below the rule",
    "within 25bp of the rule", "25–75bp above the rule", "75–125bp above the rule",
    "more than 125bp above the rule",
)


@dataclass(frozen=True)
class Spec:
    label: str
    value: float
    gloss: str


RSTAR_ROWS: tuple[Spec, ...] = (
    Spec("0.0%", 0.0, "Low end of the RBA's model estimates of the real neutral rate"),
    Spec("0.5%", 0.5, "Below the middle of the RBA's model range"),
    Spec("1.0%", 1.0, "The middle of the RBA's model range"),
    Spec("1.5%", 1.5, "Above the middle of the RBA's model range"),
    Spec("2.0%", 2.0, "High end, near the pre-GFC average real cash rate"),
)

GAP_COLUMNS: tuple[Spec, ...] = (
    Spec("u* 4.00%", 4.00, "A NAIRU at the low end of recent estimates"),
    Spec("u* 4.25%", 4.25, "Below the RBA's central estimate"),
    Spec("u* 4.50%", 4.50, "The RBA's central NAIRU estimate"),
    Spec("u* 4.75%", 4.75, "Above the RBA's central estimate"),
    Spec("u* 5.00%", 5.00, "A NAIRU at the high end, near the 2010s estimates"),
)


def band_index(diff: float) -> int:
    for i, edge in enumerate(BAND_EDGES):
        if diff > edge:
            return i
    return len(BAND_EDGES)


def prescription(r_star: float, inflation: float, unemployment: float, u_star: float,
                 pi_star: float = PI_STAR) -> float:
    return (r_star + inflation + INFLATION_GAP_COEFF * (inflation - pi_star)
            + UNEMPLOYMENT_GAP_COEFF * (u_star - unemployment))


@dataclass(frozen=True)
class Cell:
    r_star: Spec
    gap: Spec
    rate: float
    r_star_value: float
    gap_value: float          # the unemployment gap u* - u, pp
    funds_rate: float

    @property
    def diff(self) -> float:
        return self.rate - self.funds_rate

    @property
    def diff_bp(self) -> float:
        return self.diff * 100.0

    @property
    def band(self) -> int:
        return band_index(self.diff)

    @property
    def label(self) -> str:
        return f"{self.rate:.2f}"


@dataclass(frozen=True)
class Heatmap:
    rows: list[list[Cell]]
    funds_rate: float
    inflation: float
    unemployment: float
    inflation_measure: str = "trimmed mean, year-ended"
    pi_star: float = PI_STAR
    gap_weight: float = UNEMPLOYMENT_GAP_COEFF
    smoothing: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.rows)

    @property
    def cells(self) -> list[Cell]:
        return [c for r in self.rows for c in r]

    @property
    def n(self) -> int:
        return len(self.cells)

    @property
    def above(self) -> int:
        return sum(1 for c in self.cells if c.diff > 0.25)

    @property
    def below(self) -> int:
        return sum(1 for c in self.cells if c.diff < -0.25)

    @property
    def span(self) -> tuple[float, float]:
        rates = [c.rate for c in self.cells]
        return (min(rates), max(rates)) if rates else (0.0, 0.0)

    @property
    def median_bp(self) -> float | None:
        diffs = sorted(c.diff_bp for c in self.cells)
        if not diffs:
            return None
        mid = len(diffs) // 2
        return diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2


def build(inflation: float | None, unemployment: float | None,
          funds_rate: float | None) -> Heatmap:
    if inflation is None or unemployment is None or funds_rate is None:
        return Heatmap([], funds_rate or 0.0, inflation or 0.0, unemployment or 0.0,
                       error="the trimmed mean, the unemployment rate or the cash rate is "
                             "not loaded")
    rows = [[Cell(rs, gp, prescription(rs.value, inflation, unemployment, gp.value),
                  rs.value, gp.value - unemployment, funds_rate)
             for gp in GAP_COLUMNS] for rs in RSTAR_ROWS]
    return Heatmap(rows, funds_rate, inflation, unemployment)


def worked_example(cell: Cell, heat: Heatmap) -> str:
    return (f"{cell.r_star_value:.2f} + {heat.inflation:.2f} + "
            f"{INFLATION_GAP_COEFF:g}({heat.inflation:.2f} − {heat.pi_star:g}) + "
            f"{UNEMPLOYMENT_GAP_COEFF:g}({cell.gap.value:.2f} − {heat.unemployment:.2f}) "
            f"= {cell.rate:.2f}%")


def summarise(heat: Heatmap) -> str:
    if not heat.ok:
        return ""
    lo = min(c.diff_bp for c in heat.cells)
    hi = max(c.diff_bp for c in heat.cells)
    if heat.above == heat.n:
        return (f"All {heat.n} combinations put the rule above the {heat.funds_rate:.2f}% cash "
                f"rate, by {lo:+.0f} to {hi:+.0f}bp.")
    if heat.below == heat.n:
        return (f"All {heat.n} combinations put the rule below the {heat.funds_rate:.2f}% cash "
                f"rate, by {lo:+.0f} to {hi:+.0f}bp.")
    return (f"{heat.above} of {heat.n} combinations put the rule above the "
            f"{heat.funds_rate:.2f}% cash rate, {heat.below} below; the spread runs "
            f"{lo:+.0f} to {hi:+.0f}bp.")
