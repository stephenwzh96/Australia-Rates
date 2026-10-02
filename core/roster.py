"""Roster logic for the Vote count tab: score/bloc accessors, the speech-roundup
write-back, and the bloc summary -- the US module, on the RBA's Monetary Policy
Board (no rotation: `core.rotation`). Pure -- no Streamlit, no I/O."""

from __future__ import annotations

from datetime import date

from . import rotation

ROW_ORDER = ("name", "role", "bloc1", "bloc2", "score1", "score2", "is_mover", "note")
DERIVED_COLS = ("votes", "trend")
HISTORY_PREFIX = "@"        # read-only editor columns for archived meeting scores
UNSCORED_BLOCS = {"", "unscored", "unassigned", "n/a", "na", "tbd"}


def num_or_none(v) -> float | None:
    """A score as a float, or None for blanks, half-typed text and bools."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def normalize_roster(rows) -> list[dict]:
    """Migrate legacy single score/bloc rows to the score1/score2, bloc1/bloc2 pairs."""
    out = []
    for r in rows:
        r = dict(r)
        r.setdefault("score1", r.get("score", 2.5))
        r.setdefault("score2", r.get("score1", 2.5))
        r.pop("score", None)
        r.setdefault("bloc1", r.get("bloc", ""))
        r.setdefault("bloc2", r.get("bloc1", ""))
        r.pop("bloc", None)
        ordered = {k: r[k] for k in ROW_ORDER if k in r}
        ordered.update({k: v for k, v in r.items() if k not in ordered})
        out.append(ordered)
    return out


def active_score(r: dict) -> float | None:
    """Latest hawk-dove read: score2, then score1, then the legacy score."""
    for key in ("score2", "score1", "score"):
        v = r.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def baseline_score(r: dict) -> float | None:
    """The mark at the last meeting -- the base every speech mark-to-market uses."""
    for key in ("score1", "score", "score2"):
        v = r.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def active_bloc(r: dict) -> str:
    """Current bloc: bloc2, then bloc1, then the legacy bloc."""
    for key in ("bloc2", "bloc1", "bloc"):
        v = r.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def ap_bloc_now(ap) -> str:
    """An appearance's bloc re-assessment, tolerant of pre-upgrade cached objects."""
    return str(getattr(ap, "bloc_now", "") or "").strip()


def ap_bloc_note(ap) -> str:
    return str(getattr(ap, "bloc_note", "") or "").strip()


def roundup_has_blocs(r) -> bool:
    """Whether a cached roundup was built by an Appearance that carries bloc_now."""
    aps = getattr(r, "appearances", ()) or ()
    return all(hasattr(a, "bloc_now") for a in aps)


def seat_key(row: dict) -> str:
    """A seat is identified by its member's name (the Board has no regional
    seats to key on, unlike the FOMC)."""
    return f"name:{str(row.get('name', '')).strip()}"


def backfill_participants(rows: list[dict], reference: list[dict]) -> tuple[list[dict], bool]:
    """Append reference-roster seats the saved roster is missing."""
    have = {seat_key(r) for r in rows}
    missing = [r for r in normalize_roster(reference) if seat_key(r) not in have]
    return (rows + missing, True) if missing else (rows, False)


_HONOURS = {"ac", "ao", "am", "oam", "psm", "dr", "prof", "professor", "the", "hon"}


def name_key(name: str) -> str:
    """A name as a matching key: accents, case, punctuation and honours
    dropped, so "Renee Fry-McKibbin AO" is the RBA's "Renée Fry-McKibbin"."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    words = "".join(c if c.isalnum() else " " for c in plain.lower()).split()
    return " ".join(w for w in words if w not in _HONOURS)


def seat_board(rows: list[dict], board) -> tuple[list[dict], list[str], list[str]]:
    """Bring the roster in line with the Board as rba.gov.au lists it.

    `board` is the published membership (`data.rba_board.Member`s, in seat
    order). A row whose name matches a member (`name_key`: accents, case and
    honours ignored) keeps its scores and takes the official spelling. A
    member not yet on the roster takes the first blank seat of their role --
    keeping whatever score and note that seat carries -- or is added as a new
    neutral row. A named row matching no member has left the Board and is
    dropped, and so are blank seats once every member is placed, so the tally
    counts the nine who actually vote. An empty `board` (the page could not
    be read) leaves the roster alone. Returns (rows, joined, left)."""
    board = list(board or ())
    if not board:
        return rows, [], []
    rows = [dict(r) for r in rows]
    by_key = {name_key(m.name): m for m in board}
    placed: dict[str, dict] = {}
    left: list[str] = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        m = by_key.get(name_key(name))
        if m is None or m.name in placed:
            left.append(name)
            continue
        r["name"] = m.name
        placed[m.name] = r
    joined: list[str] = []
    for m in board:
        if m.name in placed:
            continue
        seat = next((r for r in rows if not str(r.get("name") or "").strip()
                     and r.get("role") == m.role
                     and all(r is not p for p in placed.values())), None)
        if seat is None:
            seat = {"name": "", "role": m.role, "bloc1": m.bloc, "bloc2": m.bloc,
                    "score1": 2.5, "score2": 2.5, "is_mover": False,
                    "note": "Score not set -- derive from the member's own speeches."}
        seat["name"] = m.name
        placed[m.name] = seat
        joined.append(m.name)
    return normalize_roster([placed[m.name] for m in board]), joined, left


def score_trend(r: dict) -> str:
    s1, s2 = num_or_none(r.get("score1")), num_or_none(r.get("score2"))
    if s1 is None or s2 is None:
        return ""
    d = s2 - s1
    if d > 0.05:
        return f"▲ +{d:.1f}"
    if d < -0.05:
        return f"▼ {d:.1f}"
    return "–"


def fomc_label(key: str | None, fmt: str = "%b") -> str:
    """"Sep RBA" for a meeting key, "last meeting" when there is none. Named as
    the US module names it, so the ported callers read identically."""
    if not key:
        return "last meeting"
    try:
        return f"{date.fromisoformat(key):{fmt}} RBA"
    except ValueError:
        return f"{key} RBA"


def history_col(fomc: str) -> str:
    return HISTORY_PREFIX + fomc


def roll(rows: list[dict], history: list[dict], closed_rows: list[dict],
         closed_baseline: str | None) -> tuple[list[dict], list[dict]]:
    """Close the books on one meeting: its Score now becomes Score @ last meeting.

    `closed_rows` is the roster of the meeting just held, read at its active
    (now) columns; `closed_baseline` is the meeting ITS @ last meeting columns
    were marked at. Those outgoing marks are archived under that key before
    being overwritten, so nothing a roll replaces is lost. Score now is left
    alone: it may already carry speeches marked since the meeting. Returns
    new lists; a baseline already archived is not archived twice."""
    closed = {str(r.get("name", "")).strip(): (active_score(r), active_bloc(r))
              for r in normalize_roster(closed_rows)}
    hist = [dict(h) for h in history or ()]
    if closed_baseline and all(h.get("fomc") != closed_baseline for h in hist):
        hist.append({"fomc": closed_baseline,
                     "scores": name_map(rows, lambda r: num_or_none(r.get("score1"))),
                     "blocs": name_map(rows, lambda r: str(r.get("bloc1") or "").strip())})
        hist.sort(key=lambda h: str(h.get("fomc") or ""))
    out = []
    for r in rows:
        row = dict(r)
        score, bloc = closed.get(str(r.get("name", "")).strip(), (None, ""))
        row["score1"] = score if score is not None else row.get("score1")
        row["bloc1"] = bloc or row.get("bloc1", "")
        out.append(row)
    return out, hist


def with_vote_flag(rows, year: int, history: list[dict] = (),
                   show: tuple[str, ...] = ()) -> list[dict]:
    """Rows plus the derived, read-only columns: votes, trend, and the archived
    score of each earlier meeting in `show`."""
    shown = [h for h in history or () if h.get("fomc") in show]
    return [{**r, "votes": rotation.votes_in(r.get("role", ""), year),
             "trend": score_trend(r),
             **{history_col(h["fomc"]): (h.get("scores") or {}).get(
                 str(r.get("name", "")).strip()) for h in shown}} for r in rows]


def without_vote_flag(rows) -> list[dict]:
    return [{k: v for k, v in dict(r).items()
             if k not in DERIVED_COLS and not str(k).startswith(HISTORY_PREFIX)}
            for r in rows]


def name_map(rows, value) -> dict:
    """{name: value(row)} over named rows where value(row) is not None/empty."""
    out = {}
    for r in rows:
        name = str(r.get("name", "")).strip()
        if not name:
            continue
        v = value(r)
        if v is not None and v != "":
            out[name] = v
    return out


def roster_updates(roundup, roster_rows: list[dict],
                   chosen: dict[str, int] | None = None,
                   include_applied: bool = False) -> list[dict]:
    """Preview of what "Update main table" would write into score2/bloc2."""
    picked = chosen or {}
    out: list[dict] = []
    for r in roster_rows:
        name = str(r.get("name", "")).strip()
        outings = roundup.by_member(name) if name else ()
        if not outings:
            continue
        idx = picked.get(name, 0)
        if not isinstance(idx, int) or not 0 <= idx < len(outings):
            idx = 0
        ap = outings[idx]
        old_score2 = active_score(r)
        baseline = num_or_none(r.get("score1"))
        new_score = ap.new_score(baseline if baseline is not None else old_score2)
        old_bloc2 = active_bloc(r)
        new_bloc2 = ap_bloc_now(ap) or old_bloc2
        new_score2 = new_score if new_score is not None else old_score2
        if (not include_applied
                and new_bloc2 == old_bloc2
                and (new_score2 is None or old_score2 is None
                     or abs(new_score2 - old_score2) < 1e-9)):
            continue
        out.append({
            "name": name,
            "bloc2": old_bloc2, "new_bloc2": new_bloc2,
            "score2": old_score2, "new_score2": new_score2,
            "note": str(r.get("note", "") or "").strip(),
            "new_note": (str(getattr(ap, "score_note", "") or "").strip()
                         or ap_bloc_note(ap)),
            "bloc1": (r.get("bloc1") or r.get("bloc") or "").strip(),
            "score1": baseline,
            "delta_after": ((new_score2 - baseline)
                            if baseline is not None and new_score2 is not None
                            else None),
            "when": ap.when, "source": ap.source,
            "bloc_note": ap_bloc_note(ap), "score_note": ap.score_note,
            "outing_idx": idx, "n_outings": len(outings),
        })
    return out


def apply_roster_updates(roster_rows: list[dict], updates: list[dict]) -> list[dict]:
    """Write previewed updates into the "now" columns only; returns a new list."""
    by_name = {u["name"]: u for u in updates}
    out = []
    for r in roster_rows:
        u = by_name.get(str(r.get("name", "")).strip())
        if u is None:
            out.append(r)
            continue
        row = dict(r)
        row["bloc2"] = u["new_bloc2"]
        row["score2"] = u["new_score2"]
        if str(u.get("new_note", "") or "").strip():
            row["note"] = u["new_note"]
        out.append(row)
    return out


def edits_to_updates(edits, base_updates: list[dict]) -> tuple[list[dict], list[str]]:
    """The edited preview rows as writes, plus the rows that cannot be written."""
    by_name = {u["name"]: u for u in base_updates}
    pending: dict[str, dict] = {}
    order: list[str] = []
    problems: list[str] = []

    def drop(name: str) -> None:
        if name in pending:
            pending.pop(name)
            order.remove(name)

    for e in edits or ():
        if not isinstance(e, dict):
            continue
        name = str(e.get("name", "")).strip()
        base = by_name.get(name)
        if base is None:
            continue
        if not e.get("include", True):
            drop(name)
            continue
        score = num_or_none(e.get("new_score2"))
        if score is None or score != score:
            problems.append(f"{name}: score is empty — skipped")
            continue
        if not 0.0 <= score <= 5.0:
            problems.append(f"{name}: score {score:.2f} is outside 0–5 — skipped")
            continue
        bloc = str(e.get("new_bloc2") or "").strip()
        note = str(e.get("new_note") or "").strip()
        note_changes = bool(note) and note != str(base.get("note") or "").strip()
        if (bloc == str(base.get("bloc2") or "").strip()
                and abs(score - (base.get("score2") or score)) < 1e-9
                and not note_changes):
            drop(name)
            continue
        if name not in pending:
            order.append(name)
        pending[name] = {**base, "new_bloc2": bloc, "new_score2": score,
                         "new_note": note}
    return [pending[n] for n in order], problems


def bloc_summary(rows, year: int, voters_only: bool = True):
    """Per-bloc means of score1/score2 over the roster as edited."""
    order: list[str] = []
    groups: dict[str, list[tuple[str, float | None, float | None]]] = {}
    excluded: list[str] = []

    for r in rows:
        if voters_only and not rotation.votes_in(r.get("role", ""), year):
            continue
        bloc = active_bloc(r)
        name = str(r.get("name") or "").strip() or "(unnamed)"
        if bloc.lower() in UNSCORED_BLOCS:
            excluded.append(name)
            continue
        if bloc not in groups:
            groups[bloc] = []
            order.append(bloc)
        groups[bloc].append((name, num_or_none(r.get("score1")),
                             num_or_none(r.get("score2"))))

    def fmt_row(label, n, s1, s2):
        m1 = sum(s1) / len(s1) if s1 else None
        m2 = sum(s2) / len(s2) if s2 else None
        delta = (m2 - m1) if m1 is not None and m2 is not None else None
        return [label, n,
                f"{m1:.2f}" if m1 is not None else "—",
                f"{m2:.2f}" if m2 is not None else "—",
                f"{delta:+.2f}" if delta is not None else "—",
                f"{min(s2):.1f}–{max(s2):.1f}" if len(s2) > 1 else "—"]

    out: list[list] = []
    all_s1: list[float] = []
    all_s2: list[float] = []
    for bloc in order:
        members = groups[bloc]
        s1 = [m[1] for m in members if m[1] is not None]
        s2 = [m[2] for m in members if m[2] is not None]
        all_s1.extend(s1)
        all_s2.extend(s2)
        label = bloc + (" †" if len(s2) != len(members) else "")
        out.append(fmt_row(label, len(members), s1, s2))

    if all_s2:
        out.append(fmt_row("All members", len(all_s2), all_s1, all_s2))

    footnotes: list[str] = []
    if any(r[0].endswith("†") for r in out):
        footnotes.append("† n counts every member of the bloc; the mean covers "
                         "only those with a numeric mark.")
    if excluded:
        footnotes.append("Excluded as unscored, not read as neutral: "
                         + ", ".join(excluded) + ".")
    return out, footnotes
