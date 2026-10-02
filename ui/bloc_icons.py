"""Pictograms for the roster's blocs, as one-colour SVG data URIs.

A hawk and a dove for the two ends of the policy line, a balance for the
centre that holds it, a bar chart for the members who move with the prints.
The Board's seat groups (Executive, Ex officio, External) -- the roster's
default blocs -- carry none.
Drawn on a 24-unit grid from solid shapes only, so they survive being shown
at 14px, and filled with one ink so they take the theme and never compete
with the chart's red/blue, which already means "moved"."""

from __future__ import annotations

import base64

from core import roster as R

HAWK = (
    # heraldic raptor: wings raised in a V, fingered primaries, head in
    # profile with a hooked beak, fanned tail
    '<polygon points="11,10.3 6.6,7.7 2.6,5.1 0.5,5.2 2.2,6.8 0.7,7.4 2.9,8.2 '
    '1.6,9.2 4.3,9.5 3.5,10.6 6.2,10.7 8.4,11.9 10.8,12.9"/>'
    '<polygon points="13,10.3 17.4,7.7 21.4,5.1 23.5,5.2 21.8,6.8 23.3,7.4 '
    '21.1,8.2 22.4,9.2 19.7,9.5 20.5,10.6 17.8,10.7 15.6,11.9 13.2,12.9"/>'
    '<ellipse cx="12" cy="12.9" rx="2.05" ry="3.8"/>'
    '<circle cx="12.1" cy="8.2" r="1.65"/>'
    '<polygon points="10.8,7.3 9.0,7.7 8.6,8.4 9.3,9.3 9.5,8.6 10.6,8.9"/>'
    '<polygon points="10.4,15.4 13.6,15.4 15.7,20.3 12,21.1 8.3,20.3"/>'
)

DOVE = (
    # dove in flight, wing raised, olive sprig in the beak
    '<ellipse cx="11" cy="13.2" rx="6.2" ry="3.2" transform="rotate(-8 11 13.2)"/>'
    '<circle cx="5.3" cy="10.6" r="2.1"/>'
    '<polygon points="3.4,10.2 1.7,11.1 3.5,11.5"/>'
    '<polygon points="8.3,11.2 10.5,6.8 14,3.8 19.6,2.0 17.9,4.3 20.2,4.3 '
    '17.7,6.4 19.8,6.9 16.9,8.5 18.3,9.4 14.5,11.6"/>'
    '<polygon points="15.6,12.2 22.6,14.4 22.2,16.9 15.8,15.6"/>'
    '<path d="M2.6 11.3C1.6 12.6 1.4 14 1.7 15.6" fill="none" stroke-width=".6" '
    'stroke-linecap="round"/>'
    '<ellipse cx="1.2" cy="12.8" rx=".95" ry=".42" transform="rotate(-35 1.2 12.8)"/>'
    '<ellipse cx="2.7" cy="14" rx=".95" ry=".42" transform="rotate(30 2.7 14)"/>'
    '<ellipse cx="1.1" cy="15.1" rx=".95" ry=".42" transform="rotate(-35 1.1 15.1)"/>'
)

BALANCE = (
    # the centre: post, beam, two level pans, a base
    '<circle cx="12" cy="4" r="1.4"/>'
    '<rect x="11.2" y="4.5" width="1.6" height="14.5" rx=".4"/>'
    '<rect x="3.5" y="5.8" width="17" height="1.5" rx=".75"/>'
    '<path d="M7.5 19.2h9c.6 0 1 .5 1 1v.8h-11v-.8c0-.5.4-1 1-1z"/>'
    '<path d="M5 7.2l-3 6M5 7.2l3 6M19 7.2l-3 6M19 7.2l3 6" fill="none" '
    'stroke-width=".8" stroke-linecap="round"/>'
    '<path d="M1.3 13.2h7.4a3.7 3.7 0 0 1-7.4 0zM15.3 13.2h7.4a3.7 3.7 0 0 1-7.4 0z"/>'
)

DATA = (
    # bars read against a trend line: policy set by the prints
    '<rect x="3" y="12" width="4" height="8" rx=".6"/>'
    '<rect x="10" y="8" width="4" height="12" rx=".6"/>'
    '<rect x="17" y="13.5" width="4" height="6.5" rx=".6"/>'
    '<rect x="1.5" y="20.6" width="21" height="1.4" rx=".7"/>'
    '<path d="M3 8.5l6-4 5 3 7-4.5" fill="none" stroke-width="1.4" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="3" cy="8.5" r="1.3"/><circle cx="9" cy="4.5" r="1.3"/>'
    '<circle cx="14" cy="7.5" r="1.3"/><circle cx="21" cy="3" r="1.3"/>'
)

OTHER = (
    # any label outside the four: a plain ring, present but unclaimed
    '<circle cx="12" cy="12" r="6.5" fill="none" stroke-width="2.4"/>'
)

ICONS = {"hawks": HAWK, "hawk": HAWK, "doves": DOVE, "dove": DOVE,
         "board centre": BALANCE, "centre": BALANCE, "center": BALANCE,
         "data dependent": DATA}

#: The roster's default blocs on the Board are its seat groups, not policy
#: leanings; they get no pictogram rather than three identical rings.
SEAT_BLOCS = {"executive", "ex officio", "external"}


def icon_uri(bloc: str | None, colour: str) -> str | None:
    """The bloc's pictogram in `colour`; None for a blank or unscored bloc."""
    key = str(bloc or "").strip().lower()
    if key in R.UNSCORED_BLOCS or key in SEAT_BLOCS:
        return None
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
           f'viewBox="0 0 24 24" fill="{colour}" stroke="{colour}" stroke-width="0">'
           f'{ICONS.get(key, OTHER)}</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
