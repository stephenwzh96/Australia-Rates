"""Shared presentation pieces."""

from __future__ import annotations

import base64
import html
import re
import time
from datetime import date
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

import streamlit as st

from .theme import Palette

CSS_FILE = Path(__file__).with_name("style.css")


def inject_css(p: Palette | None = None) -> None:
    """The stylesheet, preceded by the active theme's colours as variables.

    Streamlit exposes no CSS variables for its theme, and a sticky bar needs an
    opaque background in the page's own colour -- so the palette the charts
    already resolve (light or dark, following the viewer's setting) is written
    out as `--fomc-*` on every run."""
    tokens = ""
    if p is not None:
        tokens = (f":root{{--fomc-bg:{p.surface};--fomc-ink:{p.ink};"
                  f"--fomc-ink2:{p.ink_secondary};--fomc-muted:{p.muted};"
                  f"--fomc-grid:{p.grid};--fomc-rule:{p.rule};--fomc-select:{p.select};"
                  f"--fomc-pos:{p.positive};--fomc-neg:{p.negative};"
                  f"--fomc-warn:{p.warning};--fomc-crit:{p.critical};"
                  f"--fomc-good:{p.good}}}")
    st.markdown(f"<style>{tokens}{CSS_FILE.read_text(encoding='utf-8')}</style>",
                unsafe_allow_html=True)


def formula_help(latex: str, plain: str, worked: str, key: str) -> None:
    """The small circular `?` next to a figure."""
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
        st.markdown(f'<div class="fomc-lm-v" style="{style}">{html.escape(value)}</div>',
                    unsafe_allow_html=True)
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


def tape(event: str, countdown: str, countdown_colour: str | None,
         facts: Sequence[tuple[str, str | None]],
         figures: Sequence[tuple[str, str, str | None]]) -> None:
    """The header tape: the meeting and its countdown, the facts about it,
    and the trade's live figures -- one line, on every tab.

    `facts` are (text, colour-or-None); `figures` are (label, value,
    colour-or-None), set in the numeric face so they read like a quote line.
    """
    esc = html.escape

    def style(colour: str | None) -> str:
        return f' style="color:{colour}"' if colour else ""

    fact_html = "".join(f'<span class="fomc-tape-fact"{style(c)}>{esc(t)}</span>'
                        for t, c in facts)
    fig_html = "".join(
        f'<span class="fomc-tape-fig"><span class="l">{esc(label)}</span>'
        f'<span class="v"{style(c)}>{esc(value)}</span></span>'
        for label, value, c in figures)
    st.markdown(
        f'<div class="fomc-tape">'
        f'<span class="fomc-tape-event">{esc(event)}</span>'
        f'<span class="fomc-tape-count"{style(countdown_colour)}>{esc(countdown)}</span>'
        f'<span class="fomc-tape-facts">{fact_html}</span>'
        f'<span class="fomc-tape-figs">{fig_html}</span>'
        f'</div>', unsafe_allow_html=True)


def table(headers: Sequence[str], rows: Iterable[Sequence[object]],
          numeric: Sequence[int] = (), mark_rows: Sequence[int] = (),
          colours: dict[tuple[int, int], str] | None = None,
          row_bg: dict[int, str] | None = None,
          cell_bg: dict[tuple[int, int], str] | None = None,
          nowrap: bool = False) -> None:
    """Compact HTML table. `mark_rows` get the ▸ prefix and bold treatment."""
    cols = {i for i in numeric}
    cell_colour = colours or {}
    bg = row_bg or {}
    cell_fill = cell_bg or {}
    head = "".join(
        f'<th class="{"n" if i in cols else ""}">'
        f'{h if isinstance(h, str) and h.startswith("<") else html.escape(str(h))}</th>'
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
            fill = cell_fill.get((r_i, c_i))
            bits = ([f"color:{colour}"] if colour else []) + (
                [f"background:{fill}"] if fill else [])
            style = f' style="{";".join(bits)}"' if bits else ""
            tds.append(
                f'<td class="{"n" if c_i in cols else ""}"{style}>{cell if isinstance(cell, str) and cell.startswith("<") else html.escape(str(cell))}</td>'
            )
        body.append(f"<tr{cls}{style_attr}>{''.join(tds)}</tr>")
    tbl = (f'<table class="fomc-table{" nowrap" if nowrap else ""}">'
           f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>')
    # Always inside a scroller: a figure column never wraps, so in a narrow
    # column the table is wider than its column and, unscrolled, slid under the
    # verdict rail beside it.
    st.markdown(f'<div class="fomc-scroll">{tbl}</div>', unsafe_allow_html=True)


def stat_row(items: Sequence[tuple[str, str, str | None]]) -> None:
    """A tight inline row of small (label, value) pairs, e.g. a chart's own
    key readouts. Each item is (label, value, colour-or-None). Deliberately
    smaller and denser than `st.metric` -- for a secondary readout under a
    chart or picker, three stacked metric columns are heavier than the
    number they carry; this is one line, hairline-divided, sized to match
    a caption rather than a headline figure."""
    parts = []
    for i, (label, value, colour) in enumerate(items):
        if i:
            parts.append('<span class="fomc-stat-div"></span>')
        style = f' style="color:{colour}"' if colour else ""
        parts.append(
            '<span class="fomc-stat">'
            f'<span class="fomc-stat-label">{html.escape(label)}</span>'
            f'<span class="fomc-stat-value"{style}>{html.escape(value)}</span>'
            '</span>'
        )
    st.markdown(f'<div class="fomc-stat-row">{"".join(parts)}</div>', unsafe_allow_html=True)


class Fig(NamedTuple):
    """One headline figure on a quote board."""

    label: str
    value: str
    sub: str = ""
    colour: str | None = None
    help: str = ""


def bar(key: str):
    """A row of controls that belong together, packed left.

    Inside it every column is as wide as its control and the row gap is the only
    space between neighbours (the control standard in style.css). A control
    that would stretch -- a dropdown, a number or date box -- needs its own
    `width=` in pixels; a button takes `width="content"`. Leave the last column
    empty, or give it the row's note, so the free width has somewhere to go."""
    return st.container(key=f"bar_{key}")


def board(figs: Sequence[Fig]) -> None:
    """A panel's headline figures as one quote board: packed left on a single
    line, a hairline between them, each a small-caps label over the figure in
    the numeric face over a one-line context.

    The alternative, `st.metric` in `st.columns`, spreads three numbers across
    the full width and lets the gaps do the talking -- on a wide screen the
    eye travels a third of the page between "Terminal" and "Peak priced".
    A figure's `help` is its hover text, marked by a dotted underline on the
    label."""
    esc = html.escape
    cells = []
    for f in figs:
        tip = f' title="{esc(f.help)}"' if f.help else ""
        style = f' style="color:{f.colour}"' if f.colour else ""
        sub = f'<span class="fomc-fig-s">{esc(f.sub)}</span>' if f.sub else ""
        cells.append(f'<div class="fomc-fig"><span class="fomc-fig-l"{tip}>{esc(f.label)}</span>'
                     f'<span class="fomc-fig-v"{style}>{esc(f.value)}</span>{sub}</div>')
    st.markdown(f'<div class="fomc-board">{"".join(cells)}</div>', unsafe_allow_html=True)


def reads(items: Sequence[tuple[str, str]]) -> None:
    """Short labelled paragraphs side by side -- a speaker's read on each topic.

    For prose a table cannot hold: `table` keeps its first column to one line
    and gives every cell a row's height, so a sentence per cell overruns the
    page. Here each (label, text) is its own column, wraps freely, and the
    grid folds to fewer columns on a narrow screen. Empty texts are dropped."""
    cells = "".join(
        f'<div class="fomc-read"><div class="fomc-read-label">{html.escape(label)}</div>'
        f'<p>{html.escape(text)}</p></div>'
        for label, text in items if str(text or "").strip())
    if cells:
        st.markdown(f'<div class="fomc-reads">{cells}</div>', unsafe_allow_html=True)


def legend(items: Sequence[tuple[str, str]]) -> None:
    """A small inline colour key: [(label, css-colour), ...], with an optional
    third item per entry: a text glyph, or an image data URI drawn as an icon."""
    parts = []
    for item in items:
        label, colour = item[0], item[1]
        glyph = item[2] if len(item) > 2 else None
        swatch = (f'<img class="fomc-legend-icon" src="{html.escape(glyph)}" alt="">'
                  if glyph and glyph.startswith("data:image/") else
                  f'<span class="fomc-legend-mark" style="color:{colour}">'
                  f'{html.escape(glyph)}</span>' if glyph else
                  f'<span class="fomc-legend-dot" style="background:{colour}"></span>')
        parts.append(f'<span class="fomc-legend-item">{swatch}'
                     f'{html.escape(label)}</span>')
    st.markdown(f'<div class="fomc-legend">{"".join(parts)}</div>',
                unsafe_allow_html=True)


#: Unit words that do not make a value "text": 13.5bp, 19.0pp, 35d, 61 lots.
_UNIT_WORDS = re.compile(r"(?<![A-Za-z])(bp|pp|d|lots?|x|k|m|of)(?![A-Za-z])", re.I)


def is_figure(value: str) -> bool:
    """Whether a value reads as a figure (set in the numeric face) or as words.

    "+34,420 / -29,321" and "61 lots" are figures; "Receive — fade 25bp hike"
    and "Hawks 4 · Data Dependent 4" are words that happen to hold numbers, and
    a monospaced face makes those read like code."""
    stripped = _UNIT_WORDS.sub("", value)
    letters = sum(ch.isalpha() for ch in stripped)
    return any(ch.isdigit() for ch in value) and letters <= 3


def verdict_row(key: str, value: str, colour: str | None = None, emph: bool = False) -> str:
    style = f' style="color:{colour}"' if colour else ""
    cls = "fomc-verdict-row emph" if emph else "fomc-verdict-row"
    vcls = "v num" if is_figure(value) else "v"
    return (f'<div class="{cls}"><span class="k">{html.escape(key)}</span>'
            f'<span class="{vcls}"{style}>{html.escape(value)}</span></div>')


def sub(title: str, caption: str = "") -> None:
    """A heading INSIDE a panel -- the level below `section`."""
    esc = html.escape
    cap = f'<span class="fomc-h-sub">{esc(caption)}</span>' if caption else ""
    st.markdown(f'<div class="fomc-h2">{esc(title)}{cap}</div>',
                unsafe_allow_html=True)


#: Only emphasis is supported in a note, and it is applied post-escape.
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def note(text: str) -> None:
    """An explanatory aside. `**bold**` is honoured; nothing else is.

    The text is HTML-escaped first and the emphasis applied to the escaped
    string afterwards, so the markup can only ever produce a `<strong>` -- a
    stray angle bracket in a note stays inert rather than becoming a tag."""
    if text:
        body = _BOLD.sub(r"<strong>\1</strong>", html.escape(text))
        st.markdown(f'<div class="fomc-note">{body}</div>', unsafe_allow_html=True)


def vintage(line: str) -> None:
    """The source-vintage line, for a chart whose title is a Streamlit header."""
    if line:
        st.markdown(f'<div class="fomc-vintage">{html.escape(line)}</div>',
                    unsafe_allow_html=True)


def section(title: str, subtitle: str = "") -> None:
    """A panel header: small-caps label on a hairline, qualifier beside it."""
    esc = html.escape
    sub = f'<span class="fomc-h-sub">{esc(subtitle)}</span>' if subtitle else ""
    st.markdown(f'<div class="fomc-h"><span class="fomc-h-t">{esc(title)}</span>{sub}</div>',
                unsafe_allow_html=True)


def heading(title: str, subtitle: str = "", help: tuple | None = None,
            level: str = "section") -> None:
    """A section or sub heading with an optional `?` formula popover aligned
    on the same line. `help` is `(latex, plain, worked, key)`."""
    draw = section if level == "section" else sub
    if help is None:
        draw(title, subtitle)
        return
    head, q = st.columns([1, 0.06], vertical_alignment="center", gap="small")
    with head:
        draw(title, subtitle)
    with q:
        formula_help(*help)


def choice(label: str, options: Sequence, *, key: str, default=None, fallback=None, **kw):
    """A single-choice segmented control that cannot be emptied.

    Streamlit's segmented control deselects when its active option is clicked
    again (or Enter is pressed on it) and returns None, and every caller fell
    back to a fixed default: a second click on the open section jumped to the
    default tab, and Enter on "Cut" silently priced hikes. Here an emptied
    control is put straight back on the option it held -- in the widget's own
    callback, so the highlight never vanishes either.

    `default` is the widget's opening value (passed to Streamlit); `fallback`
    is used only when there is no previous choice to restore (no default, no
    state yet), for callers that seed the value through session state."""
    options = list(options)
    last = f"_{key}__last"
    spare = default if default is not None else (
        fallback if fallback is not None else (options[0] if options else None))
    user_cb = kw.pop("on_change", None)

    def restore() -> None:
        if st.session_state.get(key) is None:
            prev = st.session_state.get(last)
            st.session_state[key] = prev if prev in options else spare
        if user_cb is not None:
            user_cb()

    if default is not None:
        kw["default"] = default
    value = st.segmented_control(label, options, key=key, on_change=restore, **kw)
    if value is None or value not in options:
        prev = st.session_state.get(last)
        value = prev if prev in options else spare
    st.session_state[last] = value
    return value


def window_start(choice: str, anchor: date) -> date | None:
    """Cutoff date for a history-window control ("1Y", "5Y", "All")."""
    if not choice or choice == "All" or not choice.endswith("Y"):
        return None
    years = int(choice[:-1])
    try:
        return anchor.replace(year=anchor.year - years)
    except ValueError:
        return date(anchor.year - years, 2, 28)


#: Every history-window selector opens here unless a caller says otherwise.
DEFAULT_HISTORY_WINDOW = "5Y"


def history_window(key: str, default: str = DEFAULT_HISTORY_WINDOW,
                   options: Sequence[str] = ("1Y", "2Y", "3Y", "5Y", "10Y", "All"),
                   help: str | None = None) -> str:
    return choice("History window", options, default=default, key=key,
                  label_visibility="collapsed", help=help)


def monogram_uri(name: str, score: float | None, pal: Palette) -> str:
    """Initials on a disc tinted along the hawk-dove scale, as an SVG data URI."""
    parts = [w for w in str(name).split() if w]
    initials = ((parts[0][:1] + parts[-1][:1]) if len(parts) > 1
                else (parts[0][:2] if parts else "?")).upper()
    t = 0.0 if score is None else max(0.0, min(1.0, score / 5.0))
    a, b = pal.seq_low.lstrip("#"), pal.seq_high.lstrip("#")
    rgb = [round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (0, 2, 4)]
    fill = "#" + "".join(f"{c:02x}" for c in rgb)
    lum = rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114
    ink = "#0b0b0b" if lum > 140 else "#ffffff"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
           f'<circle cx="20" cy="20" r="19" fill="{fill}"/>'
           f'<text x="20" y="21" font-family="Helvetica,Arial,sans-serif" '
           f'font-size="15" font-weight="600" fill="{ink}" '
           f'text-anchor="middle" dominant-baseline="central">{html.escape(initials)}</text>'
           f'</svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def good(text: str, p: Palette) -> None:
    st.markdown(f'<div class="fomc-note" style="color:{p.good}">{html.escape(text)}</div>',
                unsafe_allow_html=True)


def eyebrow(text: str) -> None:
    """A small uppercase divider label inside a panel."""
    st.markdown(f'<div class="fomc-eyebrow">{html.escape(text)}</div>',
                unsafe_allow_html=True)


def progress_clock(total: int, label: str):
    """A progress bar plus an `on_progress(done, total)` callback that fills it
    with elapsed time and an ETA. Returns `(bar, callback, t0)`."""
    t0 = time.monotonic()
    bar = st.progress(0.0, text=f"0/{total} {label} — starting…")

    def on_progress(done: int, n: int) -> None:
        elapsed = time.monotonic() - t0
        msg = f"{done}/{n} {label} · {fmt_secs(elapsed)} elapsed"
        if done:
            msg += f" · ~{fmt_secs(elapsed / done * (n - done))} left"
        bar.progress(min(done / n, 1.0), text=msg)

    return bar, on_progress, t0


def fmt_secs(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def data_source_header(name: str, ok: bool, detail: str, p: Palette) -> None:
    """One line of health for an external source: name, a coloured dot, a
    one-line detail. The Data tab's whole point is a consistent read across
    unrelated sources (ASX, RBA, ABS) -- a future source reuses this
    instead of inventing its own status treatment."""
    dot_colour = p.good if ok else p.critical
    st.markdown(
        f'<div class="fomc-src">'
        f'<span class="fomc-src-name">{html.escape(name)}</span>'
        f'<span class="fomc-src-dot" style="color:{dot_colour}">{"●" if ok else "○"}</span>'
        f'<span class="fomc-note">{html.escape(detail)}</span>'
        f'</div>', unsafe_allow_html=True)
