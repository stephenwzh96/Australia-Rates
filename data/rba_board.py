"""The Monetary Policy Board as rba.gov.au lists it: who sits, in which seat,
since when, and their official portrait -- the roster's seed and the Vote
count transition chart's headshots (the role `fed_photos` plays in the US app).

One page carries all nine members (rba.gov.au/about-rba/boards/
monetary-policy-board/), so the roster is read rather than typed and a change
of membership -- a new non-executive member, a new Treasury Secretary --
shows up on the next fetch. Portraits are centre-cropped thumbnails, disk
cached; a member whose photo fails falls back to a monogram.
"""

from __future__ import annotations

import html
import io
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

try:
    from PIL import Image
except Exception:                       # pragma: no cover - PIL is a hard dep
    Image = None

PAGE = "https://www.rba.gov.au/about-rba/boards/monetary-policy-board/"
ROOT = "https://www.rba.gov.au"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
}
_TIMEOUT = 12
_CACHE = Path(__file__).resolve().parent / "cache" / "photos"
_MEMBERS = Path(__file__).resolve().parent / "cache" / "rba_board.json"
_HIT_TTL = 60 * 60 * 24 * 30            # a portrait does not change monthly
_MISS_TTL = 60 * 60 * 6                 # but re-try a failed fetch the same day
_MEMBERS_TTL = 60 * 60 * 24             # membership changes a few times a year
_THUMB = 128                            # output square, px

#: The page's seat heading -> the roster's role. The roles are what
#: `core.dots.group_of` and the seat order key on.
ROLES = {
    "chair": "Governor, Chair",
    "deputy chair": "Deputy Governor",
    "ex officio member": "Secretary to the Treasury",
    "non-executive member": "External member",
}
SEAT_ORDER = ("Governor, Chair", "Deputy Governor", "Secretary to the Treasury",
              "External member")
#: Seat -> the roster's default bloc: the seat group, the one split a published
#: but unattributed vote can be read against.
SEAT_BLOC = {"Governor, Chair": "Executive", "Deputy Governor": "Executive",
             "Secretary to the Treasury": "Ex officio", "External member": "External"}


@dataclass(frozen=True)
class Member:
    name: str
    role: str
    since: str          # "1 March 2025" as printed, "" when absent
    photo: str          # absolute URL, "" when absent
    page: str           # absolute URL of the member's own page

    @property
    def bloc(self) -> str:
        return SEAT_BLOC.get(self.role, "External")


_ITEM = re.compile(r'<div class="item"[^>]*>(.*?)(?=<div class="item"|<h2)', re.S)
_IMG = re.compile(r'<img[^>]+src="([^"]+)"')
_NAME = re.compile(r'<h3 class="name">\s*([^:<]+):\s*<a href="([^"]+)">(.*?)</a>', re.S)
_SINCE = re.compile(r"Member since\s+([^<]+)")


def _text(fragment: str) -> str:
    """Tag-free, entity-decoded, whitespace-collapsed; honours (AM, AO, PSM)
    dropped so the name matches the roster and the minutes."""
    fragment = re.sub(r"<abbr[^>]*>.*?</abbr>", "", fragment, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def parse(page: str) -> list[Member]:
    """Every member listed on the Board page, in seat order (Chair, Deputy,
    Treasury, then the externals as the page lists them)."""
    out: list[Member] = []
    for block in _ITEM.findall(page):
        m = _NAME.search(block)
        if not m:
            continue
        seat = _text(m.group(1)).lower()
        role = ROLES.get(seat)
        if role is None:
            continue
        img = _IMG.search(block)
        since = _SINCE.search(block)
        out.append(Member(
            name=_text(m.group(3)), role=role,
            since=_text(since.group(1)) if since else "",
            photo=(ROOT + img.group(1).split("?")[0]) if img else "",
            page=ROOT + m.group(2)))
    return sorted(out, key=lambda x: SEAT_ORDER.index(x.role))


def members(max_age: float = _MEMBERS_TTL) -> list[Member]:
    """The current Board, from the page, disk-cached for a day; the stale cache
    when the page cannot be read; [] when there is neither."""
    cached: list[Member] = []
    try:
        cached = [Member(**r) for r in json.loads(_MEMBERS.read_text(encoding="utf-8"))]
        if time.time() - _MEMBERS.stat().st_mtime < max_age and cached:
            return cached
    except (OSError, ValueError, TypeError):
        pass
    raw = _get(PAGE)
    fresh = parse(raw.decode("utf-8", "ignore")) if raw else []
    if not fresh:
        return cached
    try:
        _MEMBERS.parent.mkdir(parents=True, exist_ok=True)
        _MEMBERS.write_text(json.dumps([asdict(m) for m in fresh], indent=1), encoding="utf-8")
    except OSError:
        pass
    return fresh


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


def _read_cached(name: str) -> bytes | None | str:
    """`bytes` on a hit, the string ``"miss"`` on a fresh miss, `None` when the
    cache has nothing usable and a fetch should run."""
    hit = _CACHE / f"{_safe(name)}.png"
    miss = _CACHE / f"{_safe(name)}.miss"
    now = time.time()
    try:
        if hit.exists() and now - hit.stat().st_mtime < _HIT_TTL:
            return hit.read_bytes()
        if miss.exists() and now - miss.stat().st_mtime < _MISS_TTL:
            return "miss"
    except OSError:
        pass
    return None


def _write_hit(name: str, png: bytes) -> None:
    try:
        _CACHE.mkdir(parents=True, exist_ok=True)
        (_CACHE / f"{_safe(name)}.png").write_bytes(png)
        (_CACHE / f"{_safe(name)}.miss").unlink(missing_ok=True)
    except OSError:
        pass


def _write_miss(name: str) -> None:
    try:
        _CACHE.mkdir(parents=True, exist_ok=True)
        (_CACHE / f"{_safe(name)}.miss").write_bytes(b"")
    except OSError:
        pass


def _get(url: str) -> bytes | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.content:
        return None
    return r.content


def _to_thumb_png(raw: bytes) -> bytes | None:
    """Centre-crop to a square, resize, emit PNG. A headshot's subject sits a
    little above centre, so the crop is biased up."""
    if Image is None:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
    except Exception:
        return None
    w, h = im.size
    if min(w, h) < 24:
        return None
    side = min(w, h)
    left = (w - side) // 2
    top = max(0, (h - side) // 2 - int(side * 0.10))
    im = im.crop((left, top, left + side, top + side)).resize(
        (_THUMB, _THUMB), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=True)
    return out.getvalue()




def photo_png(name: str, url: str | None = None) -> bytes | None:
    """A normalised PNG thumbnail for one member, or None if no source
    resolves. Disk-cached, misses included."""
    name = str(name).strip()
    if not name:
        return None
    cached = _read_cached(name)
    if isinstance(cached, bytes):
        return cached
    if cached == "miss":
        return None
    if url is None:
        url = next((m.photo for m in members() if m.name == name), "")
    raw = _get(url) if url else None
    png = _to_thumb_png(raw) if raw and len(raw) >= 512 else None
    if png is None:
        _write_miss(name)
        return None
    _write_hit(name, png)
    return png


def photos(names) -> dict[str, bytes]:
    """`{name: png}` for every name that resolved -- absent names get nothing
    and the caller falls back to a monogram. Fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    urls = {m.name: m.photo for m in members()}
    names = [str(n).strip() for n in names]
    with ThreadPoolExecutor(max_workers=8) as ex:
        pngs = list(ex.map(lambda n: photo_png(n, urls.get(n, "")), names))
    return {n: png for n, png in zip(names, pngs) if png is not None}


def clear_cache() -> int:
    """Drop every cached thumbnail and miss marker. Returns the file count."""
    n = 0
    try:
        for f in _CACHE.glob("*"):
            f.unlink()
            n += 1
    except OSError:
        pass
    return n
