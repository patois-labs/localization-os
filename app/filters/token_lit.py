"""Token look-alikes in source text (#814.1, 11-07), the shared half every filter uses.

A source can hold text that merely LOOKS like one of our inline tokens (documentation about
LocOS, a template, a pasted export): ``Use ⟦x2⟧ as the marker``. The unit text carries it
unchanged, so a writer that re-reads a translation string cannot tell it from the unit's
own code ``x2``, and used to delete it. The rule (the same as Bilingua's profile token
layer, ``bilingua-model/src/profile/tokens.rs``):

* a look-alike in the source stays TEXT;
* the unit's real codes skip the numbers look-alikes use, so a look-alike never names a
  real code (``reserved_numbers`` / ``skip_map`` / ``next_free``);
* a token in a translation that names no code of the unit is kept as text, not dropped.

Numbering that skips changes a stored unit's token numbers only when its source holds such
text, which could not be exported correctly before. A parse that does it stamps the
document (``base.note_new_read``) and a stored document without the stamp keeps the old
numbering (``base.legacy_read``).
"""

import re

from app.services.segment import _PLACEHOLDER_RE

_NUM_RE = re.compile(r"\d+")


def reserved_numbers(text: str) -> set[int]:
    """The numbers of every token look-alike (``⟦x2⟧``, ``⟦/g1⟧``, ``⟦3⟧`` is not one)
    in the plain text ``text``."""
    if "⟦" not in text:
        return set()
    return {int(_NUM_RE.search(m.group(0)).group(0)) for m in _PLACEHOLDER_RE.finditer(text)}


def next_free(n: int, reserved) -> int:
    """The smallest number above ``n`` that no look-alike uses."""
    n += 1
    while n in reserved:
        n += 1
    return n


def skip_map(numbers, reserved) -> dict[int, int]:
    """old number -> new number when the k-th code takes the k-th free number. Empty when
    nothing moves (no reserved number among the numbers the codes would have used)."""
    ordered = sorted(numbers)
    if not reserved or not ordered:
        return {}
    last = 0
    out = {}
    for old in ordered:
        last = next_free(last, reserved)
        out[old] = last
    return out if any(k != v for k, v in out.items()) else {}


def token_number(tok: str) -> str:
    """The number a token names: ``⟦/g12⟧`` -> ``"12"``."""
    return _NUM_RE.search(tok).group(0)


def code_tokens(text: str, known) -> list:
    """The tokens of the translation ``text`` that name a code of the unit, in order (#814.1,
    the build half). ``known`` holds the unit's code numbers (strings, as inline maps key
    them) or is a callable ``number -> bool``.

    A token whose number names no code of the unit is not returned, so a writer that walks
    these matches leaves it inside the text around it and writes it with that text's own
    escaping: the translator's (or the source's) ``⟦x9999⟧`` is kept as text instead of
    deleted. ``quality.tag_damage`` still reports it as an added token."""
    if "⟦" not in text:
        return []
    has = known if callable(known) else (lambda n: n in known)
    return [m for m in _PLACEHOLDER_RE.finditer(text) if has(token_number(m.group(0)))]


def renumber_tokens(text: str, mapping: dict[int, int]) -> str:
    """``text`` (only tokens, no literal text) with each token's number mapped."""

    def sub(m):
        tok = m.group(0)
        d = _NUM_RE.search(tok)
        new = mapping.get(int(d.group(0)))
        return tok if new is None else tok[: d.start()] + str(new) + tok[d.end() :]

    return _PLACEHOLDER_RE.sub(sub, text)
