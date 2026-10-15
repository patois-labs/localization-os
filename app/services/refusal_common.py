"""refusal: shared leaf helpers (layered pure move)

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re

_FENCE_NAME_LIST = ("translation_context", "style_guide", "lqa_findings")


_FENCE_NAMES = "|".join(_FENCE_NAME_LIST)


_FENCE_BLOCK_RE = re.compile(
    r"<\s*(" + _FENCE_NAMES + r")\b[^>]*>.*?<\s*/\s*\1\s*>", re.DOTALL | re.IGNORECASE
)


_FENCE_TRUNC_BLOCK_RE = re.compile(
    r"<\s*(" + _FENCE_NAMES + r")\b[^>]*>.*?<\s*/\s*\1\b[^>\n]*$", re.DOTALL | re.IGNORECASE
)


_FENCE_TAG_RE = re.compile(r"<\s*/?\s*(" + _FENCE_NAMES + r")\b[^>]*>", re.IGNORECASE)


_FENCE_TRUNC_RE = re.compile(r"<\s*/?\s*([a-z_]{8,40})[^>\n]{0,200}$", re.IGNORECASE)


_TRUNC_TAIL_CAP = 300


_SRC_FENCE_RE = re.compile(r"<\s*/?\s*(" + _FENCE_NAMES + r")(?![a-z0-9_])", re.IGNORECASE)


_BLANK_RUN_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


# --- LAYER (1): remove OUR OWN injected fence blocks a model echoed back ------------
# The three fence tag NAMES this pipeline injects. A raw occurrence of any of these in a
# produced translation is, by construction (angle brackets are neutralized in every
# injected value), an echo of what we injected — never legitimate translated content.
# NOTE: entity-ESCAPED echoes (`&lt;translation_context&gt;`) are deliberately NOT handled
# here — they carry no raw angle bracket, cannot break a downstream fence, and read as
# ordinary escaped text; they stay accepted-as-residual (no code).
# Optional whitespace after `<` / `</` is tolerated (`< translation_context ...>`), so a
# space-padded echo still strips. A full block: <name ...attrs...> ... </name> (backref
# pins the closer to its opener). DOTALL so a multi-line block matches.
# A TRUNCATED-CLOSER block: a well-formed opener, its body, then a closer cut off at
# END-OF-STRING (missing the trailing `>`) — e.g. `<translation_context ...>\ndomain: y\n
# </translation_context` when generation ran out of tokens mid-closing-tag. The backref
# pins the truncated closer to its opener's name; `[^>\n]*$` keeps the truncated closer on
# the final line (so `.*?` can never swallow past a real closer of the same name earlier).
# This removes the WHOLE span (opener + body + half-closer) instead of leaving the fence
# body as residual scaffolding. ASYMMETRIC by necessity: an UNCLOSED opener with NO closer
# at all (`<translation_context ...>\n<body>` and then real content) only has its opener
# TAG line stripped (via _FENCE_TAG_RE) — its body is indistinguishable from real content
# with no closing delimiter to bound it, so that body is an accepted residual.
# A bare orphaned WELL-FORMED tag (opening OR closing) left after block removal.
# A lone TRUNCATED tag: the output was cut off mid-tag (missing the trailing `>`). This is
# matched ANCHORED at a single position on a BOUNDED tail slice (see _strip_trunc_tail) —
# NEVER free-scanned over the whole text — so a garbled runaway output can't drive
# super-linear backtracking (ReDoS). Bounded quantifiers throughout: name {8,40}, attrs
# {0,200}. The captured run must ALSO contain an underscore (validated in _strip_trunc_tail)
# so plain prose / XLIFF "<translation" is never eaten as a prefix of translation_context.
# How much of the final line to inspect for a truncated tag. A tag is `<` + `/` + name(<=40)
# + attrs(<=200) worst case, comfortably under this cap; bounding the slice is what makes
# the scan linear regardless of total output size.
# SOURCE-SIDE keep-guard probe: does the source mention one of our fence tags as PROSE?
# `(?![a-z0-9_])` mirrors the stripping regexes' `\b` so an unrelated SUPERSTRING like
# `<style_guideline>` does NOT protect `style_guide` (which would let a real echo ship).
# Collapse a run of 2+ blank lines (3+ consecutive newlines, whitespace-only) to ONE blank
# line — the seam a removed block leaves behind. A single blank line is preserved.
def _strip_trunc_tail(text: str, protected: set, names: set) -> str:
    """Strip a LONE truncated fence tag at end-of-output, ReDoS-safe. Truncation only ever
    happens at EOS (generation cut off mid-tag), so this inspects ONLY a bounded tail — the
    final line, capped to its last `_TRUNC_TAIL_CAP` chars — and runs ONE anchored match at
    the tail's last `<`. Returns `text` cleaned (or unchanged). A matched name is kept only
    when it is a prefix of one of our three names AND contains an underscore (so plain
    "<translation" prose is never eaten) AND is not source-protected."""
    tail_start = max(text.rfind("\n") + 1, len(text) - _TRUNC_TAIL_CAP)
    tail = text[tail_start:]
    lt = tail.rfind("<")
    if lt < 0:
        return text
    m = _FENCE_TRUNC_RE.match(tail, lt)  # anchored at the last '<' -> single attempt
    if not m:
        return text
    g = m.group(1).lower()
    if "_" not in g:  # #2: a real name always has an underscore
        return text
    for n in _FENCE_NAME_LIST:
        if n.startswith(g):
            if n in protected:
                return text
            names.add(n)
            return text[: tail_start + m.start()]
    return text
