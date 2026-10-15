"""Contentful Rich Text AST walker (the Contentful twin of app/filters/adf.py).

Contentful stores a Rich Text field value as a JSON document tree
(https://www.contentful.com/developers/docs/concepts/rich-text/ and the node enums in
https://github.com/contentful/rich-text, packages/rich-text-types/src):

    {"nodeType": "document", "data": {}, "content": [<block>, ...]}

    block   {"nodeType": "paragraph" | "heading-1".."heading-6" | "ordered-list" |
             "unordered-list" | "list-item" | "blockquote" | "hr" | "table" | "table-row" |
             "table-cell" | "table-header-cell" | "embedded-entry-block" |
             "embedded-asset-block" | "embedded-resource-block", "data": {}, "content": [...]}
    inline  {"nodeType": "hyperlink" | "entry-hyperlink" | "asset-hyperlink" |
             "resource-hyperlink" (wrapper, content = inline/text children) |
             "embedded-entry-inline" | "embedded-resource-inline" (void, content []),
             "data": {"uri": ...} or {"target": {"sys": ...}}}
    text    {"nodeType": "text", "value": "...", "marks": [{"type": "bold"}, ...], "data": {}}
    marks   bold, italic, underline, code, superscript, subscript, strikethrough

Same approach as the Jira ADF inline units (adf.extract_inline / reinsert_inline, #644):
ONE translatable unit per inline container (a paragraph, heading, ...; list items and
table cells hold paragraphs, so those are covered), with placeholder tokens in the
shared syntax. A marked text node or a hyperlink wrapper becomes an open/close pair
(g1 ... /g1), a void inline node or a code-marked span becomes a point token (x2). The
translated string is parsed back with a stack, copying marks, link data and embedded
nodes from the SOURCE tree. Contentful hyperlinks may contain marked text, so unlike the
ADF rebuild this one supports nesting (a bold span inside a link).

Anything the walker does not model (unknown block types, void blocks, embedded entries
and assets, `data` payloads) is preserved byte-identical because rebuilding is
deep-clone-of-source plus replacing the `content` of a recognised container only.

A field is all-or-nothing: rebuild() returns (None, bad) unless EVERY unit has a
translation whose tokens round-trip and the non-text node skeleton of the result equals
the source's. The caller then keeps the field's stored value and holds the pair back.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"⟦(/?)([gx])(\d+)⟧")

# Inline nodes that wrap translatable text (their children are text / more inlines).
_WRAPPER_INLINES = frozenset(
    {"hyperlink", "entry-hyperlink", "asset-hyperlink", "resource-hyperlink"}
)
# Inline nodes carrying meaning in an id / target, not prose: kept verbatim.
_VOID_INLINES = frozenset({"embedded-entry-inline", "embedded-resource-inline"})
_ALL_INLINES = _WRAPPER_INLINES | _VOID_INLINES

# Block nodes the walker descends into. Everything else (embedded blocks, hr, unknown or
# future block types) is never entered, so its content is preserved byte-identical.
_TEXT_BLOCKS = frozenset({"paragraph"} | {f"heading-{n}" for n in range(1, 7)})
_STRUCT_BLOCKS = frozenset(
    {
        "document",
        "ordered-list",
        "unordered-list",
        "list-item",
        "blockquote",
        "table",
        "table-row",
        "table-cell",
        "table-header-cell",
    }
)

# A code-marked span is code, not prose: it rides along as an untranslated point token.
_CODE_MARK = "code"


@dataclass
class RtUnit:
    """One translatable inline container. `path` is the alternating ("content", i, ...)
    address of the container from the document root; `tokens` maps token number ->
    ("m", text-node-without-value) | ("w", wrapper-node-without-content) |
    ("x", verbatim-node)."""

    p_index: int
    text: str
    path: tuple = ()
    tokens: dict = field(default_factory=dict)


def is_document(value) -> bool:
    return isinstance(value, dict) and value.get("nodeType") == "document"


def _is_text(node) -> bool:
    return (
        isinstance(node, dict)
        and node.get("nodeType") == "text"
        and isinstance(node.get("value"), str)
    )


def _is_inline_child(node) -> bool:
    return _is_text(node) or (isinstance(node, dict) and node.get("nodeType") in _ALL_INLINES)


def _has_marks(node) -> bool:
    return bool(node.get("marks"))


def _is_code(node) -> bool:
    return any(isinstance(m, dict) and m.get("type") == _CODE_MARK for m in node.get("marks") or [])


def _plain_text_node(node) -> bool:
    """A text node that can be written as bare text: no marks and no data payload."""
    return not _has_marks(node) and not node.get("data")


def _unsafe_text(node) -> bool:
    """Text that contains the placeholder brackets themselves cannot be tokenised without
    ambiguity: its container is left untranslated and verbatim."""
    return "⟦" in node["value"] or "⟧" in node["value"]


def _serialize(children: list, tokens: dict, counter: list) -> str | None:
    """Tokenised string for a list of inline children, or None when something in it
    cannot be represented safely (the whole container is then left verbatim)."""
    parts: list = []
    for c in children:
        if _is_text(c):
            if _unsafe_text(c):
                return None
            if _is_code(c):
                counter[0] += 1
                tokens[counter[0]] = ("x", copy.deepcopy(c))
                parts.append(f"⟦x{counter[0]}⟧")
            elif _plain_text_node(c):
                parts.append(c["value"])
            else:
                counter[0] += 1
                n = counter[0]
                shell = {k: copy.deepcopy(v) for k, v in c.items() if k != "value"}
                tokens[n] = ("m", shell)
                parts.append(f"⟦g{n}⟧{c['value']}⟦/g{n}⟧")
        elif (
            isinstance(c, dict)
            and c.get("nodeType") in _WRAPPER_INLINES
            and all(_is_inline_child(k) for k in (c.get("content") or []))
        ):
            counter[0] += 1
            n = counter[0]
            shell = {k: copy.deepcopy(v) for k, v in c.items() if k != "content"}
            tokens[n] = ("w", shell)
            inner = _serialize(c.get("content") or [], tokens, counter)
            if inner is None:
                return None
            parts.append(f"⟦g{n}⟧{inner}⟦/g{n}⟧")
        else:
            counter[0] += 1
            tokens[counter[0]] = ("x", copy.deepcopy(c))
            parts.append(f"⟦x{counter[0]}⟧")
    return "".join(parts)


def _translatable(text: str) -> bool:
    """True when the container holds words (not only tokens, whitespace or digits)."""
    return any(ch.isalpha() for ch in _TOKEN_RE.sub("", text))


def _walk(node, path: tuple, out: list) -> None:
    if not isinstance(node, dict):
        return
    kind = node.get("nodeType")
    if kind not in _TEXT_BLOCKS and kind not in _STRUCT_BLOCKS:
        return  # unknown / embedded / void node: never entered, never translated
    content = node.get("content")
    if not isinstance(content, list) or not content:
        return
    has_block_child = any(
        isinstance(c, dict) and c.get("nodeType") in (_TEXT_BLOCKS | _STRUCT_BLOCKS)
        for c in content
    )
    # A text block holds inline content: an unknown inline child rides along as a verbatim
    # point token (_serialize), it neither blocks the paragraph nor gets translated itself.
    # A structural block (list item, quote, cell) is a unit only when it holds bare inline
    # content; normally it holds paragraphs, which are walked below.
    if (kind in _TEXT_BLOCKS and not has_block_child) or (
        all(_is_inline_child(c) for c in content)
        and any(
            _is_text(c) or (isinstance(c, dict) and c.get("nodeType") in _WRAPPER_INLINES)
            for c in content
        )
    ):
        tokens: dict = {}
        text = _serialize(content, tokens, [0])
        if text is not None and _translatable(text):
            out.append(RtUnit(p_index=len(out), text=text, path=path, tokens=tokens))
        return
    for i, child in enumerate(content):
        _walk(child, path + ("content", i), out)  # noqa: RUF005


def extract(doc) -> list:
    """One RtUnit per translatable inline container, in document order."""
    out: list = []
    if is_document(doc):
        _walk(doc, (), out)
    return out


def _node_at(tree: dict, path: tuple):
    node = tree
    it = iter(path)
    for key in it:
        arr = node.get(key) if isinstance(node, dict) else None
        idx = next(it, None)
        if not isinstance(arr, list) or not isinstance(idx, int) or not (0 <= idx < len(arr)):
            return None
        node = arr[idx]
    return node


def _text_node(value: str, shell: dict | None) -> dict:
    node = copy.deepcopy(shell) if shell else {"nodeType": "text", "marks": [], "data": {}}
    node["value"] = value
    return node


def _rebuild(unit: RtUnit, translated: str):
    """New `content` list for the container, or None when the translated tokens do not
    match the source's (dropped, added, duplicated, crossed or unopened)."""
    # "m" and "w" tokens appear as g pairs, "x" as point tokens
    expected = sorted(("g" if k in ("m", "w") else "x", n) for n, (k, _s) in unit.tokens.items())
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
    leftover = _TOKEN_RE.sub("", translated)
    if "⟦" in leftover or "⟧" in leftover:
        return None
    root: list = []
    # stack frames: [token number, kind, children list]
    stack: list = []
    pos = 0

    def cur() -> list:
        return stack[-1][2] if stack else root

    def add_text(s: str) -> None:
        if not s:
            return
        if stack and stack[-1][1] == "m":
            stack[-1][2].append(s)  # raw text inside a marked span
        else:
            cur().append(_text_node(s, None))

    for m in _TOKEN_RE.finditer(translated):
        add_text(translated[pos : m.start()])
        pos = m.end()
        closing, kind, num = m.group(1), m.group(2), int(m.group(3))
        if kind == "x":
            cur().append(copy.deepcopy(unit.tokens[num][1]))
            continue
        tkind, shell = unit.tokens[num]
        if not closing:
            if stack and stack[-1][1] == "m":
                return None  # a marked text span cannot contain another token
            stack.append([num, tkind, []])
            continue
        if not stack or stack[-1][0] != num:
            return None  # crossed or unopened pair
        _n, tkind, children = stack.pop()
        if tkind == "m":
            value = "".join(children)
            if value:
                cur().append(_text_node(value, shell))
        else:
            if not children:
                return None  # an empty link would be invalid in Contentful
            node = copy.deepcopy(shell)
            node["content"] = children
            cur().append(node)
    if stack:
        return None
    add_text(translated[pos:])
    return root


def _skeleton(node) -> list:
    """nodeType of every non-text node, depth first: the structure fingerprint."""
    if not isinstance(node, dict):
        return []
    out = [] if node.get("nodeType") == "text" else [node.get("nodeType")]
    content = node.get("content")
    if isinstance(content, list):
        for c in content:
            out.extend(_skeleton(c))
    return out


def rebuild(doc: dict, translations: dict) -> tuple:
    """Deep-clone `doc` and replace each container's content with its translation.

    Returns (new_doc, bad). `new_doc` is None, and `bad` lists the p_index of each
    offending unit, unless every unit has a non-empty translation, every translation's
    tokens round-trip and the rebuilt tree has the source's node skeleton. The source is
    never mutated."""
    units = extract(doc)
    clone = copy.deepcopy(doc)
    bad: list = []
    for u in units:
        new = translations.get(u.p_index)
        if new is None:
            new = translations.get(str(u.p_index))
        if not isinstance(new, str) or not new.strip():
            bad.append(u.p_index)
            continue
        rebuilt = _rebuild(u, new)
        node = _node_at(clone, u.path) if u.path else clone
        if rebuilt is None or not rebuilt or not isinstance(node, dict):
            bad.append(u.p_index)
            continue
        node["content"] = rebuilt
    # The skeleton is compared as a MULTISET: _rebuild already proved every token is used
    # exactly once and correctly nested, and everything outside a unit is a clone, so the
    # only legitimate difference is the ORDER of sibling inlines, which a target language
    # may change (a CJK sentence can put an inline entry before a link). A dropped, added
    # or duplicated node still changes the counts and holds the field.
    if bad or Counter(map(repr, _skeleton(doc))) != Counter(map(repr, _skeleton(clone))):
        return None, bad or [-1]
    return clone, []
