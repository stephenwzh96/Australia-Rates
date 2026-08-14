"""CPI hierarchy recovery, breadth, weights and the cycle split.

The hierarchy parse is the part of this module worth testing hardest, because
it is the only place here that infers structure rather than computing a number,
and every chart on the Inflation tab counts over what it returns. Get it wrong
and Bread is counted once on its own and again inside Bread and cereal
products, which nothing downstream can detect.

The fixtures below are not invented. Each one is a cut of the real June 2026
CPI holding a trap the parse actually fell into during development:

    Pork 0.27 / Lamb 0.27        a leaf whose neighbour matches it exactly
    Automotive fuel, March 2026  a leaf that matches two neighbours in ONE
                                 quarter and neither of the others
    Bread and cereal products    a parent published 0.01 below its children
    Child care, June 2020        a class whose index falls ~95% in a quarter

No network. A test that downloads a 5MB workbook is not a test of this module.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import inflation as infl

Q = [date(2025, 12, 1), date(2026, 3, 1), date(2026, 6, 1)]


def flat(rows: dict[str, list[float]]) -> dict[str, list[tuple[date, float]]]:
    """`{name: [v1, v2, v3]}` -> a three-quarter panel."""
    return {k: list(zip(Q, v)) for k, v in rows.items()}


# --------------------------------------------------------------------------
# Hierarchy
# --------------------------------------------------------------------------

# Meat and seafoods and its six real children, June 2026 values in the third
# slot. Pork and Lamb and goat both publish 0.27 in every quarter -- they are
# genuinely the same to the ABS's two decimals, so no tolerance can separate
# them and only the two-child minimum can.
MEAT = ["Meat and seafoods", "Beef and veal", "Pork", "Lamb and goat",
        "Poultry", "Other meats", "Fish and other seafood"]
MEAT_PANEL = flat({
    "Meat and seafoods":      [2.14, 2.18, 2.20],
    "Beef and veal":          [0.44, 0.46, 0.47],
    "Pork":                   [0.27, 0.27, 0.27],
    "Lamb and goat":          [0.26, 0.27, 0.27],
    "Poultry":                [0.41, 0.42, 0.42],
    "Other meats":            [0.39, 0.39, 0.40],
    "Fish and other seafood": [0.37, 0.37, 0.37],
})


def test_pork_is_a_leaf_not_a_one_child_parent():
    """Pork equals Lamb exactly; a greedy parse made Lamb its only child."""
    tree = infl.build_tree(MEAT, MEAT_PANEL)
    assert tree.children["Pork"] == []
    assert tree.children["Lamb and goat"] == []
    assert tree.children["Meat and seafoods"] == MEAT[1:]
    assert len(tree.leaves) == 6


def test_a_one_child_parent_is_never_inferred():
    """Two series, the second equal to the first. Both are leaves.

    Not a special case -- a one-child aggregate carries exactly its child's
    contribution, so there is nothing in the data to distinguish it from a
    leaf, and inferring one lets it swallow the sibling after it.
    """
    tree = infl.build_tree(["A", "B"], flat({"A": [1.0] * 3, "B": [1.0] * 3}))
    assert tree.children["A"] == []
    assert tree.leaves == ["A", "B"]


# Private motoring, where Automotive fuel (a leaf) happens to equal the two
# classes after it in March 2026 -- 3.46 against 2.12 + 1.34 -- and nowhere
# near them in December or June.
MOTOR = ["Private motoring", "Motor vehicles",
         "Spare parts and accessories for motor vehicles", "Automotive fuel",
         "Maintenance and repair of motor vehicles",
         "Other services in respect of motor vehicles"]
MOTOR_PANEL = flat({
    "Private motoring":                              [11.01, 11.25, 11.22],
    "Motor vehicles":                                [3.42, 3.46, 3.45],
    "Spare parts and accessories for motor vehicles": [0.87, 0.86, 0.87],
    "Automotive fuel":                               [3.29, 3.46, 3.39],
    "Maintenance and repair of motor vehicles":      [2.09, 2.12, 2.16],
    "Other services in respect of motor vehicles":   [1.34, 1.34, 1.35],
})


def test_a_coincidence_in_one_quarter_does_not_promote_a_leaf():
    tree = infl.build_tree(MOTOR, MOTOR_PANEL)
    assert tree.children["Automotive fuel"] == []
    assert tree.children["Private motoring"] == MOTOR[1:]


def test_one_quarter_alone_is_not_enough_to_get_it_right():
    """The March quarter on its own genuinely is ambiguous.

    Pinned so the multi-quarter requirement cannot be quietly dropped later:
    if this ever starts passing, either the data changed or a new rule is doing
    the work, and both are worth knowing.
    """
    march = {k: [v[1]] for k, v in MOTOR_PANEL.items()}
    tree = infl.build_tree(MOTOR, march)
    assert tree.children["Automotive fuel"] != []


def test_a_parent_may_sit_below_its_children_by_the_abs_rounding():
    """Bread and cereal products publishes 1.53; its four children sum to 1.54."""
    order = ["Bread and cereal products", "Bread", "Cakes and biscuits",
             "Breakfast cereals", "Other cereal products"]
    tree = infl.build_tree(order, flat({
        "Bread and cereal products": [1.51, 1.53, 1.53],
        "Bread":                     [0.60, 0.61, 0.61],
        "Cakes and biscuits":        [0.64, 0.65, 0.65],
        "Breakfast cereals":         [0.10, 0.10, 0.10],
        "Other cereal products":     [0.17, 0.18, 0.18],
    }))
    assert tree.children["Bread and cereal products"] == order[1:]


def test_depth_and_reconciliation_on_a_three_level_tree():
    order = ["Root", "GroupA", "A1", "A2", "GroupB", "B1", "B2"]
    panel = flat({"Root": [10.0] * 3, "GroupA": [6.0] * 3, "A1": [4.0] * 3,
                  "A2": [2.0] * 3, "GroupB": [4.0] * 3, "B1": [1.0] * 3,
                  "B2": [3.0] * 3})
    tree = infl.build_tree(order, panel)
    assert tree.aggregates == ["Root", "GroupA", "GroupB"]
    assert tree.leaves == ["A1", "A2", "B1", "B2"]
    assert tree.depth_of("Root") == 0
    assert tree.depth_of("GroupA") == 1
    assert tree.depth_of("B2") == 2
    at = {k: v[-1][1] for k, v in panel.items()}
    assert infl.reconciles(tree, at) == pytest.approx(0.0)


def test_reconciles_catches_a_double_count():
    """The one number that says the parse worked."""
    order = ["Root", "A", "B"]
    tree = infl.build_tree(order, flat({"Root": [3.0] * 3, "A": [1.0] * 3,
                                        "B": [2.0] * 3}))
    broken = infl.Tree(order, {n: [] for n in order})   # everything a leaf
    at = {"Root": 3.0, "A": 1.0, "B": 2.0}
    assert infl.reconciles(tree, at) == pytest.approx(0.0)
    assert infl.reconciles(broken, at) == pytest.approx(3.0)


def test_an_empty_or_unparseable_workbook_does_not_raise():
    assert infl.build_tree([], {}).leaves == []
    junk = infl.build_tree(["A", "B", "C"],
                           flat({"A": [5.0] * 3, "B": [1.0] * 3, "C": [1.0] * 3}))
    assert junk.leaves == ["A", "B", "C"]      # nothing balances; all leaves


# --------------------------------------------------------------------------
# Rates of change
# --------------------------------------------------------------------------

def test_annualised_compounds_rather_than_multiplying_by_four():
    """1% in a quarter is 4.06% annualised, not 4.00%.

    The breadth charts count items against a fixed threshold, so a systematic
    bias of the wrong sign moves items across the line and changes the count.
    """
    series = [(date(2025, 3, 1), 100.0), (date(2025, 6, 1), 101.0)]
    (_, v), = infl.annualised(series)
    assert v == pytest.approx(4.0604, abs=1e-4)


QUARTERS = [date(2025, 6, 1), date(2025, 9, 1), date(2025, 12, 1),
            date(2026, 3, 1), date(2026, 6, 1)]


def test_year_ended_is_a_four_quarter_change():
    (_, v), = infl.year_ended(list(zip(QUARTERS, [100.0, 101.0, 102.0,
                                                  103.0, 104.0])))
    assert v == pytest.approx(4.0)


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------

def test_index_point_contributions_recovers_the_weight():
    """A contribution is `weight * index`, so the weight divides straight out.

    Checked by round trip: the reconstructed contribution at the base date is
    the published one, and at any other date it is the same weight against
    that date's index.
    """
    idx = {"X": [(date(2025, 6, 1), 80.0), (date(2026, 6, 1), 100.0)]}
    out = infl.index_point_contributions(idx, {"X": 5.0}, date(2026, 6, 1))
    assert dict(out["X"])[date(2026, 6, 1)] == pytest.approx(5.0)
    assert dict(out["X"])[date(2025, 6, 1)] == pytest.approx(4.0)


def test_a_class_with_no_reading_at_the_base_date_is_dropped():
    idx = {"X": [(date(2025, 6, 1), 80.0)]}
    assert infl.index_point_contributions(idx, {"X": 5.0}, date(2026, 6, 1)) == {}


# --------------------------------------------------------------------------
# Breadth
# --------------------------------------------------------------------------

def test_breadth_counts_and_weights_can_disagree():
    """Three classes over threshold by count, one by weight -- and it is heavy."""
    d0, d1 = date(2026, 3, 1), date(2026, 6, 1)
    idx = {"hot": [(d0, 100.0), (d1, 102.0)],       # 8.2% annualised
           "cold1": [(d0, 100.0), (d1, 100.1)],
           "cold2": [(d0, 100.0), (d1, 100.1)]}
    w = {"hot": [(d0, 90.0), (d1, 90.0)],
         "cold1": [(d0, 5.0), (d1, 5.0)], "cold2": [(d0, 5.0), (d1, 5.0)]}
    (pt,) = infl.breadth(idx, w, threshold=3.0)
    assert (pt.n_above, pt.n_total) == (1, 3)
    assert pt.share_by_count == pytest.approx(100 / 3)
    assert pt.share_by_weight == pytest.approx(90.0)


def test_breadth_counts_whatever_is_live_in_each_quarter():
    """A share is a ratio, so a class arriving mid-series is not a break."""
    d0, d1 = date(2026, 3, 1), date(2026, 6, 1)
    idx = {"a": [(date(2025, 12, 1), 100.0), (d0, 100.0), (d1, 104.0)],
           "late": [(d0, 100.0), (d1, 100.0)]}
    pts = infl.breadth(idx, {}, threshold=3.0)
    assert [(p.when, p.n_total) for p in pts] == [(d0, 1), (d1, 2)]


# --------------------------------------------------------------------------
# Composition and the cycle split
# --------------------------------------------------------------------------

def test_sub_index_holds_membership_constant():
    """A class entering mid-series must not step the level.

    The regression: summing whatever was live each quarter put a jump into the
    aggregate the moment Tertiary education or Deposit and loan facilities
    arrived, and the year-ended change read that jump as 900 per cent.
    """
    d = [date(2025, 3, 1), date(2025, 6, 1), date(2025, 9, 1)]
    points = {"a": list(zip(d, [10.0, 10.0, 10.0])),
              "late": [(d[2], 50.0)]}
    out = infl.sub_index(points, ["a", "late"])
    assert out == [(d[2], 60.0)]
    assert infl.sub_index(points, ["a"]) == list(zip(d, [10.0] * 3))


def test_sub_index_survives_a_class_collapsing_to_zero():
    """Free child care, June 2020: a 95% fall and a full recovery.

    The construction this replaced compounded each member's quarterly ratio to
    the fourth power, so the recovery quarter alone produced 15,000 per cent.
    """
    d = [date(2020, 3, 1), date(2020, 6, 1), date(2020, 9, 1)]
    points = {"childcare": list(zip(d, [0.83, 0.04, 0.83])),
              "rest": list(zip(d, [24.0, 24.0, 24.0]))}
    level = infl.sub_index(points, ["childcare", "rest"])
    assert [v for _, v in level] == pytest.approx([24.83, 24.04, 24.83])


def test_contribution_to_change_divides_by_the_previous_total():
    d0, d1 = date(2026, 3, 1), date(2026, 6, 1)
    out = infl.contribution_to_change({"bucket": [(d0, 10.0), (d1, 11.0)]},
                                      [(d0, 100.0), (d1, 101.0)])
    assert dict(out["bucket"])[d1] == pytest.approx(1.0)


def test_unclaimed_classes_land_in_the_residual():
    d = date(2026, 6, 1)
    points = {"a": [(d, 1.0)], "b": [(d, 2.0)], "orphan": [(d, 4.0)]}
    out = infl.bucket_contributions(points, [infl.Bucket("named", ["a", "b"])],
                                    ["a", "b", "orphan"])
    assert dict(out["named"])[d] == pytest.approx(3.0)
    assert dict(out[infl.RESIDUAL])[d] == pytest.approx(4.0)


def test_cycle_split_partitions_the_leaves():
    flatv = lambda a, b: [a, a, a, a, b]     # four quiet quarters, then a move
    points = {n: list(zip(QUARTERS, v)) for n, v in
              {"c1": flatv(10.0, 11.0), "c2": flatv(10.0, 10.5),
               "n1": flatv(10.0, 10.0), "n2": flatv(10.0, 10.2)}.items()}
    split = infl.cycle_split(points, ["c1", "c2"], ["c1", "c2", "n1", "n2"])
    assert (split.n_cyclical, split.n_non_cyclical) == (2, 2)
    assert split.ok
    assert dict(split.cyclical)[QUARTERS[-1]] == pytest.approx(7.5)
    assert dict(split.non_cyclical)[QUARTERS[-1]] == pytest.approx(1.0)


def test_a_name_no_longer_in_the_basket_is_ignored_not_fatal():
    """The classification file is hand-edited and the ABS renames classes."""
    points = {"c1": list(zip(QUARTERS, [10.0] * 4 + [11.0])),
              "n1": list(zip(QUARTERS, [10.0] * 5))}
    split = infl.cycle_split(points, ["c1", "Renamed away"], ["c1", "n1"])
    assert split.n_cyclical == 1
    assert split.ok


# --------------------------------------------------------------------------
# Classification file
# --------------------------------------------------------------------------

def test_the_shipped_classification_names_only_real_classes():
    """Every name in the file must be an expenditure class the ABS publishes.

    The file is edited by hand, and a typo would silently move a class into the
    residual rather than raising anything.
    """
    root = Path(__file__).resolve().parents[1]
    buckets, cyclical = infl.load_classification(
        str(root / "meetings" / "_cpi_classification.json"))
    assert buckets and cyclical

    classes = set((root / "tests" / "fixtures" / "cpi_classes.txt")
                  .read_text(encoding="utf-8").split("\n")) - {""}
    named = [m for b in buckets for m in b.members]
    assert set(named) - classes == set()
    assert set(cyclical) - classes == set()
    assert len(named) == len(set(named)), "a class is claimed by two buckets"
    assert classes - set(named) == set(), "a class no bucket claims"


def test_a_missing_classification_file_is_not_an_exception():
    assert infl.load_classification("/nonexistent/_cpi.json") == ([], [])


def test_prose_keys_are_not_buckets():
    import json
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"_comment": ["not a bucket"],
                   "composition": {"_comment": ["nor this"], "Real": ["A"]},
                   "cyclical": {"_comment": ["nor this"], "members": ["A"]}}, fh)
        path = fh.name
    buckets, cyclical = infl.load_classification(path)
    assert [b.name for b in buckets] == ["Real"]
    assert cyclical == ["A"]
