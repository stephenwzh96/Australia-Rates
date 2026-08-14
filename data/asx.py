"""ASX short-end futures strips -- IB (30 day interbank cash rate) and IR
(90 day bank accepted bill).

WHERE THIS COMES FROM, AND WHY NOT YAHOO
-----------------------------------------
The US original reads its fed funds strip off Yahoo Finance's chart endpoint,
which carries the CBOT contracts. That route does not exist here: Yahoo serves
live ZQ and SR3 quotes but has no ASX rate futures at all -- every symbol
convention (`IRU26.ASX`, `IRU26.SFE`, `IR=F`, bare `IRU26`) returns 404. Checked
directly rather than assumed.

What does work is the JSON feed behind ASX's own derivatives price pages:

    https://asx.api.markitdigital.com/asx-research/1.0/derivatives/
        interest-rate/{IB|IR}/futures?days=1&height=179&width=179

Free, keyless, and richer than the Yahoo route it replaces -- bid, ask,
previous settlement, session volume and contract high/low, for the whole strip
in ONE request instead of eighteen parallel ones. It is an undocumented vendor
endpoint rather than a published API, so it is treated as fragile: every field
is read defensively and any failure degrades to an error string, never an
exception. A dead quote must never block the Verdict, and the dashboard has to
stay usable offline with prices typed by hand.

TWO TRAPS IN THE PAYLOAD, BOTH CONFIRMED LIVE
----------------------------------------------
1. `dateExpiry` IS NOT THE LAST TRADING DAY. It reports exactly two calendar
   days early on every single contract -- verified across all 18 IB and all 18
   IR contracts. IB's real last trading day is the final business day of the
   delivery month; IR's is the business day before the second Friday. Deriving
   the settlement window from this field would shift IB's averaging month and
   silently corrupt every implied probability in `core.strip`. So the contract
   month is parsed from the SYMBOL (`IBQ2026` -> August 2026), and the real
   dates come from `core.contracts` / `core.holidays`.

2. QUOTES GO MISSING AND ARRIVE OUT OF ORDER. Illiquid contracts come back with
   no `priceContract` key at all (confirmed on IRU2029, IRZ2029, IRM2030, and
   both IR serials), and the two IR serial months are appended AFTER the
   quarterlies despite being front-month. Everything is therefore re-sorted by
   the parsed contract month, and a missing price becomes a `Quote` carrying an
   error rather than a `KeyError`.

SETTLEMENT MECHANICS the rest of the arithmetic depends on: an IB contract
settles at 100 minus the SIMPLE AVERAGE of the daily cash rate across its
delivery month, so `100 - price` is the market's average expected cash rate for
that whole month, not the rate on any single day -- which is precisely why a
meeting has to be un-blended out of it (see `core.strip`). An IR contract
settles at 100 minus a single 3-month BBSW fix, so `100 - price` is a forward
BBSW rate and needs no un-blending at all (see `core.bbsw`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

from core.contracts import MONTH_CODES, ib_contract_code, ir_contract_code

from . import cache
from .common import Observation

BASE = ("https://asx.api.markitdigital.com/asx-research/1.0/"
        "derivatives/interest-rate/{kind}/futures")

# The page's own query string. `days` is accepted but inert -- days=1 and
# days=250 return byte-identical payloads, so there is no history here and
# volatility is calibrated from RBA rate series instead (see `data.rba`).
PARAMS = {"days": 1, "height": 179, "width": 179}

TIMEOUT = 10

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}

# IR trades the quarterly cycle plus a couple of serials. The quarterlies are
# where the open interest is and what a desk quotes, but the serials are kept:
# unlike the US serial SOFR contracts, an IR serial can be the only contract
# whose fix sits on the near side of a meeting.
QUARTERLY_MONTHS = (3, 6, 9, 12)


@dataclass(frozen=True)
class Quote:
    month: date                      # first of the delivery month, parsed from the symbol
    symbol: str                      # the feed's own form, e.g. "IBQ2026"
    price: float | None = None
    prev_close: float | None = None  # last TRADED price -- see `source`, not a mark
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    asof: date | None = None         # date of the price actually used
    last_trade: date | None = None   # when it last changed hands
    change_one_day: float | None = None   # the feed's own day change, in price
    source: str = "settlement"       # "settlement" | "midpoint" | "last trade"
    error: str | None = None
    kind: str = "ib"                 # "ib" | "ir"

    @property
    def ok(self) -> bool:
        return self.error is None and self.price is not None

    @property
    def label(self) -> str:
        return self.month.strftime("%b %Y")

    @property
    def code(self) -> str:
        """The short code a STIR desk says out loud, e.g. IBU6, IRZ6."""
        return (ir_contract_code if self.kind == "ir" else ib_contract_code)(self.month)

    @property
    def implied_rate(self) -> float | None:
        """Percent. IB: average cash rate over the delivery month. IR: the
        3-month BBSW fix the contract settles against."""
        return None if self.price is None else 100.0 - self.price

    @property
    def change_bp(self) -> float | None:
        """Session change in RATE terms (bp). Price up = rate down, so signs flip.

        Taken from the feed's own `priceChangeOneDay` rather than differencing
        our chosen price against the last trade: those two can be weeks apart
        on a back contract, and subtracting them would report staleness as if
        it were a market move.
        """
        if self.change_one_day is None:
            return None
        return -self.change_one_day * 100.0

    @property
    def spread_bp(self) -> float | None:
        """Bid/ask width in bp -- the liquidity read the US feed could not give.
        A wide spread on a back contract is the same warning `is_stale` gives,
        arriving a day earlier."""
        if self.bid is None or self.ask is None:
            return None
        return abs(self.ask - self.bid) * 100.0

    @property
    def days_since_trade(self) -> int | None:
        if self.asof is None or self.last_trade is None:
            return None
        return (self.asof - self.last_trade).days


def month_from_symbol(symbol: str) -> date | None:
    """`IBQ2026` -> 2026-08-01. The only trustworthy month source in the feed.

    Returns None rather than raising on anything unexpected: a new contract
    naming convention should cost one row, not the whole strip.
    """
    if len(symbol) < 4:
        return None
    code, year = symbol[2], symbol[3:]
    if code not in MONTH_CODES or not year.isdigit():
        return None
    try:
        return date(int(year), MONTH_CODES.index(code) + 1, 1)
    except ValueError:
        return None


def _f(row: dict, *keys: str) -> float | None:
    """First present, numeric value among `keys`. The feed omits keys entirely
    on illiquid contracts rather than sending null, so `.get(k)` alone is not
    enough -- the type has to be checked too."""
    for k in keys:
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _parse(row: dict, kind: str) -> Quote | None:
    symbol = row.get("symbol")
    if not isinstance(symbol, str):
        return None
    month = month_from_symbol(symbol)
    if month is None:
        return None

    def _date(*keys: str) -> date | None:
        for k in keys:
            v = row.get(k)
            if isinstance(v, str):
                try:
                    return date.fromisoformat(v[:10])
                except ValueError:
                    continue
        return None

    bid, ask = _f(row, "priceBid"), _f(row, "priceAsk")
    settle = _f(row, "pricePreviousSettlement")
    traded = _f(row, "priceContract", "priceLastTrade")

    # PRICE PRIORITY. Settlement first, and this ordering is the whole
    # correctness of the strip.
    #
    # `priceContract` reads like the obvious choice and is what the ASX page
    # displays, but it is the LAST TRADED price, not a daily mark -- and the
    # back of the IB strip barely trades. Measured against the same payload:
    # IBV2027's last trade was two months old and sat 17.5bp away from its
    # settlement price, IBG2027 15.5bp, IBF2027 10.5bp. Reading the strip off
    # those would put most of a rate move of pure staleness into every implied
    # probability behind them, which is exactly the failure `is_stale` exists
    # to catch in the US original.
    #
    # `pricePreviousSettlement` is ASX's official daily mark: present on all 18
    # contracts of both strips and stamped with the same current date, whereas
    # `priceContract` is missing on 8 of 18. The bid/ask midpoint sits between
    # the two in reliability -- live, but only as good as the quoted width.
    if settle is not None:
        price, asof, source = settle, _date("datePreviousSettlement"), "settlement"
    elif bid is not None and ask is not None:
        price, asof, source = (bid + ask) / 2.0, _date("datePreviousSettlement"), "midpoint"
    elif traded is not None:
        # Last resort, and stamped with the date it actually traded so
        # `is_stale` can see how old it is rather than inheriting today's.
        price, asof, source = traded, _date("dateLastTrade"), "last trade"
    else:
        return Quote(month, symbol, kind=kind, bid=bid, ask=ask,
                     asof=_date("datePreviousSettlement"), error="no price")

    return Quote(
        month=month, symbol=symbol, price=price,
        prev_close=_f(row, "priceContract", "priceLastTrade"),
        bid=bid, ask=ask, volume=_f(row, "combinedVolume", "volume"),
        asof=asof, last_trade=_date("dateLastTrade"),
        change_one_day=_f(row, "priceChangeOneDay"), source=source, kind=kind,
    )


def fetch_strip(kind: str = "ib") -> list[Quote]:
    """The whole strip in one request, sorted by delivery month.

    `kind` is "ib" or "ir". Never raises: on any failure the strip comes back
    empty and the caller falls through to typed prices.
    """
    if kind not in ("ib", "ir"):
        raise ValueError(f"unknown strip kind {kind!r}")
    try:
        r = requests.get(BASE.format(kind=kind.upper()), params=PARAMS,
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException:
        return []
    except ValueError:
        return []

    items = (payload or {}).get("data", {}).get("items")
    if not isinstance(items, list):
        return []

    out = [q for q in (_parse(row, kind) for row in items if isinstance(row, dict))
           if q is not None]
    # The feed appends serials after the quarterlies; the strip is only
    # meaningful in date order.
    out.sort(key=lambda q: q.month)
    return out


def fetch_strip_cached(kind: str = "ib") -> list[Quote]:
    """`fetch_strip` with the last good response kept on disk.

    The endpoint is undocumented and could disappear without notice, so the
    most recent successful strip is retained and served -- clearly stamped with
    its own `asof` -- rather than showing an empty Curve tab. A stale strip that
    announces its date is more useful than no strip; a stale strip pretending to
    be live is not, which is what `is_stale` and `coverage_end` below are for.
    """
    quotes = fetch_strip(kind)
    if quotes:
        cache.write_json(f"asx-{kind}-strip", [_to_dict(q) for q in quotes])
        return quotes
    rows = cache.read_json(f"asx-{kind}-strip")
    return [_from_dict(r, kind) for r in rows] if rows else []


def _to_dict(q: Quote) -> dict:
    return {"symbol": q.symbol, "price": q.price, "prev_close": q.prev_close,
            "bid": q.bid, "ask": q.ask, "volume": q.volume,
            "change_one_day": q.change_one_day, "source": q.source,
            "asof": q.asof.isoformat() if q.asof else None,
            "last_trade": q.last_trade.isoformat() if q.last_trade else None}


def _from_dict(r: dict, kind: str) -> Quote:
    month = month_from_symbol(r.get("symbol", "")) or date.today().replace(day=1)

    def _d(key: str) -> date | None:
        v = r.get(key)
        try:
            return date.fromisoformat(v) if v else None
        except (ValueError, TypeError):
            return None

    return Quote(month=month, symbol=r.get("symbol", ""), price=r.get("price"),
                 prev_close=r.get("prev_close"), bid=r.get("bid"), ask=r.get("ask"),
                 volume=r.get("volume"), change_one_day=r.get("change_one_day"),
                 source=r.get("source", "settlement"),
                 asof=_d("asof"), last_trade=_d("last_trade"), kind=kind)


def latest_asof(quotes: list[Quote]) -> date | None:
    stamps = [q.asof for q in quotes if q.ok and q.asof]
    return max(stamps) if stamps else None


# A contract that has not traded in this many calendar days is being marked
# rather than priced. ASX still publishes a settlement for it, so the number is
# not wrong -- it is just an exchange model's opinion, not a market's. Three
# weeks is deliberately generous: the front IB contracts often go a week
# between trades while still quoting half a basis point, and treating those as
# unreliable would throw away the most informative part of the strip.
TRADE_GAP_DAYS = 15

# Wider than this and the quoted market is not tight enough to read a meeting
# out of: half a rate move of ambiguity in the price itself.
#
# Calibrated against the live strip rather than guessed. IB's quoted width
# SATURATES AT EXACTLY 3.0bp from about six months out -- every one of the back
# eleven contracts shows 3.0, which is a market-maker obligation cap, not a
# liquidity reading. So a threshold at or below 3 would flag the entire back
# strip on what is really one dealer's quoting rule, and a threshold just above
# it discriminates nothing for IB at all. IR is the opposite: genuine widths
# run 1-2bp on the liquid quarterlies, 5-8bp on the 2029-30 tail, and 32-42bp
# on the two untraded serials. 5bp is set to catch that tail and those serials
# while leaving IB's obligation cap alone -- for IB, the trade-gap and
# never-traded tests below are what actually do the work.
WIDE_SPREAD_BP = 5.0


def is_stale(quote: Quote, reference: date | None,
             tolerance_days: int = 3) -> bool:
    """True when a contract's price should stay out of the headline numbers.

    THIS IS NOT THE SAME TEST AS THE US VERSION, because the feed is not the
    same shape. Yahoo gave one price per contract and a timestamp, so "has it
    printed lately" was the only available question. ASX publishes a fresh
    SETTLEMENT for every contract every day, so a date check alone would never
    fire -- every quote looks current, including ones nobody has traded in two
    months.

    So staleness here is about the MARKET, not the timestamp. Three ways in:

      * the price we ended up using is not a settlement (see `_parse`),
      * nothing has traded for `TRADE_GAP_DAYS`,
      * or the quoted bid/ask is wider than `WIDE_SPREAD_BP`.

    Reading a meeting off any of those produces a confident number from very
    little, so they are marked rather than quietly trusted -- `core.strip`
    keeps them in the ladder but out of the terminal rate and the peak.
    """
    if not quote.ok:
        return False
    if quote.source == "last trade":
        return True
    if reference is not None and quote.asof is not None:
        if (reference - quote.asof).days > tolerance_days:
            return True
    gap = quote.days_since_trade
    if gap is not None and gap > TRADE_GAP_DAYS:
        return True
    if quote.last_trade is None and not quote.volume:
        return True
    spread = quote.spread_bp
    return spread is not None and spread > WIDE_SPREAD_BP


def coverage_end(quotes: list[Quote], reference: date | None,
                 tolerance_days: int = 3) -> date | None:
    """Last day the strip can actually speak for.

    The end of the final delivery month that is both quoted and printing with
    the rest of the strip. Anything derived past this is extrapolation dressed
    as a market price -- the whole point of computing it is to refuse to.
    """
    import calendar
    fresh = [q.month for q in quotes
             if q.ok and not is_stale(q, reference, tolerance_days)]
    if not fresh:
        return None
    last = max(fresh)
    return date(last.year, last.month, calendar.monthrange(last.year, last.month)[1])


def stale_codes(quotes: list[Quote], reference: date | None,
                tolerance_days: int = 3) -> set[str]:
    return {q.code for q in quotes if is_stale(q, reference, tolerance_days)}
