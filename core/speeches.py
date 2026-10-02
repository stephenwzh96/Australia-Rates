"""Public-appearance roundup for the Monetary Policy Board -- prompt and parsing.

The US module, on the RBA's Board. Two differences shape the brief: every
member votes (no rotation, so nobody is flagged as a non-voter), and the six
non-executive members rarely speak on the record -- the Governor's
post-meeting media conference, her and the Deputy Governor's speeches, and
Senate Estimates / House Economics Committee testimony carry most of the
signal, and silence from an external member is the normal answer, not a gap.

WHAT THIS IS FOR
----------------
Step 8 decomposes q into P(bloc moves) x P(centre joins), and `core.votes` says
why: "The bloc's behaviour is broadly readable from speeches. The centre's is
not." This module is the reading. It turns a date window plus a roster into a
research brief, and turns the answer back into rows the Vote count tab can put
next to each member's hawk-dove score.

FABRICATION IS THE FAILURE MODE, NOT ERROR
------------------------------------------
Everything else in this app degrades to a blank or a warning when it cannot get
data. A research agent does not: asked who spoke last week, it can produce a
fluent, plausible, entirely invented answer, and nothing about the output looks
different from a real one. So:

  * every appearance must carry a SOURCE URL, and a row without one is dropped
    on parse rather than shown unsourced -- the citation is the receipt;
  * quotes are demanded verbatim, because a paraphrase attributed inside quote
    marks is the specific thing that would get traded on;
  * a member with nothing found is reported as nothing found. "No appearances"
    is a real and common answer for a week, and a prompt that does not say so
    invites the model to fill the silence;
  * names are matched back against the roster, so an appearance attributed to
    someone who is not on the Board cannot enter the table.

The roster is all nine members, every one of them a voter. The `non_voters`
plumbing is kept so the ported tab reads the same, and is always empty here.

`parse` therefore drops bad rows rather than failing the batch: one malformed
record should cost that record, not the roundup, which is the same
degrade-rather-than-fail rule the data adapters follow.

THE RECORD AND THE JUDGEMENT ARE DIFFERENT FIELDS
-------------------------------------------------
`inflation`, `labour`, `economy`, `quotes` and `source` are the record: what a
sourced speaker actually said. `implication`, `direction`, `delta` and
`score_note` are a rates strategist's judgement ON that record -- the policy
path it argues for, and whether the desk's standing mark on that member should
move. The brief asks for both and labels them separately, and the UI renders
them separately, because the failure mode of mixing them is a confident-looking
inference presented with the authority of a quote.

This module never imports `data/` and never touches the network -- it takes a
roster and returns a prompt, and takes a string and returns readings. The SDK
call lives in `data/agent.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

# How far back the window opens by default, in days. A week is the natural
# cadence: RBA speakers cluster in the days after a decision and before a
# blackout, and a seven-day look-back is short enough that "nothing found"
# means something rather than being an artefact of a long quiet stretch.
DEFAULT_WINDOW_DAYS = 7

# Vocabulary the UI colours on. Anything the model returns outside these sets
# is normalised to the last entry -- an unrecognised label should read as "not
# established", never silently as one of the real positions.
LEANS = ("hawkish", "dovish", "neutral", "unclear")

# Where the SPEAKER puts the current stance of policy. "unclear" is the
# normalisation fallback and a legitimate answer for a ceremonial or narrowly
# regulatory appearance, but it is a last resort: the brief asks for the stance
# to be read out of the remarks and their context (a speaker arguing to remove
# restraint has told you policy is restrictive, whether or not they used the
# word), because a column of "unclear" carries none of the information the
# speech actually contained.
STANCES = ("restrictive", "neutral", "accommodative", "unclear")

# Which way this outing argues the member's hawk-dove score should move.
DIRECTIONS = ("higher", "unchanged", "lower")

# Largest score move a single appearance may argue for, on the roster's 0-5
# scale. One speech is one observation: a member who genuinely re-rates by
# more than this has usually done something the desk already knows about, and
# an unbounded delta lets a single strong quote rewrite a judgement built over
# several meetings. Clamped rather than rejected -- the direction is still the
# useful part when the size is overcooked.
MAX_DELTA = 1.5

# Bumped whenever the research brief changes in a way that would change the
# ANSWER, not just the wording: the mark-to-market ladder, what counts as a
# move, or the JSON shape. The caller folds this into its cache key, so an edit
# here retires every cached roundup instead of serving figures produced under
# the old brief -- which is the only way a recalibration can silently fail to
# take effect.
PROMPT_VERSION = 3

# The roster's own scale, for clamping the resulting score.
SCORE_MIN, SCORE_MAX = 0.0, 5.0

# Fields every appearance must carry to be shown at all.
REQUIRED = ("member", "source")


@dataclass(frozen=True)
class Appearance:
    """One public outing by one member, as reported and sourced."""
    member: str
    when: date | None
    venue: str            # city, institution, or programme
    kind: str             # speech | interview | panel | testimony | remarks
    inflation: str
    labour: str
    economy: str
    rate_stance: str      # where THEY put policy now -- see STANCES
    lean: str             # hawk-dove read of this outing -- see LEANS
    quotes: tuple[str, ...]
    source: str           # URL; a row without one never reaches here
    # What this outing argues about the member's roster score, measured FROM
    # Score @ last meeting (the baseline the brief hands the model), never from
    # an intermediate read. `delta` is signed on the hawk-dove scale and is
    # always consistent with `direction` -- see `_score`, which reconciles the
    # two rather than trusting either alone.
    direction: str = "unchanged"
    delta: float = 0.0
    score_note: str = ""
    # The strategist's read of what the outing means for the path. Defaults
    # empty so a roundup saved before it existed still loads.
    implication: str = ""
    # Which bloc this speaker belongs to AFTER this outing, and why. Both
    # default empty so a roundup saved before they existed still loads -- and
    # so an appearance the agent did not re-assess is visibly not assessed,
    # rather than reading as "bloc unchanged" when nobody looked.
    bloc_now: str = ""
    bloc_note: str = ""

    @property
    def dated(self) -> str:
        return self.when.strftime("%a %d %b") if self.when else "date unclear"

    def new_score(self, baseline: float | None) -> float | None:
        """The score this outing argues for, clamped to the roster's scale.

        `baseline` is Score @ last meeting, not the current mark: `delta` is
        defined in the brief as a move from the last-meeting baseline, so any
        other base gives a number that means nothing.
        """
        if baseline is None:
            return None
        return max(SCORE_MIN, min(SCORE_MAX, baseline + self.delta))

    @property
    def move(self) -> str:
        """How the adjustment reads in the table."""
        if self.direction == "unchanged" or not self.delta:
            return "unchanged"
        return f"{self.direction} {self.delta:+.1f}"


@dataclass(frozen=True)
class Roundup:
    start: date
    end: date
    appearances: tuple[Appearance, ...] = ()
    silent: tuple[str, ...] = ()       # roster members with nothing found
    error: str | None = None           # nothing came back at all
    partial: str = ""                  # some batches failed, others did not

    @property
    def ok(self) -> bool:
        return self.error is None

    def by_member(self, name: str) -> tuple[Appearance, ...]:
        return tuple(a for a in self.appearances if a.member == name)

    def latest(self) -> dict[str, "Appearance"]:
        """The most recent appearance per member.

        `appearances` is already sorted latest-first, so first one wins. This
        is deliberately not the sum of a member's outings: a member who spoke
        three times has one CURRENT position, arrived at over three steps, and
        writing all three back would let an older appearance overwrite a newer
        one depending on iteration order.
        """
        out: dict[str, Appearance] = {}
        for a in self.appearances:
            out.setdefault(a.member, a)
        return out

    @property
    def speakers(self) -> tuple[str, ...]:
        """Who spoke, most recent first.

        `appearances` is already sorted latest-first, so first appearance wins
        -- the roster's own order would bury this week's news under whoever
        happens to sit at the top of the table.
        """
        out: list[str] = []
        for a in self.appearances:
            if a.member not in out:
                out.append(a.member)
        return tuple(out)

    @property
    def leans(self) -> dict[str, int]:
        out: dict[str, int] = {k: 0 for k in LEANS}
        for a in self.appearances:
            out[a.lean] = out.get(a.lean, 0) + 1
        return out


# Members per search. Small batches are what make the roundup finish at all:
# one agent asked about a dozen people searches them one after another, and a
# measured four-member run took 386 seconds. Three keeps each search short
# enough to come back while still amortising the model's start-up over more
# than one name.
BATCH_SIZE = 3


def batches(members: list[str], size: int = BATCH_SIZE) -> list[list[str]]:
    """Split the roster into independently searchable groups."""
    return [members[i:i + size] for i in range(0, len(members), size)] if members else []


def merge(parts: list[Roundup], members: list[str],
          start: date, end: date) -> Roundup:
    """Fold the per-batch roundups back into one, in roster terms.

    Errors are collected rather than swallowed: a batch that failed is not the
    same as a batch that found nothing, and reporting the second when the
    first happened would hide a broken search behind a plausible silence.
    """
    rows = [a for p in parts for a in p.appearances]
    rows.sort(key=lambda a: (a.when or date.min, a.member), reverse=True)

    failed = [p.error for p in parts if p.error]
    spoke = {a.member for a in rows}
    silent = tuple(m for m in members if m not in spoke)
    # Every batch failed: there is no roundup, only an error.
    error = failed[0] if failed and not rows and len(failed) == len(parts) else None
    partial = ("" if not failed else
               f"{len(failed)} of {len(parts)} searches failed: {failed[0]}")
    return Roundup(start, end, tuple(rows), silent, error, partial)


# --------------------------------------------------------------------------
# Persistence -- a Roundup as plain JSON, for the history store
# --------------------------------------------------------------------------
#
# A finished roundup is expensive (minutes, real tokens) and the whole point
# of keeping several is to look at one again without re-asking. These convert
# to and from plain dicts rather than reusing dataclasses.asdict, because the
# read direction has to tolerate a hand-edited or older-schema file the same
# way `parse` tolerates a wayward agent reply -- `.get()` with a safe default,
# never a bare `[...]` that turns a missing key into a crash.


def appearance_to_dict(a: Appearance) -> dict:
    return {
        "member": a.member, "when": a.when.isoformat() if a.when else None,
        "venue": a.venue, "kind": a.kind,
        "inflation": a.inflation, "labour": a.labour, "economy": a.economy,
        "rate_stance": a.rate_stance, "lean": a.lean,
        "quotes": list(a.quotes), "source": a.source,
        "direction": a.direction, "delta": a.delta, "score_note": a.score_note,
        "implication": a.implication,
        # getattr, not a bare attribute: the speech roundup cache is a
        # cache_resource and survives a source change, so it can hand back an
        # Appearance built before these two fields existed. Saving one of those
        # would otherwise crash the write for an object that is otherwise fine.
        "bloc_now": getattr(a, "bloc_now", ""),
        "bloc_note": getattr(a, "bloc_note", ""),
    }


def appearance_from_dict(d: dict) -> Appearance:
    when = d.get("when")
    return Appearance(
        member=d.get("member", ""),
        when=date.fromisoformat(when) if when else None,
        venue=d.get("venue", ""), kind=d.get("kind", ""),
        inflation=d.get("inflation", ""), labour=d.get("labour", ""),
        economy=d.get("economy", ""),
        rate_stance=_one_of(d.get("rate_stance"), STANCES),
        lean=_one_of(d.get("lean"), LEANS),
        quotes=tuple(d.get("quotes") or ()),
        source=d.get("source", ""),
        direction=d.get("direction") if d.get("direction") in DIRECTIONS else "unchanged",
        delta=float(d.get("delta") or 0.0),
        score_note=d.get("score_note", ""),
        implication=_clean(d.get("implication")),
        bloc_now=_clean(d.get("bloc_now")),
        bloc_note=_clean(d.get("bloc_note")),
    )


def roundup_to_dict(r: Roundup) -> dict:
    return {
        "start": r.start.isoformat(), "end": r.end.isoformat(),
        "appearances": [appearance_to_dict(a) for a in r.appearances],
        "silent": list(r.silent), "error": r.error, "partial": r.partial,
    }


def roundup_from_dict(d: dict) -> Roundup:
    return Roundup(
        start=date.fromisoformat(d["start"]), end=date.fromisoformat(d["end"]),
        appearances=tuple(appearance_from_dict(a)
                          for a in d.get("appearances") or []),
        silent=tuple(d.get("silent") or ()),
        error=d.get("error"), partial=d.get("partial", ""),
    )


def default_window(as_of: date, days: int = DEFAULT_WINDOW_DAYS) -> tuple[date, date]:
    """The look-back window ending at the as-of date.

    Anchored on the as-of rather than the wall clock, so reviewing a past
    meeting asks about the speeches that had actually happened by then.
    """
    return as_of - timedelta(days=days), as_of


# --------------------------------------------------------------------------
# The brief
# --------------------------------------------------------------------------

SYSTEM = (
    "You are an experienced Australian rates strategist covering the RBA. Two jobs, and they are kept "
    "strictly apart. FIRST, report only what you can source: never infer a view "
    "a speaker did not express, treat 'no appearances found' as a normal and "
    "useful answer, and put accuracy about who said what, and when, above "
    "coverage. SECOND, once the record is straight, think critically about it "
    "the way a strategist would in a morning meeting -- what the remarks imply "
    "for the policy path and the Board's reaction function, what would have "
    "to be true for that read to be wrong, and whether the speaker actually "
    "moved relative to their colleagues or merely restated a known position. "
    "Label the two clearly: quotes and view fields are the record, the "
    "implication and score fields are your judgement. When you mark a speaker's "
    "hawk-dove score you move it only on evidence of a CHANGE in stance, never "
    "on reputation or on how forcefully they spoke."
)


def _peer_context(scores: dict[str, float],
                  non_voters: set[str] | None = None) -> str:
    """The WHOLE Board as the analyst sees it, so a move is judged RELATIVE.

    Without this the model re-rates each speaker in isolation and every
    hawkish-sounding speech reads as a hawkish shift. The question is whether
    this member moved against the Board, not whether they sounded hawkish.

    Deliberately the full roster and not the batch being searched. The roster
    is split into small groups so the searches finish, and scoring a hawk
    against a batch of three other hawks makes the mean he is compared to move
    with the batching -- a member would re-rate differently depending on who he
    happened to be grouped with, which is not a property of anything real.
    """
    if not scores:
        return ""
    quiet = non_voters or set()
    mean = sum(scores.values()) / len(scores)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    lines = "\n".join(
        f"  - {m}: {s:.1f}  ({'above' if s > mean else 'below' if s < mean else 'at'} "
        f"the mean, rank {i} of {len(ranked)} most hawkish"
        + ("; NO VOTE this year)" if m in quiet else "; votes)")
        for i, (m, s) in enumerate(ranked, 1))
    return f"""
THE DESK'S SCORES AS AT THE LAST MEETING -- the FULL Board (0-5 hawk-dove,
5 = most hawkish), Board mean {mean:.1f}. This is the BASELINE you are
marking to market: where the desk had each member at the last meeting, before
anything in this search window was said. Every move you report is measured
from the number below, not from any later read. Judge it against this mean and
against these peers, not against the handful of members you were asked to
search:

{lines}

Every member of the Monetary Policy Board votes at every meeting; the Board
decides by majority, the Governor holding a casting vote. The RBA publishes the
vote split (e.g. "five members voted to increase ...; four members voted to
leave ...") but never who voted which way, so a member's own public remarks
are the only attributable evidence of where they stand.
"""


def _bloc_context(blocs: dict[str, str], members: list[str]) -> str:
    """The desk's current bloc map, so a re-assessment starts from it.

    A bloc is a slow-moving read of who caucuses with whom, so the prior is
    STRONG: the default answer is the bloc the member is already in, and the
    brief says so. Without the vocabulary in front of it the model invents
    near-duplicate labels ("Hawk-leaning" beside "Hawks") and the roster
    fragments into groups of one, which is worse than no re-assessment at all.

    Only the searched members are listed individually -- the vocabulary is
    what needs to be complete, and listing all nineteen would bury it.
    """
    if not blocs:
        return ""
    vocabulary = sorted({b.strip() for b in blocs.values() if b and b.strip()})
    if not vocabulary:
        return ""
    mine = "\n".join(f"  - {m}: {blocs.get(m, '').strip() or 'unassigned'}"
                     for m in members)
    return f"""
THE DESK'S CURRENT BLOCS -- the standing read of who moves together:

{mine}

The full set of bloc labels in use, so you answer with an EXISTING name rather
than a near-duplicate of one: {", ".join(vocabulary)}
"""


def prompt(members: list[str], start: date, end: date,
           scores: dict[str, float] | None = None,
           non_voters: set[str] | None = None,
           blocs: dict[str, str] | None = None) -> str:
    """The research brief.

    `members` is who to SEARCH -- one batch. `scores` is the whole Board,
    which is what the score read is judged against; the two differ on purpose.
    `non_voters` marks who holds no seat this year: it changes how a shift is
    WEIGHED, never whether they are searched. `blocs` is the current bloc map,
    which the re-assessment starts from -- see `_bloc_context`.
    """
    quiet = non_voters or set()
    roster = "\n".join(f"  - {m}" + ("  [no vote this year]" if m in quiet else "")
                       for m in members)
    return f"""Find the public appearances of these Reserve Bank of Australia Monetary \
Policy Board members between \
{start:%d %B %Y} and {end:%d %B %Y} inclusive:

{roster}
{_peer_context(scores or {}, quiet)}
{_bloc_context(blocs or {}, members)}

Search for speeches, interviews, panel appearances, fireside chats, moderated \
Q&A, the Governor's post-meeting media conference, and parliamentary testimony \
(Senate Economics Legislation Committee Estimates, the House of \
Representatives Standing Committee on Economics) in that window. rba.gov.au \
carries official speech texts and media-conference transcripts; aph.gov.au \
carries Hansard; wire and press coverage (Reuters, Bloomberg, the AFR, ABC) \
carries interviews and unscripted remarks. The non-executive members speak on \
the record rarely -- for them "silent" is the usual and correct answer. The \
Board's minutes and statements are unattributed: never attribute a view in \
them to a named member.

PART 1 -- THE RECORD. For each appearance, report what the speaker said about:
  - inflation
  - the labour market
  - the economy overall
  - whether they consider policy restrictive, neutral, or accommodative
  - quotes that show a hawkish or dovish lean

RULES, in order of importance:

1. Every appearance needs a working source URL. If you cannot source it, leave
   it out. Do not include an appearance you are recalling rather than reading.
2. Quotes must be VERBATIM from the source. Never put a paraphrase inside
   quotation marks. If you have no exact quote, use an empty list.
3. Only the names listed above. Do not add other RBA staff or officials.
4. Only that date window. A speech just outside it does not count.
5. If a member made no public appearance in the window, list their name under
   "silent". Most weeks several members will be silent, and that is expected.
6. For each of the view fields, write one plain sentence, or "not addressed" if
   they did not cover it. Do not infer a view from their general reputation.
7. "rate_stance" -- where THE SPEAKER thinks policy sits right now -- must be
   worked out from what they actually said and the context of the speech, not
   left at "unclear" because they did not use the word "restrictive". Read the
   signals: "policy remains meaningfully tight", "we are at or near neutral",
   "there is still restraint in the pipeline", arguing for FURTHER restraint
   (restrictive-and-not-done), arguing for REMOVING restraint or normalising
   (restrictive but easing), describing rates as "well positioned" while
   inflation runs above target (restrictive), or defending current settings as
   "appropriate" after a series of cuts (neutral). A speaker who discusses the
   stance of policy at all has almost always told you where they put it.
   Reserve "unclear" for a speech that genuinely gives nothing to read -- a
   ceremonial address, a narrow regulatory or payments topic, remarks purely
   about the outlook with no reference to the setting of policy. This is
   inference from the SPEECH, which rule 6 asks for; it is not inference from
   the speaker's reputation, which rule 6 forbids.

PART 2 -- THE STRATEGIST'S READ. Give "implication": two to four sentences of
critical analysis, in your own words, of what this appearance means for
monetary policy. This is the section a rates desk actually reads, so make it
carry an argument rather than restate the summary above. Cover, in whatever
order the speech makes natural:
  - what it implies for the POLICY PATH -- the next meeting specifically, and
    the direction of travel beyond it;
  - what it says about this speaker's REACTION FUNCTION: which data would move
    them, what they have set as the bar for cutting or hiking, and what they
    are explicitly willing to look through;
  - whether this is genuinely NEW information or a restatement of a position
    the market already holds, and if new, what it changes;
  - where it puts them against the rest of the Board, naming the colleagues
    they are now closer to or further from;
  - the honest counter-argument -- what in the remarks cuts the other way, or
    what would have to happen for this read to be wrong.
Be specific and be willing to draw a conclusion. "This is consistent with a
hold in November but the bar for another hike is higher than her August remarks
implied" is useful; "he struck a balanced tone" is not.

PART 3 -- MARK TO MARKET. The scores listed above are the desk's marks AS AT
THE LAST MEETING -- the baseline, from before this window. Mark each speaker to
market against that baseline:

  - "direction" is whether THIS appearance argues the member's last-meeting score
    should go higher (more hawkish), lower (more dovish), or is unchanged.
  - "delta" is the signed move on the 0-5 scale FROM THE LAST-MEETING MARK:
    positive for higher, negative for lower, exactly 0 for unchanged. It is
    not a move from some intermediate read -- a member who spoke twice in the
    window is marked on where their LATEST outing leaves them relative to the
    baseline, so that outing's delta carries the whole cycle's shift.
  - "score_note" is one clause: why the mark moves or holds, and where it
    leaves them against the Board mean and against the specific colleagues
    nearest them in the ranking.

SIZE THE MOVE ON THIS LADDER. Pick the rung the evidence earns, not the
smallest one available. A delta is a claim about how far the desk's view should
change, and grading every shift at the bottom of the ladder throws away the
information the search was run to get:

  +/-0.1  Tone only: sharper or more emphatic wording, same policy conclusion.
          Use it rarely -- if their reaction function is unchanged, the answer
          is 0.
  +/-0.3  A firmer restatement of a view already in the mark: the same
          conclusion argued harder, or supported by a new reason.
  +/-0.5  A clear shift. They move their own bar -- a new or withdrawn
          condition for acting, a leg of the reaction function switched on or
          off, or a stance call (restrictive/neutral/accommodative) they had
          not made before.
  +/-1.0  A re-rating. They cross a line the desk had them on the other side
          of: joining or leaving a dissent camp, opening a policy action they
          had previously ruled out, or reversing an earlier call.
  +/-{MAX_DELTA:g}  Reserved for a member repositioning against the whole
          Board -- a ceiling, not a target.

A speaker who has actually changed what they would DO has moved at least 0.5,
and marking that 0.2 loses it. Equally, do not inflate a restatement: a known
hawk sounding hawkish is 0 -- that is already in the mark. A known dove
conceding on inflation is a move.

Judge it RELATIVE to the Board: the question is whether this member
shifted against their peers and their own rank in the list above, not whether
the speech sounded hawkish in isolation.

PART 4 -- WHERE THEY SIT NOW. Re-assess which BLOC this speaker belongs to, in
the light of what they actually said in this appearance:

  - "bloc_now": the bloc they sit in AFTER this outing. Use one of the labels
    listed above.
  - "bloc_note": one clause, why it holds or why it moved, naming the
    colleagues they have moved towards or away from.

A bloc is the slowest-moving thing on the roster: it is a read of who a member
caucuses with over months, not of how one speech sounded. So the prior is
strong and the correct answer is usually the bloc they are ALREADY in. Move
them only when this outing genuinely places them with a different group --
because they named a condition that puts them on the other side of the line
their bloc is defined by, or because they explicitly aligned with, or broke
from, a colleague on the other side of it.

Do not invent a new label. Adding a bloc for one speech fragments the map into
groups of one, which is worse than no re-assessment. Only propose a new label
if this outing moves them clear of EVERY existing group, and say so plainly in
"bloc_note" -- a new label is a claim about the Board, not about the
speaker, and it should read like one.

Return ONLY a JSON object in this exact shape, and nothing else:

{{
  "appearances": [
    {{
      "member": "exact name from the list above",
      "date": "YYYY-MM-DD",
      "venue": "city, institution, or programme name",
      "kind": "speech | interview | panel | testimony | remarks",
      "inflation": "one sentence, or 'not addressed'",
      "labour": "one sentence, or 'not addressed'",
      "economy": "one sentence, or 'not addressed'",
      "rate_stance": "restrictive | neutral | accommodative | unclear \
(infer it from the remarks; 'unclear' only if the speech says nothing about \
the setting of policy)",
      "lean": "hawkish | dovish | neutral | unclear",
      "quotes": ["verbatim quote", "another verbatim quote"],
      "implication": "2-4 sentences: path, reaction function, what is new, \
position vs peers, and the counter-argument",
      "source": "https://...",
      "direction": "higher | unchanged | lower",
      "delta": 0.0,
      "score_note": "one clause: why the mark moves or holds, and where it \
leaves them vs the mean and their nearest peers",
      "bloc_now": "one of the bloc labels listed above",
      "bloc_note": "one clause: why the bloc holds or moves, and which \
colleagues they have moved towards or away from"
    }}
  ],
  "silent": ["members with no appearance found in the window"]
}}"""


# --------------------------------------------------------------------------
# Reading the answer back
# --------------------------------------------------------------------------


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


def _clean(value: object, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split()).strip() or fallback


def _when(value: object, start: date, end: date) -> date | None:
    """A date inside the window, or None.

    Out-of-window dates are blanked rather than dropped: the appearance may
    still be real and sourced, and showing it with an unclear date is more
    honest than either silently keeping a date the brief excluded or binning
    the row.
    """
    if not isinstance(value, str):
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if not m:
        return None
    try:
        got = date.fromisoformat(m.group())
    except ValueError:
        return None
    return got if start <= got <= end else None


def _one_of(value: object, allowed: tuple[str, ...]) -> str:
    got = _clean(value).lower()
    return got if got in allowed else allowed[-1]


def _score(direction: object, delta: object) -> tuple[str, float]:
    """A reconciled `(direction, delta)` pair.

    The two can disagree -- "higher" with a negative delta, or "unchanged" with
    a delta attached -- and a table that showed one while the arithmetic used
    the other would be quietly wrong in the most expensive direction. The
    SIGN of the number is treated as the real answer where both are present,
    because the delta is what moves a score; a direction word with no usable
    number degrades to unchanged rather than inventing a size.
    """
    word = _clean(direction).lower()
    try:
        size = float(delta)                         # type: ignore[arg-type]
    except (TypeError, ValueError):
        size = 0.0
    if size != size or size in (float("inf"), float("-inf")):   # NaN / inf
        size = 0.0
    size = max(-MAX_DELTA, min(MAX_DELTA, size))

    if size > 0:
        return "higher", round(size, 2)
    if size < 0:
        return "lower", round(size, 2)
    # No usable magnitude: a bare direction word is a claim without a size.
    return ("unchanged", 0.0) if word in DIRECTIONS else ("unchanged", 0.0)


def _quotes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out = [_clean(q) for q in value]
    return tuple(q.strip('"“”') for q in out if q)


def parse(raw: str, members: list[str], start: date, end: date) -> Roundup:
    """Readings from the agent's answer. Never raises.

    A malformed record costs that record; the rest of the roundup survives.
    """
    payload = extract_json(raw or "")
    if payload is None:
        return Roundup(start, end, error="No JSON object in the agent's reply.")

    # Case-insensitive match back to the roster. A name the roster does not
    # carry is not a voter this year, so its appearance cannot be shown against
    # a score -- and a hallucinated attribution looks exactly like this.
    known = {m.casefold(): m for m in members}

    seen: set[tuple[str, str]] = set()
    rows: list[Appearance] = []
    for item in payload.get("appearances") or []:
        if not isinstance(item, dict):
            continue
        if any(not _clean(item.get(k)) for k in REQUIRED):
            continue
        member = known.get(_clean(item.get("member")).casefold())
        source = _clean(item.get("source"))
        if member is None or not source.lower().startswith(("http://", "https://")):
            continue
        # One row per member per source: the same speech written up twice is a
        # duplicate, not two appearances.
        key = (member, source)
        if key in seen:
            continue
        seen.add(key)
        direction, delta = _score(item.get("direction"), item.get("delta"))
        rows.append(Appearance(
            member=member,
            when=_when(item.get("date"), start, end),
            venue=_clean(item.get("venue"), "venue not given"),
            kind=_clean(item.get("kind"), "remarks"),
            inflation=_clean(item.get("inflation"), "not addressed"),
            labour=_clean(item.get("labour"), "not addressed"),
            economy=_clean(item.get("economy"), "not addressed"),
            rate_stance=_one_of(item.get("rate_stance"), STANCES),
            lean=_one_of(item.get("lean"), LEANS),
            quotes=_quotes(item.get("quotes")),
            source=source,
            direction=direction,
            delta=delta,
            score_note=_clean(item.get("score_note")),
            implication=_clean(item.get("implication")),
            bloc_now=_clean(item.get("bloc_now")),
            bloc_note=_clean(item.get("bloc_note")),
        ))

    # Latest first. An undated appearance sorts last rather than to the top:
    # `date.min` would put the one row we are least sure about above every
    # row we are sure about.
    rows.sort(key=lambda a: (a.when or date.min, a.member), reverse=True)

    # Silent is derived, not trusted: whoever has no surviving row is silent,
    # so a member dropped for a missing source cannot also be reported as
    # having said nothing.
    spoke = {a.member for a in rows}
    silent = tuple(m for m in members if m not in spoke)
    return Roundup(start, end, tuple(rows), silent)
