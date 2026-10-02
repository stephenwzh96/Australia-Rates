"""Per-meeting persistence.

One JSON file per meeting under `meetings/<YYYY-MM-DD>.json`, keyed on the
decision date. Cloning forward carries the roster, kill criteria and checklist
structure but clears market pricing and path inputs -- a stale 8bp surviving
into a new meeting is exactly the kind of error the framework exists to prevent.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEETINGS_DIR = ROOT / "meetings"
RUNS_DIR = ROOT / "runs"
ROSTER_FILE = MEETINGS_DIR / "_roster.json"
CALENDAR_FILE = MEETINGS_DIR / "_calendar.json"
SPEECH_HISTORY_FILE = MEETINGS_DIR / "_speech_history.json"
CALL_HISTORY_FILE = MEETINGS_DIR / "_call_history.json"
RELEASE_CARDS_FILE = MEETINGS_DIR / "_release_cards.json"
RECS_HISTORY_FILE = MEETINGS_DIR / "_recs_history.json"

# Kept rather than the full history: each entry embeds a whole roundup
# (sources, quotes, score reads), and a research pass is expensive enough that
# ten genuinely distinct queries already span more than a normal week's use.
MAX_SPEECH_HISTORY = 10

# Cleared when a meeting is cloned forward.
_MARKET_INPUTS = ("points", "far_leg_points")


_RESEARCH_BACKUP_DIR = MEETINGS_DIR / ".research_backups"


def _backup_stamp(record: dict[str, Any]) -> str:
    """A filename-safe id for one run -- its ISO stamp with the colons taken
    out, or a wall-clock fallback when a record somehow carries no stamp."""
    raw = str(record.get("stamp") or clock.stamp())
    return "".join(c if (c.isalnum() or c in "-T") else "-" for c in raw)


def _backup_research_run(kind: str, record: dict[str, Any],
                         limit: int = MAX_SPEECH_HISTORY) -> None:
    """Write one run to its own file and prune that kind to the newest
    `limit`. Never raises -- a backup that fails must not take the real
    save down with it."""
    try:
        _RESEARCH_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _write(_RESEARCH_BACKUP_DIR / f"{kind}-{_backup_stamp(record)}.json",
               record)
        kept = sorted(_RESEARCH_BACKUP_DIR.glob(f"{kind}-*.json"),
                      key=lambda f: f.stat().st_mtime, reverse=True)
        for stale in kept[limit:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _restored_research_runs(kind: str) -> list[dict[str, Any]]:
    """Every backed-up run of `kind`, newest first, best-effort."""
    out: list[dict[str, Any]] = []
    try:
        files = sorted(_RESEARCH_BACKUP_DIR.glob(f"{kind}-*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for f in files:
        try:
            out.append(_read(f))
        except (OSError, ValueError):
            continue
    return out


def _merge_history(primary: list[dict[str, Any]], backups: list[dict[str, Any]],
                   limit: int) -> list[dict[str, Any]]:
    """The aggregate file's runs, topped up with any backed-up run it is
    missing (matched on `stamp`), newest first, capped at `limit`. When the
    aggregate has been wiped this returns the backups on their own."""
    seen = {str(r.get("stamp")) for r in primary if r.get("stamp")}
    merged = list(primary)
    for r in backups:
        if str(r.get("stamp")) not in seen:
            merged.append(r)
            seen.add(str(r.get("stamp")))
    merged.sort(key=lambda r: str(r.get("stamp") or ""), reverse=True)
    return merged[:limit]


def _read(p: Path) -> dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _write(p: Path, data: dict[str, Any]) -> None:
    """Atomic write -- temp file then replace, the same pattern `data/cache.py`
    uses, so a crash mid-write or two concurrent Streamlit sessions saving the
    same meeting can never leave a truncated or interleaved file behind."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
    """Write one meeting's state, stamping when it was last saved."""
    from core import clock
    data["saved_at"] = clock.stamp()
    p = meeting_path(key)
    _write(p, data)
    return p


def delete_trade(key: str) -> bool:
    """Remove one saved trade file. `False` when no such file existed."""
    p = meeting_path(key)
    if not p.exists():
        return False
    p.unlink()
    return True


def _trade_title(data: dict[str, Any]) -> str:
    """Short unique title: the decision month, e.g. "Nov 2026"."""
    end = (data.get("meeting") or {}).get("end")
    if end:
        try:
            return f"{date.fromisoformat(str(end)):%b %Y}"
        except ValueError:
            return str(end)
    return str(data.get("label") or "RBA trade")


def _contract_label(key: str, as_of: str | None) -> str:
    """Human label for a saved contract key, incl. keys that rolled off strip."""
    anchor = None
    if as_of:
        try:
            anchor = date.fromisoformat(as_of)
        except ValueError:
            anchor = None
    from core import sizing
    return sizing.contract_for(key, anchor).label


def describe_trade(key: str) -> dict[str, Any] | None:
    """One saved meeting as a sidebar recall row; `None` when nothing is saved."""
    p = meeting_path(key)
    if not p.exists():
        return None
    try:
        data = _read(p)
    except (OSError, ValueError):
        return None
    if data.get("example"):
        return None
    trade = data.get("trade") or {}
    size, points = trade.get("size"), trade.get("points")
    side, direction = trade.get("side", "fade"), trade.get("direction", "hike")
    leg = (f"{side} {size:g}bp {direction}"
           if isinstance(size, (int, float)) and size else str(side))
    leg += " · unpriced" if points is None else f" · {points:g}bp priced"
    prob = data.get("probability") or {}
    q = None
    if points is not None:
        if prob.get("mode") == "manual" and prob.get("override_q") is not None:
            q = f"q {prob['override_q']:.1%}"
        elif prob.get("p_bloc") is not None and prob.get("p_centre") is not None:
            q = (f"q {prob['p_bloc'] * prob['p_centre']:.1%} "
                 f"({prob['p_bloc']:.0%} × {prob['p_centre']:.0%})")
    contract = (data.get("sizing") or {}).get("contract")
    instrument = code = None
    if contract:
        try:
            instrument = _contract_label(str(contract), data.get("as_of"))
        except Exception:
            instrument = str(contract)
        head = instrument.split(" — ", 1)[0].strip()
        code = head if head and " " not in head else None
    return {"key": key, "title": _trade_title(data), "text": leg, "code": code,
            "instrument": instrument, "q": q, "as_of": data.get("as_of"),
            "saved_at": data.get("saved_at")}


def saved_trade_rows() -> list[dict[str, Any]]:
    """Every saved meeting as a recall row, newest decision first."""
    return [r for r in (describe_trade(k) for k in reversed(list_saved())) if r is not None]


def load_roster() -> list[dict[str, Any]]:
    """The shared voter roster. `load_or_seed` below calls this UNGUARDED on
    every meeting resolution, so a corrupt or hand-edited `_roster.json` must
    degrade to an empty roster rather than raising and wedging every rerun --
    the same failure `load_or_seed`'s own try/except already guards against
    for a meeting file."""
    try:
        return _read(ROSTER_FILE)["voters"]
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return []


def load_calendar() -> list[dict[str, Any]] | None:
    if not CALENDAR_FILE.exists():
        return None
    try:
        return _read(CALENDAR_FILE)["meetings"]
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def save_calendar(meetings: list[dict[str, Any]]) -> None:
    _write(CALENDAR_FILE, {"meetings": meetings})


def previous_key(key: str) -> str | None:
    saved = [k for k in list_saved() if k < key]
    return saved[-1] if saved else None


def baseline_key(state: dict[str, Any], key: str) -> str | None:
    """The meeting a roster's Score @ last meeting was marked at."""
    return state.get("score_baseline") or previous_key(key)


def roll_roster(state: dict[str, Any], source_key: str,
                source: dict[str, Any]) -> dict[str, Any]:
    """Carry the scores forward from the meeting a state was cloned from: its
    Score now becomes this meeting's Score @ last meeting, and the marks that
    replaces are archived in `score_history` under the meeting they were taken
    at. Stamps `score_baseline`, so a state is rolled once and only once."""
    from core import roster as R
    if not state.get("roster"):
        return state
    rows, history = R.roll(R.normalize_roster(state.get("roster") or []),
                           state.get("score_history") or [],
                           source.get("roster") or [],
                           baseline_key(source, source_key))
    state["roster"] = rows
    state["score_history"] = history
    state["score_baseline"] = source_key
    return state


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

    # The full-employment panel carries forward untouched: window, comparison
    # date and the typed commercial readings are macro settings, not per-trade
    # inputs, and re-typing them at every meeting is how they go stale.
    out.setdefault("employment", {})
    out["structure"]["calendar"] = []
    out["structure"]["calendar_note"] = ""
    out.pop("notes", None)      # free-text notes no longer exist anywhere
    out.pop("score_baseline", None)
    return roll_roster(out, source_key, src)


# Seeded on every new meeting so the two-strikes rule has something to count.
# The first three are wired to RBA table F1 and colour themselves; the rest are
# manual toggles, because no free Australian series publishes them daily.
#
# Chosen for Australia rather than translated from the US list. Brent and the
# 5y5y breakeven drive the US version because a Fed thesis dies on an oil shock
# or an expectations un-anchoring; an RBA thesis is more often killed by the
# labour market or the exchange rate, and Australia has no liquid inflation
# breakeven to read expectations off at all.
DEFAULT_KILL_CRITERIA: tuple[dict[str, Any], ...] = (
    {"label": "3-month BBSW/cash spread blows out",
     "comparator": "above", "threshold": 45.0, "series": "BBSW_CASH_SPREAD",
     "unit": "bp", "triggered": False,
     "note": "Bank funding stress. Widens the wedge between what you are "
             "trading and the policy rate you have a view on."},
    {"label": "Cash rate trades away from target",
     "comparator": "above", "threshold": 3.0, "series": "CASH_GAP_ABS",
     "unit": "bp", "triggered": False,
     "note": "AONIA normally sits on the target under ample reserves. A "
             "persistent gap means IB is settling on something other than "
             "the rate you are forecasting."},
    {"label": "3-month BBSW moves more than 25bp in a fortnight",
     "comparator": "above", "threshold": 25.0, "series": "BBSW_14D_MOVE",
     "unit": "bp", "triggered": False,
     "note": "The short end has repriced without you. Whatever your q was, "
             "it was set against a different curve."},
    {"label": "Trimmed mean CPI surprises by more than 0.3pp",
     "comparator": "manual", "threshold": None, "series": None,
     "unit": "pp", "triggered": False,
     "note": "The RBA targets the trimmed mean. Quarterly, so this fires "
             "rarely and hard."},
    {"label": "Unemployment rate moves 0.3pp from the RBA's forecast",
     "comparator": "manual", "threshold": None, "series": None,
     "unit": "pp", "triggered": False,
     "note": "Full employment is half the mandate. Set against the latest "
             "SMP forecast, not the last print."},
    {"label": "AUD/USD moves more than 5% in a month",
     "comparator": "manual", "threshold": None, "series": None,
     "unit": "%", "triggered": False,
     "note": "A big currency move does the RBA's work for it, or undoes it. "
             "Matters more here than for a closed economy."},
    {"label": "Governor or Deputy signals a change of stance",
     "comparator": "manual", "threshold": None, "series": None,
     "unit": "", "triggered": False,
     "note": "Only two voices reliably move the curve. Outside the blackout, "
             "a speech can reprice the meeting on its own."},
)


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
            "instrument": "RBA-dated OIS", "instrument_kind": "ois",
            "direction": "hike", "side": "fade",
            # sizing conventions live under "sizing"; see core.sizing
            "points": None, "size": 25.0,
            "target_range_low": None, "target_range_high": None,
            "kelly_fraction": 0.25,
        },
        "probability": {"mode": "decomposition", "p_bloc": 0.25, "p_centre": 0.40, "override_q": None},
        "sizing": {"max_drawdown": 250_000.0, "daily_limit": 50_000.0,
                   "bets_per_year": 8, "contract": "ib", "custom_dv01": None,
                   "contract_yield": None},
        "sensitivity_grid": [0.05, 0.10, 0.15, 0.20, 0.25, 0.40],
        "kelly_grid": [0.10, 0.15, 0.20, 0.25],
        "centre_grid": [0.20, 0.30, 0.40, 0.50],
        "path": {"ladder_levels": [], "stop_level": None, "target_level": None,
                 "current_price": None, "vol_override_bp": None, "vol_uncertainty": False,
                 "mark_tier1": True, "mark_tier2": False, "mark_tier3": False,
                 "no_move_target_hit": 0, "no_move_never_breached": 0,
                 "no_move_falsely_stopped": 0, "move_target_hit": 0,
                 "move_stopped_early": 0, "move_gapped_through": 0, "note": ""},
        "kill_criteria": [dict(k) for k in DEFAULT_KILL_CRITERIA],
        # Full-employment panel settings. The manual readings are the three
        # commercial series with no free feed; they are seeded EMPTY rather
        # than with a plausible number, because an unsourced figure that looks
        # like data is worse than a blank one.
        "employment": {
            "window_start": 2000, "window_end": 2020,
            "prior_date": "2025-12-31",
            "manual": {"labour_constraint": {}, "job_ads": {},
                       "employment_intentions": {}},
        },
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
    state = None
    if exists(key):
        # A meeting file that is corrupt, hand-edited or written by an older
        # schema must not wedge the app. `build` reads `state["meeting"]`
        # unguarded, so a missing key there raises on EVERY rerun and the only
        # way out is deleting the file by hand. Falling through to a fresh
        # meeting keeps the app usable; `_recovered` tells the UI to say so
        # rather than silently discarding what was on disk.
        try:
            state = load(key)
        except (OSError, ValueError, json.JSONDecodeError):
            state = None          # unreadable or not JSON at all
        if not isinstance(state, dict) or "meeting" not in state:
            state = None          # readable, but not a meeting
    if state is None:
        recovered = exists(key)
        target = next((m for m in meetings if m.get("end") == key), None)
        prev = previous_key(key)
        if prev and target:
            state = clone_forward(prev, target, today)
        else:
            state = new_meeting(target or {"start": key, "end": key, "verified": False}, today)
        if recovered:
            state["_recovered"] = True
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
    primary = (_read(SPEECH_HISTORY_FILE).get("runs") or []
               if SPEECH_HISTORY_FILE.exists() else [])
    return _merge_history(primary, _restored_research_runs("speech"),
                          MAX_SPEECH_HISTORY)


def save_speech_run(record: dict[str, Any],
                    limit: int = MAX_SPEECH_HISTORY) -> None:
    """Add one completed run and drop anything past `limit`."""
    _backup_research_run("speech", record, limit)
    primary = (_read(SPEECH_HISTORY_FILE).get("runs") or []
               if SPEECH_HISTORY_FILE.exists() else [])
    runs = _merge_history([record] + primary,
                          _restored_research_runs("speech"), limit)
    _write(SPEECH_HISTORY_FILE, {"runs": runs})


def load_call_history() -> list[dict[str, Any]]:
    """Past bank-call runs, newest first. `[]` if none have been saved.
    Rehydrated from the per-run backups, like `load_speech_history`."""
    primary = (_read(CALL_HISTORY_FILE).get("runs") or []
               if CALL_HISTORY_FILE.exists() else [])
    return _merge_history(primary, _restored_research_runs("call"),
                          MAX_SPEECH_HISTORY)


def save_call_run(record: dict[str, Any],
                  limit: int = MAX_SPEECH_HISTORY) -> None:
    """Same contract as `save_speech_run`, in its own file."""
    _backup_research_run("call", record, limit)
    primary = (_read(CALL_HISTORY_FILE).get("runs") or []
               if CALL_HISTORY_FILE.exists() else [])
    runs = _merge_history([record] + primary,
                          _restored_research_runs("call"), limit)
    _write(CALL_HISTORY_FILE, {"runs": runs})


def load_recs_history() -> list[dict[str, Any]]:
    """Past trade-rec runs, newest first. `[]` if none have been saved."""
    primary = (_read(RECS_HISTORY_FILE).get("runs") or []
               if RECS_HISTORY_FILE.exists() else [])
    return _merge_history(primary, _restored_research_runs("recs"),
                          MAX_SPEECH_HISTORY)


def save_recs_run(record: dict[str, Any],
                  limit: int = MAX_SPEECH_HISTORY) -> None:
    """Same contract as `save_call_run`, in its own file. A run is billed and
    minutes long, so it is kept like the other research runs."""
    _backup_research_run("recs", record, limit)
    primary = (_read(RECS_HISTORY_FILE).get("runs") or []
               if RECS_HISTORY_FILE.exists() else [])
    runs = _merge_history([record] + primary,
                          _restored_research_runs("recs"), limit)
    _write(RECS_HISTORY_FILE, {"runs": runs})


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
