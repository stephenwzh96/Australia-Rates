"""The Board's published decisions and vote splits, from the RBA's statements.

Since the Monetary Policy Board first met (March 2025) every decision
statement ends with the vote, unattributed:

    "Today's policy decision was unanimous."
    "Today's policy decision was made by majority: five members voted to
     increase the cash rate target by 25 basis points to 4.10 per cent; four
     members voted to leave the cash rate target unchanged at 3.85 per cent."

That count is the only published read of the Board, so it is what the Vote
count tab scores a roster against -- the role the FOMC's named dissents play
in the US app. The decision list comes from the year index pages
(rba.gov.au/monetary-policy/int-rate-decisions/{year}/); a statement never
changes once published, so each is disk-cached for good and only the current
year's index is re-read.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = "https://www.rba.gov.au"
INDEX = ROOT + "/monetary-policy/int-rate-decisions/{year}/"
FIRST_YEAR = 2025
BOARD_START = date(2025, 3, 1)  # the Monetary Policy Board replaced the Reserve Bank Board
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36")}
_TIMEOUT = 15
_CACHE = Path(__file__).resolve().parent / "cache" / "rba_decisions"
_INDEX_TTL = 60 * 60 * 6

_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine".split())}

HIKE, HOLD, CUT = "hike", "hold", "cut"


@dataclass(frozen=True)
class Decision:
    day: str                    # ISO date of the announcement
    action: str                 # hike / hold / cut
    change_bp: int              # signed
    level: float                # cash rate target after the decision, %
    unanimous: bool | None      # None: the statement carries no vote (pre-2025 Board)
    votes: dict                 # action -> members, e.g. {"hike": 5, "hold": 4};
                                # "against" when the statement does not say which way
    url: str

    @property
    def when(self) -> date:
        return date.fromisoformat(self.day)

    @property
    def members(self) -> int:
        return sum(self.votes.values())

    @property
    def dissents(self) -> int:
        return self.members - self.votes.get(self.action, 0)

    @property
    def split(self) -> str:
        """"9–0", "5–4": for the decision, against it."""
        if not self.votes:
            return "—"
        return f"{self.votes.get(self.action, 0)}–{self.dissents}"

    @property
    def dissent_side(self) -> str:
        """"hawkish" when a dissent wanted a higher rate than was set, "dovish"
        when lower, "" when there was none (or both ways at once)."""
        order = {CUT: -1, HOLD: 0, HIKE: 1}
        sides = {("hawkish" if order[a] > order[self.action] else "dovish")
                 for a, n in self.votes.items() if a != self.action and n and a in order}
        return sides.pop() if len(sides) == 1 else ""


def _text(page: str) -> str:
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).replace("’", "'")


def _action(clause: str) -> tuple[str, int | None]:
    """(action, signed bp) from "increase the cash rate target by 25 basis
    points"; bp is None when the clause gives only the new level ("lower the
    cash rate target to 4.10 per cent", the early-2025 wording)."""
    m = re.search(r"\b(increase|raise|lower|reduce|decrease|cut)\w* the cash rate target"
                  r"(?: by (\d+) basis)?", clause)
    if m is None:
        return HOLD, 0
    up = m.group(1) in ("increase", "raise")
    if m.group(2) is None:
        return (HIKE if up else CUT), None
    bp = int(m.group(2))
    return (HIKE, bp) if up else (CUT, -bp)


def _count(word: str) -> int | None:
    word = word.lower()
    return int(word) if word.isdigit() else _WORDS.get(word)


#: The minority's preference where the statement gave only "N against": the
#: July 2025 statement (the first with a vote) said "6 in favour, 3 against";
#: the minutes and the Governor's media conference give the three as wanting a
#: 25bp cut.
_AGAINST = {"2025-07-08": CUT}


def parse(page: str, url: str = "") -> Decision | None:
    """One decision statement, or None when the page is not one."""
    t = _text(page)
    head = re.search(r"Date (\d{1,2} \w+ \d{4}) At its meeting today, the Board decided to (.*?per cent)", t)
    if not head:
        return None
    try:
        day = datetime.strptime(head.group(1), "%d %B %Y").date()
    except ValueError:
        return None
    clause = head.group(2)
    action, bp = _action(clause)
    lv = re.search(r"(\d+\.\d+) per cent", clause)
    if lv is None:
        return None
    votes: dict[str, int] = {}
    unanimous: bool | None = None
    if re.search(r"decision was unanimous", t):
        # The statement gives no count for a unanimous vote; all nine members
        # have sat at every Board meeting, so nine is recorded.
        unanimous, votes = True, {action: 9}
    else:
        m = re.search(r"decision was made by majority:? (.*?)(?: More on| Enquiries|$)", t)
        if m:
            unanimous = False
            body = m.group(1)
            short = re.match(r"(\w+) in favour, (\w+) against", body)
            if short and _count(short.group(1)) is not None and _count(short.group(2)) is not None:
                votes[action] = _count(short.group(1))
                other = _AGAINST.get(day.isoformat(), "against")
                votes[other] = votes.get(other, 0) + _count(short.group(2))
            for part in body.split(";"):
                n = re.match(r"\s*(\w+) members? voted to (.*)", part)
                if n and _count(n.group(1)) is not None:
                    a, _ = _action(n.group(2))
                    votes[a] = votes.get(a, 0) + _count(n.group(1))
    return Decision(day.isoformat(), action, 0 if bp is None else bp, float(lv.group(1)),
                    unanimous, votes, url)


def _get(url: str) -> str | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    return r.text if r.status_code == 200 and r.text else None


def _links(year: int, current: bool) -> list[str]:
    """A year's decision links. The current year's index is re-read every few
    hours; a past year's cache is final only if it was written after that
    year ended -- one cached mid-year would otherwise miss its last meetings
    for good once the year rolls over."""
    path = _CACHE / f"index-{year}.json"
    try:
        written = path.stat().st_mtime
        closed = datetime(year + 1, 1, 1).timestamp()
        fresh = (time.time() - written < _INDEX_TTL) if current else written >= closed
        if fresh:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    page = _get(INDEX.format(year=year))
    if page is None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    links = list(dict.fromkeys(ROOT + h for h in re.findall(
        r'href="(/media-releases/\d{4}/mr-\d\d-\d+\.html)"', page)))
    try:
        _CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(links), encoding="utf-8")
    except OSError:
        pass
    return links


def _decision(url: str) -> Decision | None:
    path = _CACHE / (url.rsplit("/", 1)[-1].removesuffix(".html") + ".json")
    try:
        return Decision(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    page = _get(url)
    d = parse(page, url) if page else None
    if d is not None:
        try:
            _CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(d)), encoding="utf-8")
        except OSError:
            pass
    return d


def history(today: date) -> list[Decision]:
    """Every Monetary Policy Board decision up to `today`, oldest first (the
    old Reserve Bank Board's, before March 2025, published no vote)."""
    from concurrent.futures import ThreadPoolExecutor

    urls = [u for y in range(FIRST_YEAR, today.year + 1)
            for u in _links(y, current=y == today.year)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = [d for d in ex.map(_decision, urls) if d is not None]
    out = sorted((d for d in found if BOARD_START <= d.when <= today), key=lambda d: d.day)
    # Early statements give the new level only: the move is the step from the last.
    for i in range(1, len(out)):
        d, prev = out[i], out[i - 1]
        if d.action != HOLD and d.change_bp == 0:
            out[i] = replace(d, change_bp=round((d.level - prev.level) * 100))
    return out
