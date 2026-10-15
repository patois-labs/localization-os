"""Deterministic OUTPUT guards for llm_agent step output (GitHub #343).

Three LLM-free, language-independent checks that catch what the refusal / length /
language perimeter let through when a step prompt leaks into a SHORT segment:

  * instruction_echo    -- the output contains a run of 4+ consecutive words taken from the
                           step's own literal prompt template / system prompt that the
                           SOURCE does not contain ("1. using the following style guide").
                           A run must carry at least two non-stopword words, so common
                           English phrases ("the meaning of the") never match.
                           The content-fence sentence (#353, `fence_echo`) is matched separately,
                           by run length: only a run of 10+ consecutive words of it (or a
                           shorter whole-output run with content words) that the source
                           lacks counts, because its 4-grams are ordinary prose.
  * short_source_blowup -- a source of at most 4 non-space characters ("1.", "[4]") whose
                           target is longer than 3 * weight(source) + 12, where a wide CJK
                           character weighs 3 and an all-caps Latin acronym (RSVP, FAQ)
                           weighs 8 per letter (each letter stands for a whole word). The
                           echo is usually TRANSLATED ("1. usando la siguiente guia de
                           estilo ..."), so the word-run check cannot see it.
  * number_mismatch     -- a dropped or added number ("[4]" lost, an invented "9"), judged by
                           the locale-aware `qa.number_findings` (kind "count" only), so
                           digit grouping, non-Latin digits and tag tokens never trip it.
                           SOFT by default (NUMBER_MISMATCH_WITHHOLDS): the translation is
                           kept and the segment is flagged for review.

Scope: only the SEGMENTED llm_agent path is guarded. A FULL_TEXT document step is not.

Why the older guards missed the two live cases (#343): `quality_common.length_runaway`
needs an absolute surplus of 120 chars AND a 5x ratio, so a 2-char source whose target is
43 chars (+41) never clears the surplus lock; `qa.number_findings` treats a merely DROPPED
number as a minor `count` finding (only a changed value is `value`/major) and ignores a
target that only appends prose; `refusal.is_refusal` needs a hedge phrase; and the
language guards abstain on a target that is genuinely in the target language.

Flags are stored like every other qa_flags entry ({type, severity, message}) with their own
types and are merged/cleared by TYPE so they compose with the refusal/nontranslation/qe
families. The orchestrator quarantines an echo / blow-up exactly like a refusal (target not
written, NEEDS_REVIEW, TM-ineligible), soft-flags a number mismatch (text kept, NEEDS_REVIEW,
TM-ineligible) unless NUMBER_MISMATCH_WITHHOLDS, and never blocks the run.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re
import unicodedata

INSTRUCTION_ECHO_FLAG_TYPE = "instruction_echo"
SHORT_SOURCE_BLOWUP_FLAG_TYPE = "short_source_blowup"
NUMBER_MISMATCH_FLAG_TYPE = "number_mismatch"

OUTPUT_GUARD_FLAG_TYPES = (
    INSTRUCTION_ECHO_FLAG_TYPE,
    SHORT_SOURCE_BLOWUP_FLAG_TYPE,
    NUMBER_MISMATCH_FLAG_TYPE,
)

# OWNER DECISION (owner, pending; this is the recommended default): a dropped / added number
# (and a number word: "3 options" -> "tres opciones") FLAGS the segment for review but KEEPS
# the translation as the target, like the existing soft changed-number disposition, so the
# reviewer sees the text. Set True to withhold it (target not written) instead. Echo and
# blow-up ALWAYS withhold: their target is the prompt, not a translation.
NUMBER_MISMATCH_WITHHOLDS = False

ECHO_RUN_WORDS = 4
ECHO_MIN_CONTENT_WORDS = 2
SHORT_SOURCE_MAX_CHARS = 4
SHORT_SOURCE_FACTOR = 3
SHORT_SOURCE_SLACK = 12
WIDE_CHAR_WEIGHT = 3
# An all-caps Latin acronym expands to one word per letter (~8 chars with the space).
ACRONYM_LETTER_WEIGHT = 8

_STOPWORDS = frozenset(
    (
        # articles, prepositions, conjunctions
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "by",
        "with",
        "without",
        "into",
        "onto",
        "about",
        "as",
        "and",
        "or",
        "but",
        "nor",
        "so",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        # pronouns, auxiliaries, quantifiers
        "it",
        "its",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "not",
        "no",
        "any",
        "all",
        "each",
        "every",
        "some",
        "only",
        "also",
        "such",
        "other",
        "more",
        "most",
        "you",
        "your",
        "yours",
        "we",
        "our",
        "they",
        "their",
        "them",
        "he",
        "she",
        "his",
        "her",
        "i",
        "me",
        "my",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "have",
        "has",
        "had",
        "there",
        "here",
        "which",
        "who",
        "whom",
        "what",
        "when",
        "where",
        "while",
        "how",
        "why",
        "one",
    )
)

_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _fold(text: str | None) -> str:
    """Case- and accent-insensitive form: NFKD, combining marks dropped, casefolded."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _words(text: str | None) -> list[str]:
    return _WORD_RE.findall(_fold(text))


def _ngrams(words: list[str], n: int = ECHO_RUN_WORDS) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _content_words(gram: tuple[str, ...]) -> int:
    return sum(1 for w in gram if w not in _STOPWORDS)


def literal_prompt_ngrams(*prompt_texts: str | None) -> set[tuple[str, ...]]:
    """Every 4-word run in the LITERAL text of the given prompts that carries at least
    ECHO_MIN_CONTENT_WORDS non-stopword words. `{placeholder}` spans are cut out first and act
    as a break, so a run never bridges substituted content. A run made almost only of
    stopwords ("the meaning of the") is ordinary prose, not evidence of an echo."""
    grams: set[tuple[str, ...]] = set()
    for text in prompt_texts:
        for piece in _PLACEHOLDER_RE.split(text or ""):
            grams |= {
                g for g in _ngrams(_words(piece)) if _content_words(g) >= ECHO_MIN_CONTENT_WORDS
            }
    return grams


def instruction_echo(
    output: str | None, source: str | None, prompt_grams: set[tuple[str, ...]]
) -> str:
    """A one-line reason when `output` reproduces a 4+ word run of the step prompt that the
    source does not also contain, else "". `prompt_grams` comes from `literal_prompt_ngrams`
    (computed once per step)."""
    if not prompt_grams:
        return ""
    out_words = _words(output)
    if len(out_words) < ECHO_RUN_WORDS:
        return ""
    src_grams = _ngrams(_words(source))
    for i in range(len(out_words) - ECHO_RUN_WORDS + 1):
        gram = tuple(out_words[i : i + ECHO_RUN_WORDS])
        if gram in prompt_grams and gram not in src_grams:
            return "output repeats the step instructions: '%s'" % " ".join(gram)  # noqa: UP031
    return ""


# #353: the fence sentence is short and made of ordinary words ("the text to work on", "output
# only the result"), so a lone 4-gram of it is common in legitimate same-language rewrites.
# It counts as an echo only when the output reproduces a run of this many CONSECUTIVE words of
# the sentence (about half of it; the shortest real echo, "Output only the result for that text,
# without the markers.", is 10 words), or when the whole output is a shorter run of its words
# that still carries ECHO_MIN_CONTENT_WORDS non-stopwords in some 4-word window.
FENCE_ECHO_MIN_RUN = 10


def _fence_run_lengths(out_words: list[str], fence_words: list[str]) -> list[int]:
    """For each position i in `out_words`, the length of the longest run of consecutive words
    ending at i that appears, in order, in `fence_words`. O(len(out_words) * len(fence_words));
    the fence sentence is a fixed ~20 words, so linear in the output."""
    lengths: list[int] = []
    prev = [0] * len(fence_words)
    for w in out_words:
        cur = [0] * len(fence_words)
        best = 0
        for j, fw in enumerate(fence_words):
            if fw == w:
                cur[j] = (prev[j - 1] if j else 0) + 1
                if cur[j] > best:
                    best = cur[j]
        lengths.append(best)
        prev = cur
    return lengths


def fence_echo(output: str | None, source: str | None, fence_text: str | None) -> str:
    """A one-line reason when `output` echoes the content-fence sentence: ANY run of
    FENCE_ECHO_MIN_RUN+ consecutive words of it (or a shorter run of 4+ words that is the whole
    output and carries content words) that the source does not also contain. Generic fragments
    ("the text to work on") are not."""
    fence_words = _words(fence_text)
    out_words = _words(output)
    if not fence_words or len(out_words) < ECHO_RUN_WORDS:
        return ""
    lengths = _fence_run_lengths(out_words, fence_words)
    src_grams: set[tuple[str, ...]] | None = None
    for end, length in enumerate(lengths):
        if length < ECHO_RUN_WORDS:
            continue
        whole = length == len(out_words)
        if length < FENCE_ECHO_MIN_RUN and not whole:
            continue
        run = out_words[end - length + 1 : end + 1]
        grams = _ngrams(run)
        if length < FENCE_ECHO_MIN_RUN and not any(
            _content_words(g) >= ECHO_MIN_CONTENT_WORDS for g in grams
        ):
            continue
        if src_grams is None:
            src_grams = _ngrams(_words(source))
        if not all(g in src_grams for g in grams):
            return "output repeats the step instructions: '%s'" % " ".join(run)  # noqa: UP031
    return ""


def _source_weight(text: str) -> int:
    """The expansion budget of a short source, character by character: an uppercase ASCII
    letter (an acronym letter stands for a whole word: Q&A, KPIs, B2C) weighs
    ACRONYM_LETTER_WEIGHT; an East Asian wide / full-width character weighs
    SHORT_SOURCE_FACTOR * WIDE_CHAR_WEIGHT (a CJK character carries roughly a word of
    meaning); everything else weighs SHORT_SOURCE_FACTOR. So "1." and "4." stay tight."""
    total = 0
    for c in text:
        if "A" <= c <= "Z":
            total += ACRONYM_LETTER_WEIGHT
        elif unicodedata.east_asian_width(c) in ("W", "F"):
            total += SHORT_SOURCE_FACTOR * WIDE_CHAR_WEIGHT
        else:
            total += SHORT_SOURCE_FACTOR
    return total


def short_source_blowup(source: str | None, target: str | None) -> str:
    """A one-line reason when a source of <= 4 non-space chars produced a target longer than
    3 * weight(source) + 12 chars, else "". Wide CJK characters weigh 3; an all-caps Latin
    acronym (RSVP, FAQ) weighs ACRONYM_LETTER_WEIGHT per letter because each letter stands
    for a whole word of the expansion. Empty source or target -> "" (other guards' job)."""
    src = (source or "").strip()
    tgt = (target or "").strip()
    if not src or not tgt:
        return ""
    if len(re.sub(r"\s+", "", src)) > SHORT_SOURCE_MAX_CHARS:
        return ""
    limit = _source_weight(src) + SHORT_SOURCE_SLACK
    if len(tgt) <= limit:
        return ""
    return "a %d-character source produced a %d-character target (limit %d)" % (  # noqa: UP031
        len(src),
        len(tgt),
        limit,
    )


def strip_mock_prefix(target: str | None, source: str | None, locale: str | None) -> str:
    """`target` without the offline MockProvider's leading "[<locale>] " marker (exactly as
    `providers_hub._mock_translate` writes it; repeated once per chained mock step). The
    marker carries the locale's digits ("es-419"), which are not translated content. Left
    alone when the source itself starts with that marker."""
    text = target or ""
    if not locale:
        return text
    prefix = "[%s] " % locale  # noqa: UP031
    if (source or "").startswith(prefix):
        return text
    while text.startswith(prefix):
        text = text[len(prefix) :]
    return text


def number_mismatch(source: str | None, target: str | None, locale: str | None = None) -> str:
    """A one-line reason when numbers were dropped or added, else "". Judged by the existing
    locale-aware `qa.number_findings`, acting on kind "count" only: digit regrouping
    (10000 -> 10.000 / 10 000), non-Latin digits and inline-tag tokens are not mismatches,
    and a changed value (kind "value") is `quality.number_violation`'s job."""
    from app.services import qa  # lazy: keep this module import-light

    for f in qa.number_findings(source, target, locale):
        if f.get("kind") == "count":
            return str(f.get("message") or "dropped or added number")
    return ""


def withholds(flag: dict | None) -> bool:
    """True when this output-guard entry must WITHHOLD the target (quarantine); False when
    it only flags the segment for review and keeps the translation."""
    if not flag:
        return False
    return flag.get("type") != NUMBER_MISMATCH_FLAG_TYPE or NUMBER_MISMATCH_WITHHOLDS


def check_output(
    source: str | None,
    output: str | None,
    prompt_grams: set[tuple[str, ...]],
    *,
    fence_text: str | None = None,
    number_already_gated: bool = False,
    locale: str | None = None,
) -> dict | None:
    """The first firing guard as a qa_flags entry, or None when the output is clean.
    Order: instruction echo, short-source blow-up, number mismatch.

    `number_already_gated` lets the caller skip the number check when the write seam's own
    value-aware number gate (`quality.number_violation`, a soft commit + `number_integrity`
    flag) already fires on this pair, so a changed figure keeps that established disposition
    and is not double-flagged here. The count check still covers what that gate does not:
    a merely dropped or added number. `locale` is the target locale (number formatting and
    the mock-prefix strip). `fence_text` is the content-fence sentence (#353), matched by run
    length rather than by lone n-grams."""
    number_output = strip_mock_prefix(output, source, locale)
    for flag_type, reason in (
        (
            INSTRUCTION_ECHO_FLAG_TYPE,
            instruction_echo(output, source, prompt_grams)
            or fence_echo(output, source, fence_text),
        ),
        (SHORT_SOURCE_BLOWUP_FLAG_TYPE, short_source_blowup(source, output)),
        (
            NUMBER_MISMATCH_FLAG_TYPE,
            "" if number_already_gated else number_mismatch(source, number_output, locale),
        ),
    ):
        if reason:
            return output_guard_flag(flag_type, reason)
    return None


_MESSAGES = {
    INSTRUCTION_ECHO_FLAG_TYPE: "Routed to review: instruction echo",
    SHORT_SOURCE_BLOWUP_FLAG_TYPE: "Routed to review: short source blow-up",
    NUMBER_MISMATCH_FLAG_TYPE: "Routed to review: number mismatch",
}


def output_guard_flag(flag_type: str, reason: str, source: str | None = None) -> dict:
    """The qa_flags entry for one guard: major, one human-readable line."""
    entry = {
        "type": flag_type,
        "severity": "major",
        "message": f"{_MESSAGES[flag_type]} ({reason})",
        "reason": reason,
    }
    if source:
        entry["source"] = source
    return entry


def merge_output_guard_flag(existing: list | None, entry: dict) -> list:
    """qa_flags with any prior output-guard entry replaced by `entry` (all else kept).
    Fresh list so SQLAlchemy sees the JSON column as changed."""
    return [*(clear_output_guard_flags(existing) or []), entry]


def clear_output_guard_flags(existing: list | None) -> list | None:
    """qa_flags without any output-guard entry (stale-badge clearing on a clean commit)."""
    if not any(
        isinstance(f, dict) and f.get("type") in OUTPUT_GUARD_FLAG_TYPES for f in existing or []
    ):
        return existing  # nothing to clear: leave None / the same list untouched
    return [
        f
        for f in existing or []
        if not (isinstance(f, dict) and f.get("type") in OUTPUT_GUARD_FLAG_TYPES)
    ]
