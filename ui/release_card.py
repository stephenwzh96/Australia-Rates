"""The release-prep reaction card (filled in by the Releases port)."""

from __future__ import annotations

from ui import components as C
from ui.context import Ctx


def prep_card(ctx: Ctx) -> None:
    C.heading("The reaction card", "written before the print, not after it")
