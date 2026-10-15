"""ADF (Atlassian Document Format) rich-text AST walker.

This is a rich-text *adapter* in the "one walker, N node-shape adapters" shape of
CONNECTOR_FRAMEWORK_DESIGN.md §3.2 — a sibling to the html/docx/xliff filters, but
for Jira's ProseMirror-derived JSON tree rather than a file the user uploads. It is
NOT a `Filter` subclass (ADF is never resolved by file extension); it exposes the
§3.2 walk/reinsert contract as two plain functions the Jira connector composes:

    extract(doc)                -> AdfExtract(units, tree)
    reinsert(tree, translations)-> new ADF doc (deep-cloned, leaves replaced)

The §3.2 contract, applied to ADF:
  - **leaf `text` node** -> a translatable unit. Its `marks` (bold/em/link/code/
    strike/subsup/textColor/underline) are preserved byte-identical: we translate
    ONLY `node["text"]`, never the marks. A `link` mark's `href` is a mark attr, so
    it stays intact automatically — the link TEXT (the leaf) translates, the href
    does not.
  - **non-translatable inline nodes** — `mention`, `emoji`, `inlineCard`, `date`,
    `status`, `media`, `mediaSingle`, `mediaGroup`, `hardBreak`, `rule` — are
    preserved verbatim: the walker records NO unit for them and copies them
    byte-identical on reinsert.
  - **codeBlock**: structure preserved, but the code text is NOT translated (its
    child text leaves are skipped) — code content must survive untouched.
  - **structure** — `paragraph`, `bulletList`, `orderedList`, `listItem`, `table`,
    `tableRow`, `tableCell`, `tableHeader`, `panel`, `blockquote`, `heading`,
    `doc` — preserved exactly (nodeType/attrs/nesting), we only recurse.

Reinsert is deep-clone-of-source + leaf replacement at recorded paths (never a
rebuild-from-scratch), so any node shape we did not explicitly model still round-
trips intact. The integrity guard (`structure_ok`) mirrors the DOCX/XLIFF
tag-count round-trip convention: if the reinserted tree's node count or leaf-unit
count diverges from the source, the caller falls back to keeping the original and
surfaces a per-pair error (needs_review-style) rather than writing corrupted ADF.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

# Inline nodes that carry meaning in an id/attrs, not in prose — never translate,
# never descend for text. Preserved byte-identical on reinsert (they are copied by
# the deep-clone; the walker simply records no unit for them).
_OPAQUE_INLINE = frozenset(
    {
        "mention",
        "emoji",
        "inlineCard",
        "blockCard",
        "embedCard",
        "date",
        "status",
        "media",
        "mediaSingle",
        "mediaGroup",
        "mediaInline",
        "hardBreak",
        "rule",
        "placeholder",
    }
)

# Block nodes whose *text* must not be translated (code content stays verbatim),
# even though the node itself and its structure are preserved.
_CODE_BLOCKS = frozenset({"codeBlock"})


@dataclass
class AdfUnit:
    """One translatable text leaf, tagged with the path to reinsert it.

    `path` is the sequence of `content`-array indices from the doc root to this
    text node — the exact address reinsert() walks to replace `text`. `marks` is
    carried only for reference/tests; reinsert never touches marks (they live on
    the cloned node already)."""

    p_index: int
    text: str
    path: tuple = ()
    marks: list = field(default_factory=list)


@dataclass
class AdfExtract:
    units: list  # list[AdfUnit]
    tree: dict  # the (unmodified) source ADF doc


def _is_text_leaf(node) -> bool:
    return (
        isinstance(node, dict) and node.get("type") == "text" and isinstance(node.get("text"), str)
    )


def _walk(node, path: tuple, out: list, in_code: bool) -> None:
    """Depth-first walk collecting text-leaf units. `in_code` propagates down a
    codeBlock subtree so code text leaves are structurally preserved but recorded
    as NON-translatable (no unit)."""
    if not isinstance(node, dict):
        return
    ntype = node.get("type")
    if ntype in _OPAQUE_INLINE:
        return  # opaque: no unit, do not descend (preserved verbatim by the clone)
    if _is_text_leaf(node):
        if not in_code:
            out.append(
                AdfUnit(
                    p_index=len(out), text=node["text"], path=path, marks=node.get("marks") or []
                )
            )
        return
    entering_code = in_code or (ntype in _CODE_BLOCKS)
    content = node.get("content")
    if isinstance(content, list):
        for i, child in enumerate(content):
            # Record the FULL alternating path ("content", i) at every level so
            # _node_at can walk it uniformly (each index is into a `content` array).
            _walk(child, path + ("content", i), out, entering_code)  # noqa: RUF005


def extract(doc) -> AdfExtract:
    """Walk an ADF document, emitting one AdfUnit per translatable text leaf.

    Accepts a dict ADF doc (or None / a bare string, which yields no units so the
    caller can fall back to plain-text handling)."""
    if not isinstance(doc, dict):
        return AdfExtract(units=[], tree=doc if isinstance(doc, dict) else {})
    out: list = []
    content = doc.get("content")
    if isinstance(content, list):
        for i, child in enumerate(content):
            _walk(child, ("content", i), out, in_code=False)
    return AdfExtract(units=out, tree=doc)


def _node_at(tree: dict, path: tuple):
    """Resolve a node by the (('content', i), ...) path produced by extract."""
    node = tree
    it = iter(path)
    for key in it:
        # path alternates: "content" (the array key) then an int index.
        arr = node.get(key)
        try:
            idx = next(it)
        except StopIteration:
            return None
        if not isinstance(arr, list) or not (0 <= idx < len(arr)):
            return None
        node = arr[idx]
    return node


def reinsert(tree: dict, translations: dict) -> dict:
    """Deep-clone the source ADF and replace each recorded leaf's `text` with its
    translation. `translations` maps p_index (int OR str, tolerant of the
    segment-store's str keys) -> translated text. Leaves without a translation keep
    their source text. Marks/structure/opaque nodes ride along on the clone."""
    clone = copy.deepcopy(tree)
    units = extract(clone).units  # re-walk the clone -> same paths, same order
    for u in units:
        new = translations.get(u.p_index)
        if new is None:
            new = translations.get(str(u.p_index))
        if new is None:
            continue
        node = _node_at(clone, u.path)
        if _is_text_leaf(node):
            node["text"] = new
    return clone


def _count_nodes(node) -> int:
    """Total node count over the ADF tree — the structure-integrity fingerprint
    (analogue of the DOCX/XLIFF tag count)."""
    if not isinstance(node, dict):
        return 0
    n = 1
    content = node.get("content")
    if isinstance(content, list):
        for c in content:
            n += _count_nodes(c)
    return n


def structure_ok(source: dict, target: dict) -> bool:
    """Round-trip integrity guard (DOCX/XLIFF convention): the reinserted target
    must have the SAME total node count AND the SAME number of translatable text
    leaves as the source. A divergence means the clone-and-replace produced a
    structurally different tree (only possible if a caller hand-built the target
    rather than using reinsert, or the source was mutated mid-flight) — the caller
    treats that as needs_review and keeps the original. Text-content differences do
    NOT trip this (translation changes text, not structure)."""
    if not isinstance(source, dict) or not isinstance(target, dict):
        return False
    if _count_nodes(source) != _count_nodes(target):
        return False
    return len(extract(source).units) == len(extract(target).units)


# ---------------------------------------------------------------------------
# Inline-fidelity units (Issue #644): ONE translatable unit per inline container
# (a paragraph, heading, table cell paragraph, ...) instead of one per text leaf.
#
# A bold span or a link used to split a sentence into several segments that were
# translated without their sentence, which breaks word order in languages like
# Japanese. Now the whole sentence is one segment, with the same placeholder tokens
# the HTML/DOCX filters use: a marked text leaf becomes ⟦gN⟧text⟦/gN⟧ and an opaque
# inline node (mention, emoji, hardBreak, ...) a point token ⟦xN⟧. reinsert_inline()
# rebuilds the container from the translated string, copying each leaf's marks and
# each opaque node from the SOURCE tree. A container whose translated tokens do not
# match the source is reported (never guessed at) so the caller keeps the original.
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"⟦(/?)([gx])(\d+)⟧")


@dataclass
class AdfInlineUnit:
    """One translatable inline container. `path` addresses the container node;
    `text` is its content with placeholder tokens; `tokens` maps token number ->
    ("g", marks) for a marked leaf or ("x", node) for an opaque inline node."""

    p_index: int
    text: str
    path: tuple = ()
    tokens: dict = field(default_factory=dict)


def _is_inline_child(node) -> bool:
    return _is_text_leaf(node) or (isinstance(node, dict) and node.get("type") in _OPAQUE_INLINE)


def _walk_inline(node, path: tuple, out: list, in_code: bool) -> None:
    if not isinstance(node, dict) or node.get("type") in _OPAQUE_INLINE:
        return
    ntype = node.get("type")
    content = node.get("content")
    if not isinstance(content, list):
        return
    in_code = in_code or (ntype in _CODE_BLOCKS)
    has_text = any(_is_text_leaf(c) for c in content)
    if has_text and all(_is_inline_child(c) for c in content):
        if not in_code:
            parts: list = []
            tokens: dict = {}
            n = 0
            for c in content:
                if _is_text_leaf(c):
                    marks = c.get("marks") or []
                    if marks:
                        n += 1
                        tokens[n] = ("g", copy.deepcopy(marks))
                        parts.append(f"⟦g{n}⟧{c['text']}⟦/g{n}⟧")
                    else:
                        parts.append(c["text"])
                else:
                    n += 1
                    tokens[n] = ("x", copy.deepcopy(c))
                    parts.append(f"⟦x{n}⟧")
            out.append(
                AdfInlineUnit(p_index=len(out), text="".join(parts), path=path, tokens=tokens)
            )
        return
    for i, child in enumerate(content):
        _walk_inline(child, path + ("content", i), out, in_code)  # noqa: RUF005


def extract_inline(doc) -> list:
    """One AdfInlineUnit per inline container of an ADF doc, in document order. A
    container that mixes non-inline children with text falls back to recursing, so
    nothing is ever dropped."""
    out: list = []
    if isinstance(doc, dict):
        _walk_inline(doc, (), out, False)
    return out


def _rebuild_inline(unit: AdfInlineUnit, translated: str):
    """New `content` list for the container, or None when the translated tokens do not
    match the source's (a dropped, added or crossed token)."""
    expected = sorted((kind, num) for num, (kind, _v) in unit.tokens.items())
    found_open = [
        (m.group(2), int(m.group(3))) for m in _TOKEN_RE.finditer(translated) if not m.group(1)
    ]
    found_close = [
        (m.group(2), int(m.group(3))) for m in _TOKEN_RE.finditer(translated) if m.group(1)
    ]
    if sorted(found_open) != expected:
        return None
    if sorted(found_close) != sorted(k for k in expected if k[0] == "g"):
        return None
    if _TOKEN_RE.sub("", translated).count("⟦") or _TOKEN_RE.sub("", translated).count("⟧"):
        return None
    out: list = []
    marks_stack: list = []
    pos = 0

    def emit(text, marks):
        if text:
            node = {"type": "text", "text": text}
            if marks:
                node["marks"] = copy.deepcopy(marks)
            out.append(node)

    for m in _TOKEN_RE.finditer(translated):
        emit(translated[pos : m.start()], marks_stack[-1][1] if marks_stack else None)
        pos = m.end()
        closing, kind, num = m.group(1), m.group(2), int(m.group(3))
        if kind == "x":
            out.append(copy.deepcopy(unit.tokens[num][1]))
        elif not closing:
            if marks_stack:
                return None  # nested marked spans would silently drop the outer marks
            marks_stack.append((num, unit.tokens[num][1]))
        else:
            if not marks_stack or marks_stack[-1][0] != num:
                return None  # crossed or unopened pair
            marks_stack.pop()
    if marks_stack:
        return None
    emit(translated[pos:], None)
    return out


def reinsert_inline(tree: dict, translations: dict) -> tuple:
    """Deep-clone the source ADF and replace each inline container's content with its
    translation. Returns (new_tree, bad) where `bad` lists the p_index of every unit
    whose translated tokens did not match the source: those containers keep the SOURCE
    content and the caller must treat the field as failed (keep-original)."""
    clone = copy.deepcopy(tree)
    bad: list = []
    for u in extract_inline(clone):
        new = translations.get(u.p_index)
        if new is None:
            new = translations.get(str(u.p_index))
        if new is None:
            continue
        rebuilt = _rebuild_inline(u, new)
        node = _node_at(clone, u.path) if u.path else clone
        if rebuilt is None or not isinstance(node, dict):
            bad.append(u.p_index)
            continue
        node["content"] = rebuilt or node["content"]
    return clone, bad


def _skeleton(node) -> list:
    """Node types of every NON-text node, depth first: the structure fingerprint of an
    inline-rebuilt tree (text leaves may legitimately merge or split)."""
    if not isinstance(node, dict):
        return []
    out = [] if node.get("type") == "text" else [node.get("type")]
    content = node.get("content")
    if isinstance(content, list):
        for c in content:
            out.extend(_skeleton(c))
    return out


def structure_ok_inline(source: dict, target: dict) -> bool:
    """Round-trip guard for an inline-rebuilt tree: same non-text node skeleton."""
    return (
        isinstance(source, dict)
        and isinstance(target, dict)
        and _skeleton(source) == _skeleton(target)
    )


def plain_text(doc) -> str:
    """The legacy flattener's replacement for read-side callers that genuinely want
    a plain string (e.g. a title preview). Concatenates translatable leaves with a
    space so it never returns code content or opaque-node ids as prose."""
    if isinstance(doc, str):
        return doc
    return " ".join(u.text for u in extract(doc).units)
