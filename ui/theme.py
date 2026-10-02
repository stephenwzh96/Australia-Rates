"""Palette and theme resolution."""

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
    voter_ink: str = "#0f7a4f"
    #: The selection colour (.streamlit/config.toml primaryColor): selected
    #: tabs, focus, the countdown when a decision is close. Never a P&L colour.
    select: str = "#f0a53a"
    #: Hairline for structure on the page (the theme's borderColor).
    rule: str = "#262a30"
    # Eight categorical slots, assigned in this fixed order and never cycled:
    # the CPI composition stack and the multi-line charts are the only ones
    # that need more than three identities. Validated as a set in both modes on
    # the adjacent pairlist (worst adjacent CVD dE 9.1 light, 8.4 dark).
    categorical: tuple[str, ...] = ()


#: `surface` is the page itself (.streamlit/config.toml backgroundColor), so a
#: chart's paper is invisible and every chart -- Plotly or Altair -- sits flush
#: on the page instead of on a card of a slightly different grey. `plane`, the
#: plot area, is the same colour: structure comes from the grid hairlines.
LIGHT = Palette(
    mode="light",
    surface="#fbfaf7", plane="#fbfaf7",
    ink="#16181c", ink_secondary="#4e5058", muted="#8a8c93",
    grid="#e6e3da", baseline="#c9c5ba",
    positive="#2a78d6", negative="#e34948", accent="#1baf7a",
    seq_low="#bee2d5", seq_high="#033a26",
    voter_ink="#0f7a4f",
    warning="#996903", serious="#c74817",
    select="#b86e00", rule="#dcd9d0",
    categorical=("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                 "#e87ba4", "#008300", "#4a3aa7", "#e34948"),
)

DARK = Palette(
    mode="dark",
    surface="#0c0d0f", plane="#0c0d0f",
    ink="#e7e5e0", ink_secondary="#b8b6af", muted="#858a93",
    grid="#1f2227", baseline="#3a3e46",
    positive="#3987e5", negative="#e66767", accent="#199e70",
    seq_low="#21634d", seq_high="#bdfae5",
    voter_ink="#5ad39a",
    categorical=("#3987e5", "#d95926", "#199e70", "#c98500",
                 "#d55181", "#008300", "#9085e9", "#e66767"),
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


#: Release families: the four-colour language every calendar row, Monte Carlo
#: release marker and reaction card shares. The US version's "Business surveys"
#: slot is Activity here -- Australia's tier-1 non-labour, non-price print is
#: the National Accounts, not a PMI -- and the Fed's slot is the RBA's own.
RELEASE_FAMILIES = ("labour", "inflation", "activity", "rba")
FAMILY_NAME = {"labour": "Labour market", "inflation": "Inflation",
               "activity": "Activity", "rba": "RBA calendar"}
FAMILY_SOLID = {"labour": "#3b82f6", "inflation": "#d97706",
                "activity": "#0d9488", "rba": "#8b5cf6"}
FAMILY_TINT = {"labour": "rgba(59,130,246,0.11)", "inflation": "rgba(217,119,6,0.12)",
               "activity": "rgba(13,148,136,0.11)", "rba": "rgba(139,92,246,0.12)"}

#: Calendar row label -> family (`core.econ_calendar.Event.label`).
EVENT_FAMILY = {
    "Labour Force (employment, unemployment rate)": "labour",
    "Wage Price Index": "labour",
    "CPI, quarterly (headline and trimmed mean)": "inflation",
    "Monthly CPI indicator": "inflation",
    "Monthly CPI": "inflation",
    "National Accounts (GDP)": "activity",
    "Household Spending Indicator": "activity",
}

#: Release (the report, `Event.release`) -> family, for the Monte Carlo
#: markers and the vol-multiplier table.
RELEASE_FAMILY = {
    "Labour Force": "labour",
    "Wage Price Index": "labour",
    "CPI (quarterly)": "inflation",
    "Monthly CPI indicator": "inflation",
    "National Accounts (GDP)": "activity",
    "RBA decision": "rba",
    "Statement on Monetary Policy": "rba",
    "RBA minutes": "rba",
}
RELEASE_FAMILY_COLOUR = {name: FAMILY_SOLID[fam] for name, fam in RELEASE_FAMILY.items()}


def event_family(label: str) -> str | None:
    """Which release family a calendar row belongs to, if any. RBA rows carry
    a suffix when the meeting has a Statement, so they match by prefix."""
    fam = EVENT_FAMILY.get(label)
    if fam:
        return fam
    if label.startswith("RBA decision") or label.startswith("RBA minutes"):
        return "rba"
    return None


def tint(colour: str, alpha: float) -> str:
    """A hex palette colour as a translucent `rgba()` fill."""
    h = colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "transparent"
    return f"rgba({r},{g},{b},{max(0.0, min(1.0, alpha)):.3f})"
