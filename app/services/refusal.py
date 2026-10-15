"""Downstream refusal / meta-output GUARD for produced translations.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _FENCE_NAME_LIST, _FENCE_NAMES, _FENCE_BLOCK_RE,
_FENCE_TRUNC_BLOCK_RE, _FENCE_TAG_RE, _FENCE_TRUNC_RE, _TRUNC_TAIL_CAP, _SRC_FENCE_RE,
_BLANK_RUN_RE, _strip_trunc_tail, _strip_injected_fences, _LANG_MIN_CHARS, _LANG_MIN_TOKENS,
_LANG_MIN_DISTINCT_EN, _LANG_MIN_EN_RATIO, _EN_FUNCTION_WORDS, _LATIN_TARGET_STOPWORDS,
_NON_LATIN_SCRIPTS, _WORD_RE, _base_lang, looks_untranslated, NONTRANSLATION_FLAG_TYPE,
NONTRANSLATION_REASON, nontranslation_flag, merge_nontranslation_flag, _OVER_TRANSLATION_SHAPES,
OVER_TRANSLATION_REASON_PREFIX, over_translation_reason, over_translation_flag, _ARABIC,
_DEVANAGARI, _BENGALI, _GURMUKHI, _GUJARATI, _ORIYA, _TAMIL, _TELUGU, _KANNADA, _MALAYALAM,
_SINHALA, _THAI, _LAO, _KHMER, _MYANMAR, _TIBETAN, _GEORGIAN, _ARMENIAN, _GREEK, _HEBREW, _ETHIOPIC,
_THAANA, _CYRILLIC, _HAN, _KANA, _HANGUL, _EXPECTED_SCRIPTS, _WRONG_SCRIPT_MIN_LETTERS,
_DENSE_SCRIPTS, _is_dense_script, _NONCONTENT_RE, WRONG_SCRIPT_FLAG_TYPE, WRONG_SCRIPT_REASON,
wrong_script, wrong_script_flag from app.services.refusal_common, app.services.refusal_part2.

THE BUG THIS CLOSES (found in LIVE testing against real Jira): a cheap model
(haiku default), handed a short/ambiguous label-like source field (e.g.
`[Example] Explain an internal process`), responded CONVERSATIONALLY instead of
translating — "I'm ready to translate ... However, I don't see any source text
... Please provide the actual source text ..." — and the pipeline wrote that hedge
verbatim into the target field and posted it to real Jira. Prompt hardening
(prompts.translation_prompt) is the primary fix; THIS module is the across-the-
board net that catches ANY path/model that still emits a non-translation.

CONTRACT (must never silently corrupt good output):
  * `is_refusal(text)` matches a produced OUTPUT (never the source) against a small
    CURATED set of high-confidence refusal / meta-assistant markers, case-
    insensitively. It is CONSERVATIVE by design: every marker is a phrase that a
    genuine translation of real KB/issue content essentially never contains, so a
    legitimate translation is not downgraded. When in doubt it returns False (let
    the output through) — the guard only ever DOWNGRADES a suspected non-translation
    to review, it NEVER rewrites or drops good text.
  * The caller (translate._apply_result / _apply_result_ft, orchestrator write-back)
    on a True verdict: does NOT write target_text, sets the segment to
    SegStatus.NEEDS_REVIEW with a qa_flag rationale, and counts it as an error. The
    connector write-back already excludes NEEDS_REVIEW, so a flagged unit is never
    posted back to the external system.
  * Gated at every call site by `config.REFUSAL_GUARD_ENABLED` (default True) —
    a runtime kill switch (env var, no redeploy) restores byte-identical pre-guard
    behavior if this net ever needs to be turned off in a hurry.

ANCHORED DESIGN (post-review hardening): a genuine refusal is the model talking
INSTEAD of translating, so it LEADS the output — e.g. the real live-Jira failure
started "I'm ready to translate from English (US) to Spanish (Mexico). However, I
don't see any source text ...". Real KB/support content this TMS routinely
translates, on the other hand, legitimately CONTAINS refusal-adjacent vocabulary
mid-paragraph ("as an AI feature", "the text you provided", "please provide the
source" as an instructional sentence, "I cannot translate" as a quoted UI string,
"appears to be a placeholder" as a description of a UI element). Free substring
matching on ~20 markers ANYWHERE in the output flagged 10 of 21 realistic strings
in adversarial review, and "as an ai" even matched mid-word ("aircraft").

So matching is now split into two tiers:
  1. STEM markers — matched ONLY against the START of the output (first ~80 chars
     of the stripped+lowercased text, i.e. roughly the first sentence/line). This is
     where a real refusal's hedge lives; KB copy that merely mentions this
     vocabulary somewhere in its body no longer trips it.
  2. FULL-PHRASE markers — a tiny, very-high-confidence set that may match ANYWHERE
     in the output, reserved for phrases so specific to an assistant talking about
     the task (not doing it) that they essentially never occur in real translated
     content regardless of position.
Both tiers are substring (case-insensitive) on the STRIPPED output.

C2 HARDENING (2026-07-06 connector-surface audit): the anchored-stem tier missed
two realistic evasions the audit reproduced — (a) a hedge whose marker sits just
PAST the 80-char anchor because a courteous preamble ("Sure! Of course. ...")
pushed it there, and (b) the "no text present / nothing to translate" family the
stem set never listed. Two conservative additions close both WITHOUT widening the
false-positive surface (the guard still only ever DOWNGRADES to review, never
rewrites): a leading-politeness PREAMBLE strip that re-runs the SAME anchored stem
check from the first substantive sentence, and a small set of extra high-confidence
markers (stems + one full phrase). Every addition keeps the anchored/assistant-
voiced discipline so a genuine translation that merely mentions this vocabulary
mid-body is still not flagged (the reviewer-found FP floor in verify_refusal_guard
is re-asserted, and new FP guards are added for the new markers).

C2 TARGET-LANGUAGE HARDENING (audit C2 residual, this branch): the stems above are
ENGLISH ONLY, so a model that apologizes/declines IN THE TARGET LANGUAGE (it says
"No puedo traducir ...", "Je ne peux pas traduire ...", "翻訳するテキストがありません
...") sailed straight through. This adds curated, HIGH-PRECISION refusal families for
the target languages this TMS actually serves (es/fr/de/it/pt/nl/ru/ja/zh/ko — the
locales exercised across the connector + flow fixtures) under the SAME anchored
discipline, split exactly like the English tiers:

RUSSIAN PARITY (live QA campaign K4, 2026-08-03). These tiers shipped WITHOUT a
Russian row, and the gap was not theoretical: the live runaway "Привет! Я готов
перевести UI-строку ..." is the EXACT phrase this module already lists in five other
languages ("estoy listo para traducir", "je suis prêt à traduire", "ich bin bereit zu
übersetzen", "sono pronto a tradurre", "estou pronto para traduzir"), and controls
confirmed the identical output in Spanish or English IS caught. Inconsistent
per-language coverage is itself the defect, so ru is brought to the same coverage as
its five peers across all four tiers. NOTE THE CONSEQUENCE, accepted deliberately
(The maintainer's call): `is_refusal` WITHHOLDS the write, so a Russian value of this shape now
lands EMPTY at needs_review rather than flagged-with-text — exactly what already
happens for Spanish and German today. The structural, language-independent
`quality.length_runaway` gate is the primary K4 fix and is what covers the next
language before its phrases are ever listed.
  * `_TARGET_STEMS` — first-person hedges pinned to the TRANSLATION VERB
    ("no puedo TRADUCIR", "je ne peux pas TRADUIRE", "ich kann ... nicht ÜBERSETZEN").
    Anchored to the first _ANCHOR_CHARS (and re-checked after a preamble strip, like
    `_STEMS`). Pinning to the verb is what makes them high-precision: a genuine
    translation of real content essentially never OPENS with a first-person
    "I-can't-TRANSLATE", whereas bare target-language modals ("no puedo", "je ne peux
    pas") are everyday content and are deliberately NOT listed.
  * `_TARGET_LEADING_STEMS` — the impersonal "no text TO TRANSLATE / nothing to
    translate" family, POSITION-0 only (like `_LEADING_STEMS`, and NOT re-checked after
    a preamble strip — the F2 discipline). Also pinned to the translation verb so an
    ordinary empty-state string ("No hay texto en este campo todavia.", a real KB/UI
    sentence) is spared, while "No hay texto PARA TRADUCIR." (the refusal) is caught.
  * `_TARGET_PREAMBLES` — target-language courtesy openers + pivots ("Claro,",
    "Por supuesto.", "Bien sur,", "Sin embargo,", ...) folded into the SAME bounded
    preamble strip, so a first-person target hedge pushed past the anchor by courtesy
    padding is still caught. The strip alone never flags — only a following
    `_TARGET_STEMS` match does — so adding openers cannot manufacture a false positive.
Deliberately conservative vs the English set (a higher-FP surface, since this TMS
routinely translates docs ABOUT translation/AI): standalone "as an AI / language
model" identity metas are NOT ported (they collide with legit product copy that
describes the engine), and every target marker carries the explicit translation-verb
anchor. The impersonal no-text family additionally requires the FRAMED existential
form (es "no HAY texto ...", fr "il N'Y A ...", de "es GIBT ...", it "non C'È ...",
nl "er IS ..."), never a bare copula-less label ("Nessun testo da tradurre.",
"Geen tekst om te vertalen.") — the same precision line the English tier draws — so a
localized empty-state UI label passes clean in every language. For Russian, which has no
bare-NP empty-state shape (the label IS "Нет текста для перевода" / "Нечего переводить"),
parity means those two forms are LISTED NOWHERE and only the verb-carrying framed refusals
are — see the tier's own note; the first cut had this inverted and destroyed exactly the
strings it was meant to spare. Known residual RECALL
gaps (documented, precision-first): an apology-prefixed CJK no-text refusal whose
clause is not at position 0, and a subject-less CJK bare "can't translate"
(indistinguishable from a UI error string).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

from app.services.refusal_common import (
    _BLANK_RUN_RE,
    _FENCE_BLOCK_RE,
    _FENCE_NAME_LIST,
    _FENCE_NAMES,
    _FENCE_TAG_RE,
    _FENCE_TRUNC_BLOCK_RE,
    _FENCE_TRUNC_RE,
    _SRC_FENCE_RE,
    _TRUNC_TAIL_CAP,
    _strip_trunc_tail,
)
from app.services.refusal_part2 import (
    _ARABIC,
    _ARMENIAN,
    _BENGALI,
    _CYRILLIC,
    _DENSE_SCRIPTS,
    _DEVANAGARI,
    _EN_FUNCTION_WORDS,
    _ETHIOPIC,
    _EXPECTED_SCRIPTS,
    _GEORGIAN,
    _GREEK,
    _GUJARATI,
    _GURMUKHI,
    _HAN,
    _HANGUL,
    _HEBREW,
    _KANA,
    _KANNADA,
    _KHMER,
    _LANG_MIN_CHARS,
    _LANG_MIN_DISTINCT_EN,
    _LANG_MIN_EN_RATIO,
    _LANG_MIN_TOKENS,
    _LAO,
    _LATIN_TARGET_STOPWORDS,
    _MALAYALAM,
    _MYANMAR,
    _NON_LATIN_SCRIPTS,
    _NONCONTENT_RE,
    _ORIYA,
    _OVER_TRANSLATION_SHAPES,
    _SINHALA,
    _TAMIL,
    _TELUGU,
    _THAANA,
    _THAI,
    _TIBETAN,
    _WORD_RE,
    _WRONG_SCRIPT_MIN_LETTERS,
    ENGINE_LIMIT_REASON_PREFIX,
    ENGINE_LIMIT_SHAPE,
    NONTRANSLATION_FLAG_TYPE,
    NONTRANSLATION_REASON,
    OVER_TRANSLATION_REASON_PREFIX,
    WRONG_SCRIPT_FLAG_TYPE,
    WRONG_SCRIPT_REASON,
    _base_lang,
    _is_dense_script,
    _strip_injected_fences,
    looks_untranslated,
    merge_nontranslation_flag,
    nontranslation_flag,
    over_translation_flag,
    over_translation_reason,
    wrong_script,
    wrong_script_flag,
)

__all__ = [
    "ENGINE_LIMIT_REASON_PREFIX",
    "ENGINE_LIMIT_SHAPE",
    "NONTRANSLATION_FLAG_TYPE",
    "NONTRANSLATION_REASON",
    "OVER_TRANSLATION_REASON_PREFIX",
    "WRONG_SCRIPT_FLAG_TYPE",
    "WRONG_SCRIPT_REASON",
    "_ARABIC",
    "_ARMENIAN",
    "_BENGALI",
    "_BLANK_RUN_RE",
    "_CYRILLIC",
    "_DENSE_SCRIPTS",
    "_DEVANAGARI",
    "_EN_FUNCTION_WORDS",
    "_ETHIOPIC",
    "_EXPECTED_SCRIPTS",
    "_FENCE_BLOCK_RE",
    "_FENCE_NAMES",
    "_FENCE_NAME_LIST",
    "_FENCE_TAG_RE",
    "_FENCE_TRUNC_BLOCK_RE",
    "_FENCE_TRUNC_RE",
    "_GEORGIAN",
    "_GREEK",
    "_GUJARATI",
    "_GURMUKHI",
    "_HAN",
    "_HANGUL",
    "_HEBREW",
    "_KANA",
    "_KANNADA",
    "_KHMER",
    "_LANG_MIN_CHARS",
    "_LANG_MIN_DISTINCT_EN",
    "_LANG_MIN_EN_RATIO",
    "_LANG_MIN_TOKENS",
    "_LAO",
    "_LATIN_TARGET_STOPWORDS",
    "_MALAYALAM",
    "_MYANMAR",
    "_NONCONTENT_RE",
    "_NON_LATIN_SCRIPTS",
    "_ORIYA",
    "_OVER_TRANSLATION_SHAPES",
    "_SINHALA",
    "_SRC_FENCE_RE",
    "_TAMIL",
    "_TELUGU",
    "_THAANA",
    "_THAI",
    "_TIBETAN",
    "_TRUNC_TAIL_CAP",
    "_WORD_RE",
    "_WRONG_SCRIPT_MIN_LETTERS",
    "_base_lang",
    "_is_dense_script",
    "_strip_injected_fences",
    "_strip_trunc_tail",
    "looks_untranslated",
    "merge_nontranslation_flag",
    "nontranslation_flag",
    "over_translation_flag",
    "over_translation_reason",
    "re",
    "wrong_script",
    "wrong_script_flag",
]

# How much of the (stripped, lowercased) output counts as "the start" for anchored
# matching — roughly the first sentence/line of a genuine refusal's leading hedge.
_ANCHOR_CHARS = 80

# Leading/separating "edge" punctuation the preamble strip peels around a courtesy
# opener. Includes the Spanish inverted marks (¡ ¿) and guillemets so a preamble that
# a model wraps in them ("¡Claro!", "«Por supuesto»") is still recognized and peeled.
_EDGE_CHARS = " \t\r\n.,!:;-–—\"'¡¿«»"

# Anchored refusal STEMS: matched ONLY if they occur within the first _ANCHOR_CHARS
# of the output. These are real refusal OPENERS — sentence-initial hedges like
# "please provide the" or "as an ai" are unambiguously the model talking about the
# task when they lead the output, but the exact same words mid-paragraph are
# legitimate KB/support vocabulary (an instructional sentence telling a USER to
# provide something, a UI string quoting "as an AI feature", etc.) — anchoring is
# what tells the two apart. Keep multi-word / assistant-voiced so a translation
# note or quoted UI copy that happens to START with one of these is vanishingly
# unlikely (translations do not open with first-person assistant hedges).
_STEMS: tuple[str, ...] = (
    "i'm ready to translate",
    "i am ready to translate",
    "i'm ready to help",
    "i am ready to help",
    "i cannot translate",
    "i can't translate",
    "i'm unable to translate",
    "i am unable to translate",
    "i don't see any",
    "i do not see any",
    "as an ai",
    "as a language model",
    "i can help you translate",
    "i'd be happy to translate",
    "i would be happy to translate",
    "i'm sorry",
    "i apologize",
    "unfortunately, i",
    "could you please provide",
    "please provide the",
)

# QA-3: assistant-voiced META self-description family — a model that describes ITSELF
# (the engine) instead of translating. The headline live-QA miss was a first-person
# meta-description that LED the output ("I am one step in an automated localization
# pipeline, specifically a machine translation model, and I don't have ...") which the
# stems above did not cover, so it committed as a clean `mt` translation and — once
# approved — seeded shared TM as a 100%-match that then bypasses this very guard on
# every later document (the C3 poison path). Same anchored + first-person discipline as
# `_STEMS`: matched only within the first `_ANCHOR_CHARS` (and re-checked after a
# preamble strip), so a genuine translation that merely MENTIONS "machine translation
# model" mid-body is not flagged — a real translation essentially never OPENS by
# identifying itself as the AI/MT engine. Pinned to the AI/MT/model/pipeline/assistant/
# system vocabulary the report names, keeping the false-positive bar identical to the
# existing "as an ai" / "as a language model" stems.
#
# RIGHT-BOUNDARY DISCIPLINE (adversarial-review hardening): unlike `_STEMS`, this tier is
# matched by `_meta_stem_hit`, which requires a right WORD BOUNDARY after the stem (end-
# of-text or a non-alphanumeric char) — the same token-boundary check `_strip_preamble`
# already applies to a `_PREAMBLES` opener. Without it these SHORT identity stems
# overmatch ordinary first-person English by bare substring: "i am an ai" would fire on
# "I am an AIR traffic controller" / "I am an AID worker" / "I'm an AIRLINE pilot". The
# boundary alone is NOT enough for two idiom-prone families, so they carry an extra pin:
#   * the "one step" family is pinned to the pipeline-component preposition "one step
#     IN/OF" (a bare "one step" + boundary still admits "I am one step CLOSER/AWAY", the
#     everyday idioms — a space follows "step" there too);
#   * the "automated" family is pinned to the "automated TRANSLATION/LOCALIZATION"
#     vocabulary (a bare "an automated" + boundary still admits "I am an automated-TESTING
#     engineer", a legit first-person testimonial, because the boundary check treats the
#     hyphen as an edge).
# Every retained short stem's own genuine continuation is a space/punctuation ("... AI
# language model", "... translation model."), so the boundary never costs recall.
_META_STEMS: tuple[str, ...] = (
    # "I am (just/only) one step IN/OF ... [a pipeline/process]" — pinned to the
    # preposition so the everyday idioms "one step closer / away / ahead" are spared.
    "i am one step in",
    "i'm one step in",
    "i am one step of",
    "i'm one step of",
    "i am just one step in",
    "i'm just one step in",
    "i am just one step of",
    "i'm just one step of",
    "i am only one step in",
    "i'm only one step in",
    "i am only one step of",
    "i'm only one step of",
    # "I am a machine translation ..." / "As a machine translation ..."
    "i am a machine translation",
    "i'm a machine translation",
    "as a machine translation",
    # "I am an AI ..." — the right-boundary check spares "AIR/AID/AIMS/AIRLINE...".
    "i am an ai",
    "i'm an ai",
    # "I am a (large) language model"
    "i am a language model",
    "i'm a language model",
    "i am a large language model",
    "i'm a large language model",
    # "I am a translation model / assistant / system / tool"
    "i am a translation model",
    "i'm a translation model",
    "i am a translation assistant",
    "i'm a translation assistant",
    "i am a translation system",
    "i'm a translation system",
    "i am a translation tool",
    "i'm a translation tool",
    # "I am (part of) an automated TRANSLATION/LOCALIZATION ..." — pinned to the
    # translation/localization vocabulary so "I am an automated-testing engineer" (a
    # legit first-person testimonial) is not flagged.
    "i am an automated translation",
    "i'm an automated translation",
    "i am an automated localization",
    "i'm an automated localization",
    "i am part of an automated translation",
    "i'm part of an automated translation",
    "i am part of an automated localization",
    "i'm part of an automated localization",
)

# C2: the "no text present / nothing to translate" family. Unlike `_STEMS` (matched
# anywhere in the first _ANCHOR_CHARS), these must lead the output at POSITION 0
# (after a preamble strip). The distinction matters: a real reviewer-found FP string
# embeds "there is no text to translate" mid-sentence but still WITHIN the 80-char
# window ("Esta plantilla se dejo vacia: there is no text to translate aqui ...") —
# treating this family as a first-80-chars stem would re-flag it. A genuine refusal
# in THIS family, by contrast, opens the output with the observation itself, so
# requiring position-0 catches the real hedge and spares the mid-sentence FP.
_LEADING_STEMS: tuple[str, ...] = (
    "there is no text",
    "there's no text",
    "there is no source",
    "there's no source",
    "there is nothing to translate",
    "there's nothing to translate",
    "no text was provided",
    "no source text was provided",
    "it appears there is no",
    "it looks like there is no",
    "it seems there is no",
)

# Leading-politeness PREAMBLE openers (C2): a courteous lead-in a model sometimes
# emits BEFORE its refusal ("Sure! Of course! Certainly, ..."), which pushes the
# real hedge past the _ANCHOR_CHARS window and evades the anchored check. When the
# output STARTS with one of these we strip it (and any following punctuation/space)
# and re-run the SAME anchored stem check from the first substantive character — so
# the anchored discipline is preserved, only the courtesy padding is discounted.
# Kept to unambiguous assistant-courtesy openers so a real translation that happens
# to begin with, say, "Sure" as content is not stripped into a false positive.
_PREAMBLES: tuple[str, ...] = (
    "sure",
    "sure!",
    "sure,",
    "of course",
    "of course!",
    "of course,",
    "certainly",
    "certainly!",
    "certainly,",
    "absolutely",
    "absolutely!",
    "no problem",
    "okay",
    "ok",
    "got it",
    "happy to help",
    "glad to help",
    "i'd be glad to help",
    "i'd be happy to help",
    "hi",
    "hello",
    "thanks",
    "thank you",
    # the refusal PIVOTS a courtesy lead-in commonly hands off to ("Sure! However,
    # ...", "Of course. That said, ..."). Stripping them lets the SAME anchored stem
    # check see the hedge that follows. Kept to the two-word/comma-terminated pivots
    # that essentially never LEAD legitimate translated target text — deliberately
    # NOT bare "but" (too common a legitimate sentence opener), so the strip can't
    # manufacture a false positive off ordinary content.
    "however",
    "however,",
    "that said",
    "that said,",
)

# High-confidence FULL PHRASES that may match ANYWHERE in the output. Reserved for
# wording so specific to an assistant describing/declining the task — rather than
# performing it — that it essentially never appears in real translated KB/issue
# content in any position. Keep this set TINY: every addition here is exempt from
# the anchor, so it carries a higher false-positive bar than a stem.
_FULL_PHRASES: tuple[str, ...] = (
    "i'll deliver only the translation",
    "i will deliver only the translation",
    "there is no source text to translate in your message",
    "provide the actual source text",
    # C2: two more wordings so specific to an assistant declining/describing the task
    # that they have no realistic occurrence in translated KB/issue content anywhere.
    "there is no text present to translate",
    "there is nothing for me to translate",
    # #633: the sentinel a provider returns when the API itself reported a refusal
    # (Claude stop_reason "refusal", OpenAI-compatible finish_reason "content_filter").
    "[provider refusal]",
)

#: #633: what a provider returns INSTEAD of the model's text when the API ended the message
#: with a refusal signal. It carries no model output (a refused message may hold a partial
#: or policy text), and is_refusal() matches it anywhere, so every path that already guards
#: on is_refusal (plain translate, workflows, Advanced, /api/translate/text, TM writes)
#: quarantines it and counts it like any other refusal; the LQA judge cannot parse it as a
#: verdict and fails closed to JUDGE_UNAVAILABLE.
PROVIDER_REFUSAL_TEXT = (
    "[provider refusal] The engine declined this request (the API reported a refusal), "
    "so no translation was produced."
)

# =============================================================================
# C2 (target-language) — curated HIGH-CONFIDENCE refusal families, same anchored
# discipline as the English tiers above. Every entry is pinned to the TRANSLATION
# VERB (or, for CJK, carries an explicit first-person subject), so a genuine
# translation of real content essentially never opens with one; bare target-language
# modals / apologies ("no puedo", "je ne peux pas", "lo siento") are everyday content
# and are deliberately NOT listed (an apology that leads a real refusal is caught by
# the verb-anchored stem that FOLLOWS it within the anchor window). Lowercase — matched
# case-insensitively against the stripped+lowercased output.
# =============================================================================

# First-person hedges, verb-anchored. Matched within the first _ANCHOR_CHARS AND after
# a preamble strip (mirrors _STEMS). "I-can't/ready-to TRANSLATE" essentially never
# opens a real translation unless the SOURCE itself was first-person about not
# translating (the same accepted tradeoff as English "i cannot translate").
_TARGET_STEMS: tuple[str, ...] = (
    # Spanish (es)
    "no puedo traducir",
    "no soy capaz de traducir",
    "no puedo ayudar a traducir",
    "no puedo ayudarte a traducir",
    "no veo ningún texto para traducir",
    "no veo texto para traducir",
    "estoy listo para traducir",
    "estoy lista para traducir",
    # French (fr)
    "je ne peux pas traduire",
    "je ne suis pas en mesure de traduire",
    "je ne peux pas vous aider à traduire",
    "je ne vois aucun texte à traduire",
    "je suis prêt à traduire",
    "je suis prête à traduire",
    # German (de) — verb-final "kann ... nicht übersetzen"; kept as contiguous forms
    # because a single separated "nicht übersetzen" alone is legit 2nd/3rd-person UI copy.
    "ich kann diesen text nicht übersetzen",
    "ich kann das nicht übersetzen",
    "ich kann keinen text übersetzen",
    "ich sehe keinen text zum übersetzen",
    "ich bin bereit zu übersetzen",
    # Italian (it)
    "non posso tradurre",
    "non sono in grado di tradurre",
    "non vedo alcun testo da tradurre",
    "sono pronto a tradurre",
    "sono pronta a tradurre",
    # Portuguese (pt)
    "não posso traduzir",
    "não consigo traduzir",
    "não sou capaz de traduzir",
    "não vejo nenhum texto para traduzir",
    "estou pronto para traduzir",
    "estou pronta para traduzir",
    # Dutch (nl)
    "ik kan deze tekst niet vertalen",
    "ik kan dat niet vertalen",
    "ik kan geen tekst vertalen",
    "ik zie geen tekst om te vertalen",
    # Chinese (zh) — explicit first-person 我 ("I") keeps these assistant-voiced rather
    # than the subject-less UI-error form, so they carry the same low FP bar as English
    # "i cannot translate".
    "我无法翻译",
    "我不能翻译",
    # Russian (ru) — RUSSIAN PARITY (live QA K4). This tier already carried the
    # ready-to-translate hedge in FIVE languages ("estoy listo para traducir", "je suis
    # prêt à traduire", "ich bin bereit zu übersetzen", "sono pronto a tradurre", "estou
    # pronto para traduzir") and NOTHING in Russian, so the identical live output was
    # caught in es/de/en and shipped clean in ru. Same verb-anchored discipline: the
    # explicit first-person "я" plus the translation verb (перевести / перевод), so bare
    # modals ("не могу") stay ordinary content. Gender pairs (готов/готова) because
    # Russian marks it on the short-form adjective.
    "я не могу перевести",
    "я не могу помочь с переводом",
    "я не в состоянии перевести",
    "я не способен перевести",
    "я не вижу текста для перевода",
    "я не вижу текст для перевода",
    "я готов перевести",
    "я готова перевести",
    "я готов помочь с переводом",
    "я готова помочь с переводом",
)

# QA-3 (target-language META): the target-language sibling of `_META_STEMS`. First-person
# "I am a translation model / an AI / a translation assistant" self-descriptions, pinned
# to the AI/MT/model vocabulary. FIRST-PERSON ("soy un modelo", "je suis un modèle", "ich
# bin ein modell") is what keeps these high-precision even though the C2 note deliberately
# did NOT port the descriptive "as an AI" identity metas: legit product copy that describes
# the engine says it in the THIRD person ("nuestro producto es un asistente de IA", "como
# asistente de inteligencia artificial ..."), never "I AM a translation model". es/fr/de
# per the report ("where cheap"); accented + common unaccented variants (mirrors the
# `_TARGET_PREAMBLES` accent handling) so a model that drops diacritics is still caught.
# Anchored to the first `_ANCHOR_CHARS` and re-checked after a preamble strip, exactly like
# `_META_STEMS` / `_TARGET_STEMS`.
_TARGET_META_STEMS: tuple[str, ...] = (
    # Spanish (es)
    "soy un modelo de traducción",
    "soy un modelo de traduccion",
    "soy un modelo de lenguaje",
    "soy una inteligencia artificial",
    "soy un asistente de traducción",
    "soy un asistente de traduccion",
    "soy un sistema de traducción",
    "soy un sistema de traduccion",
    "soy un modelo de ia",
    # French (fr)
    "je suis un modèle de traduction",
    "je suis un modele de traduction",
    "je suis un modèle de langage",
    "je suis un modele de langage",
    "je suis un assistant de traduction",
    "je suis une intelligence artificielle",
    "je suis un système de traduction",
    "je suis un systeme de traduction",
    # German (de) — umlaut ü + both the bare-vowel and the "ue/ae"-digraph renderings a
    # model may emit ("übersetzung" / "ubersetzung" / "uebersetzung").
    "ich bin ein übersetzungsmodell",
    "ich bin ein ubersetzungsmodell",
    "ich bin ein uebersetzungsmodell",
    "ich bin ein sprachmodell",
    "ich bin ein übersetzungsassistent",
    "ich bin ein ubersetzungsassistent",
    "ich bin ein uebersetzungsassistent",
    "ich bin eine künstliche intelligenz",
    "ich bin eine kunstliche intelligenz",
    "ich bin eine kuenstliche intelligenz",
    "ich bin ein übersetzungssystem",
    "ich bin ein ubersetzungssystem",
    "ich bin ein uebersetzungssystem",
    "ich bin ein ki-modell",
    # Russian (ru) — RUSSIAN PARITY (live QA K4). Russian drops the present-tense copula,
    # so the SAME self-description is written three ways ("Я — языковая модель", "Я -
    # языковая модель", "Я языковая модель") and a model emits any of them; all are
    # listed. Same first-person + model/AI-vocabulary discipline as es/fr/de above:
    # "я переводчик" ("I am a translator" — a PERSON) is deliberately NOT listed, and
    # legit product copy describing the engine speaks in the third person.
    "я — языковая модель",
    "я - языковая модель",
    "я языковая модель",
    "я — модель перевода",
    "я - модель перевода",
    "я модель перевода",
    "я — переводческая модель",
    "я - переводческая модель",
    "я переводческая модель",
    "я — система перевода",
    "я - система перевода",
    "я система перевода",
    "я — искусственный интеллект",
    "я - искусственный интеллект",
    "я искусственный интеллект",
    "я — ии-модель",
    "я - ии-модель",
    "я ии-модель",
)

# Impersonal "no text / nothing TO TRANSLATE" observations. POSITION-0 only and NOT
# re-checked after a preamble strip (mirrors _LEADING_STEMS + the F2 discipline): a
# translation-anchored "no text to translate" is a refusal ONLY when it truly leads the
# raw output, so an ordinary mid-sentence occurrence ("El sistema indica que no hay
# texto para traducir en la celda.") and a "<pivot>, no hay texto para traducir ..." KB
# sentence are both spared. The translation-verb anchor is also what spares a bare
# empty-state label ("No hay texto en este campo todavia.").
# COPULA/FRAME DISCIPLINE (precision symmetry with the English tier + the es stems):
# every entry requires the FRAMED existential form (es "no HAY texto ...", fr "il N'Y A
# ... texte", de "es GIBT keinen text ...", it "non C'È ... testo", nl "er IS geen tekst
# ..."), NOT the bare copula-less label ("Aucun texte a traduire.", "Kein Text zum
# Uebersetzen.", "Nessun testo da tradurre.", "Geen tekst om te vertalen."). The English
# tier draws its line the same way — it flags "there is nothing to translate" but PASSES
# the bare "Nothing to translate." label — because a bare label is common legit UI copy
# (an empty-state string this TMS routinely localizes). Requiring the frame keeps all
# languages symmetric so a localized empty-state label passes clean everywhere.
_TARGET_LEADING_STEMS: tuple[str, ...] = (
    # Spanish
    "no hay texto para traducir",
    "no hay texto que traducir",
    "no hay ningún texto para traducir",
    "no hay nada que traducir",
    "no se proporcionó ningún texto",
    "no se proporcionó texto",
    "parece que no hay texto",
    # French (framed: "il n'y a ..." — the bare "aucun texte a traduire" label is spared)
    "il n'y a pas de texte à traduire",
    "il n'y a aucun texte à traduire",
    "il n'y a rien à traduire",
    # German (framed: "es gibt/ist/wurde ..." — the bare "kein text zum übersetzen"
    # label is spared)
    "es gibt keinen text zu übersetzen",
    "es ist kein text zu übersetzen",
    "es wurde kein text angegeben",
    # Italian (framed: "non c'è ..." — the bare "nessun testo da tradurre" label is spared)
    "non c'è testo da tradurre",
    "non c'è nessun testo da tradurre",
    "non c'è niente da tradurre",
    # Portuguese
    "não há texto para traduzir",
    "não há nenhum texto para traduzir",
    "não há nada para traduzir",
    "não foi fornecido nenhum texto",
    # Dutch (framed: "er is ..." — the bare "geen tekst om te vertalen" label is spared)
    "er is geen tekst om te vertalen",
    "er is niets om te vertalen",
    # Japanese — subject-less; "...テキストがありません / 見つかりません" = "there is no text to
    # translate". Position-0, so an apology-prefixed variant is a known recall gap.
    "翻訳するテキストがありません",
    "翻訳するテキストが見つかりません",
    "翻訳する内容がありません",
    "翻訳するテキストがない",
    # Chinese
    "没有要翻译的文本",
    "没有需要翻译的内容",
    "没有可翻译的文本",
    "没有要翻译的内容",
    # Korean
    "번역할 텍스트가 없습니다",
    "번역할 내용이 없습니다",
    "번역할 텍스트가 보이지 않습니다",
    # Russian — RUSSIAN PARITY (live QA K4), corrected in the fix round (finding F4).
    # THE FIRST CUT OF THIS TIER WAS STRICTER THAN EVERY PEER IT CLAIMED PARITY WITH: it
    # listed "нечего переводить" and "нет текста для перевода", and asserted in this very
    # comment that a "bare copula-less label ('Текст для перевода.')" was the spared form.
    # That inverted the classification. `Нет текста для перевода` / `Нечего переводить` are
    # the UNMARKED Russian EMPTY-STATE UI LABELS — the direct equivalents of the German
    # "Kein Text zum Übersetzen", French "Rien à traduire", Italian "Niente da tradurre",
    # Dutch "Niets om te vertalen" and English "Nothing to translate" that this tier
    # deliberately SPARES in every one of those languages. `Текст для перевода.` ("Text for
    # translation.") is a HEADING and was never at risk. Russian has no bare-NP empty-state
    # shape to spare instead — "нет" IS how the language writes the label — so parity with
    # the peers means those two forms are NOT listed. Consequence of getting this wrong was
    # not cosmetic: `is_refusal` WITHHOLDS the write, so a shipped ru empty-state string
    # would have had its translation destroyed on the HARD path.
    # RETAINED: the FRAMED forms — the analogue of the peers' "es gibt keinen Text zu
    # übersetzen" / "there is nothing to translate", which every peer language flags while
    # sparing its bare label. Stated as the criterion that ACTUALLY holds (final-round
    # F11): the earlier wording here — "forms carrying an explicit lexical VERB or a
    # relative clause, which no empty-state label uses" — is true of four of these five
    # and FALSE of the fifth, so it was a claim the list did not support.
    #   * Four carry an explicit predicate: the participle `не предоставлен`, the verb
    #     `отсутствует`, or the relative clause `который нужно перевести`.
    #   * `нет ничего для перевода` carries NEITHER a lexical verb nor a relative clause.
    #     It is retained on the other ground — it is the FRAMED EXISTENTIAL: `нет` plus
    #     the negative PRONOUN `ничего` in place of the content noun. That is precisely
    #     the shape the peers flag ("There is NOTHING to translate", "Il n'y a RIEN à
    #     traduire", "Non c'è NIENTE da tradurre", "Er is NIETS om te vertalen", "No hay
    #     NADA que traducir" — all five verified flagged), and it differs from the SPARED
    #     label `нет текста для перевода` only in that pronoun.
    # That last distinction is FINE, and it is a list-membership decision rather than a
    # rule the code derives — which is why it is written out here instead of left to be
    # re-inferred by the next reader.
    "нет текста, который нужно перевести",
    "нет ничего для перевода",
    "не предоставлен текст для перевода",
    "текст для перевода не предоставлен",
    "текст для перевода отсутствует",
)

# Target-language courtesy openers + refusal pivots, folded into the SAME bounded
# preamble strip as `_PREAMBLES`. The strip alone never flags — it only re-runs the
# verb-anchored `_TARGET_STEMS`/`_STEMS` check — so adding openers cannot manufacture a
# false positive (stripping a legit "Claro," off ordinary content exposes no stem).
_TARGET_PREAMBLES: tuple[str, ...] = (
    # Spanish (openers + pivots)
    "claro",
    "por supuesto",
    "desde luego",
    "con gusto",
    "encantado de ayudar",
    "encantada de ayudar",
    "hola",
    "gracias",
    "sin embargo",
    "no obstante",
    "dicho esto",
    # French
    "bien sûr",
    "bien sur",
    "certainement",
    "avec plaisir",
    "volontiers",
    "bonjour",
    "merci",
    "cependant",
    "toutefois",
    "cela dit",
    "ceci dit",
    # German
    "natürlich",
    "naturlich",
    "gerne",
    "selbstverständlich",
    "selbstverstandlich",
    "klar",
    "hallo",
    "danke",
    "jedoch",
    "allerdings",
    # Italian
    "certo",
    "certamente",
    "volentieri",
    "con piacere",
    "ciao",
    "grazie",
    "tuttavia",
    "detto questo",
    # Portuguese
    "com certeza",
    "com prazer",
    "olá",
    "ola",
    "obrigado",
    "obrigada",
    "no entanto",
    "porém",
    "porem",
    "dito isto",
    # Dutch
    "natuurlijk",
    "zeker",
    "met plezier",
    "graag",
    "bedankt",
    "echter",
    # Russian — RUSSIAN PARITY (live QA K4). The live runaway opened "Привет! Я готов
    # перевести ..." and this tier had hola/bonjour/hallo/ciao/olá but no "привет", so the
    # courtesy opener pushed the hedge past the anchor with nothing to peel it. The strip
    # alone never flags — it only re-runs the verb-anchored stem check — so these cannot
    # manufacture a false positive.
    "привет",
    "здравствуйте",
    "добрый день",
    "конечно",
    "разумеется",
    "с удовольствием",
    "спасибо",
    "однако",
    "тем не менее",
    "хорошо",
)

# The full opener set the preamble strip walks (English + target-language).
_ALL_PREAMBLES: tuple[str, ...] = _PREAMBLES + _TARGET_PREAMBLES

# Reason string stored on the segment's qa_flag + surfaced in the run summary, so a
# reviewer sees WHY the unit was routed (mirrors the QE gate's rationale badge).
REASON = "suspected non-translation / model refusal"


def is_refusal(text: str | None) -> bool:
    """True when `text` (a PRODUCED translation) looks like a conversational refusal
    or meta-assistant hedge rather than a translation.

    Anchored + conservative: a STEM only counts if it appears within the first
    `_ANCHOR_CHARS` of the stripped+lowercased output (where a genuine refusal's
    leading hedge lives); a FULL PHRASE may match anywhere but the set is
    deliberately tiny and high-confidence. C2: an anchored stem is ALSO checked
    against a second window taken AFTER stripping a leading-politeness preamble, so a
    hedge pushed past the anchor by courtesy padding is still caught — the same
    anchored/assistant-voiced discipline, only the courtesy lead-in is discounted.
    Empty/whitespace -> False (an empty target is handled by the existing empty-output
    guards, not this one)."""
    if not text:
        return False
    low = text.strip().lower()
    if not low:
        return False
    if _anchored_stem_hit(low, leading_stems=True):
        return True
    # C2: retry the anchored check after discounting a leading-politeness preamble
    # (e.g. "Sure! Of course, ..."), which otherwise pushes the hedge past the anchor.
    # CRITICAL (F2 fix): the post-strip retry runs ONLY the first-person assistant-
    # voiced `_STEMS` window — NOT the impersonal `_LEADING_STEMS` "no text" tier.
    # Reason: legit KB/UI sentences of shape "<pivot>, there is no text/source ..."
    # ("However, there is no text present until you click Generate.") are ordinary
    # content this product routinely translates; stripping the pivot and then matching
    # the impersonal "there is no text" family at position 0 would downgrade them (a
    # false positive). A genuine refusal after a courtesy pivot is FIRST-PERSON ("Sure,
    # but I can't translate ..."), which `_STEMS` still catches; the impersonal family
    # only ever triggers when it TRULY leads the raw output (checked above).
    stripped = _strip_preamble(low)
    if stripped != low and _anchored_stem_hit(stripped, leading_stems=False):
        return True
    return any(m in low for m in _FULL_PHRASES)


def _anchored_stem_hit(low: str, leading_stems: bool) -> bool:
    """True if any anchored `_STEMS` marker occurs within the first `_ANCHOR_CHARS` of
    `low`. When `leading_stems` is True, ALSO True if any `_LEADING_STEMS` marker
    occurs at POSITION 0. `low` is already stripped+lowercased.

    `leading_stems` is True only for the raw-output check and False for the preamble-
    strip retry (F2): the impersonal `_LEADING_STEMS` "no text" family must match ONLY
    when it truly leads the raw output — after a courtesy/pivot preamble is stripped,
    "there is no text ..." is ordinary KB content, not a refusal, so the retry runs
    the first-person `_STEMS` tier only. Position-0 (not first-80-chars) for
    `_LEADING_STEMS` so a mid-sentence occurrence of the same vocabulary is not
    flagged either (see its definition).

    C2 target-language: `_TARGET_STEMS` share the first-`_ANCHOR_CHARS` window with
    `_STEMS`, and `_TARGET_LEADING_STEMS` share the POSITION-0 + `leading_stems` gate
    with `_LEADING_STEMS` — so the preamble-strip retry (which passes
    `leading_stems=False`) never fires the impersonal target "no text" family, exactly
    as it never fires the English one (the F2 discipline, generalized)."""
    head = low[:_ANCHOR_CHARS]
    if (
        any(m in head for m in _STEMS)
        or any(m in head for m in _TARGET_STEMS)
        or _meta_stem_hit(low)
        or any(m in head for m in _TARGET_META_STEMS)
    ):
        return True
    if not leading_stems:
        return False
    return any(low.startswith(m) for m in _LEADING_STEMS) or any(
        low.startswith(m) for m in _TARGET_LEADING_STEMS
    )


def _meta_stem_hit(low: str) -> bool:
    """True if any `_META_STEMS` marker occurs within the first `_ANCHOR_CHARS` of `low`
    AND ends on a right WORD BOUNDARY (end-of-text or a non-alphanumeric character).

    The boundary is what keeps this NEW short-identity tier from the substring OVERMATCH
    a bare `m in head` would cause on ordinary first-person English: "i am an ai" must
    fire on "I am an AI language model" but NOT on "I am an AIR traffic controller" /
    "I am an AID worker" / "I'm an AIRLINE pilot", and "i am a language model" must not
    fire inside "language modeler". It mirrors the token-boundary check `_strip_preamble`
    already applies to a `_PREAMBLES` opener (`rest[0] in _EDGE_CHARS`). The LEFT side is
    anchored implicitly by the assistant-voiced "i am"/"i'm"/"as a" lead every meta stem
    carries plus the first-`_ANCHOR_CHARS` window; the two idiom-prone families
    ("one step", "automated") carry an extra in-list pin (see `_META_STEMS`) because a
    bare right boundary alone still admits "one step closer/away" and "automated-testing".
    `low` is already stripped+lowercased. The boundary char is read from the FULL `low`
    (not the truncated head) so a stem ending exactly at the anchor is judged on its real
    successor, never a false edge created by the slice."""
    limit = min(len(low), _ANCHOR_CHARS)
    for m in _META_STEMS:
        start = 0
        while True:
            i = low.find(m, start)
            if i < 0 or i + len(m) > limit:  # whole stem must fall within the anchor
                break
            end = i + len(m)
            if end >= len(low) or not low[end].isalnum():
                return True
            start = i + 1
    return False


def _strip_preamble(low: str) -> str:
    """Discount a leading-politeness preamble (English `_PREAMBLES` + target-language
    `_TARGET_PREAMBLES`, walked together as `_ALL_PREAMBLES`), returning the text from
    the first SUBSTANTIVE character. Strips at most a few courtesy openers plus the
    punctuation/whitespace that separates them from the real sentence; returns the
    input UNCHANGED when no known preamble leads it (so the second anchored check only
    ever runs on text that genuinely started with courtesy padding — never a free
    re-anchor on arbitrary output). Conservative: bounded to 3 openers so it cannot
    chew arbitrarily far into real content."""
    out = low
    for _ in range(3):  # at most a few stacked courtesy openers ("Sure! Of course, ...")
        stripped = out.lstrip(_EDGE_CHARS)
        matched = None
        for p in _ALL_PREAMBLES:
            # match a whole opener token followed by a boundary (space/punct/end), so
            # "sure" strips a courtesy "Sure," but not a content word merely starting
            # with those letters (e.g. "surely the ...").
            if stripped.startswith(p):
                rest = stripped[len(p) :]
                if not rest or rest[0] in _EDGE_CHARS:
                    matched = rest
                    break
        if matched is None:
            break
        out = matched
    return out.lstrip(_EDGE_CHARS)


# Placeholder written into a StepRun's persisted output_text / the SSE `step` event
# in place of a flagged step's RAW hedge (C1/C4: the orchestrator per-step guard).
# The raw refusal must not persist to the run trace (run_view.html renders it, and it
# lives in history) nor stream to the client — the trace shows the disposition, not
# the hedge, mirroring how target_text is withheld on the fresh-MT write-back sites.
WITHHELD_OUTPUT = f"[withheld — {REASON}]"

# run-190 Item D / M-1: the DISTINCT blob marker for an over-translation (page-dump)
# withhold. The model did NOT refuse — it over-translated (returned a page-shaped dump
# instead of a single unit), so the run trace must NOT reuse the refusal marker above.
# Sibling of WITHHELD_OUTPUT (same `[withheld — ...]` family so the two render alike in
# the joined StepRun blob), but its own reason so an operator can tell WHICH guard fired
# and reach the right lever (over_translation_guard off / OVER_TRANSLATION_MAX_RATIO 0).
WITHHELD_OUTPUT_OVER_TRANSLATION = "[withheld — over-translation (page dump)]"

# #683: the blob marker for a segment too long for the engine's output limit that no safe
# cut could fit (nothing was sent to the engine). Same `[withheld — ...]` family.
WITHHELD_OUTPUT_ENGINE_LIMIT = "[withheld — too long for the engine's output limit]"

# #343: the blob marker for an llm_agent OUTPUT-GUARD withhold (instruction echo, short-source
# blow-up, number mismatch; see app/services/output_guard.py). Same `[withheld — ...]` family.
WITHHELD_OUTPUT_GUARD = "[withheld — output guard]"  # the per-segment flag names the rule


# =============================================================================
# OUTPUT HYGIENE — llm_agent step output cleanup: (1) remove OUR OWN injected fence blocks
# a model echoed back, and (2) a DELIBERATELY NARROW leading meta-LINE strip.
#
# LIVE CAPTURE BUG #2 (prod run 13): a fix step (llama-3-8b) echoed the injected
# `<translation_context ...>...</translation_context>` block VERBATIM at the TOP of its
# revised translation, and that echo became the shipped target. The leading-label strip
# below does NOT catch it (it is a fence block, not a meta label). LAYER (1) removes any of
# OUR injected fence blocks — `<translation_context>`, `<style_guide>`, `<lqa_findings>`
# (opening tag + optional attributes through the matching closing tag), plus a bare
# orphaned opening OR closing tag line — WHEREVER they appear, then collapses the leftover
# blank lines. This is SAFE BY CONSTRUCTION: context_pkg / style_guides / lqa sanitizers
# neutralize angle brackets in EVERY injected value (`<`->`&lt;`), so a RAW fence tag can
# never be legitimate translated content — only an echo of what we injected.
#
# LIVE CAPTURE BUG #1: a fix/finalizer step emitted "# Revised Target Text" as its FIRST
# line (its OWN line, followed by a blank line, then the real translation), which was
# absorbed into the working translation and chained onward as if it were part of the
# target. LAYER (2) removes ONLY that shape.
#
# PRECISION IS THE WHOLE GAME (adversarial-review HIGH). A false positive DELETES a real
# translation, and the intuitive "starts with 'here is'/'voici'" rule is a false-positive
# machine: the output is in the TARGET language, so an ordinary translated heading or
# lead-in ("**Aquí está tu plan de crecimiento**", "## Voici les étapes à suivre", "Voici
# pourquoi :", "**Hier ist die Lösung**") trips it. So the rule is a FULL-LINE WHITELIST,
# nothing more:
#   * the first line, after stripping optional markdown decoration (#/##/**/*) and an
#     optional trailing colon, must EQUAL (not merely start with) one of a small set of
#     unambiguous MULTI-WORD meta labels;
#   * that line must be followed by a BLANK line (the observed "\n\n<body>" shape);
#   * a non-empty body must remain;
#   * and the SOURCE's own first line must NOT itself normalize to a whitelist label — a
#     document that genuinely carried a "Target text"/"Here is the translation" HEADING is
#     being translated, not chatting, so its translated heading is kept (the one place the
#     source is consulted; a plain source-language "did the source open this way" check
#     would otherwise be inert, since source and target are different languages).
# There is NO prefix branch and NO single-common-word label ("Translation"/"Traducción"/
# "Traduction"/"Übersetzung" are common real section titles and are NOT stripped). A step
# that genuinely wants findings/context injected uses the explicit placeholders; this is
# purely a defense against the one observed meta-label-heading footgun.
# =============================================================================

# The FULL-LINE meta-label whitelist (multi-word only). A leading line whose normalized
# content EQUALS one of these — and which is followed by a blank line + non-empty body —
# is the meta-label-heading footgun and is removed. en/es/fr/de + close/unaccented
# variants a model may emit. NEVER a single common word (see the section header).
_HY_LABELS: frozenset[str] = frozenset(
    {
        # English
        "revised target text",
        "revised translation",
        "revised text",
        "final translation",
        "final target text",
        "corrected translation",
        "translated text",
        "target text",
        "here is the translation",
        "here's the translation",
        "here is your translation",
        "here is the revised text",
        "here is the revised translation",
        "here is the final translation",
        # Spanish
        "aquí está la traducción",
        "aqui esta la traduccion",
        "texto traducido",
        "texto de destino",
        "traducción revisada",
        "traduccion revisada",
        # French
        "voici la traduction",
        "voici la traduction révisée",
        "voici la traduction revisee",
        "texte traduit",
        "texte cible",
        # German
        "hier ist die übersetzung",
        "hier ist die ubersetzung",
        "hier ist die uebersetzung",
        "übersetzter text",
        "ubersetzter text",
        "uebersetzter text",
    }
)

# Leading markdown heading / bold / bullet / quote decoration peeled before matching.
_HY_DECOR = "#*_>-\t "


def _hy_norm(line: str) -> str:
    """The first line's normalized content: leading markdown decoration (#/##/**/*/-/>)
    peeled, trailing colon/bold/space stripped iteratively, lowercased. Used ONLY for a
    FULL-LINE equality test against `_HY_LABELS` (never a prefix/substring match)."""
    s = line.strip().lstrip(_HY_DECOR)
    prev = None
    while s != prev:
        prev = s
        s = s.strip().rstrip(":*_")
    return s.strip().lower()


# --- LAYER (2): the narrow leading meta-label strip ---------------------------------
def _strip_leading_label(text: str, source: str | None):
    """Remove a leading meta-label-heading line (LAYER 2). Returns (text, stripped_line)
    or (text, None). Strips ONLY when: the first line normalizes to a FULL-LINE match
    against the multi-word `_HY_LABELS` whitelist; it is followed by a BLANK line; a
    non-empty body remains; and the SOURCE's own first line does NOT itself normalize to a
    whitelist label (a real heading being translated is kept)."""
    lines = text.split("\n")
    if len(lines) < 3:
        return text, None  # need: label line, a blank line, a body
    if lines[1].strip():
        return text, None  # the label must be followed by a BLANK line
    if _hy_norm(lines[0]) not in _HY_LABELS:
        return text, None  # not a full-line whitelist match
    src_first = (source or "").split("\n", 1)[0]
    if _hy_norm(src_first) in _HY_LABELS:
        return text, None  # source carried this heading -> keep it
    body = "\n".join(lines[2:]).strip()
    if not body:
        return text, None  # never empty the output
    return body, lines[0].strip()


def strip_meta_preamble(text: str | None, source: str | None = None):
    """Clean an llm_agent step output: remove OUR OWN echoed injected fence blocks
    (LAYER 1), then a leading meta-label heading (LAYER 2). See the section header.

    Returns (possibly_cleaned_text, note_or_None). `note` is truthy iff something was
    removed; the CALLER records it on the step trace (the honest-note plumbing). The note
    DISTINGUISHES the two causes: LAYER 1 -> "fence-echo (<names>)"; LAYER 2 -> the raw
    stripped label line; both -> the two joined. Returns (text, None) UNCHANGED when
    nothing matched — deterministic MockProvider output ("[xx] ...") never does, so the
    offline/hermetic suite is byte-identical."""
    if not text:
        return text, None
    out = text
    fence_note = None
    fenced, fnote = _strip_injected_fences(out, source)
    if fnote:
        out, fence_note = fenced, fnote
    labeled, label_line = _strip_leading_label(out, source)
    if label_line:
        out = labeled
    if fence_note and label_line:
        return out, f"{fence_note}; label {label_line!r}"
    if fence_note:
        return out, fence_note
    if label_line:
        return out, label_line
    return text, None


# --- persistence into Segment.qa_flags ---------------------------------------
# Stored like a qa.check_segment flag ({type,severity,message}) so the CAT editor
# renders the badge with the same code path (mirrors qe.qe_flag). type=="refusal"
# so it never collides with the QE gate's "qe" entry.
FLAG_TYPE = "refusal"


def refusal_flag(source: str | None = None) -> dict:
    """The single qa_flags entry describing a routed non-translation. 'major' so the
    editor shows it as a warning, with the reason a reviewer needs to act.

    `source` (optional) is a short provenance tag for the choke point
    (quality.commit_mt_output) — "mt", "workflow", "tmx-import", ... — recorded as an
    ADDITIVE `source` key so a reviewer sees WHICH path produced the hedge. The
    `message`/`reason`/`type`/`severity` fields are byte-identical to before this
    parameter existed (callers that pass no source get the exact pre-seam dict), so
    every existing refusal-path assertion still holds."""
    entry = {
        "type": FLAG_TYPE,
        "severity": "major",
        "message": f"Routed to review: {REASON}",
        "reason": REASON,
    }
    if source:
        entry["source"] = source
    return entry


def merge_flag(existing: list | None, entry: dict) -> list:
    """qa_flags with any prior refusal entry replaced by `entry` (other flags kept).
    A fresh list each call so SQLAlchemy sees the JSON column as changed (it does not
    track in-place mutation) — same pattern as qe.merge_qe_flag."""
    out = [f for f in (existing or []) if not (isinstance(f, dict) and f.get("type") == FLAG_TYPE)]
    out.append(entry)
    return out
