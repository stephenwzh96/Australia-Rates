"""Monthly CPI arithmetic, the item tree, and the quarterly dispersion."""

from datetime import date

from core import cpi_monthly as cm
from core import dispersion


def test_target_pace_compounds_to_the_band_midpoint():
    assert abs((1 + cm.target_pace(12) / 100) ** 12 - 1.025) < 1e-12
    assert abs((1 + cm.target_pace(4) / 100) ** 4 - 1.025) < 1e-12


def test_pct_skips_a_missing_month():
    lv = [(date(2025, 8, 1), 100.0), (date(2025, 9, 1), 101.0), (date(2025, 11, 1), 102.0)]
    assert cm._pct(lv) == [(date(2025, 9, 1), (101 / 100 - 1) * 100)]


def test_tree_keeps_only_the_expenditure_branch():
    codelist = [("10001", "All groups CPI", ""), ("20001", "Food", "10001"),
                ("30002", "Bread and cereal", "20001"), ("40005", "Bread", "30002"),
                ("999900", "Underlying", ""), ("999902", "Trimmed Mean", "999900")]
    nodes = cm.tree(codelist)
    assert [(n.code, n.level) for n in nodes] == [("10001", 0), ("20001", 1), ("30002", 2),
                                                 ("40005", 3)]


def test_breakdown_reads_the_sa_headline_for_all_groups():
    nodes = cm.tree([("10001", "All groups CPI", ""), ("20001", "Food", "10001")])
    months = [date(2026, m, 1) for m in (6, 7, 8)]
    panel = {cm.key(cm.INDEX, cm.HEADLINE_SA, cm.SA): list(zip(months, [100.0, 101.0, 101.5])),
             cm.key(cm.PCT_PERIOD, "20001", cm.ORIGINAL): list(zip(months, [0.1, 0.2, 0.3]))}
    b = cm.breakdown(panel, nodes, {"20001": 17.0})
    assert b.months == [date(2026, 8, 1), date(2026, 7, 1)]
    root, food = b.rows
    assert root.row.basis == "SA" and abs(root.values[0] - (101.5 / 101 - 1) * 100) < 1e-12
    assert food.row.basis == "original" and food.values == [0.3, 0.2]


def test_quarterly_dispersion_reconciles_on_its_own_weights():
    quarters = [date(y, m, 1) for y in range(2017, 2027) for m in (3, 6, 9, 12)][:38]
    classes, weights = {}, {}
    for i, name in enumerate(("a", "b", "c", "d")):
        level, rows = 100.0, []
        for k, q in enumerate(quarters):
            level *= 1 + (0.3 + 0.2 * i + 0.05 * (k % 3)) / 100
            rows.append((q, level))
        classes[name] = rows
        weights[name] = [(q, 10.0 + i) for q in quarters]
    disp = dispersion.from_classes(classes, weights)
    pts = [p for r in disp.regimes for p in r.points]
    assert disp.dropped == 0 and pts
    assert all(abs(p.residual_bp) < 1e-9 and abs(p.coverage - 100.0) < 1e-9 for p in pts)
