"""Street calls: the sell-side's published views for this meeting."""

from __future__ import annotations

from ui import research
from ui.context import Ctx


def render(ctx: Ctx) -> None:
    research.bank_call_book(ctx)
