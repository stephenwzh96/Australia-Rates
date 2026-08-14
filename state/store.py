"""Per-meeting persistence.

One JSON file per meeting under `meetings/<YYYY-MM-DD>.json`, keyed on the
decision date. Cloning forward carries the roster, kill criteria and checklist
structure but clears market pricing and path inputs -- a stale 8bp surviving
into a new meeting is exactly the kind of error the framework exists to prevent.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEETINGS_DIR = ROOT / "meetings"
RUNS_DIR = ROOT / "runs"
ROSTER_FILE = MEETINGS_DIR / "_roster.json"
CALENDAR_FILE = MEETINGS_DIR / "_calendar.json"
SPEECH_HISTORY_FILE = MEETINGS_DIR / "_speech_history.json"
RELEASE_CARDS_FILE = MEETINGS_DIR / "_release_cards.json"

# Kept rather than the full history: each entry embeds a whole roundup
# (sources, quotes, score reads), and a research pass is expensive enough that
# ten genuinely distinct queries already span more than a normal week's use.
MAX_SPEECH_HISTORY = 10

# Cleared when a meeting is cloned forward.
_MARKET_INPUTS = ("points", "far_leg_points")


def _read(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _write(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def meeting_path(key: str) -> Path:
    return MEETINGS_DIR / f"{key}.json"


def list_saved() -> list[str]:
    return sorted(
        p.stem for p in MEETINGS_DIR.glob("*.json") if not p.stem.startswith("_")
    )


def exists(key: str) -> bool:
    return meeting_path(key).exists()


def load(key: str) -> dict[str, Any]:
    return _read(meeting_path(key))


def save(key: str, data: dict[str, Any]) -> Path:
    p = meeting_path(key)
    _write(p, data)
    return p


def load_roster() -> list[dict[str, Any]]:
    return _read(ROSTER_FILE)["voters"]


def load_calendar() -> list[dict[str, Any]] | None:
    if CALENDAR_FILE.exists():
        return _read(CALENDAR_FILE)["meetings"]
    return None


def save_calendar(meetings: list[dict[str, Any]]) -> None:
    _write(CALENDAR_FILE, {"meetings": meetings})


def previous_key(key: str) -> str | None:
    saved = [k for k in list_saved() if k < key]
    return saved[-1] if saved else None


def clone_forward(source_key: str, target: dict[str, Any], as_of: date) -> dict[str, Any]:
    """Deep-copy a prior meeting's state onto a new meeting.

    Kept: roster, kill-criteria labels/thresholds, checklist items, sensitivity
    and Kelly grids, instrument kind, direction, side, target range, Kelly
    fraction. Cleared: points priced, far-leg points, triggered flags,
    per-meeting notes, the economic calendar, and the checklist's done/note state.
    """
    src = copy.deepcopy(load(source_key))
    out = copy.deepcopy(src)

    out["meeting"] = target
    out["as_of"] = as_of.isoformat()
    out["label"] = date.fromisoformat(target["end"]).strftime("%B %Y")
    out["cloned_from"] = source_key

    for field in _MARKET_INPUTS:
        if field in out.get("trade", {}):
            out["trade"][field] = None
        if field in out.get("structure", {}):
            out["structure"][field] = None

    # instrument_kind carries forward unchanged (e.g. "far" for "the following
    # month's fed funds future") -- the app re-derives the contract CODE fresh
    # from the new meeting's own date, so the label never names a stale month.
    # Only free-text "custom" carries forward as literal text, since there is
    # no reliable way to auto-strip a meeting reference from arbitrary prose.

    out["path"]["ladder_levels"] = []
    out["path"]["note"] = ""
    # Exit levels are quoted in bp priced, which only means anything against
    # the entry they were chosen around, and the price is a live mark. Carried
    # forward they would look like decisions already taken about a trade that
    # has not been priced yet.
    out["path"]["stop_level"] = None
    out["path"]["target_level"] = None
    out["path"]["current_price"] = None
    out["path"]["vol_override_bp"] = None

    for k in out.get("kill_criteria", []):
        k["triggered"] = False

    for c in out.get("structure", {}).get("checklist", []):
        c["done"] = False

    out["structure"]["calendar"] = []
    out["structure"]["calendar_note"] = ""
    out.pop("notes", None)      # free-text notes no longer exist anywhere
    return out


def new_meeting(target: dict[str, Any], as_of: date, roster: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A blank meeting when there is nothing to clone from."""
    end = date.fromisoformat(target["end"])
    return {
        "schema": 1,
        "meeting": target,
        "as_of": as_of.isoformat(),
        "label": end.strftime("%B %Y"),
        "roster": roster if roster is not None else load_roster(),
        "trade": {
            "instrument": "FOMC-dated OIS", "instrument_kind": "ois",
            "direction": "hike", "side": "fade",
            # sizing conventions live under "sizing"; see core.sizing
            "points": None, "size": 25.0,
            "target_range_low": None, "target_range_high": None,
            "kelly_fraction": 0.25,
        },
        "probability": {"mode": "decomposition", "p_bloc": 0.25, "p_centre": 0.40, "override_q": None},
        "sizing": {"max_drawdown": 250_000.0, "daily_limit": 50_000.0,
                   "bets_per_year": 8, "contract": "zq", "custom_dv01": None},
        "sensitivity_grid": [0.05, 0.10, 0.15, 0.20, 0.25, 0.40],
        "kelly_grid": [0.10, 0.15, 0.20, 0.25],
        "centre_grid": [0.20, 0.30, 0.40, 0.50],
        "path": {"ladder_levels": [], "stop_level": None, "target_level": None,
                 "current_price": None, "vol_override_bp": None, "vol_uncertainty": False,
                 "mark_tier1": True, "mark_tier2": False, "mark_tier3": False,
                 "no_move_target_hit": 0, "no_move_never_breached": 0,
                 "no_move_falsely_stopped": 0, "move_target_hit": 0,
                 "move_stopped_early": 0, "move_gapped_through": 0, "note": ""},
        "kill_criteria": [],
        "structure": {"spread_enabled": True, "spread_note": "", "far_leg_points": None,
                      "far_leg_size": 25.0, "scenarios": [], "calendar": [],
                      "calendar_note": "", "checklist": []},
    }


def roster_for(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Meeting-local roster if present, else the shared seed."""
    return data.get("roster") or load_roster()


def load_or_seed(key: str, meetings: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Resolve a meeting key to state: saved file, clone-forward, or blank.

    Lives here rather than in the app so the meeting-switch path is testable
    without standing up Streamlit.
    """
    if exists(key):
        state = load(key)
    else:
        target = next((m for m in meetings if m.get("end") == key), None)
        prev = previous_key(key)
        if prev and target:
            state = clone_forward(prev, target, today)
        else:
            state = new_meeting(target or {"start": key, "end": key, "verified": False}, today)
    # A meeting without a roster would silently render empty vote arithmetic.
    state.setdefault("roster", load_roster())
    return state


def save_run(key: str, markdown: str, stamp: str) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    p = RUNS_DIR / f"{key}-{stamp}.md"
    p.write_text(markdown, encoding="utf-8")
    return p


def load_speech_history() -> list[dict[str, Any]]:
    """Past speech-summary runs, newest first. `[]` if none have been saved."""
    if not SPEECH_HISTORY_FILE.exists():
        return []
    return _read(SPEECH_HISTORY_FILE).get("runs") or []


def save_speech_run(record: dict[str, Any],
                    limit: int = MAX_SPEECH_HISTORY) -> None:
    """Prepend one completed run and drop anything past `limit`.

    Newest-first by construction -- the caller never has to sort, and the
    oldest entry is simply whatever falls off the end of the slice.
    """
    runs = [record] + load_speech_history()
    _write(SPEECH_HISTORY_FILE, {"runs": runs[:limit]})


# --------------------------------------------------------------------------
# Release prep cards
# --------------------------------------------------------------------------
#
# Keyed by release, not by meeting, and NOT capped like the speech history: a
# card belongs to a print, and last month's payrolls card is exactly what you
# want open while writing this month's. One file rather than one per card --
# they are small, and a single read serves the picker.

def load_release_cards() -> dict[str, Any]:
    """Every saved card, keyed `"YYYY-MM-DD|Release Name"`. `{}` if none."""
    if not RELEASE_CARDS_FILE.exists():
        return {}
    cards = _read(RELEASE_CARDS_FILE).get("cards")
    return cards if isinstance(cards, dict) else {}


def save_release_card(key: str, record: dict[str, Any]) -> None:
    """Write one card, leaving every other card untouched."""
    cards = load_release_cards()
    cards[key] = record
    _write(RELEASE_CARDS_FILE, {"cards": dict(sorted(cards.items()))})
