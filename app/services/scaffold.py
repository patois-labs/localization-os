"""Prompt-scaffold markers + the scaffold-echo detector (core-QA campaign Q10, K4-class).

Live prod evidence (doc09.docx = doc 83,
es-ES): stored segment TARGETS contained our OWN prompt scaffolding, echoed back by a weak
local model and persisted as content. Segment 0 / 12 targets were the fenced STYLE-GUIDE
block (`<style_guide source="Core QA es" trust="data-only">Guía de estilo — Core QA …`);
segment 11 stored `… MQM LQA encuentra para resolver:` — the auto-injected LQA-findings
preamble ("MQM LQA findings to resolve:") echoed and half-translated. Segments 0/12 sat at
needs_review, but segment 11 was stored as CLEAN `mt` (mt_confidence 0.9). The structural
LENGTH-RUNAWAY gate did not fire (the echo was not disproportionately long relative to its
source). This is the K4 class: model chatter / our own scaffolding stored as a real
translation.

WHY A MODEL PRODUCES THIS AT ALL. The reviewer / post-editor default prompts (orchestrator
`ROLE_PRESETS`) open with `{context}{style_guide}` and, on a correction loop, append the
`{lqa_findings}` block. Those placeholders substitute CORRECTLY — into the context region at
the top of the prompt, exactly where they belong — so there is no template mis-substitution
bug to fix. The leak is a weak local model ECHOING its fenced input instead of returning
only the corrected translation. The fences are echo-RESISTANT by design (`trust="data-only"`
tells the model the block is data, not instructions) but not echo-PROOF against a weak model.
So "prompts persuade, gates enforce": this module is the enforcement.

SINGLE SOURCE OF TRUTH (the whole reason this is its own leaf module). The prompt BUILDERS
that inject the scaffolding — `style_guides` (the `<style_guide>` fence), `context_pkg` (the
`<translation_context>` fence), `lqa` (the `<lqa_findings>` fence) and `orchestrator` (the
`MQM LQA findings to resolve:` preamble) — all construct their fences FROM the tokens defined
here, and the detector below matches on the SAME tokens. A marker therefore cannot drift from
what actually goes into a prompt: change a fence, and both the builder and the gate move
together. `tests/verify_scaffold_echo.py` additionally pins that every rendered fence contains
its marker.

This is a LEAF module: it imports nothing from `app`, so `quality` (the sole MT write seam)
and every prompt builder can depend on it without a cycle. Deterministic, LLM-free,
language-independent, SAFE OFFLINE.

Documented in: docs/architecture/quality-lqa-evaluations.md
"""

import re as _re

# --- the canonical scaffold tokens (the prompt builders construct their fences from these)
# The attribute stamped on every DATA-ONLY injection fence. Shared verbatim by the
# `<style_guide>` and `<translation_context>` fences; a real translation never contains it.
TRUST_ATTR = 'trust="data-only"'

# The tag NAMES of the fenced blocks our builders wrap scaffolding in.
STYLE_GUIDE_TAG = "style_guide"
CONTEXT_TAG = "translation_context"
LQA_FINDINGS_TAG = "lqa_findings"
JEV_REFINE_TAG = "jev_refine"


def fence(tag: str, text: str) -> str:
    """Wrap a prompt section with the shared scaffold convention."""
    return f"<{tag} {TRUST_ATTR}>\n{text}\n</{tag}>"


# The human-readable label that prefixes an auto-injected LQA-findings block
# (`orchestrator._LQA_FINDINGS_PREAMBLE` is built from this). "MQM LQA" — two adjacent
# quality-framework acronyms — is the invariant token that survives even when a model
# TRANSLATES the rest of the preamble (segment 11's "MQM LQA encuentra para resolver:").
LQA_FINDINGS_LABEL = "MQM LQA findings to resolve"
_LQA_FINDINGS_INVARIANT = "MQM LQA"


def markers() -> tuple:
    """The literal substrings whose presence in MODEL OUTPUT means our own prompt
    scaffolding was echoed back as a translation.

    STRUCTURAL and low-false-positive by construction:
      * `trust="data-only"` — the shared data-only fence attribute (style guide + context);
        segments 0/12's echoes carry it.
      * the fence OPENING and CLOSING tags (`<style_guide `/`</style_guide>`, the context
        pair, the `<lqa_findings`/`</lqa_findings>` pair) — a real translation never emits
        an XML-ish fence tag; caught whether the echo kept the opener, the closer, or both.
      * `MQM LQA` — the invariant token of the LQA-findings preamble, which survives a model
        translating the surrounding words (segment 11). This is the ONE marker with a
        non-zero false-positive class: a document genuinely ABOUT MQM/LQA tooling could
        contain the bigram. That residual is acceptable precisely because the gate is SOFT
        (see `quality.commit_mt_output`): a false positive costs one review flag, never a
        dropped or rewritten translation, and the abstain-when-in-source rule below spares a
        source that legitimately discusses it.

    Deliberately NOT markers: bare "style guide" / "findings" as prose (no fence tokens),
    which is exactly the false-positive discipline `tests/verify_scaffold_echo.py` pins."""
    return (
        TRUST_ATTR,
        f"<{JEV_REFINE_TAG}>",
        f"<{JEV_REFINE_TAG} ",
        f"</{JEV_REFINE_TAG}>",
        f"<{STYLE_GUIDE_TAG} ",
        f"</{STYLE_GUIDE_TAG}>",
        f"<{CONTEXT_TAG} ",
        f"</{CONTEXT_TAG}>",
        f"<{LQA_FINDINGS_TAG} ",
        f"<{LQA_FINDINGS_TAG}>",
        f"</{LQA_FINDINGS_TAG}>",
        _LQA_FINDINGS_INVARIANT,
    )


SCAFFOLD_ECHO_REASON = (
    "suspected prompt-scaffold echo: the target reproduces our own injected prompt structure"
)


def scaffold_echo(target_text: str | None, source_text: str | None = None) -> str:
    """A short reason string when `target_text` contains one of our prompt-scaffold
    `markers()`, or "" when it is clean.

    Case-insensitive substring match (a model may re-case an echoed marker). ABSTAINS per
    marker when the SAME marker is also present in `source_text`: a source that legitimately
    carries `</style_guide>` or the "MQM LQA" bigram would hand it to a faithful translation,
    so that is not a scaffold LEAK. This is also what keeps the offline MockProvider safe —
    its plain echo returns the SOURCE (no markers unless the source itself had them), and only
    the echo-of-PROMPT path (markers present in output but NOT the source) is a real leak.

    Deterministic, LLM-free, language-independent. Empty/whitespace target -> "" (an empty
    output is the empty-output guards' business, not a scaffold echo)."""
    tgt = target_text or ""
    if not tgt.strip():
        return ""
    low_tgt = tgt.lower()
    low_src = (source_text or "").lower()
    for m in markers():
        lm = m.lower()
        if lm in low_tgt and lm not in low_src:
            return SCAFFOLD_ECHO_REASON + (f": the output carries the prompt-scaffold marker {m!r}")
    return ""


# --- the prompt's own SECTION LABELS (#664) -----------------------------------------------
# `prompts.translation_prompt` ends with "Source:\n<text>" and the adaptive/context builders
# fence their column-0 lines against forging these. ONE definition, imported by all three.
SOURCE_LABEL = "Source:"
TARGET_LABEL = "Target:"
STRUCTURE_TOKENS = (SOURCE_LABEL, TARGET_LABEL)

LABEL_ECHO_FLAG_TYPE = "label_echo_stripped"
LABEL_ECHO_STRIPPED_REASON = (
    "the model repeated a prompt section label before the translation; it was removed"
)
# The review verdict is a scaffold-echo reason (it starts with SCAFFOLD_ECHO_REASON), so it rides
# the existing flag, clear and subordination machinery with no new family.
LABEL_ECHO_REVIEW_REASON = (
    SCAFFOLD_ECHO_REASON + ": the output starts with a prompt section label that is followed "
    "by more than the translation, or may be part of it; keep it only if it translates the source"
)


# A label as a model may re-emit it: any case, optional space before an ASCII or fullwidth colon.
_LABEL_RES = tuple(
    _re.compile(r"(?i:" + _re.escape(t[:-1]) + r")[ \t]*[:：]") for t in STRUCTURE_TOKENS
)
# A source that opens with its own short "word:" label (a "Quelle: ..." citation line). A target
# that opens "Source: ..." on the same line may then be the faithful translation, not an echo.
_SRC_LABELLED_RE = _re.compile(r"^\W{0,3}[^\s:\uff1a]+(?: [^\s:\uff1a]+){0,2}[ \t]*[:\uff1a]")


def _lead_label(text):
    """Length of a leading section label (case-insensitive, `Source :` and fullwidth colon
    tolerated) at the start of `text`, or 0."""
    for rx in _LABEL_RES:
        m = rx.match(text)
        if m:
            return m.end()
    return 0


def label_echo(target_text, source_text=None, target_locale=None):
    """(text, verdict) for a model output that may start with one of the prompt's own section
    labels. verdict is:
      ""         nothing to do (text returned unchanged),
      "stripped" the output was ONLY a label plus the translation: text is the translation,
      "review"   more than the label precedes the translation (the source echoed back, a second
                 label, or nothing after the label), or the label could be a faithful translation
                 (a labelled source line, a line-count mismatch, or a same-line "Source: x" into
                 English / an unknown locale / from a source line with a colon): text is returned
                 UNCHANGED, never guessed at.
    `target_locale` (the segment's) decides the same-line shape only; None means "review".
    Abstains when the SOURCE itself starts with a section label. Anchored to the output start,
    case-insensitive and tolerant of `Source :` / a fullwidth colon, label must be the whole token
    ("Sources:" is content). Labels the model TRANSLATED are not recognised (a real "Fuente:" is
    indistinguishable from content). Deterministic, LLM-free, safe offline."""
    if not isinstance(target_text, str) or not target_text.strip():
        return target_text, ""
    src = (source_text or "").lstrip()
    lead = target_text.lstrip()
    n = _lead_label(lead)
    if not n or _lead_label(src):
        return target_text, ""
    after = lead[n:]
    rest = after.lstrip()
    if not rest:
        return target_text, "review"
    # More than the label before the translation: a second label up front, the source repeated
    # right after the label, or a column-0 label line later on (a "Source: .. Target: .." echo).
    if _lead_label(rest):
        return target_text, "review"
    stripped_src = (source_text or "").strip()
    if stripped_src and rest.startswith(stripped_src):
        return target_text, "review"
    for line in rest.splitlines()[1:]:
        if _lead_label(line):
            return target_text, "review"
    # Review gate: strip ONLY when the label cannot be a faithful translation of source content.
    # Anything ambiguous goes to review (a reviewer's minute), never a silent content loss.
    first = src.splitlines()[0] if src else ""
    # A source that opens with its own label ("Quelle: x", "Quelle:\nx", "[Quelle] x") may be
    # translated as "Source: x" / "Source:\nx" on either shape.
    if _SRC_LABELLED_RE.match(first) or first[:1] in _SRC_BRACKETS:
        return target_text, "review"
    # The label took a line of its own: the translation after it must have exactly as many
    # non-blank lines as the source ("目標\n100" -> "Target:\n100" is a translation, not an echo).
    if _line_count(rest) != _line_count(source_text or ""):
        return target_text, "review"
    gap = after[: len(after) - len(rest)]
    if "\n" not in gap:
        # Same-line "Source: x" (not the prompt's own "Source:\n" shape). Into English it can be
        # the real rendering of an unlabelled source ("出典 Wikipedia" -> "Source: Wikipedia"),
        # and so can any source line that carries a colon: strip only for a known non-English
        # target and a colon-free source line.
        lang = (target_locale or "").replace("_", "-").split("-")[0].strip().lower()
        if not lang or lang == "en" or any(c in first for c in _COLONS):
            return target_text, "review"
    return rest, "stripped"


_SRC_BRACKETS = "[【［(（"
_COLONS = ":：﹕∶"


def _line_count(text):
    return sum(1 for line in text.splitlines() if line.strip())
