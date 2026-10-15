"""Sanity Portable Text walker (the Sanity twin of app/filters/contentful_rich_text.py).

Portable Text is Sanity's rich text: a JSON ARRAY of blocks
(https://www.sanity.io/docs/developer-guides/presenting-block-text and the portabletext spec):

    [{"_type": "block", "_key": "a1", "style": "normal" | "h1".. | "blockquote",
      "listItem": "bullet" | "number", "level": 1,
      "markDefs": [{"_key": "lk1", "_type": "link", "href": "https://..."}],
      "children": [{"_type": "span", "_key": "s1", "text": "Hello ", "marks": []},
                   {"_type": "span", "_key": "s2", "text": "docs", "marks": ["strong", "lk1"]},
                   {"_type": "inlineObject", "_key": "i1", ...}]},
     {"_type": "image", ...}, {"_type": "code", ...}, any custom block object]

Same approach as the Contentful Rich Text walker and the Jira ADF inline units: ONE translatable
unit per text block (every list item is its own block in Portable Text), with placeholder tokens in
the shared syntax. A span that carries marks (a decorator such as strong/em, or a link annotation
key) becomes an open/close pair (g1 ... /g1) that keeps the span's `_key` and `marks` byte
identical; a code-decorated span and an inline object become a point token (x2) copied
verbatim. Plain spans are written as bare text. Spans cannot nest in Portable Text, so a pair
may not contain another token.

Anything the walker does not model (image, code and every custom block object, `markDefs`,
`style`, `listItem`, `level`, unknown keys) is preserved byte-identical, because rebuilding is a
deep clone of the source plus replacing the `children` of a recognised text block only.

A field is all-or-nothing: rebuild() returns (None, bad) unless EVERY unit has a translation whose
tokens round-trip. The caller then keeps the field's stored value and holds the pair back.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"⟦(/?)([gx])(\d+)⟧")

_PLAIN_KEYS = frozenset({"_key", "_type", "text", "marks"})
_CODE_MARK = "code"


@dataclass
class PtUnit:
    """One translatable text block. `index` is the block's position in the array; `tokens`
    maps token number -> ("m", span-without-text) | ("x", verbatim-child)."""

    p_index: int
    text: str
    index: int = 0
    tokens: dict = field(default_factory=dict)


def is_portable_text(value) -> bool:
    """True for a Portable Text array: a list holding at least one text block."""
    return isinstance(value, list) and any(
        isinstance(b, dict) and b.get("_type") == "block" and isinstance(b.get("children"), list)
        for b in value
    )


def _is_span(node) -> bool:
    return (
        isinstance(node, dict) and node.get("_type") == "span" and isinstance(node.get("text"), str)
    )


def _is_plain_span(node) -> bool:
    return _is_span(node) and not node.get("marks") and set(node) <= _PLAIN_KEYS


def _is_code_span(node) -> bool:
    return _is_span(node) and _CODE_MARK in (node.get("marks") or [])


def _unsafe(node) -> bool:
    return "⟦" in node["text"] or "⟧" in node["text"]


def _serialize(children: list, tokens: dict) -> str | None:
    parts: list = []
    n = 0
    for c in children:
        if _is_span(c):
            if _unsafe(c):
                return None
            if _is_plain_span(c):
                parts.append(c["text"])
            elif _is_code_span(c):
                n += 1
                tokens[n] = ("x", copy.deepcopy(c))
                parts.append(f"⟦x{n}⟧")
            else:
                n += 1
                shell = {k: copy.deepcopy(v) for k, v in c.items() if k != "text"}
                tokens[n] = ("m", shell)
                parts.append(f"⟦g{n}⟧{c['text']}⟦/g{n}⟧")
        else:
            n += 1
            tokens[n] = ("x", copy.deepcopy(c))
            parts.append(f"⟦x{n}⟧")
    return "".join(parts)


def _translatable(text: str) -> bool:
    return any(ch.isalpha() for ch in _TOKEN_RE.sub("", text))


def extract(blocks) -> list:
    """One PtUnit per translatable text block, in array order."""
    out: list = []
    if not isinstance(blocks, list):
        return out
    for i, b in enumerate(blocks):
        if not (isinstance(b, dict) and b.get("_type") == "block"):
            continue
        children = b.get("children")
        if not isinstance(children, list) or not children:
            continue
        tokens: dict = {}
        text = _serialize(children, tokens)
        if text is not None and _translatable(text):
            out.append(PtUnit(p_index=len(out), text=text, index=i, tokens=tokens))
    return out


def _new_span(text: str, taken: set, seed: str, counter: list) -> dict:
    while True:
        counter[0] += 1
        key = f"{seed}t{counter[0]}"
        if key not in taken:
            taken.add(key)
            return {"_type": "span", "_key": key, "text": text, "marks": []}


def _rebuild_children(unit: PtUnit, translated: str, block: dict):
    """New `children` list, or None when the translated tokens do not match the source's
    (dropped, added, duplicated, crossed, nested or unopened)."""
    expected = sorted(("g" if k == "m" else "x", n) for n, (k, _s) in unit.tokens.items())
    found_open = sorted(
        (m.group(2), int(m.group(3))) for m in _TOKEN_RE.finditer(translated) if not m.group(1)
    )
    found_close = sorted(
        (m.group(2), int(m.group(3))) for m in _TOKEN_RE.finditer(translated) if m.group(1)
    )
    if found_open != expected:
        return None
    if found_close != [t for t in expected if t[0] == "g"]:
        return None
    if "⟦" in _TOKEN_RE.sub("", translated) or "⟧" in _TOKEN_RE.sub("", translated):
        return None
    taken = {
        c.get("_key")
        for c in (block.get("children") or [])
        if isinstance(c, dict) and c.get("_key")
    }
    seed = str(block.get("_key") or "b")
    counter = [0]
    out: list = []
    pos = 0
    open_num = None
    open_start = 0

    def add_text(s: str) -> None:
        if s:
            out.append(_new_span(s, taken, seed, counter))

    for m in _TOKEN_RE.finditer(translated):
        closing, kind, num = m.group(1), m.group(2), int(m.group(3))
        if open_num is None:
            add_text(translated[pos : m.start()])
        elif not (closing and kind == "g" and num == open_num):
            return None  # a pair holds text only: no token nested inside it
        pos = m.end()
        if kind == "x":
            out.append(copy.deepcopy(unit.tokens[num][1]))
        elif not closing:
            open_num = num
            open_start = m.end()
        else:
            inner = translated[open_start : m.start()]
            open_num = None
            if inner:
                span = copy.deepcopy(unit.tokens[num][1])
                span["text"] = inner
                out.append(span)
    if open_num is not None:
        return None
    add_text(translated[pos:])
    return out


def rebuild(blocks, translations: dict) -> tuple:
    """Deep-clone `blocks` and replace each text block's children with its translation.

    Returns (new_blocks, bad). `new_blocks` is None, and `bad` lists the p_index of each
    offending unit, unless every unit has a non-empty translation whose tokens round-trip.
    The source is never mutated."""
    units = extract(blocks)
    clone = copy.deepcopy(blocks)
    bad: list = []
    for u in units:
        new = translations.get(u.p_index)
        if new is None:
            new = translations.get(str(u.p_index))
        if not isinstance(new, str) or not new.strip():
            bad.append(u.p_index)
            continue
        block = clone[u.index]
        rebuilt = _rebuild_children(u, new, block)
        if rebuilt is None or not rebuilt:
            bad.append(u.p_index)
            continue
        block["children"] = rebuilt
    if bad:
        return None, bad
    return clone, []
