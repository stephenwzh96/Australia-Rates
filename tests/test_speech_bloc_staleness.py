"""A roundup cached before the bloc re-assessment existed must not crash.

The speech roundup lives in a `@st.cache_resource`, which survives a source
change. After `Appearance` gained `bloc_now` / `bloc_note`, that cache could
still hand back instances built by the PREVIOUS version of the class: same
class name, attribute simply absent. Reading `ap.bloc_now` directly then
raised `AttributeError` in `render_roundup` -- on exactly the roundups a user
is most likely to be looking at right after an upgrade.

These tests build objects that stand in for that stale instance and assert the
three ways the app touches the fields all survive it. The helpers live in `core.roster`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core import speeches

from core import roster as _roster

_NAMES = {"_edits_to_updates": "edits_to_updates", "_num_or_none": "num_or_none", "_apply_roster_updates": "apply_roster_updates", "_roster_updates": "roster_updates", "_active_score": "active_score", "_active_bloc": "active_bloc", "_ap_bloc_now": "ap_bloc_now", "_ap_bloc_note": "ap_bloc_note", "_baseline_score": "baseline_score", "_roundup_has_blocs": "roundup_has_blocs"}


def _ns():
    return {old: getattr(_roster, new) for old, new in _NAMES.items()}


@pytest.fixture(scope="module")
def h():
    return _ns()


_REQUIRED = dict(when=None, venue="", kind="", inflation="", labour="",
                 economy="", rate_stance="", lean="neutral", quotes=(),
                 source="")


def _appearance(**kw):
    """A real `Appearance` -- the first ten fields have no defaults."""
    return speeches.Appearance(**{**_REQUIRED, **kw})


class _StaleAppearance:
    """What the old `Appearance` looked like: no bloc_now, no bloc_note."""

    def __init__(self, member="Austan Goolsbee"):
        self.member = member
        self.lean = "neutral"
        self.direction = "unchanged"
        self.delta = 0.0
        self.score_note = ""
        self.quotes = ()
        self.inflation = self.labour = self.economy = self.rate_stance = ""
        self.venue = self.kind = self.source = self.implication = ""
        self.when = None

    def new_score(self, current):
        return current


class _StaleRoundup:
    def __init__(self, appearances):
        self.appearances = tuple(appearances)


# --- the crash that was reported ------------------------------------------

def test_direct_access_is_what_broke(h):
    """Guards the premise: the bare attribute access genuinely raises."""
    stale = _StaleAppearance()
    with pytest.raises(AttributeError):
        _ = stale.bloc_now


def test_tolerant_accessors_survive_stale_appearance(h):
    stale = _StaleAppearance()
    # Empty, not a guess: "not re-assessed" is what the UI renders for it.
    assert h["_ap_bloc_now"](stale) == ""
    assert h["_ap_bloc_note"](stale) == ""


def test_tolerant_accessors_pass_through_real_values(h):
    ap = _appearance(member="Austan Goolsbee", bloc_now=" Data Dependent ",
                     bloc_note=" moved on the labour read ")
    assert h["_ap_bloc_now"](ap) == "Data Dependent"
    assert h["_ap_bloc_note"](ap) == "moved on the labour read"


def test_accessors_tolerate_none(h):
    ap = _appearance(member="X", bloc_now=None, bloc_note=None)
    assert h["_ap_bloc_now"](ap) == ""
    assert h["_ap_bloc_note"](ap) == ""


# --- cache validity probe --------------------------------------------------

def test_stale_roundup_is_detected(h):
    assert h["_roundup_has_blocs"](_StaleRoundup([_StaleAppearance()])) is False


def test_current_roundup_passes(h):
    fresh = _StaleRoundup([_appearance(member="X", bloc_now="Doves")])
    assert h["_roundup_has_blocs"](fresh) is True


def test_empty_roundup_passes(h):
    """Nothing to probe, nothing to be wrong about -- must not force a re-ask."""
    assert h["_roundup_has_blocs"](_StaleRoundup([])) is True
    assert h["_roundup_has_blocs"](object()) is True


def test_mixed_roundup_is_rejected(h):
    """Half-stale is stale: one bad appearance is enough to lose the answer."""
    mixed = _StaleRoundup([_appearance(member="X", bloc_now="Doves"),
                           _StaleAppearance()])
    assert h["_roundup_has_blocs"](mixed) is False


# --- the save direction ----------------------------------------------------

def test_saving_a_stale_appearance_does_not_crash():
    """History writes go through appearance_to_dict, which must tolerate it."""
    d = speeches.appearance_to_dict(_StaleAppearance())
    assert d["bloc_now"] == ""
    assert d["bloc_note"] == ""


def test_stale_appearance_round_trips_into_a_full_one():
    """Saved, then reloaded, an old-shape object gains the newer fields."""
    d = speeches.appearance_to_dict(_StaleAppearance(member="Austan Goolsbee"))
    ap = speeches.appearance_from_dict(d)
    assert isinstance(ap, speeches.Appearance)
    assert ap.bloc_now == ""
    assert ap.bloc_note == ""


# --- the call sites --------------------------------------------------------

def test_no_call_site_reads_the_attributes_directly():
    """The regression guard: every read must go through the tolerant helpers.

    The helpers above are only a fix if they are actually used. This walks
    app.py for `X.bloc_now` / `X.bloc_note` and fails on any hit outside the
    two helpers, where the bare access is the point.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    bad: list[str] = []
    files = [root / "app.py", *(root / "ui").glob("*.py"), *(root / "ui" / "tabs").glob("*.py")]
    for node, path in ((n, f) for f in files
                       for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in ("bloc_now", "bloc_note"):
            continue
        # Where the attribute is defined or named literally is fine; only
        # reading it off an object can raise.
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            continue
        bad.append(f"{path.name}:{node.lineno}")

    # The two helpers read `getattr`, so they should not appear at all. A
    # stray `ap.bloc_now` anywhere else is the bug this file exists for.
    assert not bad, (
        f"the UI reads .bloc_now/.bloc_note directly at {bad}; "
        "use _ap_bloc_now()/_ap_bloc_note() so a cached pre-upgrade "
        "Appearance cannot raise AttributeError.")
