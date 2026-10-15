"""lqa: downstream findings block (prompt-package engine)

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import logging
import re
import unicodedata
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import LQAResult, Segment, WorkflowRun
from app.services import quality, scaffold

MQM_DIMENSIONS = [
    "Accuracy",
    "Fluency",
    "Terminology",
    "Locale convention",
    "Style",
    "Verity",
    "Design",
    "Internationalization",
]


PENALTY = {"minor": 1, "major": 5, "critical": 10, "neutral": 0}


LQA_PASS_THRESHOLD = 90


JUDGE_SKIPPED = "skipped"


JUDGE_OK = "ok"


JUDGE_UNAVAILABLE = "unavailable"


# The DEFAULT numeric pass bar for an LQA step's gate: a card PASSES when its
# score >= this AND it carries no major/critical error. Historically this was the
# literal `90` inlined in `_score`; it is a named constant now because an LQA
# workflow step may configure its OWN threshold (WorkflowStep.lqa_threshold). NULL /
# unset resolves to THIS value, so every existing workflow gates exactly as before.
# --- LLM-judge availability states (issue #56) --------------------------------
# `_llm_errors` returns one of these as its 5th tuple element so `score_text` can tell
# apart three states that used to collapse to the same empty-findings result:
#   * JUDGE_SKIPPED     — NO live judge was configured (offline/mock install, or a not-
#                         live provider). Structural+rulebook-only scoring is the CORRECT
#                         and complete answer; the gate may pass on it exactly as before.
#   * JUDGE_OK          — a live judge ran AND returned a parseable verdict (its findings
#                         may legitimately be empty — a genuine clean pass).
#   * JUDGE_UNAVAILABLE — a live judge WAS configured but did not deliver a verdict (dead
#                         engine, provider error, empty/blank completion, reasoning-burned
#                         finish=length, or unparseable output). This must NEVER read as a
#                         clean 100: the gate fails closed to needs_review and the card
#                         carries an explicit `llm_judge="unavailable"` marker.
# "Judge could not run" and "judge found zero issues" are different facts and must not be
# the same number on a release/workflow gate.
def clamp_threshold(n):
    """Coerce a configured LQA pass threshold to an int in [0, 100], or None.

    None -> None (the caller left it unset: `_score`/`score_text` fall back to the
    built-in LQA_PASS_THRESHOLD, byte-identical to the pre-configurable gate). Junk
    (non-numeric) ALSO -> None, so a garbage value degrades to the safe default
    rather than to an arbitrary number. A real value is clamped to the sane band —
    this mirrors `qe.clamp_threshold`, except qe defaults junk to 70 (its own gate's
    default) whereas an unset LQA threshold means "inherit 90", expressed as None."""
    if n is None:
        return None
    try:
        v = int(n)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, v))


def _rulebook_errors(target: str, rulebook: str) -> list[dict]:
    out = []
    for line in (rulebook or "").splitlines():
        line = line.strip()
        if line.upper().startswith("FORBID "):
            term = line[7:].strip()
            if term and term.lower() in (target or "").lower():
                out.append(
                    {
                        "dimension": "Style/Awkward",
                        "severity": "minor",
                        "note": f"forbidden term '{term}'",
                        "source": "rulebook",
                    }
                )
        elif line.upper().startswith("REQUIRE "):
            # Positive assertion (Track B / B1): the target MUST contain <term>.
            # Matcher choice — WHOLE-TOKEN quality._term_present, deliberately NOT
            # FORBID's lowercase-substring test: a REQUIRE must not be trivially
            # satisfied by the term hiding inside a larger word (REQUIRE "cat" is a
            # genuine miss on a target whose only "cat" is inside "category"), the
            # mirror of the substring false-positive the DNT/glossary gate avoids.
            # Case-insensitive (the DSL has no per-line case flag; rulebook lines are
            # authored loosely, matching FORBID's case-insensitivity).
            # Severity — Terminology / MAJOR: by _score (below) a single major error
            # forces passed=False regardless of the numeric score (100-5=95 still
            # clears the >=90 floor, so a MINOR would silently pass). MAJOR is the
            # deliberate choice that makes glossary/term adherence GATE-ELIGIBLE — the
            # LQA step then produces a durable gate 'failed' that run_condition/branch
            # route into a Fix step (the correction loop). FORBID stays byte-identical.
            term = line[8:].strip()
            if term and not quality._term_present(term, target or "", case_sensitive=False):
                out.append(
                    {
                        "dimension": "Terminology",
                        "severity": "major",
                        "note": f"required term '{term}' missing",
                        "source": "rulebook",
                    }
                )
    return out


STYLE_GUIDE_OFF = "NO_STYLE_GUIDE"


# --- B3: style-guide adherence (SPEC-workflows-redesign Track B) --------------
# When an LQA step runs AND an APPROVED style guide resolves for the run's scope
# (the SAME resolution B2 wired for translate steps), its RENDERED, injection-fenced
# block is appended to the LLM judge prompt with an instruction to check adherence and
# report each violation under the MQM "Style" dimension (severity minor/major per the
# judge). ON BY DEFAULT: the whole point of an LQA step is to verify the package that
# was injected, so a resolvable guide is checked automatically — there is no
# WorkflowStep off-switch (that would need schema, out of B3's scope). The escape
# hatch is a ZERO-SCHEMA rulebook directive, authored in the SAME rulebook the LQA
# step already loads, in the FORBID/REQUIRE spirit: a lone `NO_STYLE_GUIDE` line
# suppresses guide injection for that language pair.
def style_guide_suppressed(rulebook: str) -> bool:
    """True when the pair's rulebook opts OUT of style-guide adherence checking via a
    lone `NO_STYLE_GUIDE` directive line (case-insensitive, whitespace-tolerant). The
    per-pair off switch that needs NO WorkflowStep schema change — the caller passes the
    resolved guide unconditionally and score_text drops it here when suppressed, so the
    whole opt-out is centralized + testable at the service seam (mirrors how FORBID/
    REQUIRE live in the same rulebook the step already loads)."""
    for line in (rulebook or "").splitlines():  # noqa: SIM110
        if line.strip().upper() == STYLE_GUIDE_OFF:
            return True
    return False


def _style_clause(style_guide: str) -> str:
    """The style-adherence instruction appended to the judge prompt, or "" when no guide
    block is supplied. EMPTY -> the clause occupies ZERO bytes so the judge prompt is
    BYTE-IDENTICAL to the pre-B3 prompt (the sacred gate, proved by a captured-prompt
    regression). The block is B2's already-fenced/sanitized render (reused verbatim, not
    re-sanitized here — forging the fence was defended at render time)."""
    if not style_guide:
        return ""
    return (
        "Additionally, verify the translation ADHERES to the style guide below. Report "
        'EACH deviation as an error whose "dimension" is exactly "Style" and whose '
        '"severity" is "minor" or "major" depending on how seriously it breaks the '
        "guide:\n"
        f"{style_guide}\n\n"
    )


def _memory_clause(memory_block: str) -> str:
    """Judge-recall PLUMBING (Train D, SPEC-llmemory-phase2 §7.4, LAW 5). Returns "" —
    and the judge prompt is BYTE-IDENTICAL — unless the default-OFF
    `llmemory_judge_recall_enabled` flag is on AND a caller supplied a memory block. No
    orchestrator caller wires it live, so this is inert by construction; the injection
    point is IDENTIFIED, not turned on.

    LAW 5: the LQA/QE judge stays memory-blind unless the flag is explicitly on, and it
    may be turned on ONLY with Train E's four-arm ablation harness measuring — the judge
    is the independent measure of whether memory helped, so feeding it memory without the
    Lab watching destroys that property. Turning it on without the Lab is a policy error
    (settings.py rationale forbids it)."""
    block = (memory_block or "").strip()
    if not block:
        return ""
    from app import extensions
    from app.services import prompts

    if not extensions.memory.judge_recall_enabled():
        return ""
    # #362: the judge prompt is one message, so the reference-data sentence follows the block.
    return f"{block}\n{prompts.MEMORY_FENCE_INSTRUCTION}\n\n"


GLOSSARY_CAP = 2000


APPROVED_TERM_MISUSE = "Terminology/Approved-term-misuse"


_DNT_RENDER = "(do not translate; keep verbatim)"


# --- Phase 2 (A): the glossary-aware judge -----------------------------------
# PROPOSAL-lqa-glossary-adjudication §4.A. The judge was the ONLY major consumer of
# translation quality in this codebase that could not see the glossary — while the
# FIXER it gates receives `{glossary}` on every segment. The two halves of the
# correction loop were therefore given CONTRADICTORY terminology instructions, and the
# judge's only terminology authority was the STYLE GUIDE (`_style_clause` tells it to
# report EACH deviation), which is the exact inversion of the standing policy that the
# GLOSSARY is always the terminology authority and a style guide carries mechanics only.
#
# The terms are injected as DATA-WITH-CAVEAT, never as ground truth (§6 alternative 2
# — "fully-aware judge" — is explicitly REJECTED: that makes a stale entry
# unchallengeable and makes a PROMPT the guarantee). The clause therefore does three
# things at once: it states the precedence rule, it withholds permission to flag the
# mere USE of an approved term, and it grants EXPLICIT licence to flag an approved term
# that is wrong in THIS context under its own dimension. Deterministic adjudication
# (Phase 3, `lqa_safety.adjudicate`) is the actual mechanism; this clause only reduces
# the false-positive rate at source so bad findings never pollute the raw count.
#
# BYTE-IDENTICAL WHEN NOTHING MATCHES: the whole block — including the `span` request
# Phase 3 consumes — renders to ZERO bytes when no term matched, so an install with no
# glossary sends the pre-Phase-2 judge prompt exactly (the `_style_clause` "" precedent,
# proved by a captured-prompt regression). Only MATCHED terms are ever injected (never
# the whole termbase): that is both the over-deference mitigation (§8 risk 1) and the
# scoping rule (§8 risk 7 — the caller passes ALREADY-RESOLVED, org/customer-scoped
# terms; this module never queries the glossary itself).
# whole-block ceiling (the style-guide / FINDINGS_CAP convention)
# The one finding class the judge keeps that a pre-filter architecture throws away: an
# APPROVED term used in the wrong sense / part of speech / grammatically mis-integrated.
# Phase 3 must NEVER adjudicate this dimension away, or A and B cancel each other out.
def _glossary_entry_line(term: dict) -> str:
    """One `- source -> target` line for the approved-terminology block, sanitized with
    the SAME fence-break defense `_san_finding` applies to harvested findings (a term is
    customer-authored text and can carry `<`, `>` or the full-width homoglyphs). ASCII
    arrow, matching `lqa_safety._terms_label`, not the `prompts.glossary_line` U+2192."""
    src = _san_finding(term.get("source_term") or "")
    if term.get("dnt"):  # noqa: SIM108
        tgt = _DNT_RENDER
    else:
        tgt = _san_finding(term.get("target_term") or "")
    line = f"- {src} -> {tgt}"
    sense = _san_finding(term.get("sense_label") or "")
    if sense:
        # Only a MULTI-SENSE entry carries a sense_label (glossary.match_terms sets it
        # to None otherwise), and it names the sense ALREADY CHOSEN for this text — so
        # the judge is told which reading was approved here, not offered a menu.
        line += f"  [sense: {sense}]"
    return line


def _glossary_order_key(term: dict, target: str):
    """Ordering under the cap (§4.A "Block shape"): (1) entries whose target_term is
    already PRESENT in the translation — those are the false-positive risk this clause
    exists to remove — then (2) DNT entries, then (3) the rest. Presence uses
    `quality._term_present`, the same whole-token, CJK-safe, case-rule-honouring matcher
    the write-back gate and Phase 1 use, so the three never disagree about "present"."""
    surface = (term.get("source_term") if term.get("dnt") else term.get("target_term")) or ""
    present = bool(surface) and quality._term_present(
        surface, target or "", bool(term.get("case_sensitive"))
    )
    if present:
        return 0
    return 1 if term.get("dnt") else 2


FINDINGS_CAP = 2000


FINDINGS_NOTE_CAP = 300


# --- downstream findings block (prompt-package engine) -----------------------
# An LQA scorecard rendered as ONE compact, injection-fenced block a DOWNSTREAM
# llm_agent step can consume via {lqa_findings} (or the orchestrator's auto-inject).
# whole-block ceiling (~ the style-guide cap); honest truncation
# per-finding note cap so one giant note can't dominate the block
def _san_finding(text) -> str:
    """Newline-flatten + neutralize angle brackets so a finding value can never forge a
    closing </lqa_findings> tag or a column-0 structure line — the SAME fence-break
    defense context_pkg._san applies to harvested metadata (layered: fencing alone is
    the weakest technique). Both ASCII `<`/`>` AND the full-width homoglyphs `＜` (U+FF1C)
    / `＞` (U+FF1E) are neutralized so a homoglyph closing fence can't render either."""
    s = " ".join(str(text if text is not None else "").split())
    return s.replace("<", "&lt;").replace(">", "&gt;").replace("＜", "&lt;").replace("＞", "&gt;")


def render_findings_with_spans(card, cap: int = FINDINGS_CAP) -> tuple[str, dict]:
    """Render an LQA scorecard as ONE compact, injection-fenced `<lqa_findings>` block for
    a downstream llm_agent prompt, or "" when `card` is falsy / carries no score.

    Each error becomes a line "N. [Dimension/severity] note"; the fence header carries the
    score + gate. Every value is sanitized (angle brackets neutralized, whitespace
    flattened) like context_pkg's fence-break defense, per-note capped, and the whole
    block is capped at `cap` chars with an HONEST "(+N more truncated)" marker. "" when no
    LQA has run yet -> the caller injects NOTHING (byte-identical prompt).

    PHASE 3 (§4.B "Interaction with the fixer block"): this is an INSTRUCTION surface,
    not an audit surface. ADJUDICATED findings (severity downgraded to `neutral` because
    their span targets a legitimately-applied approved glossary term) are WITHHELD here
    — telling a model to "resolve" the correct use of an approved term is exactly how
    the correction loop breaks the term. They are NOT deleted: they remain in the card,
    in the persisted LQAResult rows and in the report. The block gains ONE honest
    trailing line so the fixer is never QUIETLY handed a partial picture, in the spirit
    of the existing "(+N more truncated)" marker. ONE filter, so a future TM-segment
    rule (Phase 4 / D2) adds a second reason to the SAME withheld line rather than
    handing the fixer two contradictory partial pictures (§8 risk 8).

    ALSO returns `{finding_number: span}` for the lines actually emitted (#163). The
    judge supplies `span` as the verbatim substring of the translation an error is
    about, and the orchestrator attributes findings to segments with it. The map is
    produced HERE, beside the numbering it keys on, so the two can never drift: the
    number in the rendered line and the key in the map come from the same `i`.
    Findings with no span are simply absent from the map -- the caller treats an
    absent span as "unattributable", never as "belongs nowhere"."""
    if not card:
        return "", {}
    score = card.get("score")
    if score is None:
        return "", {}
    gate = _san_finding(card.get("gate") or "")
    # Built from the SHARED scaffold token (app.services.scaffold.LQA_FINDINGS_TAG) so this
    # fence and the scaffold-echo gate that detects it can never drift. Byte-identical to the
    # prior literal `<lqa_findings score="%s"%s>` / `</lqa_findings>`.
    head = '<{} score="{}"{}>'.format(
        scaffold.LQA_FINDINGS_TAG,
        _san_finding(score),
        (f' gate="{gate}"') if gate else "",
    )
    tail = f"</{scaffold.LQA_FINDINGS_TAG}>"
    all_errors = [e for e in (card.get("errors") or []) if isinstance(e, dict)]

    # `isinstance(..., dict)` on the marker, not truthiness: only the record
    # `lqa_safety.adjudicate` writes can withhold a finding from the fixer (the same
    # shape `result_note` requires). Defence in depth behind the boundary strip in
    # `_llm_errors` — a bare `"adjudicated": true` from a judge must never suppress.
    def _adj(e):
        return isinstance(e.get("adjudicated"), dict)

    withheld = sum(1 for e in all_errors if _adj(e))
    errors = [e for e in all_errors if not _adj(e)]
    lines: list[str] = []
    spans: dict = {}
    dropped = 0
    for i, e in enumerate(errors, 1):
        dim = _san_finding(e.get("dimension") or "?")
        sev = _san_finding(e.get("severity") or "?")
        note = _san_finding(e.get("note") or "")
        if len(note) > FINDINGS_NOTE_CAP:
            note = note[:FINDINGS_NOTE_CAP].rstrip() + "..."
        frag = "%d. [%s/%s]%s" % (i, dim, sev, (" " + note) if note else "")  # noqa: UP031
        candidate = head + "\n" + "\n".join(lines + [frag]) + "\n" + tail  # noqa: RUF005
        if len(candidate) > cap and lines:
            dropped = len(errors) - (i - 1)
            break
        lines.append(frag)
        _span = e.get("span")
        if isinstance(_span, str) and _span.strip():
            spans[i] = _span
    if dropped:
        lines.append("(+%d more finding%s truncated)" % (dropped, "" if dropped == 1 else "s"))  # noqa: UP031
    if withheld:
        lines.append(
            "(%d finding%s adjudicated as approved-glossary usage and withheld)"  # noqa: UP031
            % (withheld, "" if withheld == 1 else "s")
        )
    body = "\n".join(lines) if lines else "(no specific findings)"
    return head + "\n" + body + "\n" + tail, spans


def render_findings(card, cap: int = FINDINGS_CAP) -> str:
    """The rendered block alone, for every caller that does not need the span map."""
    return render_findings_with_spans(card, cap)[0]


def result_note(error: dict) -> str:
    """The note text to PERSIST for one finding (`LQAResult.note`).

    Byte-identical to `error["note"]` for every ordinary finding. An ADJUDICATED finding
    is prefixed with an ASCII, self-describing marker so the audit trail survives in the
    surfaces that already exist — the LQA scorecard page, the reports rollup and the
    `lqa` CLI — with NO new storage schema (`LQAResult.severity` already documents
    "Major | Minor | Neutral", so the downgrade itself persists in the existing column).
    §4.B wants the adjudication VISIBLE everywhere the finding is; this is that, minus
    the migration the phase brief explicitly scoped out."""
    note = str(error.get("note") or "")
    adj = error.get("adjudicated")
    if not isinstance(adj, dict):
        return note
    return "[adjudicated: {} ({}); original severity {}] {}".format(
        adj.get("rule") or "?",
        adj.get("term") or "?",
        adj.get("original_severity") or "?",
        note,
    )


def canon_severity(raw) -> str:
    """Canonical severity for PERSIST (§3.1.4). The judge/rulebook vocabulary is unbounded
    model text; fold it to a PENALTY key or the explicit `"other"` bucket so an unknown
    severity is VISIBLE in the report's `other` column rather than lost between columns.

    Applied ONLY when constructing an LQAResult row, so `_score` and `render_findings`
    (which read the in-memory card) stay byte-identical — a card's own errors are never
    mutated. Well-formed internal findings already carry a lowercase PENALTY key, so this
    is a no-op on them (the re-baselined adjudication counts are unaffected)."""
    s = str(raw if raw is not None else "").strip().casefold()
    return s if s in PENALTY else "other"


def canon_dimension(raw) -> str:
    """Canonical MQM dimension for PERSIST (§3.1.4). Split on the FIRST `/`; if the head
    matches MQM_DIMENSIONS case-insensitively, keep the canonical capitalization plus the
    ORIGINAL sub-category (the persisted vocabulary carries sub-categories — e.g.
    `Accuracy/Omission` — and we do NOT collapse them). Otherwise bucket under `Other/`
    with the raw value run through `_san_finding` (the HTML/fence neutralizer) and capped
    at 32 chars.

    This is the persist-time vocabulary FOLD. The render-time axis-key helper (Train B,
    reports.py) layers the CSV formula-injection guard (`=`/`+`/`-`/`@` → apostrophe) on
    top and sanitizes EVERY key before HTML/CSV/JSON — so a matched dimension's original
    sub-category, kept raw here, is still sanitized at the surface."""
    raw_s = str(raw if raw is not None else "")
    head, sep, sub = raw_s.partition("/")
    head_norm = head.strip()
    for d in MQM_DIMENSIONS:
        if head_norm.casefold() == d.casefold():
            return d + ("/" + sub if sep else "")
    return "Other/" + _san_finding(raw_s)[:32]


def sanitize_axis_key(raw, *, cap: int = 64) -> str:
    """The ONE report axis-key sanitizer (SPEC-wave2 §3.1.4, gate M3). Every report surface
    (HTML / CSV / JSON) MUST route a group-by key through this before rendering — Train B
    wires it at render time (SPEC Errata). Three layered defences:

      1. `_san_finding` — newline-flatten + angle-bracket / full-width-homoglyph neutralize
         (the repo's HTML/prompt-fence defence, reused not re-written);
      2. a length cap, so a model-authored key cannot dominate a header;
      3. a CSV / spreadsheet FORMULA-INJECTION guard (N10): a key whose first character is
         `=` / `+` / `-` / `@` is prefixed with an apostrophe. `--group-by dimension` puts
         model-authored text in CSV column 1, which is exactly the spreadsheet-formula
         vector `_san_finding` (an HTML defence, not a CSV one) does not close.

    Kept in `lqa` (this module already owns `_san_finding` + `canon_dimension`) so Train A
    ships and tests it without touching reports.py (Train B's file)."""
    # L13: flatten + CAP the RAW text BEFORE escaping, so the length cap can never slice an
    # HTML entity mid-sequence (`&lt;` -> `&l`). Escaping after truncation guarantees every
    # emitted entity is whole; the escaped result may exceed `cap` by a few chars, which is
    # the safe direction. `=`/`+`/`-`/`@` are not escaped, so the formula guard still sees
    # the real first character.
    flat = " ".join(str(raw if raw is not None else "").split())[:cap]
    s = _san_finding(flat)
    from app.output_safety import csv_safe_cell

    return csv_safe_cell(s)


_PLACEHOLDER = "..."


# The literal placeholder token printed in this module's judge prompt (line ~668):
#   Return ONLY JSON: {"errors":[{"dimension":"...","severity":"...","note":"..."}]}
# That template is ITSELF valid JSON, so a model that parrots it back (as a preamble
# "the format is {…}", or alone) hands llm_json.loads a real dict — which its
# verdict-preference rule would return AHEAD of the model's actual verdict later in the
# reply (R2-3). The SAME hole opens for a STRAY NON-ECHO dict — an `Example: {}`, a quoted
# `{"a":1}`, or an `{"errors":"none"}`/`{"errors":{...}}` non-list-errors shape in the
# judge's preamble is also a real dict the verdict-preference rule returns first (R2-3
# sibling, families emptyobj/straydict). BOTH are non-verdict dicts, and both are closed the
# same way: `_reject_non_verdict` (the `reject` belt, below) skips ANY dict that is not a
# real verdict — no `"errors"` key, a non-list `"errors"` value, or the pure-placeholder
# `{"errors":[...]}` echo — so the real verdict after it wins; when the non-verdict is the
# ONLY tolerant-path candidate, every candidate is rejected and loads raises -> the caller
# degrades to JUDGE_UNAVAILABLE.
#
# The CLEAN fast path (a lone stray/echo dict the parser returns BEFORE the belt runs, which
# does not screen the fast path) is closed by TWO different guards, per shape:
#   * a STRAY dict (no `"errors"` key, or a non-list `"errors"`) -> the errors-shape backstop
#     in `_llm_errors` (`not isinstance(data.get("errors"), list)` -> JUDGE_UNAVAILABLE);
#   * a template ECHO (`{"errors":[{all-"..."}]}`) HAS a list `"errors"`, so that backstop
#     does NOT fire — the placeholder severity `"..."` is off-vocabulary and the SEVERITY
#     fail-closed guard rejects it -> JUDGE_UNAVAILABLE (see §7b case 2b).
def _is_placeholder_finding(f) -> bool:
    """A single finding dict is TEMPLATE NOISE iff EVERY judged field still carries the
    literal ``"..."`` placeholder from the prompt's template — i.e. the whole finding is the
    bare template with nothing filled in. Keying off ALL fields (not any) is deliberate: a
    REAL finding may leave ONE field lazy (a ``"..."`` note beside a real dimension/severity),
    and rejecting that as noise would drop a genuine finding — the D1 regression. A finding
    with any real content falls through to the severity guard, which fails closed on any
    remaining placeholder (severity ``"..."`` is off-vocabulary)."""
    return isinstance(f, dict) and all(
        f.get(k) == _PLACEHOLDER for k in ("dimension", "severity", "note")
    )


def _reject_non_verdict(cand) -> bool:
    """`reject` predicate for llm_json.loads: True when `cand` is a dict that is NOT a real
    LQA verdict — rejected for EITHER of the two R2-3 reasons:

      * a TEMPLATE ECHO — the prompt prints a valid-JSON template, and a model that parrots
        it back hands the parser a real dict; and
      * a STRAY NON-ECHO dict — an `Example: {}`, a quoted `{"a":1}`, or a bare inner-
        template finding `{"dimension":"...",...}` the judge drops in its preamble (R2-3
        sibling, families emptyobj/straydict).

    llm_json's verdict-preference rule would return either dict AHEAD of the real
    `{"errors":[...]}` verdict later in the reply, so both must be skipped by the belt.

    The bar is a LIST-VALUED `"errors"` key. A completed shape survey found NO legit judge
    emission is a dict without it: the recorded legit shapes are `{"errors":[...]}` (possibly
    with extra keys, e.g. `{"tags":[],"errors":[...]}` — which MUST stay accepted), top-level
    arrays `[{...},...]` (lists, not dicts — untouched by this predicate; normalized
    post-parse) and a clean `[]` (also a list). So:

      * a dict with NO `"errors"` key -> rejected. This is the stray dict AND the bare inner-
        template finding, and it SUBSUMES the old bare-placeholder-finding arm (a placeholder
        finding has no `errors` key either, so it is caught here regardless of its contents).
      * a dict whose `"errors"` value is present but NOT a list (`{"errors":"none"}`,
        `{"errors":{"x":1}}`, `{"errors":"critical accuracy error"}`) -> rejected. This is not
        a verdict shape: iterating it yields zero findings (a false clean pass), and as a
        preamble aside it MASKS the real verdict after it / can launder a truncated payload.
      * ``{"errors": [...]}`` — a template ECHO iff the list is NON-EMPTY and its EVERY dict
        finding is a full placeholder (see ``_is_placeholder_finding``). A single finding with
        ANY real field keeps the candidate (the list is not pure template — the D1 discipline:
        all(), not any()). ``{"errors": []}`` is the legitimate ZERO-FINDING verdict -> NOT
        rejected. A NON-EMPTY list with NO dict findings (all strings/ints) is NOT a readable
        verdict -> rejected (it would score a false clean pass; symmetric with the
        list-normalization all-non-dict guard in ``_llm_errors``). A MIXED list (>=1 real dict
        finding + interleaved non-dict junk) is KEPT: the per-item isinstance filter downstream
        lands its dict finding(s).

    KNOWN RESIDUAL (finding 4, pre-existing, safe direction, OUT of this fix's dict-family
    scope): a stray dict WRAPPED IN A LIST (`[{"a":1}]`) before a real verdict is returned by
    llm_json's rule 2 (a list with >=1 dict IS verdict-shaped), so this dict predicate never
    sees it and the real verdict after it does NOT land. It still fails CLOSED downstream (the
    severity guard reads the empty severity as off-vocab -> JUDGE_UNAVAILABLE), never a false
    pass — documented + pinned in verify_judge_json §8, not a desired endpoint.

    Fail-closed doctrine (#56 / R2-3): a candidate that is not a readable verdict must never
    be scored as a clean pass; when the ONLY tolerant-path candidate is rejected, loads raises
    and the caller degrades to JUDGE_UNAVAILABLE rather than auto-approving flagged content.
    """
    if not isinstance(cand, dict):
        return False
    if "errors" not in cand:
        return True  # stray dict / bare inner-template finding -> not a verdict, fail closed
    errs = cand.get("errors")
    if not isinstance(errs, list):
        return True  # {"errors": <non-list>} is not a verdict shape -> reject (masks/launders)
    if not errs:
        return False  # {"errors": []} -> the legit zero-finding verdict, keep
    dict_findings = [e for e in errs if isinstance(e, dict)]
    if not dict_findings:
        # A NON-EMPTY errors list with ZERO dict findings (e.g.
        # `{"errors":["Accuracy: mistranslated","Style: too casual"]}`) is not a readable
        # verdict: the per-item isinstance filter downstream keeps zero findings, so it would
        # score a false clean pass -- and as a preamble it masks the real verdict / launders a
        # truncated payload. Fail closed. Symmetric with the list-normalization all-non-dict
        # guard in `_llm_errors`: the IDENTICAL content as a bare top-level array already fails
        # closed there, so the wrapped shape (the one the prompt actually requests) must too.
        return True
    return all(_is_placeholder_finding(e) for e in dict_findings)


def _judge_shape(res: dict) -> dict:
    """The FAILURE SHAPE of the ONE judge call (issue #96), in the same field vocabulary
    `run_completion` hands an `llm_agent` step: the HONESTLY-RESOLVED provider/model that
    actually ran, how many attempts it took, the terminal finish reason and the terminal
    provider error class. `provider`/`model` are NULL whenever no call was made -- they
    feed `StepRun.provider_used`/`model_used`, and `report --group-by engine` buckets on
    those, so an engine that ran nothing must never appear there (gate C round 1, L4).
    Metadata only -- no prompt, no response text -- so it is safe on every surface the LQA
    StepRun reaches (`--remote` included).

    WHY: before this, an `lqa` step wrote provider/attempts/error_class as NULL/0 on its
    StepRun, so `GET /api/runs/{id}` -> failure_shape reported that a judge which had just
    burned thousands of tokens had made no calls, taken no time and used no engine. A
    FAILING judge call (engine unreachable, thinking burn, truncation) was therefore
    invisible on the very surface run-190 Item C exists to provide."""
    return {
        "provider": res.get("provider_used"),
        "model": res.get("model_used"),
        "attempts": int(res.get("attempts") or 0),
        "finish_reason": res.get("finish_reason"),
        "error_class": res.get("error_class"),
    }


# --- numeral-only identical segments are ADVISORY (issue #345) --------------
# Owner decision 2026-09-29, aligning MQM LQA with the Jev structural rule from #335: a
# segment whose SOURCE is only numerals/punctuation (`3.`, `[4]`, `1)`, `2025`) and whose
# TARGET equals the source after whitespace normalisation is CORRECT as-is, so an
# "identical to source" finding on it is advisory (neutral severity, penalty 0, never
# fails the gate) rather than the Major the judge (and the structural check) emit.
# Deterministic post-processing in `score_text` — the prompt is NOT the guarantee.
ADVISORY_NUMERAL_SUFFIX = " (advisory: number-only segment, identical to source is correct)"


def numeral_only_source(source) -> bool:
    """True when a SOURCE string carries no translatable content (issue #345).

    Every character must be whitespace or fall in a NON-LETTER unicodedata category:
    N* (digits in any script), P* (punctuation, including brackets/parens), Z* (the
    Unicode separators; `str.isspace` covers the rest) or S* (symbols — currency
    marks, section signs, a `€` or `§` glued to a figure is still non-linguistic).
    At least one N*/P*/S* character is required, so blank / whitespace-only sources
    are False (an empty target is the separate `empty` defect, not this rule).
    Any letter (L*), mark (M*) or other category -> False."""
    s = str(source or "")
    if not s.strip():
        return False
    has_content = False
    for ch in s:
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] in ("N", "P", "S"):
            has_content = True
            continue
        return False
    return has_content


_IDENTICAL_SOURCE_PHRASES = (
    "untranslated",
    "identical to source",
    "identical to the source",
    "not translated",
    "same as source",
    "same as the source",
)


def _says_identical(e) -> bool:
    """The NOTE or DIMENSION uses the untranslated / identical-to-source wording."""
    if not isinstance(e, dict):
        return False
    text = " ".join(str(e.get(k) or "").casefold() for k in ("note", "dimension"))
    return any(p in text for p in _IDENTICAL_SOURCE_PHRASES)


def identical_source_finding(e, target) -> bool:
    """Whether one finding dict (issue #345) says the target is UNCHANGED SOURCE, the
    exact class that is advisory on a numeral-only segment.

    Explicit, case-insensitive (casefold) substring match of the NOTE or the
    DIMENSION against the phrase list above — that covers the judge's "untranslated" /
    "identical to source" / "same as source" wording in any capitalisation, the
    canonical `Accuracy/Untranslated` dimension, and the structural flag whose note is
    the literal "Target identical to source". Second arm: a finding whose `span`
    (the verbatim substring of the translation the error is about) EQUALS THE WHOLE
    target is, by construction, an identical-source finding regardless of wording."""
    if not isinstance(e, dict):
        return False
    if _says_identical(e):
        return True
    # Span arm: only for an empty or untranslated-type dimension. A Fluency / Style /
    # Accuracy finding that merely quotes the whole target is a different defect
    # (gate round 2, MEDIUM-1) and keeps its severity.
    dim = str(e.get("dimension") or "").casefold()
    span = e.get("span")
    return (
        isinstance(span, str)
        and bool(span)
        and span == str(target or "")
        and (not dim.strip() or "untranslated" in dim)
    )


def _norm_ws(s) -> str:
    return " ".join(str(s or "").split())


# Findings whose dimension is a locale-format or number-value defect are NEVER made
# advisory by the #345 rule: an unlocalised date/decimal or a changed number is a real
# error whatever the note or span says (gate finding 2). Matched on the casefolded
# dimension so any spelling ("Locale convention/Date", "locale_convention", "LOCALE")
# is covered.
def _protected_dimension(e) -> bool:
    dim = " ".join(re.sub(r"[^a-z0-9]+", " ", str(e.get("dimension") or "").casefold()).split())
    return dim.startswith(("locale", "number", "accuracy number"))


def downgrade_numeral_identical(errors, source, target) -> int:
    """Make "identical to source" findings on a numeral-only segment advisory (#345).

    Segment-aware: the workflow LQA step scores a whole-document blob whose segments
    are joined with SEG_JOINER, so source and target are split on it. A finding is
    downgraded only when it can be attributed to a segment that is numeral-only AND
    equal to its own source segment (whitespace-normalised): its `span` must equal
    that segment's target. A span-less finding on a multi-segment blob cannot be
    attributed and is left alone. A single-segment call (or a blob whose segment
    counts differ) uses the whole-string rule: numeral-only source and target equal
    to source. When EVERY segment qualifies (an all-numeral document) the span-less
    structural "Target identical to source" flag is downgraded too.

    Never downgraded, whatever the note or span says: a finding whose dimension is
    Locale convention* or Accuracy/Number (real format / value defects), and any
    finding on a segment whose number CHANGED (`3.` -> `4.`). Any OTHER finding (a real
    Style/Accuracy major, a rulebook REQUIRE miss) is untouched and still fails the
    gate.

    A downgraded finding becomes severity `neutral` (penalty 0, not major/critical),
    its note gains `ADVISORY_NUMERAL_SUFFIX` (idempotent) and it records
    `advisory_numeral: {"original_severity": ...}` like the glossary adjudicator does.
    Returns the count changed."""
    # Lazy import: SEG_JOINER lives in the orchestrator layer, which imports lqa (this
    # module's parent), so a module-level import would be circular.
    from app.services.orchestrator_terminology_backen import SEG_JOINER

    src_parts = str(source or "").split(SEG_JOINER)
    tgt_parts = str(target or "").split(SEG_JOINER)
    multi = len(src_parts) > 1 and len(src_parts) == len(tgt_parts)
    if not multi:
        src_parts, tgt_parts = [str(source or "")], [str(target or "")]
    # #705: a tags-only segment (only inline tags / placeholders) delivered as its own
    # copy is the same advisory class as a numeral-only one.
    from app.services.tags_only import is_tags_only

    ok = [
        (numeral_only_source(s) or is_tags_only(s)) and _norm_ws(s) == _norm_ws(t)
        for s, t in zip(src_parts, tgt_parts, strict=True)
    ]
    if not any(ok):
        return 0
    all_ok = all(ok)
    good_spans = {_norm_ws(t) for t, k in zip(tgt_parts, ok, strict=True) if k}
    bad_spans = {_norm_ws(t) for t, k in zip(tgt_parts, ok, strict=True) if not k}
    changed = 0
    # Idempotency is checked against the marker WITHOUT its leading space: an
    # empty note gets the stripped marker (no leading space), so guarding on the
    # spaced form would let a re-score append the marker a second time.
    marker = ADVISORY_NUMERAL_SUFFIX.strip()
    for e in errors or []:
        if not isinstance(e, dict) or _protected_dimension(e):
            continue
        if not multi:
            if not identical_source_finding(e, target):
                continue
        elif not all_ok:
            span = _norm_ws(e.get("span")) if isinstance(e.get("span"), str) else ""
            if not span or span not in good_spans or span in bad_spans:
                continue
            # The span only picks WHICH segment the finding is about; the wording
            # (untranslated / identical to source) is still required to downgrade.
            if not _says_identical(e):
                continue
        elif not identical_source_finding(e, target) and not (
            isinstance(e.get("span"), str)
            and _norm_ws(e.get("span")) in good_spans
            and not str(e.get("dimension") or "").strip()
        ):
            continue
        if "advisory_numeral" not in e:
            e["advisory_numeral"] = {"original_severity": str(e.get("severity") or "")}
        e["severity"] = "neutral"
        note = str(e.get("note") or "")
        if marker not in note:
            e["note"] = (note + " " + marker) if note else marker
        changed += 1
    return changed


def _score(errors: list[dict], threshold=None) -> tuple[int, bool]:
    """Score = 100 - total penalty; PASS when score >= the numeric bar AND no
    major/critical error. `threshold` is the LQA step's configured pass bar
    (WorkflowStep.lqa_threshold); None (every non-workflow caller and every unset
    step) falls back to LQA_PASS_THRESHOLD (90) so the gate is byte-identical to
    before. A supplied value is clamped to [0, 100] here too (defense in depth — the
    authoring layer already clamps at write time). The no-major-error policy is
    UNCHANGED: only the numeric bar is configurable."""
    total = sum(PENALTY.get(e.get("severity", "minor"), 1) for e in errors)
    score = max(0, 100 - total)
    # clamp_threshold coerces junk/blank/None -> None (inherit the default) and a real
    # value -> a clamped int, so a malformed threshold degrades to 90 rather than raising.
    bar = clamp_threshold(threshold)
    bar = LQA_PASS_THRESHOLD if bar is None else bar
    passed = score >= bar and not any(e.get("severity") in ("major", "critical") for e in errors)
    return score, passed


def _surviving(e) -> bool:
    """THE SURVIVING-ONLY GUARD for LLMemory capture (PROPOSAL-llmemory §4.3).

    An adjudicated finding is one the system decided was WRONG — `lqa_safety.adjudicate`
    downgrades it to `neutral` and stamps an `adjudicated` record on it because its span
    targets a legitimately-applied APPROVED GLOSSARY TERM. Learning from it would teach
    the exact opposite of the truth: LLMemory would grow a memory arguing against the
    customer's own approved terminology, and then inject that memory into future
    translate prompts. `llmemory.record_lqa_finding` CANNOT enforce this (it is handed
    one finding at a time, with no view of the card), so the filter lives here, at the
    only place that can see the adjudicator's verdict.

    TWO independent rejections, deliberately not one:
      * ANY TRUTHY `adjudicated` value — not just the `dict` record
        `lqa_safety.adjudicate` writes. This is the ONE place that deliberately reads
        the marker more loosely than `render_findings._adj` does, and the asymmetry is
        the point: for the FIXER'S BLOCK, honouring a forged `"adjudicated": true`
        would SUPPRESS a real finding, so it insists on the dict. Here the two error
        directions are reversed — a forged marker costs at most ONE UNLEARNED finding,
        while missing a real one would WRITE A WRONG BELIEF into long-lived memory and
        then inject it into future prompts. When the cheap mistake and the expensive
        mistake sit on opposite sides of a predicate, take the cheap one. (Forgery is
        already improbable: `_llm_errors` strips the key from judge output at the
        boundary, so only our own code can set it.)
      * `severity == "neutral"` — a zero-penalty finding is not evidence of a defect
        regardless of HOW it got there, so a future second adjudication rule (or a
        judge that returns `neutral` directly) is filtered by this arm without anyone
        having to remember to update this function.

    Between them these cover the same findings `render_findings` withholds from the
    fixer, so the correction loop and LLMemory can never learn different lessons from
    the same card."""
    if not isinstance(e, dict):
        return False
    if e.get("adjudicated"):
        return False
    if str(e.get("severity") or "").strip().lower() == "neutral":
        return False
    return bool(str(e.get("dimension") or "").strip())


def _capture_lqa_findings(errors, tap_meta, target_locale) -> None:
    """Record the SURVIVING findings of one scorecard as LLMemory events.

    `score_text` is deliberately session-free (it never queries the glossary — the
    hand-rolled-scope-filter defect class), so capture opens its OWN short-lived
    session, the same posture `audit.log` uses from equally pure call sites.

    Scope comes from `tap_meta`, the correlation dict the CALLER already assembles for
    the Prompt Tap (document_id / workflow_run_id). A caller with no tap_meta —
    `lqa.score_document`, the A/B harness — yields no document, so
    `llmemory._scope_for_document(None)` returns a customer-less scope and the event is
    honest but un-distillable: the safe direction, and nothing here invents an id.

    NO SEGMENT ID IS FORWARDED (re-review F3). This seam used to read
    `tap_meta["segment_id"]`, but NO caller has ever put one there: the LQA judge scores
    a whole-document blob, and the only segment-ish key any call site writes is
    `segment_index` (an order index within a document, not a `Segment.id` — reading
    it as one would attribute findings to arbitrary segments). The read was dead code
    behind a true-sounding claim, so both are gone; every LQA finding is doc-level,
    which is exactly what `llmemory._independence` assumes.

    IDENTITY IS PASSED THROUGH, NOT DROPPED (adversarial review S6). The distiller's
    ">=3 independent findings" gate counts DISTINCT VOICES, and it can only do that from
    what this seam gives it. Passing neither a document id nor a run id made every
    finding its own voice, so a SINGLE scorecard with three findings cleared a gate
    written to require three, and re-scoring the same document manufactured fresh
    "independent" evidence every time. `document_id` and `workflow_run_id` are both
    forwarded, and `llmemory._independence` derives the rest: a doc-level card's findings
    are distinguished by their normalized SPAN WITHIN THE DOCUMENT, so three findings
    about the same span are one voice and re-scoring that document — in this run or any
    later one — corroborates nothing. What it does NOT claim: three findings about three
    DIFFERENT spans of one document are three voices, on one scorecard (see
    `llmemory._independence` for why that bar is where it is).

    Fully guarded and switch-gated (the service's own `capture_enabled`): scoring is a
    gate decision the whole run depends on, and it must never fail because a memory row
    could not be written."""
    try:
        from app import extensions

        llmemory = extensions.memory
        if not llmemory.capture_enabled():
            return
        meta = tap_meta if isinstance(tap_meta, dict) else {}
        doc_id = meta.get("document_id")
        run_id = meta.get("workflow_run_id")
        survivors = [e for e in (errors or []) if _surviving(e)]
        if not survivors:
            return
        from app.db import engine as _engine

        with Session(_engine) as s:
            for e in survivors:
                llmemory.record_lqa_finding(
                    s,
                    dimension=str(e.get("dimension") or ""),
                    severity=str(e.get("severity") or ""),
                    span=str(e.get("span") or ""),
                    note=str(e.get("note") or ""),
                    document_id=doc_id,
                    workflow_run_id=run_id,
                    target_locale=target_locale or "",
                    actor="system",
                )
    except Exception:
        logging.getLogger("loc_tms.lqa").warning(
            "llmemory: lqa_finding capture failed", exc_info=True
        )


# --- reviewer-authored MQM findings (QA-2, SPEC-wave3-collaboration §6.2) ------ #
class HumanFindingError(ValueError):
    """A reviewer finding was refused at input (empty note / off-taxonomy dimension /
    editing a machine row). Surfaces map it to 400."""


class HumanFindingForbidden(Exception):
    """The caller lacks the rung/ownership to log / edit / withdraw this finding (§6.5).
    Surfaces map it to 403. Enforced in the SERVICE as defence in depth (the service is
    the one truth, §4.1) IN ADDITION to Train C's surface RBAC — and testable here, which
    is why it lives in the writer Train A owns rather than only on C's surfaces.

    NOTE (deliberate extension of §6.2's bare signature): the §6.2 signature named only
    (segment, actor, ...). The gate (H2/H3) requires attribution that cannot be forged and
    RBAC that can be tested; both need `user`, so the three helpers gain `user` +
    `trusted_operator` and derive the actor from the authenticated user (never the free
    `actor` string) exactly as query_threads does."""


def _finding_identity(user, actor: str, trusted: bool) -> str:
    """Unforgeable acting email (H2): authenticated -> user.email; trusted operator ->
    the actor string."""
    if trusted:
        return actor or ""
    return getattr(user, "email", "") or ""


def _require_reviewer(user, trusted: bool) -> None:
    from app.services import security

    if trusted:
        return
    if not security.can_review(user):
        raise HumanFindingForbidden("logging an LQA finding requires a reviewer role or higher.")


def _require_edit_owner(user, actor: str, trusted: bool, finding: LQAResult) -> None:
    """Edit/withdraw need reviewer+ AND (own the row OR can_manage+) — a manager may clear
    a departed reviewer's finding (§6.5)."""
    from app.services import security

    if trusted:
        return
    if not security.can_review(user):
        raise HumanFindingForbidden("editing an LQA finding requires a reviewer role or higher.")
    me = _finding_identity(user, actor, trusted)
    if me and me == (finding.actor or ""):
        return
    if security.can_manage(user):
        return
    raise HumanFindingForbidden("only the finding's author or a manager may edit or withdraw it.")


def _require_human_dimension(dimension: str) -> None:
    """A human picks a dimension from a dropdown, so the head (before any `/`) MUST be a
    real MQM dimension — the `Other/...` bucket is a MACHINE-output affordance and is
    refused here (§6.2). A free-text sub-category after `/` is allowed and preserved by
    canon_dimension."""
    head = str(dimension or "").partition("/")[0].strip().casefold()
    if head not in {d.casefold() for d in MQM_DIMENSIONS}:
        raise HumanFindingError("dimension must be one of: {}.".format(", ".join(MQM_DIMENSIONS)))


def log_human_finding(
    session: Session,
    *,
    segment: Segment,
    dimension: str,
    severity: str,
    note: str,
    user=None,
    actor: str = "",
    trusted_operator: bool = False,
) -> LQAResult:
    """Persist ONE reviewer-authored MQM finding (§6.2). Requires reviewer+ (§6.5, unless
    trusted operator). Canonicalizes through the SAME canon_dimension / canon_severity the
    machine path uses (no second vocabulary). Stamps finding_source='human', the
    UNFORGEABLE actor (user.email when authenticated, else the actor string — H2), the
    segment's locale, and — through the existing vendor_for_scored_content helper — the
    vendor leg that produced the scored content, or (None, None) when there is no RETURNED
    leg (nothing is invented, maintainer default #4). score / scoring_id / judge stay NULL: a
    hand-logged bug is not a scored card (§6.6).

    NEVER touches the in-memory card path — score_text / _score / render_findings stay
    byte-identical (§6.2), so the sacred judge prompt + gate are untouched.

    TRAIN C CONTRACT: the §6.2 signature is now identity-DERIVED — a surface passes the
    AUTHENTICATED `user` (web/`/api`), and the CLI passes `trusted_operator=True` + an
    `actor`. Never pass a free `actor` string alongside a `user`: it is ignored and the
    author is always `user.email` (unforgeable, H2)."""
    _require_reviewer(user, trusted_operator)
    if not (note or "").strip():
        raise HumanFindingError("a finding note cannot be empty.")
    _require_human_dimension(dimension)

    locale = segment.target_locale or ""
    vendor_id, dispatch_id = _human_vendor_attribution(session, segment, locale)
    row = LQAResult(
        document_id=segment.document_id,
        segment_id=segment.id,
        dimension=canon_dimension(dimension),
        severity=canon_severity(severity),
        note=note,
        score=None,
        target_locale=locale,
        finding_source="human",
        judge=None,
        scoring_id=None,
        vendor_id=vendor_id,
        dispatch_id=dispatch_id,
        actor=_finding_identity(user, actor, trusted_operator),
        withdrawn_at=None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _human_vendor_attribution(session: Session, segment: Segment, locale: str):
    """(vendor_id, dispatch_id) for a human finding: resolve the document's MOST RECENT
    WorkflowRun for this locale, then defer to lqa_attribution.vendor_for_scored_content
    (the RETURNED-leg-at-or-before-now rule). (None, None) when there is no run or no
    returned leg."""
    from app.services import lqa_attribution

    # L2: case-normalize BOTH sides of the locale match (the L5 lesson from score_document
    # above) so a run stored 'de-DE' and a segment locale 'de-de' still resolve the leg.
    run = session.exec(
        select(WorkflowRun)
        .where(
            WorkflowRun.document_id == segment.document_id,
            func.lower(WorkflowRun.target_locale) == (locale or "").lower(),
        )
        .order_by(WorkflowRun.id.desc())
    ).first()
    if run is None:
        return (None, None)
    return lqa_attribution.vendor_for_scored_content(
        session, workflow_run_id=run.id, target_locale=locale, scored_at=datetime.utcnow()
    )


def edit_human_finding(
    session: Session,
    *,
    finding: LQAResult,
    dimension: str,
    severity: str,
    note: str,
    user=None,
    actor: str = "",
    trusted_operator: bool = False,
) -> LQAResult:
    """Edit a reviewer finding's dimension / severity / note (§6.5). Reviewer+ AND
    (own-only OR can_manage+) — enforced here as defence in depth. Refuses the same inputs
    as the writer and refuses to touch a MACHINE row.

    TRAIN C CONTRACT: identity is derived (see log_human_finding) — pass the authenticated
    `user` from a surface; `trusted_operator=True` + `actor` only from the CLI."""
    if finding.finding_source != "human":
        raise HumanFindingError("only a reviewer-authored finding can be edited.")
    _require_edit_owner(user, actor, trusted_operator, finding)
    if not (note or "").strip():
        raise HumanFindingError("a finding note cannot be empty.")
    _require_human_dimension(dimension)
    finding.dimension = canon_dimension(dimension)
    finding.severity = canon_severity(severity)
    finding.note = note
    session.add(finding)
    session.commit()
    session.refresh(finding)
    return finding


def withdraw_human_finding(
    session: Session,
    *,
    finding: LQAResult,
    user=None,
    actor: str = "",
    trusted_operator: bool = False,
) -> LQAResult:
    """Soft-delete a reviewer finding: stamp `withdrawn_at` (§6.7). NOT a row delete — a
    hard delete would leave the append-only lakehouse stream holding a finding that
    exists nowhere. Reviewer+ AND (own-only OR can_manage+). Idempotent: a re-withdraw
    keeps the original timestamp. Refuses a machine row.

    TRAIN C CONTRACT: identity is derived (see log_human_finding) — pass the authenticated
    `user` from a surface; `trusted_operator=True` + `actor` only from the CLI."""
    if finding.finding_source != "human":
        raise HumanFindingError("only a reviewer-authored finding can be withdrawn.")
    _require_edit_owner(user, actor, trusted_operator, finding)
    if finding.withdrawn_at is None:
        finding.withdrawn_at = datetime.utcnow()
        session.add(finding)
        session.commit()
        session.refresh(finding)
    return finding
