"""Reading a JSON object out of a research agent's prose reply.

Shared by every agent-backed module (release consensus, bank calls, speeches,
trade recs) so they all tolerate the same wrappers: a ```json fence, a line of
preamble, a trailing offer of help.
"""

from __future__ import annotations

import json


def extract_json(raw: str) -> dict | None:
    """The first balanced JSON object in `raw`, or None.

    The answer arrives as agent prose that usually contains the object and
    often wraps it in a ```json fence or a sentence of preamble. Scanning for
    the first `{` and walking to its balanced close survives both, and survives
    a trailing "Let me know if..." that a strict `json.loads` on the whole
    string would choke on. Braces inside strings are skipped so a URL or a
    quote containing one cannot end the scan early.
    """
    start = raw.find("{")
    while start != -1:
        depth, in_str, escaped = 0, False, False
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        got = json.loads(raw[start:i + 1])
                    except ValueError:
                        break          # not valid -- try the next opening brace
                    return got if isinstance(got, dict) else None
        start = raw.find("{", start + 1)
    return None
