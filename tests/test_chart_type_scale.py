"""Every chart title is set in the one type scale (`ui.charts.TITLE_SIZE` and
`SUBTITLE_SIZE`), whichever library draws it.

Charts had drifted to five title sizes -- 12, 13, 14, 15 and 17px -- because
each set its own. A new chart that hard-codes a title size fails here; use
`charts.title_font(p)` / `charts.subtitle_font(p)` (Plotly) or
`charts.vintage_title` / `TITLE_SIZE` (Altair).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ui import charts
from ui.theme import DARK

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART_FILES = [ROOT / "ui" / "charts.py", ROOT / "ui" / "contributions.py"]

#: A title= that is really a legend line, not a chart title: the labour
#: channel chart names its series in their colours where the title would sit.
LEGEND_TITLES = {"labor_channels"}


AXIS_CALLS = {"update_xaxes", "update_yaxes"}


def _title_dicts(path: pathlib.Path):
    """(function name, node) for every chart `title=dict(...)` / `subtitle=dict(...)`.
    An axis's title (`update_xaxes(title=dict(...))`) is not a chart title."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call) or getattr(call.func, "attr", "") in AXIS_CALLS:
                continue
            for kw in call.keywords:
                if (kw.arg in ("title", "subtitle") and isinstance(kw.value, ast.Call)
                        and getattr(kw.value.func, "id", "") == "dict"):
                    yield fn.name, kw.value


def _literal_font_size(call: ast.Call) -> float | None:
    for kw in call.keywords:
        if kw.arg == "font" and isinstance(kw.value, ast.Call) \
                and getattr(kw.value.func, "id", "") == "dict":
            for f in kw.value.keywords:
                if f.arg == "size" and isinstance(f.value, ast.Constant):
                    return f.value.value
    return None


@pytest.mark.parametrize("path", CHART_FILES, ids=lambda p: p.name)
def test_no_plotly_title_hard_codes_its_size(path):
    offenders = [f"{fn} (line {call.lineno}): size={size}"
                 for fn, call in _title_dicts(path)
                 if fn not in LEGEND_TITLES
                 and (size := _literal_font_size(call)) is not None]
    assert not offenders, "use charts.title_font / subtitle_font: " + "; ".join(offenders)


@pytest.mark.parametrize("path", CHART_FILES, ids=lambda p: p.name)
def test_no_altair_title_hard_codes_its_size(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [f"line {n.lineno}" for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "TitleParams"
                 for kw in n.keywords
                 if kw.arg in ("fontSize", "subtitleFontSize") and isinstance(kw.value, ast.Constant)]
    assert not offenders, "use charts.TITLE_SIZE / SUBTITLE_SIZE: " + ", ".join(offenders)


def test_the_helpers_carry_the_scale():
    assert charts.title_font(DARK) == {"size": charts.TITLE_SIZE, "color": DARK.ink,
                                       "weight": charts.TITLE_WEIGHT}
    assert charts.subtitle_font(DARK)["size"] == charts.SUBTITLE_SIZE
    t = charts.vintage_title("x", charts.Vintage(last_modified=__import__("datetime").date(2026, 9, 30)),
                             DARK).to_dict()
    assert t["fontSize"] == charts.TITLE_SIZE and t["subtitleFontSize"] == charts.SUBTITLE_SIZE
