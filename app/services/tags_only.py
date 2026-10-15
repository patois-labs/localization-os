"""Segments with no translatable text are copied, never sent to an engine (GitHub #702, #705).

A unit whose source is only inline tags (the ``⟦..⟧`` codes ingest leaves for SDLXLIFF / XLIFF
/ DOCX markup), placeholders (``{name}``, ``{{count}}``, ``%s``, ``%1$d``, ``%{name}``,
``%(name)s``, ``${name}``) and whitespace has nothing an engine can translate: the right target
is the source, byte for byte. The format soak's Trados file sdlxliff/3 (1,159 tags, no text)
cost 20 minutes and about 40,000 tokens per engine or LLM step to get the source back, and on a
paid engine that is real money.

Owner decision: "Workflow runs that come across tag only segments should
copy the tag to the target and deliver. This should be an across the board thing."

This module is the ONE place that rule lives:

* `is_tags_only(source)` is the shared predicate: at least one tag or placeholder token and
  nothing but whitespace around the tokens. Any letter, digit or punctuation outside the
  tokens keeps the engine path (they can be localised). Raw markup (``<b>``) is NOT a token:
  an attribute can carry text (``alt="..."``).
* `indices(sources)` is the predicate over a list (the workflow run's dispatch exclusion).
* `commit_copy(session, seg)` is the shared action: the source is written as the target
  through the `quality.commit_mt_output` seam (its tag gates still run), status MT, never
  TM-eligible, no chunk note, and any stale step-error / gate-hold badge from an earlier run
  is cleared (a copy cannot fail a step or a gate). The caller owns the transaction.
* `is_copy(source, target)` is the delivery-side check (an Advanced Workflows staged row that
  is the deterministic copy needs no quality-lens verdict).

Callers: the plain pre-translate stream (`translate_hub2`), the workflow segmented run
(`orchestrator`: never dispatched to any step, committed at write-back), the per-segment step
engine (`orchestrator_part8._run_step_on_segment`, defense in depth for the eval runner), the
Advanced Workflows conductor and its delivery gate, the Evaluations Lab producer, the
`/api/translate/text` front door and the editor's alternates call.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

#: The provenance tag the commit seam records and the run / job summaries count.
SOURCE = "tags-only"

# Inline tag codes (⟦x1⟧, ⟦g2⟧, ⟦/g2⟧) and the placeholder grammar the commit seam's
# placeholder gate already treats as one token (`quality_common._SEAM_PLACEHOLDER_RE`, minus
# the `%%` / `{{` escapes, which are literal text).
_TOKEN_RE = re.compile(
    r"⟦[^⟧\s]{1,40}⟧"
    r"|%\{[A-Za-z_][A-Za-z0-9_]*\}"
    # flags and width/precision use DISJOINT classes (a `0` flag reads as width), so a long
    # digit run cannot backtrack quadratically (#599: request text must stay linear).
    r"|%\([A-Za-z_][A-Za-z0-9_]*\)[-#+ ]*[0-9.]*[hlL]?[diouxXeEfFgGcrsa](?![A-Za-z])"
    r"|%(?:\d+\$)?[-#+ ]*[0-9.]*[hlL]?[diouxXeEfFgGcrsa@](?![A-Za-z])"
    r"|\{\{[A-Za-z0-9_][A-Za-z0-9_.]*\}\}"
    r"|\{[A-Za-z0-9_][A-Za-z0-9_.]*\}"
    r"|\$\{[A-Za-z_][A-Za-z0-9_.]*\}"
)


_INLINE_TAG_RE = re.compile(r"⟦[^⟧\s]{1,40}⟧")


def is_tags_only(source: str | None) -> bool:
    """True when `source` has at least one inline tag or placeholder and nothing but
    whitespace around them: there is no text for an engine to translate.

    At most ONE placeholder (gate, #705): two placeholders side by side are a format string
    whose ORDER can be the translation (`{first} {last}` is `{last} {first}` in ja / zh / ko /
    hu), so `{first} {last}` or `%1$s %2$s` keeps the engine path. Inline `⟦..⟧` tags are
    markup codes, not words, and keep #702's rule (any number of them)."""
    text = source or ""
    if "⟦" not in text and "%" not in text and "{" not in text:
        return False
    rest, n = _TOKEN_RE.subn("", text)
    if not n or rest.strip():
        return False
    return n - len(_INLINE_TAG_RE.findall(text)) <= 1


def indices(sources) -> set[int]:
    """The positions in `sources` whose text is tags-only."""
    return {j for j, s in enumerate(sources) if is_tags_only(s)}


def is_copy(source: str | None, target: str | None) -> bool:
    """True when `target` is the verbatim copy of a tags-only `source`."""
    return is_tags_only(source) and (target or "") == (source or "")


def commit_copy(session, seg):
    """Write `seg.source_text` as its own translation through the commit seam (status MT,
    never TM-eligible, no engine call). Returns the seam's `CommitOutcome`; a flagged outcome
    (a hard lock, a human-approved row, a tag gate) is the seam's own disposition. Does not
    commit: the caller owns the transaction."""
    from app.services import mt_chunking, quality

    outcome = quality.commit_mt_output(
        session, seg, seg.source_text, source=SOURCE, tm_eligible=False
    )
    if getattr(outcome, "committed", False):
        mt_chunking.stamp(seg, None)
        flags = seg.qa_flags
        if quality.has_gate_hold_flag(flags):
            flags = quality.clear_gate_hold_flags(flags)
        if any(
            isinstance(f, dict) and f.get("type") == quality.STEP_ERROR_FLAG_TYPE
            for f in (flags or [])
        ):
            flags = quality.clear_step_error_flags(flags)
        if flags is not seg.qa_flags:
            seg.qa_flags = flags
        session.add(seg)
    return outcome
