"""AI Tagging — segment-granular mask/restore for inline-fidelity tokens.

WHAT THIS IS
------------
The DOCX/XLIFF/HTML filters replace mid-paragraph run formatting / inline
elements with opaque `⟦rN⟧ ⟦gN⟧ ⟦xN⟧` tokens (see `app/services/segment.py`)
before text reaches MT. A small model handed those tokens raw inside `{text}`
routinely renumbers, reorders, translates, or drops them — which then collapses
whole-paragraph formatting at export time (the headline "a whole section goes
bold instead of a title" symptom is mechanically a token DROP).

This module is the DAMAGE-REDUCTION half of the AI Tagging design: before an LLM
call, every `⟦⟧` token is replaced by an opaque sentinel `«TAG_N»` the model has
no reason to touch; after the call it is restored. It is modelled on
`app/services/pii.py`'s redact/restore, with THREE DELIBERATE DIVERGENCES the tag
problem requires (see the proposal, PHASE 1, item 1.1):

  1. SEGMENT-GRANULAR, NOT PER-CHUNK. `pii.redact` runs per chunk
     (`orchestrator.py`), which is fine for PII (each PII value is self-contained)
     but WRONG for a paired token: `⟦r1⟧…⟦/r1⟧` split across two chunks would get
     colliding per-chunk numbering. `mask()` is therefore always applied to a WHOLE
     segment's text ONCE, before chunking, so an open and its close share one
     mapping and colliding per-paragraph ids in DIFFERENT segments never clash
     (each segment gets its own fresh mapping / sentinel space).

  2. RESTORE-BEFORE-VERIFY, ALWAYS. The masking is CALL-SCOPED: `restore()` runs
     BEFORE any structural comparison, the write gate, the LQA structural path, the
     TM store, or the feedback buffer. Every tag-integrity check therefore runs on
     RESTORED `⟦⟧` forms — never on `«TAG_N»` sentinels — so `qa.check_segment`
     (which matches `⟦⟧`) always sees real tokens. Masking reduces how often the
     model mangles tokens; it never masks the check.

  3. RESIDUAL → REVIEW, NOT WARN-AND-SHIP. `pii.restore` is warn-and-ship on a
     residual `«PII_` sentinel. Tag restore instead REPORTS residual sentinels so
     the caller routes them to the verifier → NEEDS_REVIEW: a model-mangled sentinel
     must never ship as visible `«TAG_N»` garbage.

MASKING IS RECOVERABLE-BUT-POSSIBLY-WRONG, NOT LOSSLESS. It guarantees token
IDENTITY survives generation (the model translates around opaque sentinels), NOT
placement — a model that moves a sentinel to the wrong word still round-trips
cleanly here. Placement is the deterministic gate's + human review's job; this
layer only keeps the token stream intact through the call.

Documented in: docs/architecture/inline-tags.md
"""

from __future__ import annotations

import re
import secrets

from app.services import segment, settings

# COLLISION-PROOF sentinel shape (HIGH, reviewer #2). A generated sentinel is
# `«TAG<nonce>_<n>»` where <nonce> is a random per-CALL hex string. The nonce is what
# makes masking collision-proof: source prose can legitimately contain a fixed-shape
# `«TAG_1»` (French quotes around that literal string, our own docs describing this
# module) — a blanket restore of a FIXED sentinel would rewrite that prose to a real
# `⟦⟧` token and launder corruption. The random nonce guarantees a generated sentinel
# NEVER matches pre-existing text (and mask() re-rolls if it somehow does), so restore
# replaces ONLY the exact strings it generated, and the residual/gate checks below —
# which key on the nonce-bearing SHAPE, not a bare `«TAG_` — never false-positive on a
# legitimate fixed-shape `«TAG_N»` in real content.
#
# Guillemets + hex body: essentially never in real prose, no regex-special chars, and
# no digit-run/@/punctuation `pii.PATTERNS` could match, so tag masking composes
# cleanly OUTSIDE PII masking.
_NONCE_LEN = 8  # hex chars; 32 bits of per-call entropy, far more than needed

# A WHOLE generated sentinel: `«TAG<>=6 hex>_<digits>»`. The `[0-9a-f]{6,}` requirement
# is exactly what distinguishes a GENERATED sentinel from a legitimate fixed-shape
# `«TAG_5»` in source content (which has `_` immediately after `TAG`, no hex run).
_GEN_SENTINEL_RE = re.compile(r"«TAG[0-9a-f]{6,}_\d+»")
# A generated-sentinel PREFIX (nonce present), used to detect a MANGLED/unrestored
# generated sentinel (truncated `»`, renumbered to an id we never issued). Matches
# only nonce-bearing shapes, so a legit `«TAG_5»` prose mention never trips it.
# #684: also `⟦` (the engine copies the brackets of the real `⟦x1⟧` tokens around it) and
# `{` / `[` / `(` / `<` (placeholder syntax, `{{TAG…}}`).
# #726 (HubSpot rehearsal, ja-JP): keyed on the nonce-bearing BODY alone, whatever
# delimiter (or none) surrounds it. A list of opening delimiters always misses the next one
# an engine invents (fullwidth `＜`, a dropped guillemet), and then the debris is neither
# restored nor flagged. `TAG<6+ hex>_` does not occur in real prose; a fixed-shape
# `«TAG_5»` (no hex run) still never trips it.
_GEN_RESIDUAL_RE = re.compile(r"TAG[0-9a-fA-F]{6,}_")

# Delimiters an engine substitutes for the guillemets (#673, format soak 2026-10-11:
# gemma-4-12b into zh-CN wrote `「TAG<nonce>_1」` for every `«TAG<nonce>_1»`, so the exact
# replace in `restore` found nothing and every formatted segment was held for review).
# CJK corner/double/angle brackets, lenticular and tortoise-shell brackets, fullwidth
# square brackets, math angle brackets, single guillemets, ASCII / curly quotes.
# #684 (soak round 4, he-IL / pt-BR): `⟦TAG…⟧` (the brackets of the real `⟦x1⟧` tokens),
# mixed `«TAG…⟧`, and ASCII brackets `{TAG…}` / `[TAG…]` / `(TAG…)` / `<TAG…>`. Doubled
# ASCII braces `{{TAG…}}` are matched as one frame in `restore` (_DOUBLE_FRAMES).
# #726: fullwidth quotes / angle brackets / braces / parens, halfwidth corner brackets,
# CJK double-prime quotes and the white lenticular / tortoise-shell brackets.
_CJK_DELIMS = "«»「」『』《》〈〉⟨⟩‹›【】〔〕［］＜＞＂＇｢｣｛｝（）〝〞〟〖〗〘〙"
_DELIMS = _CJK_DELIMS + "\"'“”‘’„⟦⟧{}[]()<>"
_DOUBLE_FRAMES = (("{{", "}}"), ("[[", "]]"), ("((", "))"), ("<<", ">>"), ("⟦⟦", "⟧⟧"))


# A LOOSE match for anything that came from a generated sentinel — the exact form, or
# a MANGLED remnant after a model rewrote its delimiters. Live evidence (llama-3-8b,
# 2026-07-25) produced all of these from one `«TAG<nonce>_1»`:
#     «TAG41d35d19_1⟩      (closing guillemet swapped for U+27E9)
#     "TAG41d35d19_1"      (guillemets normalized to ASCII quotes)
#     «<span>TAG8ed…_1</span>»   (body wrapped in markup, delimiters split off)
# The nonce body is the reliable part, so THAT is what this keys on. Used ONLY to
# normalize text before a body COMPARISON (tag_llm_repair.body_preserved) — never to
# rewrite content, and never as the residual-detection signal (that stays the strict
# `_GEN_RESIDUAL_RE` above, so detection cannot be loosened by accident).
# #726: the CJK / fullwidth delimiters of `_CJK_DELIMS` too (「TAG…」 debris counted as a
# word). ASCII brackets stay out: stripping an author's `(` beside a sentinel on one side of
# a comparison only would read as a content change.
_ANY_CLS = "[" + re.escape(_CJK_DELIMS + "\"'“”") + "]"
_GEN_ANY_RE = re.compile(_ANY_CLS + r"?\s*TAG[0-9a-f]{6,}_\d*\s*" + _ANY_CLS + "?")


def strip_generated_sentinels(text: str | None) -> str:
    """Remove generated mask sentinels AND their mangled remnants from `text`.

    For COMPARISON ONLY. A sentinel is our own machine artifact, never author content,
    so when two versions of a segment are compared for "did the words change?" the
    sentinel debris must not count as a word on either side — otherwise a repair that
    cleans up the debris reads as a content edit and gets discarded (the live bug this
    exists to fix)."""
    return _GEN_ANY_RE.sub("", text or "")


def mask_like(text: str | None, mapping: dict) -> str:
    """Rewrite any RAW `⟦⟧` token in `text` into the sentinel that `mapping` already
    assigned to that same token, so auxiliary prompt material speaks the SAME notation
    as the masked `{text}` it accompanies.

    WHY THIS EXISTS (live-QA finding, 2026-07-25 — a hole in the Phase-1 mask layer).
    A fix step's prompt is assembled from a MASKED `{text}` plus an auto-injected
    `{lqa_findings}` block. The LQA judge scores the RESTORED text, so its notes quote
    REAL tokens ("Inline placeholder dropped ⟦/g1⟧, ⟦g1⟧"). The findings block was never
    masked, so 9 of 34 captured live prompts presented the model with BOTH notations at
    once — and the model duly started emitting raw `⟦⟧` literals into masked text,
    producing corruption like `«⟦/g1⟧TAG41d35d19_1⟦/g1⟧»`. Masking prevented damage in
    `{text}` while the findings block re-taught the very syntax it was hiding.

    Each raw token is mapped to the FIRST sentinel standing for that exact token (a
    segment carries one occurrence of an identity in the overwhelming majority of cases).
    A token with no counterpart in THIS segment cannot be named safely, so it degrades to
    a neutral "(a formatting tag)" — never a literal the model can copy."""
    # CALL-SITE GATED, not mapping-gated. An EMPTY mapping is NOT the same thing as
    # "masking is off": a segment can carry no tokens of its own while the findings still
    # quote tokens from elsewhere in the document. Live QA showed 9 such prompts still
    # handing the model raw `⟦⟧` literals to imitate on a segment that had none — so with
    # no sentinel to map to, every token must still degrade to the neutral phrase. Callers
    # MUST therefore invoke this only when masking is enabled (`ai_tagging.enabled()`);
    # when it is off they simply don't call, which keeps that path byte-identical.
    if not text:
        return ""
    first: dict[str, str] = {}
    for sent, tok in (mapping or {}).items():
        first.setdefault(tok, sent)

    def _sub(m):
        return first.get(m.group(0), "(a formatting tag)")

    return segment._PLACEHOLDER_RE.sub(_sub, text)


def enabled() -> bool:
    """Whether the MASKING layer is on (default ON). Governs ONLY mask/restore —
    the deterministic verification gate is always-on and never consults this (see
    the `ai_tagging_enabled` note in settings.DEFAULTS)."""
    return settings.get("ai_tagging_enabled") != "0"


def _fresh_nonce(text: str) -> str:
    """A random hex nonce that does NOT occur in `text` — so every generated sentinel
    built from it is guaranteed absent from the source before masking (collision-proof
    even against adversarial content that embeds a hex-shaped `«TAG…»`)."""
    for _ in range(8):
        nonce = secrets.token_hex(_NONCE_LEN // 2)
        if nonce not in text:
            return nonce
    # Astronomically unlikely; widen entropy as a last resort.
    return secrets.token_hex(_NONCE_LEN)


def mask(text: str | None) -> tuple[str, dict]:
    """Replace every inline-fidelity `⟦⟧` token in `text` with an opaque, COLLISION-
    PROOF `«TAG<nonce>_N»` sentinel (random per-call nonce), numbered in document
    order starting at 1. Returns ``(masked_text, mapping{sentinel: original_token})``.

    SEGMENT-GRANULAR by contract: pass a WHOLE segment's text (never a mid-segment
    chunk) so a `⟦r1⟧…⟦/r1⟧` pair shares one mapping and its open/close never get
    colliding numbers. Every open, close, and point token becomes its OWN sentinel
    (1:1), so restore is an exact string-replace and a duplicated/reordered sentinel
    is handled by the gate on the restored form, not hidden here.

    The random nonce guarantees a generated sentinel can never collide with a fixed-
    shape `«TAG_1»` that legitimately appears in the source (the HIGH laundering bug):
    such prose is left untouched by mask AND by restore. A `text` with no tokens
    returns ``(text, {})`` unchanged — byte-identical."""
    text = text or ""
    mapping: dict[str, str] = {}
    counter = {"n": 0}
    nonce = _fresh_nonce(text)

    def repl(m: re.Match) -> str:
        counter["n"] += 1
        tok = f"«TAG{nonce}_{counter['n']}»"
        mapping[tok] = m.group(0)
        return tok

    masked = segment._PLACEHOLDER_RE.sub(repl, text)
    return masked, mapping


def restore(text: str | None, mapping: dict) -> tuple[str, bool]:
    """Substitute each generated `«TAG<nonce>_N»` sentinel in `text` back to its
    original `⟦⟧` token. Returns ``(restored_text, has_residual)``.

    Replaces ONLY the exact nonce-bearing strings this call generated (the mapping
    keys), so a fixed-shape `«TAG_1»` present in the source as real content is NEVER
    rewritten — the HIGH collision/laundering bug is closed at the root.

    `has_residual` is True when — after replacing every generated sentinel — any
    generated-shape PREFIX survives (`_GEN_RESIDUAL_RE`): the model renumbered a
    sentinel to an id we never issued, or truncated one mid-token. That is the
    warn-and-ship divergence (1.1): the caller routes a residual to the verifier →
    NEEDS_REVIEW so `«TAG…»` garbage never ships. It keys on the NONCE-bearing shape,
    so a legitimate `«TAG_5»` in content is not a residual. `str.replace` replaces ALL
    occurrences, so a DUPLICATED sentinel round-trips to a duplicated `⟦⟧` token —
    caught deterministically by the gate on the restored form. Empty mapping →
    ``(text, False)`` unchanged (byte-identical)."""
    out = text or ""
    bodies = {tok[1:-1]: val for tok, val in (mapping or {}).items()}
    present = [b for b in bodies if b in out]
    if not present:
        return out, bool(_GEN_RESIDUAL_RE.search(out))
    # #673: the engine may rewrite the guillemets (zh/ja: 「TAG…」). The nonce-bearing BODY
    # is unique to this call, so the body framed by delimiters from `_DELIMS` (never a
    # longer id like `_12` for `_1`: `(?!\d)`) is still unambiguously this sentinel.
    # #726: optional whitespace inside the frame, and a body with NO delimiter beside it
    # (`TAG…_1`, both guillemets dropped) is restored too: nothing next to it is touched.
    # Gate (#726): a body with a delimiter on ONE side only (`「TAG…_1保存`) is NOT
    # restored. Without the source we cannot tell an engine's leftover half-frame from the
    # author's own bracket (`「«TAG_1»保存«TAG_2»」` -> `「TAG…_1保存TAG…_2」`): consuming it
    # would silently ship `⟦g1⟧保存⟦/g1⟧` without the author's 「」. It stays a residual and
    # goes to review, as do a truncated body (`「TAG…_`) and an id we never issued. Only
    # the exact generated bodies are matched, so a fixed-shape `«TAG_1»` stays untouched.
    # ONE pass over the text, so a restored `⟦g1⟧` is never rescanned: a later per-token
    # pass used to take its `⟧` as the frame of the next sentinel (`⟦g1⟦/g1⟧`).
    b = "(?:" + "|".join(re.escape(x) for x in sorted(present, key=len, reverse=True))
    b += r")(?!\d)"
    d = f"[{re.escape(_DELIMS)}]"
    alts = []
    # #684: a doubled ASCII frame (`{{TAG…}}`) is one frame, tried first so the single-
    # delimiter form does not leave `{⟦x1⟧}` behind. Gate: either side may be doubled
    # alone (`{{TAG…}`), else a stray `{` was left beside the token.
    for o, c in _DOUBLE_FRAMES:
        o1, c1 = re.escape(o[0]), re.escape(c[0])
        alts.append(rf"{o1}{o1}?\s*{b}\s*{c1}{c1}?")
    alts.append(rf"{d}\s*{b}\s*{d}")  # framed on both sides (the exact `«TAG…»` too)
    # #726: one side / none. The side groups are matched only to SEE a one-sided frame
    # (left untouched below); a delimiter that opens the NEXT sentinel is not this one's.
    alts.append(rf"(?P<pre>{d}\s*)?{b}(?P<suf>\s*{d}(?!\s*TAG[0-9a-f]{{6,}}_))?")
    body_re = re.compile(b)

    def _sub(m: re.Match) -> str:
        if m.group("pre") or m.group("suf"):
            return m.group(0)  # one-sided frame: ambiguous, stays a residual (review)
        return bodies[body_re.search(m.group(0)).group(0)]

    out = re.sub("|".join(alts), _sub, out)
    return out, bool(_GEN_RESIDUAL_RE.search(out))


def has_residual_sentinel(text: str | None, source: str | None = None) -> bool:
    """True when `text` carries a GENERATED-shape (nonce-bearing) sentinel fragment — a
    mangled/unrestored mask sentinel. Used by the write gate as a second, mapping-
    independent residual signal. Keys on the nonce-bearing shape (`_GEN_RESIDUAL_RE`),
    NOT a bare `«TAG_`, so a legitimate fixed-shape `«TAG_5»` in real content never
    false-positives (fixing HIGH-1b's quarantine of legit prose + the editor guard's
    false confirm).

    #726 gate: with `source`, a fragment the source itself carries (an identifier such as
    `BUILD_TAG20241011_rc1` copied through) is content, not debris. A generated nonce is
    never in its source (`_fresh_nonce`), so a real residual is never excused."""
    t = text or ""
    if source is None:
        return bool(_GEN_RESIDUAL_RE.search(t))
    src = source or ""
    return any(m.group(0) not in src for m in _GEN_RESIDUAL_RE.finditer(t))
