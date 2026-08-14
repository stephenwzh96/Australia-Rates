"""Shared presentation pieces.

The `?` control is the point of the dashboard: every derived number carries the
formula that produced it AND the substitution using the CURRENT inputs. That
substitution is the audit trail -- it is what makes a points-vs-probability
mix-up visible on screen instead of buried three steps downstream.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Iterable, Sequence

import streamlit as st

from .theme import Palette

CSS_FILE = Path(__file__).with_name("style.css")


def inject_css() -> None:
    st.markdown(f"<style>{CSS_FILE.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def formula_help(latex: str, plain: str, worked: str, key: str) -> None:
    """The small circular `?` next to a figure.

    Styled globally in style.css via `[data-testid="stPopoverButton"]` -- safe
    because every popover in this app is a formula help. `key` is kept for
    caller readability and to keep widget identities stable across reruns.
    Empty `latex`/`worked` (a pure text note) are skipped.
    """
    with st.popover("?", help=plain):
        if latex:
            st.latex(latex)
        st.caption(plain)
        if worked:
            st.code(worked, language="text")


def labelled_metric(label: str, value: str, latex: str, plain: str, worked: str,
                    key: str, colour: str | None = None, caption: str | None = None) -> None:
    """A figure with its formula one click away."""
    head, helpcol = st.columns([1, 0.16], gap="small", vertical_alignment="center")
    with head:
        st.markdown(f'<div class="fomc-label">{html.escape(label)}</div>', unsafe_allow_html=True)
        style = f'color:{colour};' if colour else ""
        st.markdown(
            f'<div class="fomc-num" style="font-size:1.35rem;font-weight:600;line-height:1.5;{style}">'
            f"{html.escape(value)}</div>",
            unsafe_allow_html=True,
        )
        if caption:
            st.markdown(f'<div class="fomc-note">{html.escape(caption)}</div>', unsafe_allow_html=True)
    with helpcol:
        formula_help(latex, plain, worked, key)


def chips(items: Sequence[tuple[str, str | None]]) -> None:
    """Header strip. Each item is (text, colour-or-None)."""
    parts = []
    for text, colour in items:
        style = f' style="color:{colour}"' if colour else ""
        parts.append(f'<span class="fomc-chip"{style}>{html.escape(text)}</span>')
    st.markdown(f'<div class="fomc-strip">{"".join(parts)}</div>', unsafe_allow_html=True)


def table(headers: Sequence[str], rows: Iterable[Sequence[object]],
          numeric: Sequence[int] = (), mark_rows: Sequence[int] = (),
          colours: dict[tuple[int, int], str] | None = None,
          row_bg: dict[int, str] | None = None,
          nowrap: bool = False) -> None:
    """Compact HTML table. `mark_rows` get the ▸ prefix and bold treatment.

    `row_bg` paints a whole row (a translucent CSS colour, e.g. an rgba()
    string) -- for grouping rows into visual categories without touching the
    per-cell foreground colours in `colours`, which stay legible on top of it.

    `nowrap` keeps every cell on one line and lets the table scroll sideways
    inside its own box instead. For a reference table -- a series id, a
    transform, a direction -- a wrapped cell doubles that row's height and
    breaks the eye's ability to scan a column, and the row is a lookup rather
    than prose, so scrolling beats wrapping. The scroll stays inside the
    table's own container, so the page itself never scrolls horizontally.
    """
    cols = {i for i in numeric}
    cell_colour = colours or {}
    bg = row_bg or {}
    head = "".join(
        f'<th class="{"n" if i in cols else ""}">{html.escape(str(h))}</th>'
        for i, h in enumerate(headers)
    )
    body = []
    for r_i, row in enumerate(rows):
        classes = "mark" if r_i in mark_rows else ""
        row_style = f'background:{bg[r_i]};' if r_i in bg else ""
        cls = f' class="{classes}"' if classes else ""
        style_attr = f' style="{row_style}"' if row_style else ""
        tds = []
        for c_i, cell in enumerate(row):
            colour = cell_colour.get((r_i, c_i))
            style = f' style="color:{colour}"' if colour else ""
            tds.append(
                f'<td class="{"n" if c_i in cols else ""}"{style}>{cell if isinstance(cell, str) and cell.startswith("<") else html.escape(str(cell))}</td>'
            )
        body.append(f"<tr{cls}{style_attr}>{''.join(tds)}</tr>")
    tbl = (f'<table class="fomc-table{" nowrap" if nowrap else ""}">'
           f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>')
    st.markdown(f'<div class="fomc-scroll">{tbl}</div>' if nowrap else tbl,
                unsafe_allow_html=True)


def legend(items: Sequence[tuple[str, str]]) -> None:
    """A small inline colour key: [(label, css-colour), ...]."""
    chips_html = "".join(
        f'<span class="fomc-legend-item">'
        f'<span class="fomc-legend-dot" style="background:{colour}"></span>'
        f'{html.escape(label)}</span>'
        for label, colour in items
    )
    st.markdown(f'<div class="fomc-legend">{chips_html}</div>', unsafe_allow_html=True)


def verdict_row(key: str, value: str, colour: str | None = None, emph: bool = False) -> str:
    style = f' style="color:{colour}"' if colour else ""
    cls = "fomc-verdict-row emph" if emph else "fomc-verdict-row"
    return (f'<div class="{cls}"><span class="k">{html.escape(key)}</span>'
            f'<span class="v"{style}>{html.escape(value)}</span></div>')


# Only emphasis is supported in a note, and it is applied post-escape.
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def note(text: str) -> None:
    """An explanatory aside. `**bold**` is honoured; nothing else is.

    The text is HTML-escaped first and the emphasis applied to the escaped
    string afterwards, so the markup can only ever produce a `<strong>` -- a
    stray angle bracket in a note stays inert rather than becoming a tag.
    """
    if text:
        body = _BOLD.sub(r"<strong>\1</strong>", html.escape(text))
        st.markdown(f'<div class="fomc-note">{body}</div>', unsafe_allow_html=True)


def vintage(line: str) -> None:
    """The source-vintage line, for a chart whose title is a Streamlit header.

    Charts that carry their title inside the figure get this from Altair's or
    Plotly's own subtitle slot (see `charts.vintage_title` / `apply_vintage`).
    The handful whose title is a markdown header instead -- because it needs a
    `?` popover next to it -- render the same line through here, so both kinds
    read identically. Takes the already-formatted `Vintage.line`, and draws
    nothing when it is empty.
    """
    if line:
        st.markdown(f'<div class="fomc-vintage">{html.escape(line)}</div>',
                    unsafe_allow_html=True)


def section(title: str, subtitle: str = "") -> None:
    st.markdown(f"### {title}")
    if subtitle:
        note(subtitle)


# ---------------------------------------------------------------- formatting

def data_source_header(name: str, ok: bool, detail: str, p: Palette) -> None:
    """One line of health for an external source: name, a coloured dot, a
    one-line detail. The Data tab's whole point is a consistent read across
    unrelated sources (ASX, RBA) -- a future source reuses this
    instead of inventing its own status treatment."""
    dot_colour = p.good if ok else p.critical
    st.markdown(
        f'<div style="display:flex;align-items:baseline;gap:0.5rem;margin:0.1rem 0">'
        f'<span style="font-weight:600;font-size:0.92rem">{html.escape(name)}</span>'
        f'<span style="color:{dot_colour};font-size:0.7rem">{"●" if ok else "○"}</span>'
        f'<span class="fomc-note">{html.escape(detail)}</span>'
        f'</div>', unsafe_allow_html=True)

