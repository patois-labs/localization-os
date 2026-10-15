"""Notion block-tree rich-text walker (SPEC-t3-notion-helpscout).

A rich-text *adapter* in the "one walker, N node-shape adapters" shape of
CONNECTOR_FRAMEWORK_DESIGN.md §3.2 — a sibling to adf.py (Jira's ProseMirror JSON),
but for Notion's recursive block tree of `rich_text[]` arrays. It is NOT a `Filter`
subclass (a Notion tree is a JSON structure from an API, resolved by connector, not
by file extension); it exposes the §3.2 walk/reinsert contract as three plain
functions the Notion connector composes:

    extract(blocks)                 -> NotionExtract(units, tree)
    reinsert(blocks, translations)  -> new block list (deep-cloned, leaves replaced)
    structure_ok(source, target)    -> bool (block-count integrity guard)

The §3.2 contract, applied to Notion (§3.3):
  - **`rich_text` text run** (`{type:"text", text:{content, link}, annotations,
    plain_text, href}`) -> a translatable unit. We translate ONLY `text.content`
    (and mirror it into `plain_text`); the run's `annotations` (bold/italic/
    strikethrough/underline/code/color) and `text.link`/`href` are preserved
    byte-identical. Naive concatenation of runs would destroy formatting
    boundaries, so each run stays its own unit.
  - **2000-char cap**: Notion rejects a `rich_text` run whose `content` exceeds
    2000 chars, so a translated run over the cap is SPLIT across multiple runs, each
    carrying the SAME annotations/link/href (so the split is visually seamless).
  - **code blocks** (`code`): structure preserved, but the code TEXT (`rich_text`)
    is NOT translated (its runs are skipped) — code content must survive untouched.
    Its `caption` (an image/code/embed caption is translatable prose, not code)
    IS still walked/reinserted. The `in_code` flag propagates into children.
  - **non-text runs** (`mention`, `equation`): preserved verbatim (no unit).
  - **structure** — block `type`, ids, `annotations`, `color`, `language`, nesting —
    preserved exactly; reinsert only rewrites `text.content` inside `rich_text`
    arrays, never the tree shape.
  - **containers**: `table_row.cells` (a list of rich_text arrays) and block
    `caption` arrays are walked too; nested `children` are recursed.

Reinsert is deep-clone-of-source + per-run rewrite (never a rebuild-from-scratch),
so any block shape not explicitly modelled still round-trips intact. `structure_ok`
mirrors the ADF/DOCX/XLIFF convention but counts BLOCK NODES (not runs), because
the 2000-char split deliberately changes the run count while leaving the block tree
identical.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

# Blocks whose text runs must NOT be translated (code content stays verbatim). The
# block + its structure are preserved; only its runs are skipped and `in_code`
# propagates to children.
_CODE_TYPES = frozenset({"code"})

# rich-text-bearing array keys on a block's type-object (besides table_row.cells,
# handled specially). `caption` carries image/code/embed captions.
_TEXT_ARRAY_KEYS = ("rich_text", "caption")

# Notion's hard per-run content cap (docs_platforms.md §3.3).
RICH_TEXT_MAX = 2000


@dataclass
class NotionUnit:
    """One translatable text run, tagged with its walk-order index.

    `p_index` is the 0-based order the walker emits units in — the key the flow
    engine's segment map + reinsert use to line a translation back up with its run.
    `annotations`/`link` are carried for reference/tests; reinsert reads them off
    the cloned run itself, never from here."""

    p_index: int
    text: str
    annotations: dict = field(default_factory=dict)
    link: dict | None = None


@dataclass
class NotionExtract:
    units: list  # list[NotionUnit]
    tree: list  # the (unmodified) source block list


def _is_text_run(el) -> bool:
    return (
        isinstance(el, dict)
        and el.get("type") == "text"
        and isinstance(el.get("text"), dict)
        and isinstance(el["text"].get("content"), str)
    )


def _container(block):
    """The block's type-object (`block[block['type']]`), or None."""
    if not isinstance(block, dict):
        return None
    ntype = block.get("type")
    c = block.get(ntype) if ntype else None
    return c if isinstance(c, dict) else None


def _text_arrays(container, *, include_code_text: bool = True) -> list:
    """Every rich_text array in a container, in deterministic order, as the ACTUAL
    list objects (so reinsert can rewrite them in place). Handles table_row.cells
    (a list of rich_text arrays) plus rich_text/caption.

    `include_code_text=False` is passed when walking/reinserting INSIDE a code
    block (FINDING 3 fix): the code's own `rich_text` (the code content) must stay
    untouched, but its `caption` (an image/code/embed caption is translatable
    prose, not code) still walks. Only the code block's OWN rich_text is skipped —
    a nested/sibling block's rich_text is unaffected by this flag."""
    arrays: list = []
    cells = container.get("cells")
    if isinstance(cells, list):  # table_row
        for cell in cells:
            if isinstance(cell, list):
                arrays.append(cell)
    for key in _TEXT_ARRAY_KEYS:
        if key == "rich_text" and not include_code_text:
            continue
        arr = container.get(key)
        if isinstance(arr, list):
            arrays.append(arr)
    return arrays


def _walk(blocks, out: list, in_code: bool) -> None:
    if not isinstance(blocks, list):
        return
    for block in blocks:
        container = _container(block)
        if container is None:
            continue
        ntype = block.get("type")
        this_code = in_code or (ntype in _CODE_TYPES)
        # FINDING 3 fix: a code block's `rich_text` (the code content) must NOT be
        # translated, but its `caption` still should — include_code_text=False
        # drops only rich_text from the arrays walked, caption still walks.
        for arr in _text_arrays(container, include_code_text=not this_code):
            for el in arr:
                if _is_text_run(el):
                    out.append(
                        NotionUnit(
                            p_index=len(out),
                            text=el["text"]["content"],
                            annotations=el.get("annotations") or {},
                            link=(el.get("text") or {}).get("link"),
                        )
                    )
        children = container.get("children")
        if isinstance(children, list):
            _walk(children, out, this_code)


def extract(blocks) -> NotionExtract:
    """Walk a Notion block list, emitting one NotionUnit per translatable text run.

    Accepts a list of block dicts (or None / a non-list, which yields no units so
    the caller can no-op an empty page)."""
    out: list = []
    _walk(blocks if isinstance(blocks, list) else [], out, in_code=False)
    return NotionExtract(units=out, tree=blocks if isinstance(blocks, list) else [])


def _chunks(text: str, size: int) -> list:
    """Split `text` into <= `size`-char pieces (at least one piece, even for '')."""
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]


def _split_run(el: dict, new_text: str) -> list:
    """Replace a run's content with `new_text`, splitting into multiple runs when it
    exceeds the 2000-char cap. Every produced run is a deep copy of the source run,
    so annotations + text.link + href ride along identically (a seamless split)."""
    out = []
    for ch in _chunks(new_text, RICH_TEXT_MAX):
        c = copy.deepcopy(el)
        c["text"] = dict(c.get("text") or {})
        c["text"]["content"] = ch
        c["plain_text"] = ch
        out.append(c)
    return out


def _reinsert(blocks, translations: dict, counter: list, in_code: bool) -> None:
    if not isinstance(blocks, list):
        return
    for block in blocks:
        container = _container(block)
        if container is None:
            continue
        ntype = block.get("type")
        this_code = in_code or (ntype in _CODE_TYPES)
        # FINDING 3 fix: mirror _walk's array selection so counter/p_index stay
        # aligned — a code block's caption still gets its translation spliced back
        # in; its rich_text (the code text) is skipped and left byte-identical.
        for arr in _text_arrays(container, include_code_text=not this_code):
            new_arr = []
            for el in arr:
                if _is_text_run(el):
                    pidx = counter[0]
                    counter[0] += 1
                    new = translations.get(pidx)
                    if new is None:
                        new = translations.get(str(pidx))
                    if new is None:
                        new_arr.append(el)  # untranslated -> keep source run
                    else:
                        new_arr.extend(_split_run(el, new))
                else:
                    new_arr.append(el)  # non-text run preserved verbatim
            arr[:] = new_arr  # rewrite the array in place
        children = container.get("children")
        if isinstance(children, list):
            _reinsert(children, translations, counter, this_code)


def reinsert(blocks, translations: dict):
    """Deep-clone the source blocks and replace each translatable run's content with
    its translation (splitting at the 2000-char cap, preserving annotations/link).
    `translations` maps p_index (int OR str) -> translated text; a run with no
    translation keeps its source content. Structure/annotations/non-text runs ride
    along on the clone."""
    clone = copy.deepcopy(blocks) if isinstance(blocks, list) else []
    _reinsert(clone, translations, [0], in_code=False)
    return clone


def _count_blocks(blocks) -> int:
    """Total block-node count over the tree — the structure fingerprint (the ADF
    node-count analogue). Counts blocks + nested children; run splitting does NOT
    change it (runs aren't blocks)."""
    n = 0
    if not isinstance(blocks, list):
        return 0
    for block in blocks:
        container = _container(block)
        if container is None:
            continue
        n += 1
        children = container.get("children")
        if isinstance(children, list):
            n += _count_blocks(children)
    return n


def structure_ok(source, target) -> bool:
    """Round-trip integrity guard: the reinserted target must have the SAME block-
    node count as the source (reinsert only rewrites run text, never the tree). A
    divergence means the target was hand-built rather than produced by reinsert (or
    the source mutated mid-flight) — the caller treats that as needs_review and keeps
    the original. Run counts are deliberately NOT compared (the 2000-char split
    changes them by design)."""
    return _count_blocks(source) == _count_blocks(target)
