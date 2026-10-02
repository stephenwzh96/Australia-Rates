"""Sell-side RBA calls: what the houses covering Australia expect, and what
just moved -- the US module, on the Monetary Policy Board.

The street here is the four majors' economics teams, Macquarie and AMP, and
the global houses with an Australian economist. All publish a cash-rate
call for each Board meeting and a path, which is what the table reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .speeches import extract_json

PROMPT_VERSION = 1

BANKS: tuple[str, ...] = (
    "Commonwealth Bank", "Westpac", "NAB", "ANZ", "Macquarie", "AMP",
    "Goldman Sachs", "JPMorgan", "UBS", "Citi", "Morgan Stanley",
    "Barclays", "Deutsche Bank", "HSBC", "Bank of America",
)

BATCH_SIZE = 5

CALLS = ("hike", "hold", "cut")
MAX_PATH_MOVES = 8

SYSTEM = (
    "You are a rates strategist's researcher. You are given a list of "
    "banks and one specific upcoming Reserve Bank of Australia Monetary Policy "
    "Board meeting. For each bank find its MOST RECENT published view on "
    "Australian monetary policy -- its economists' RBA cash rate call.\n\n"
    "Rules you must not break:\n"
    "1. Only report a call you can point at a dated, published source: a "
    "research note, a public commentary, a Reuters/Bloomberg write-up of it, "
    "or the firm's own published outlook. No inference from a strategist's "
    "tone, no 'likely'.\n"
    "2. If you cannot source a bank's current view, omit it. An omitted bank "
    "is honest; a guessed one is indistinguishable from a real note once it "
    "is rendered in a table.\n"
    "3. Quote the numbers the house actually published. If it publishes a "
    "path of cuts, report cuts. Do not convert a path into a different "
    "convention of your own.\n"
    "4. `changed` is true ONLY if you can name both the previous call and "
    "approximately when it changed. A hunch that a view has moved is not a "
    "change; leave it false.\n"
    "5. Every count is a number of 25bp moves over the stated horizon, or "
    "null if the source does not give it. Never invent one by dividing an "
    "annual path.\n"
    "6. Dates are ISO YYYY-MM-DD. If only a month is published, use the "
    "first of that month.\n"
    "Reply with ONE JSON object and nothing else."
)


@dataclass(frozen=True)
class BankCall:
    """One house's published view."""

    bank: str
    next_call: str = "hold"                  # hike | hold | cut
    next_bp: float | None = None             # size in bp if it is not a hold
    hikes_6m: int | None = None
    cuts_6m: int | None = None
    hikes_12m: int | None = None
    cuts_12m: int | None = None
    end_rate: float | None = None
    changed: bool = False
    prev_call: str = ""                      # what it was before
    change_date: date | None = None
    change_reason: str = ""                  # why, in the bank's own framing
    as_of: date | None = None                # date of the note
    source: str = ""                         # URL
    note: str = ""                           # anything else worth carrying
    reason_missing: bool = False

    @property
    def net_6m(self) -> int | None:
        """Net 25bp moves over six months; + = hikes."""
        return _net(self.hikes_6m, self.cuts_6m)

    @property
    def net_12m(self) -> int | None:
        return _net(self.hikes_12m, self.cuts_12m)

    @property
    def net_6m_bp(self) -> float | None:
        n = self.net_6m
        return None if n is None else n * 25.0

    @property
    def net_12m_bp(self) -> float | None:
        n = self.net_12m
        return None if n is None else n * 25.0

    @property
    def path_6m(self) -> str:
        return _fmt_path(self.hikes_6m, self.cuts_6m)

    @property
    def path_12m(self) -> str:
        return _fmt_path(self.hikes_12m, self.cuts_12m)

    @property
    def move(self) -> str:
        """'hold → cut', when a call has moved; '' otherwise."""
        if not self.changed or not self.prev_call or self.prev_call == self.next_call:
            return ""
        return f"{self.prev_call} → {self.next_call}"


def _net(hikes: int | None, cuts: int | None) -> int | None:
    if hikes is None and cuts is None:
        return None
    return (hikes or 0) - (cuts or 0)


def _fmt_path(hikes: int | None, cuts: int | None) -> str:
    """'-2' / '+1' / 'flat', or '—' when the source gave nothing."""
    net = _net(hikes, cuts)
    if net is None:
        return "—"
    if net == 0:
        return "flat"
    return f"{net:+d}"


@dataclass(frozen=True)
class CallBook:
    """The answer, as `parse` and `validate` produce it."""

    meeting_date: date | None = None
    calls: tuple[BankCall, ...] = ()
    error: str = ""
    partial: str = ""
    rejected: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def changed_calls(self) -> list[BankCall]:
        """Most recent change first; undated changes last."""
        return sorted((c for c in self.calls if c.changed),
                      key=lambda c: (c.change_date is None,
                                     c.change_date or date.min),
                      reverse=True)

    def by_name(self) -> dict[str, BankCall]:
        return {c.bank: c for c in self.calls}


def batches(banks: list[str], size: int = BATCH_SIZE) -> list[list[str]]:
    """Split the house list so several searches can run side by side."""
    return [banks[i:i + size] for i in range(0, len(banks), size)] or [[]]


def prompt(banks: list[str], meeting_date: date, today: date) -> str:
    """The question put to the research agent."""
    names = "\n".join(f"- {b}" for b in banks)
    return (
        f"Today is {today:%d %B %Y}. The next RBA Monetary Policy Board decision is "
        f"announced at 2:30pm Sydney time on "
        f"{meeting_date:%d %B %Y}.\n\n"
        f"For each of these banks, find its most recent published call on the RBA "
        f"cash rate (its Australian economics team's view):\n{names}\n\n"
        "For each bank report:\n"
        '  "bank": exact name as written above\n'
        '  "next_call": "hike", "hold" or "cut" for the '
        f"{meeting_date:%d %B %Y} meeting\n"
        '  "next_bp": size of that move in basis points (25, 50) or null if '
        'it is a hold\n'
        '  "hikes_6m" / "cuts_6m": number of 25bp hikes / cuts the bank '
        'expects over the SIX months after that meeting, or null if not '
        'published\n'
        '  "hikes_12m" / "cuts_12m": the same over TWELVE months\n'
        '  "end_rate": the RBA cash rate the bank expects at end-2027, as a '
        'percentage (e.g. 3.85), or null\n'
        '  "changed": true only if the bank revised its call for this '
        'meeting within the last eight weeks AND you can name the previous '
        'call\n'
        '  "prev_call": the previous "hike"/"hold"/"cut", or "" if unchanged\n'
        '  "change_date": ISO date of the revision, or null\n'
        '  "change_reason": the bank\'s stated reason, quoted or closely '
        'paraphrased, or "" if none was given\n'
        '  "as_of": ISO date of the note or article you are quoting\n'
        '  "source": the URL\n'
        '  "note": one short sentence of anything else a rates desk should '
        'know, or ""\n\n'
        'Reply with exactly:\n'
        '{"calls": [ {...}, ... ]}\n'
        "Omit any bank you cannot source. Do not add banks that are not on "
        "the list."
    )


def _clean(v: Any, default: str = "") -> str:
    return str(v).strip() if v is not None else default


def _int(v: Any) -> int | None:
    """A count, or None. Rejects anything that is not a whole number."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != int(f):
        return None
    return int(f)


def _when(v: Any) -> date | None:
    try:
        return date.fromisoformat(_clean(v)[:10])
    except (TypeError, ValueError):
        return None


def parse(raw: str, banks: list[str], meeting_date: date, today: date) -> CallBook:
    """Calls from the agent's answer. Never raises."""
    payload = extract_json(raw or "")
    if payload is None:
        return CallBook(meeting_date, error="No JSON object in the agent's reply.")

    known = {b.casefold(): b for b in banks}
    rows: list[BankCall] = []
    seen: set[str] = set()
    for item in payload.get("calls") or []:
        if not isinstance(item, dict):
            continue
        bank = known.get(_clean(item.get("bank")).casefold())
        source = _clean(item.get("source"))
        if bank is None or not source.lower().startswith(("http://", "https://")):
            continue
        if bank in seen:
            continue
        seen.add(bank)

        next_call = _clean(item.get("next_call")).lower()
        if next_call not in CALLS:
            continue

        changed = bool(item.get("changed"))
        prev = _clean(item.get("prev_call")).lower()
        if prev not in CALLS:
            prev = ""
        reason = _clean(item.get("change_reason"))
        if changed and not prev:
            changed = False

        rows.append(BankCall(
            bank=bank,
            next_call=next_call,
            next_bp=_num(item.get("next_bp")) if next_call != "hold" else None,
            hikes_6m=_int(item.get("hikes_6m")),
            cuts_6m=_int(item.get("cuts_6m")),
            hikes_12m=_int(item.get("hikes_12m")),
            cuts_12m=_int(item.get("cuts_12m")),
            end_rate=_num(item.get("end_rate")),
            changed=changed,
            prev_call=prev,
            change_date=_when(item.get("change_date")),
            change_reason=reason,
            as_of=_when(item.get("as_of")),
            source=source,
            note=_clean(item.get("note")),
            reason_missing=bool(changed and not reason),
        ))
    return CallBook(meeting_date, tuple(rows))


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # drop NaN


def book_from_answers(groups: list[list[str]], answers,
                      meeting_date: date, today: date) -> CallBook:
    """The book assembled from one research_many reply set."""
    calls: list[BankCall] = []
    failures: list[str] = []
    for group, (raw, err) in zip(groups, answers):
        if err:
            failures.append(err)
            continue
        parsed = parse(raw, group, meeting_date, today)
        if parsed.error:
            failures.append(parsed.error)
            continue
        calls.extend(parsed.calls)
    book = CallBook(
        meeting_date, tuple(calls),
        error=(failures[0] if failures and len(failures) == len(groups) else ""),
        partial=(f"{len(failures)} of {len(groups)} searches failed"
                 if failures and len(failures) < len(groups) else ""))
    return validate(book, today)


def validate(book: CallBook, today: date) -> CallBook:
    """Drop what cannot be defended, and say what was dropped."""
    keep: list[BankCall] = []
    rejected: list[str] = []
    floor = today - timedelta(days=60)

    for c in book.calls:
        if c.as_of is None:
            rejected.append(f"{c.bank} — no date on the note")
            continue
        if c.as_of > today + timedelta(days=1):
            rejected.append(f"{c.bank} — dated {c.as_of:%d %b}, in the future")
            continue
        if c.as_of < floor:
            rejected.append(f"{c.bank} — dated {c.as_of:%d %b}, more than 60 days old")
            continue
        bad = [n for n, v in (("6m hikes", c.hikes_6m), ("6m cuts", c.cuts_6m),
                              ("12m hikes", c.hikes_12m), ("12m cuts", c.cuts_12m))
               if v is not None and not (0 <= v <= MAX_PATH_MOVES)]
        if bad:
            rejected.append(f"{c.bank} — implausible path ({bad[0]})")
            continue
        if c.next_call != "hold" and (c.next_bp or 0) <= 0:
            rejected.append(f"{c.bank} — {c.next_call} with no size")
            continue
        if c.change_date and c.change_date > today + timedelta(days=1):
            rejected.append(f"{c.bank} — change dated in the future")
            continue
        keep.append(c)

    return CallBook(book.meeting_date, tuple(keep), book.error, book.partial,
                    tuple(rejected))


def tally(book: CallBook) -> dict[str, int]:
    """How many houses call hike / hold / cut for the next meeting."""
    out = {k: 0 for k in CALLS}
    for c in book.calls:
        out[c.next_call] = out.get(c.next_call, 0) + 1
    return out


def consensus_call(book: CallBook) -> str:
    """The modal call. 'split' when there is no single largest bucket."""
    t = tally(book)
    live = {k: v for k, v in t.items() if v}
    if not live:
        return "—"
    top = max(live.values())
    names = sorted(k for k, v in live.items() if v == top)
    return names[0] if len(names) == 1 else "split"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def median_path(book: CallBook, horizon: str) -> float | None:
    """Median net 25bp moves over '6m' or '12m'. + = hikes."""
    key = "net_6m" if horizon == "6m" else "net_12m"
    vals = [getattr(c, key) for c in book.calls]
    return _median([v for v in vals if v is not None])


def dispersion(book: CallBook, horizon: str) -> int | None:
    """Spread between the most hawkish and most dovish house."""
    key = "net_6m" if horizon == "6m" else "net_12m"
    vals = [getattr(c, key) for c in book.calls if getattr(c, key) is not None]
    return None if not vals else max(vals) - min(vals)


def book_to_dict(b: CallBook) -> dict:
    def _d(x: date | None) -> str | None:
        return x.isoformat() if x else None

    return {
        "meeting_date": _d(b.meeting_date),
        "calls": [{
            "bank": c.bank, "next_call": c.next_call, "next_bp": c.next_bp,
            "hikes_6m": c.hikes_6m, "cuts_6m": c.cuts_6m,
            "hikes_12m": c.hikes_12m, "cuts_12m": c.cuts_12m,
            "end_rate": c.end_rate,
            "changed": c.changed, "prev_call": c.prev_call,
            "change_date": _d(c.change_date),
            "change_reason": c.change_reason,
            "as_of": _d(c.as_of), "source": c.source, "note": c.note,
            "reason_missing": c.reason_missing,
        } for c in b.calls],
        "error": b.error, "partial": b.partial, "rejected": list(b.rejected),
    }


def book_from_dict(d: dict) -> CallBook:
    def _d(s: Any) -> date | None:
        try:
            return date.fromisoformat(str(s)[:10])
        except (TypeError, ValueError):
            return None

    calls = []
    for item in d.get("calls") or []:
        if not isinstance(item, dict):
            continue
        calls.append(BankCall(
            bank=_clean(item.get("bank")),
            next_call=_clean(item.get("next_call"), "hold").lower() or "hold",
            next_bp=_num(item.get("next_bp")),
            hikes_6m=_int(item.get("hikes_6m")),
            cuts_6m=_int(item.get("cuts_6m")),
            hikes_12m=_int(item.get("hikes_12m")),
            cuts_12m=_int(item.get("cuts_12m")),
            end_rate=_num(item.get("end_rate")),
            changed=bool(item.get("changed")),
            prev_call=_clean(item.get("prev_call")).lower(),
            change_date=_d(item.get("change_date")),
            change_reason=_clean(item.get("change_reason")),
            as_of=_d(item.get("as_of")),
            source=_clean(item.get("source")),
            note=_clean(item.get("note")),
            reason_missing=bool(item.get("reason_missing")),
        ))
    return CallBook(
        meeting_date=_d(d.get("meeting_date")),
        calls=tuple(calls),
        error=_clean(d.get("error")),
        partial=_clean(d.get("partial")),
        rejected=tuple(str(r) for r in (d.get("rejected") or ())),
    )
