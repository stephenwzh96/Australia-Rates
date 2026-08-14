"""Palette and theme resolution.

Colours come from the dataviz reference palette and were run through its
validator as a three-slot categorical set in both modes:

    light  #2a78d6 / #e34948 / #1baf7a   ALL CHECKS PASS
    dark   #3987e5 / #e66767 / #199e70   ALL CHECKS PASS

Aqua-vs-red sits in the 6-8 CVD warn band, which is legal only with secondary
encoding -- so the aqua "your estimate" rule is *always* direct-labelled. Do not
ship it as a bare coloured line.

Role assignment:
  blue    positive P&L          (diverging arm)
  red     negative P&L          (diverging arm)
  aqua    your probability      (always direct-labelled)
  muted   breakeven / market-implied rule (dashed, direct-labelled)
  status  kill criteria only, always with an icon and a label
  seq     magnitude (e.g. touch probability along a swept curve) -- one hue,
          light to dark, same hue angle as accent; always paired with a
          colorbar legend and a hover tooltip, per the dataviz skill's rule
          that a sequential encoding is never colour-alone.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class Palette:
    mode: str
    surface: str
    plane: str
    ink: str
    ink_secondary: str
    muted: str
    grid: str
    baseline: str
    positive: str
    negative: str
    accent: str
    good: str = "#0ca30c"
    warning: str = "#fab219"
    serious: str = "#ec835a"
    critical: str = "#d03b3b"
    seq_low: str = "#bee2d5"
    seq_high: str = "#033a26"


LIGHT = Palette(
    mode="light",
    surface="#fcfcfb", plane="#f9f9f7",
    ink="#0b0b0b", ink_secondary="#52514e", muted="#898781",
    grid="#e1e0d9", baseline="#c3c2b7",
    positive="#2a78d6", negative="#e34948", accent="#1baf7a",
    # High end pushed to a near-black-teal rather than the mid-tone accent
    # itself -- WCAG contrast between the two ends is 9.2:1, more than double
    # the original 4.1:1, so a glance at either tip of the line reads as
    # unambiguously different rather than two shades of the same green.
    seq_low="#bee2d5", seq_high="#033a26",
)

DARK = Palette(
    mode="dark",
    surface="#1a1a19", plane="#0d0d0d",
    ink="#ffffff", ink_secondary="#c3c2b7", muted="#898781",
    grid="#2c2c2a", baseline="#383835",
    positive="#3987e5", negative="#e66767", accent="#199e70",
    # Dark theme's low end is lifted well above the near-black surface (same
    # floor the grid/baseline lines already sit at) so it reads as a dim line,
    # not as background; the high end is pushed brighter than the accent
    # itself for the same reason as light theme's dark end -- endpoint
    # contrast goes from 4.3:1 to 6.1:1.
    seq_low="#21634d", seq_high="#bdfae5",
)


def active() -> Palette:
    """Resolve the viewer's theme. Falls back to light if unavailable."""
    mode = None
    try:
        mode = st.context.theme.type          # Streamlit >= 1.44
    except Exception:
        pass
    if mode is None:
        try:
            mode = st.get_option("theme.base")
        except Exception:
            mode = "light"
    return DARK if str(mode).lower() == "dark" else LIGHT


def pnl_colour(value: float, p: Palette) -> str:
    """Diverging arm for a signed P&L figure; muted at exactly zero."""
    if value > 0:
        return p.positive
    if value < 0:
        return p.negative
    return p.muted
