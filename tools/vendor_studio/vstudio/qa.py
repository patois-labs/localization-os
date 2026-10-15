"""Tier-1 deterministic QA checks — each a PURE function, unit-tested.

The canonical taxonomy from docs/research/cat-qa-engines-locking-provenance.md
(the ~16 recurring checks). Every check takes plain data (no DB, no I/O) and returns a
list of finding dicts: {"check", "message", "detail_sig"}. Severity is applied by the
caller from the editable VCheckConfig table (DEFAULT_SEVERITIES here are the seeds) purely
as an ADVISORY label (icon colour) — it never blocks anything.

QA IS ADVISORY-ONLY (decided 2026-08-08 — see the decision record in `qa_engine.py`):
findings are computed and rendered as context for the human; they NEVER prevent Complete
or Deliver. There is no "gate", no unignorable check, and no dismissal requirement — the
enforcing side is Localization OS's buyer-side admission seam (tag-integrity/glossary/
source-echo lanes flag needs_review on admit), not this vendor-side QA. The former
`unedited_mt` check was removed outright: an unchanged MT means the MT was good, which is
not a defect.
"""

from __future__ import annotations

import re

# --- check keys --------------------------------------------------------------
EMPTY_TARGET = "empty_target"
TARGET_EQUALS_SOURCE = "target_equals_source"
PLACEHOLDER_MISMATCH = "placeholder_mismatch"
NUMBER_MISMATCH = "number_mismatch"
LENGTH_OVERFLOW = "length_overflow"
LEADING_TRAILING_SPACE = "leading_trailing_space"
DOUBLE_SPACE = "double_space"
REPEATED_WORD = "repeated_word"
UNPAIRED_BRACKETS = "unpaired_brackets"
GLOSSARY_MISSING = "glossary_missing"
FORBIDDEN_TERM = "forbidden_term"
INCONSISTENT_TARGET = "inconsistent_target"

#: Default severity per check (editable via the settings page / VCheckConfig). Severity is
#: an ADVISORY label only (it colours the finding); no severity blocks delivery.
DEFAULT_SEVERITIES: dict = {
    EMPTY_TARGET: "error",
    TARGET_EQUALS_SOURCE: "warning",
    PLACEHOLDER_MISMATCH: "error",
    NUMBER_MISMATCH: "warning",
    LENGTH_OVERFLOW: "error",
    LEADING_TRAILING_SPACE: "warning",
    DOUBLE_SPACE: "warning",
    REPEATED_WORD: "warning",
    UNPAIRED_BRACKETS: "warning",
    GLOSSARY_MISSING: "warning",
    FORBIDDEN_TERM: "error",
    INCONSISTENT_TARGET: "warning",
}

ALL_CHECKS = list(DEFAULT_SEVERITIES.keys())

# --- placeholder / tag detection ---------------------------------------------
# Localization OS inline tags (⟦xN⟧ and friends), plus the common interpolation shapes
# named in the spec: {name}, %s / %d / %1$s, {{var}}.
_LOC_TAG = re.compile(r"⟦[^⟧]*⟧")
_CURLY = re.compile(r"\{\{?[^{}]*\}?\}")
_PRINTF = re.compile(r"%(?:\d+\$)?[-+ #0]*\d*(?:\.\d+)?[sdfiouxXeEgGc%]")


def placeholders(text: str) -> list:
    """The ordered multiset of placeholder/tag tokens in `text`."""
    text = text or ""
    out = []
    out += _LOC_TAG.findall(text)
    out += _CURLY.findall(text)
    out += [m.group(0) for m in _PRINTF.finditer(text) if m.group(0) != "%%"]
    return out


_NUM = re.compile(r"(?<![\w.])[+-]?\d[\d,]*(?:\.\d+)?(?![\w])")


def _numbers(text: str) -> list:
    """Bare numbers in the text (digit groups). Locale grouping is normalized away so
    1,000 and 1.000 collate; the check compares the multiset of numeric VALUES."""
    out = []
    for m in _NUM.findall(text or ""):
        cleaned = m.replace(",", "")
        try:
            out.append(float(cleaned))
        except ValueError:
            continue
    return sorted(out)


_WORD = re.compile(r"\w+", re.UNICODE)


def _finding(check: str, message: str, detail: str = "") -> dict:
    return {"check": check, "message": message, "detail_sig": detail}


# --- the checks (pure) -------------------------------------------------------
def check_empty_target(source: str, target: str) -> list:
    if not (target or "").strip():
        return [_finding(EMPTY_TARGET, "Target is empty.")]
    return []


def check_target_equals_source(source: str, target: str, target_locale: str = "") -> list:
    s, t = (source or "").strip(), (target or "").strip()
    if not t:
        return []
    lang = (target_locale or "").replace("_", "-").split("-", 1)[0].strip().lower()
    if lang == "en":
        return []  # en-US -> en-GB legitimately leaves strings identical
    if s and s == t:
        return [_finding(TARGET_EQUALS_SOURCE, "Target is identical to the source.")]
    return []


def check_placeholders(source: str, target: str, declared: list | None = None) -> list:
    """Placeholder/tag mismatch (advisory — see the module docstring). Compares the multiset
    the dispatch declared (if given, else extracted from the source) against the target's."""
    want = sorted(declared) if declared else sorted(placeholders(source))
    got = sorted(placeholders(target))
    if want != got:
        return [
            _finding(
                PLACEHOLDER_MISMATCH,
                f"Placeholder/tag mismatch: expected {want or '[]'}, target has {got or '[]'}.",
                detail=repr(want),
            )
        ]
    return []


def check_numbers(source: str, target: str) -> list:
    if _numbers(source) != _numbers(target):
        return [
            _finding(
                NUMBER_MISMATCH,
                f"Number mismatch: source {_numbers(source)} vs target {_numbers(target)}.",
            )
        ]
    return []


def check_length(source: str, target: str, char_limit: int | None) -> list:
    if char_limit and len(target or "") > char_limit:
        return [
            _finding(
                LENGTH_OVERFLOW, f"Target is {len(target)} chars, over the {char_limit} limit."
            )
        ]
    return []


def check_spaces(source: str, target: str) -> list:
    out = []
    t = target or ""
    if t != t.strip() and t.strip():
        out.append(_finding(LEADING_TRAILING_SPACE, "Leading or trailing whitespace."))
    if "  " in t:
        out.append(_finding(DOUBLE_SPACE, "Double (or more) consecutive spaces."))
    return out


def check_repeated_word(source: str, target: str) -> list:
    words = _WORD.findall((target or "").lower())
    for a, b in zip(words, words[1:], strict=False):  # noqa: RUF007
        if a == b and len(a) > 1:
            return [_finding(REPEATED_WORD, f'Repeated adjacent word: "{a} {a}".', detail=a)]
    return []


_PAIRS = {"(": ")", "[": "]", "{": "}"}
_QUOTES = ('"', "«", "»", "“", "”")


def check_unpaired(source: str, target: str) -> list:
    t = target or ""
    stack = []
    for ch in t:
        if ch in _PAIRS:
            stack.append(ch)
        elif ch in _PAIRS.values():  # noqa: SIM102
            if not stack or _PAIRS[stack.pop()] != ch:
                return [_finding(UNPAIRED_BRACKETS, "Unbalanced bracket in target.")]
    if stack:
        return [_finding(UNPAIRED_BRACKETS, "Unclosed bracket in target.")]
    # Straight double-quotes must be balanced (even count).
    if t.count('"') % 2 != 0:
        return [_finding(UNPAIRED_BRACKETS, "Odd number of straight double-quotes.")]
    return []


def check_glossary(source: str, target: str, glossary: list | None) -> list:
    """glossary is a list of term dicts: {term, target|target_term, forbidden?, dnt?}.
    Two lanes: a required target term missing when its source term is present
    (GLOSSARY_MISSING), and a forbidden/DNT term used (FORBIDDEN_TERM)."""
    out = []
    src_l, tgt_l = (source or "").lower(), (target or "").lower()
    for entry in glossary or []:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term") or "").strip()
        want = str(entry.get("target") or entry.get("target_term") or "").strip()
        if not term:
            continue
        forbidden = bool(entry.get("forbidden"))
        dnt = bool(entry.get("dnt"))
        term_in_src = term.lower() in src_l
        if forbidden:
            # A forbidden term must not appear in the TARGET.
            if term.lower() in tgt_l:
                out.append(
                    _finding(
                        FORBIDDEN_TERM,
                        f'Forbidden term used in target: "{term}".',
                        detail=term.lower(),
                    )
                )
            continue
        if dnt:
            # Do-Not-Translate: if the source has it, the target must keep it verbatim.
            if term_in_src and term.lower() not in tgt_l:
                out.append(
                    _finding(
                        FORBIDDEN_TERM,
                        f'DNT term must be preserved verbatim: "{term}".',
                        detail="dnt:" + term.lower(),
                    )
                )
            continue
        # Required term: if the source has it and a target term is defined, it must appear.
        if term_in_src and want and want.lower() not in tgt_l:
            out.append(
                _finding(
                    GLOSSARY_MISSING,
                    f'Glossary term "{term}" should render as "{want}".',
                    detail=term.lower(),
                )
            )
    return out


# --- per-segment aggregate ---------------------------------------------------
def check_segment(seg: dict, glossary: list | None = None) -> list:
    """Run every SEGMENT-LOCAL check for one segment dict. `seg` carries:
    source, target, target_locale, placeholders (declared), char_limit, provenance,
    prefill, locked. A LOCKED segment is excluded from checks (it is returned as
    dispatched)."""
    if seg.get("locked"):
        return []
    source = seg.get("source") or ""
    target = seg.get("target") or ""
    loc = seg.get("target_locale") or ""
    out = []
    out += check_empty_target(source, target)
    # Every check below is meaningful only for a non-empty target.
    if (target or "").strip():
        out += check_target_equals_source(source, target, loc)
        out += check_placeholders(source, target, seg.get("placeholders"))
        out += check_numbers(source, target)
        out += check_length(source, target, seg.get("char_limit"))
        out += check_spaces(source, target)
        out += check_repeated_word(source, target)
        out += check_unpaired(source, target)
        out += check_glossary(source, target, glossary)
    return out


def check_job_consistency(segments: list) -> dict:
    """JOB-LEVEL: same source -> different target within the job. Returns
    {segment_index: [findings]} for the flagged segments. `segments` is a list of dicts
    with source/target/locked; index is the list position."""
    by_source: dict = {}
    for i, seg in enumerate(segments):
        if seg.get("locked"):
            continue
        s = (seg.get("source") or "").strip()
        t = (seg.get("target") or "").strip()
        if not s or not t:
            continue
        by_source.setdefault(s, []).append((i, t))
    out: dict = {}
    for s, rows in by_source.items():
        targets = {t for _i, t in rows}
        if len(targets) > 1:
            for i, _t in rows:
                out.setdefault(i, []).append(
                    _finding(
                        INCONSISTENT_TARGET,
                        "Same source translated inconsistently across the job.",
                        detail=s[:60],
                    )
                )
    return out
