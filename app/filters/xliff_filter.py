"""XLIFF filter — the localization industry's bilingual interchange standard.

Every serious CAT/TMS reads and writes XLIFF; supporting it lets Localization OS sit
in the middle of an existing toolchain. We handle both 1.2 (``<trans-unit>`` with
``<source>``/``<target>``) and 2.0 (``<unit><segment>`` with the same children),
namespace-agnostically.

parse(): each ``<source>`` becomes a translatable unit (document order).
build(): write each translation into the sibling ``<target>`` of that source,
creating the ``<target>`` if it doesn't exist. Everything else in the file —
metadata, notes, structure — is preserved.

Inline-tag fidelity (Phase 5 chunk 6): a source may carry inline elements
(``<g>``/``<x>``/``<bpt>``/``<ept>``/``<ph>``/``<mrk>``). parse() replaces each with
a placeholder token (⟦g1⟧…⟦/g1⟧ for content-wrapping elements, ⟦x1⟧ for
standalone ones — see services/segment) and stashes the original element in the
TextUnit side-channel; build() rebuilds those inline elements inside the
``<target>`` with the translated text placed correctly and all original
attributes (id/ctype/…) preserved verbatim. A target with no inline tokens is
plain text, exactly as before.
"""

import re
from copy import deepcopy

from lxml import etree

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
    xml_like,
)
from app.services.segment import (
    is_point_kind,
    ph_close,
    ph_free,
    ph_open,
    ph_point,
    strip_placeholders,
)

# Standalone inline elements get a single point token (⟦xN⟧); everything else
# (content-wrapping: 1.2 <g>/<mrk>, 2.0 <pc>, or any unknown) gets paired
# open/close tokens and is recursed into. Namespace-agnostic on localname.
_POINT = {"x", "ph", "bpt", "ept", "it", "sc", "ec", "sm", "em"}
_FREE_POINT = {"x", "ph"}
# An <x>/<ph> that stands for one side of a formatting pair (Angular `START_BOLD_TEXT` /
# `CLOSE_BOLD_TEXT`, `equiv-text="<b>"`, ctype bold/italic/underline/link) must keep its order.
_PAIR_SIDE_ID = re.compile(r"(?:START|CLOSE)_")
_PAIR_CTYPES = {"bold", "italic", "underline", "link"}


# Gate (#693): the native code an <x>/<ph> stands for can itself be order-bound: an
# UNNUMBERED printf specifier (`%s`, `%d`, `%@`) or an empty `{}` takes the next argument,
# so moving it swaps the values. Such a code keeps ⟦xN⟧ whatever its id says.
_ORDER_BOUND_CODE = re.compile(
    r"%(?!\d+\$)[-+ #0']*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|q|L|z|t|j|I64|I32|I)?"
    r"[@diouxXcCsSpaAeEfFgG]|\{\}"
)


def _native_codes(el, data_refs: dict) -> list[str]:
    """What an <x>/<ph> stands for, as far as the file says: 1.2 `equiv-text` / 1.2 <ph>
    content, 2.0 `equiv` / `disp`, and a 2.0 `dataRef` resolved in the unit's
    <originalData> (one dict per unit, built once, so the walk stays linear)."""
    codes = [el.get("equiv-text"), el.get("equiv"), el.get("disp"), el.text]
    ref = el.get("dataRef")
    if ref:
        unit = next((a for a in el.iterancestors() if _local(a) == "unit"), None)
        if unit is not None:
            # keyed by the element itself: holding it keeps lxml's proxy (an id() key
            # missed on every call once the proxy was freed, a quadratic walk)
            table = data_refs.get(unit)
            if table is None:
                table = {}
                for d in unit.iter():
                    if isinstance(d.tag, str) and _local(d) == "data" and d.get("id"):
                        table.setdefault(d.get("id"), "".join(d.itertext()))
                data_refs[unit] = table
            codes.append(table.get(ref))
    return [c.strip() for c in codes if c and c.strip()]


def _free_point(el, local: str, data_refs: dict | None = None) -> bool:
    """#693: an <x id>/<ph id> placeholder that may move freely in the translation."""
    if local not in _FREE_POINT or el.get("id") is None:
        return False
    if _PAIR_SIDE_ID.match(el.get("id") or "") or _PAIR_SIDE_ID.match(el.get("equiv") or ""):
        return False
    if (el.get("ctype") or "").lower() in _PAIR_CTYPES:
        return False
    for code in _native_codes(el, {} if data_refs is None else data_refs):
        # a markup side (`<b>`, `</i>`) or an unnumbered argument keeps its order; a
        # self-closing element (`<br/>`, `<img .../>`) is a point and may move
        side = code.startswith("<") and not code.endswith("/>")
        if side or _ORDER_BOUND_CODE.fullmatch(code):
            return False
    return True


def check_markup_budget(raw: bytes, what: str = "This XLIFF file") -> None:
    """The one pre-parse budget for untrusted XLIFF (document upload #571, vendor return
    #576 gate). Raises InputLimitError before lxml builds a tree.

    #571: lxml builds the whole tree (roughly ten times the file) before any unit is
    counted. Two closing-tag endings per <source> pair bound the unit count at C speed;
    refuse at twice the per-document cap so only a hostile file ever trips it.
    #571 gate: elements that are not <source> (notes, inline tags, alt-trans) still each
    cost a tree node and Python work; forty markup characters per allowed segment
    (2000000 at the default) is far above a real file and bounds the tree."""
    if (
        raw.count(b"source>") // 2 > 2 * config.MAX_DOCUMENT_SEGMENTS
        or raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS
    ):
        raise InputLimitError(
            f"{what} has far more units than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS}). Split it into smaller files."
        )


def _parse_xml(data: bytes, capped: bool = False):
    """Parse untrusted XLIFF with the SAME hardened posture as services/vendor_xliff
    (P-B4). A document-filter .xlf upload is exactly as untrusted as a vendor return, so
    it gets the same defence: reject a DOCTYPE / internal-or-external DTD outright (byte
    prescan + charset-independent post-parse assert), then parse with a parser that
    resolves NO external entities, loads NO DTD, hits NO network and refuses a huge tree.
    An XXE / billion-laughs payload therefore cannot fetch a file, expand an entity or
    exhaust memory. Reuses vendor_xliff's seam so the two parse paths cannot drift.

    Raises vendor_xliff.XliffParseError (a ValueError) on a DOCTYPE, an undefined/DTD-
    dependent entity, or any not-well-formed XML -- caught by the upload route as a polite
    parse refusal, never a 500. Wrapping XMLSyntaxError into the SAME XliffParseError
    vendor_xliff raises keeps the two parse paths' refusal vocabulary identical, and means
    the hardened parser's own refusal (e.g. an entity it will not resolve) surfaces
    cleanly even when the ASCII DOCTYPE prescan does not fire."""
    from app.services.vendor_xliff import (
        XliffParseError,
        _assert_no_dtd,
        _make_parser,
        _reject_doctype,
    )

    raw = bytes(data) if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    _reject_doctype(raw)  # ASCII fast path
    if capped:
        check_markup_budget(raw)
    try:
        root = etree.fromstring(raw, parser=_make_parser())
    except etree.XMLSyntaxError as exc:
        raise XliffParseError(f"XLIFF is not well-formed XML: {exc}") from exc
    _assert_no_dtd(root)  # charset-independent authority (catches a UTF-16 DOCTYPE)
    return root


def _local(el) -> str:
    tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
    return tag.lower()


def _source_elements(root):
    """All <source> elements whose parent is a trans-unit (1.2) or segment (2.0),
    in document order — the order parse() and build() must agree on."""
    out = []
    for el in root.iter():
        if _local(el) == "source":
            parent = el.getparent()
            if parent is not None and _local(parent) in ("trans-unit", "segment"):
                out.append(el)
    return out


def _protected(src) -> bool:
    """True when this <source>'s unit must never be translated or overwritten."""
    from app.filters.cat_xliff import _unit_skip

    tu = src.getparent()
    return bool(_unit_skip(tu, next((c for c in tu if _local(c) == "target"), None)))


def _translate_no(src) -> bool:
    """True when the unit holding this <source> is marked translate="no": the 1.2
    <trans-unit> (its parent) or the 2.0 <unit> (the parent of its <segment>)."""
    parent = src.getparent()
    holder = parent if _local(parent) == "trans-unit" else parent.getparent()
    return holder is not None and (holder.get("translate") or "").strip().lower() == "no"


def _copy_work(src, limit: int) -> int:
    """How many element copies `_tokenize_source` makes for this <source>: each inline
    element is deep-copied with its whole subtree, so an element is copied once for
    itself and once for every inline ancestor (#571 gate). Counted in one linear pass
    that stops as soon as the count passes `limit`."""
    work = 0
    stack = [(c, 1) for c in src]
    while stack and work <= limit:
        el, depth = stack.pop()
        work += depth
        stack.extend((c, depth + 1) for c in el)
    return work


def _tokenize_source(src):
    """Return (tokenized_text, inline_map) for a <source>.

    inline_map[str(n)] = the original inline element (cloned) so build() can
    reproduce its tag, namespace and attributes verbatim. Content-wrapping
    elements (<g>, <mrk>) become ⟦gN⟧inner⟦/gN⟧; standalone elements become the
    point token ⟦xN⟧ (their own text/children are carried on the clone)."""
    inline_map: dict = {}
    counter = [0]
    data_refs: dict = {}  # #693 gate: per-unit 2.0 <originalData> lookups

    def walk(el, is_root) -> str:
        parts: list[str] = []
        if not is_root:
            local = _local(el)
            counter[0] += 1
            n = counter[0]
            inline_map[str(n)] = deepcopy(el)
            if local in _POINT:
                # standalone placeholder — do NOT recurse into its content. #693: an
                # `<x id>` / `<ph id>` is rebuilt by its number from inline_map, so the
                # translation may move it (⟦pN⟧); begin/end codes (bpt/ept/it/sc/ec/sm/em)
                # and an <x>/<ph> that opens or closes markup keep ⟦xN⟧ and their order.
                if _free_point(el, local, data_refs):
                    return ph_free(n) + (el.tail or "")
                return ph_point("x", n) + (el.tail or "")
            # content-wrapping element (g / mrk / unknown): recurse for inner text
            inner = "".join(walk(c, False) for c in el)
            return ph_open("g", n) + (el.text or "") + inner + ph_close("g", n) + (el.tail or "")
        # root <source>: emit its own text, then children, then their tails
        parts.append(el.text or "")
        for c in el:
            parts.append(walk(c, False))
        return "".join(parts)

    text = walk(src, True)
    return text, inline_map


def _clear(el) -> None:
    el.text = None
    for c in list(el):
        el.remove(c)


def _rebuild_target(tgt, text: str, inline_map: dict) -> None:
    """Populate <target> from tokenized translated text + the stored elements.

    Walks the tokens in order; text between/around tokens becomes element .text or
    .tail as appropriate. The translated <target> is NOT trusted to carry the same
    tokens as the source, so we validate each token against the source inline_map
    and degrade safely instead of corrupting the XML:

      - a token with NO matching source-map entry (unknown id) is DROPPED — never
        emitted as literal ⟦…⟧ text (which would leak into the document and, for an
        id already materialized, duplicate the id attribute → schema-invalid).
      - a source id is materialized AT MOST ONCE; a second open/point token for the
        same id is dropped (a duplicated element would repeat the id attribute).
      - a stray close (no matching open on the stack) is dropped, not kept literal.

    Surrounding translated text always survives; only the invalid token markers are
    removed. A mismatch is surfaced separately by the QA placeholder-integrity check
    (services/qa.check_segment), so the editor still flags it for the linguist."""
    import re

    from app.services.segment import _PLACEHOLDER_RE

    _clear(tgt)

    # Ids already turned into elements — a second reference must not re-emit the id.
    used: set[str] = set()
    # Stack of frames (element_or_None, close_key, phantom) for currently-open
    # paired tokens. A "phantom" frame is a dropped open (unknown/duplicate id): it
    # keeps no element and exists only so its matching close is consumed cleanly;
    # its span's text flows into the enclosing real container inline.
    stack: list[tuple] = []
    last = {"node": None}

    def container_for_text():
        for el, _key, phantom in reversed(stack):
            if not phantom:
                return el
        return tgt

    def append_text(chunk: str):
        if not chunk:
            return
        if last["node"] is None:
            c = container_for_text()
            if len(c):
                # Text after a closed PHANTOM frame continues at the end of the container,
                # i.e. as the tail of its last child, never before already-emitted
                # children (#509).
                c[-1].tail = (c[-1].tail or "") + chunk
            else:
                c.text = (c.text or "") + chunk
        else:
            last["node"].tail = (last["node"].tail or "") + chunk

    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        append_text(text[pos : m.start()])
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            # close token: pop matching open frame if any; else drop it.
            key = re.sub(r"^/[a-z]?", "", inner)
            if stack and stack[-1][1] == key:
                node, _key, phantom = stack.pop()
                # After a real element closes, following text is its .tail. After a
                # phantom closes, text continues in the enclosing container (unchanged).
                last["node"] = None if phantom else node
            # stray/unknown close -> drop marker (keep any surrounding text)
            continue
        # open / point token
        is_point = is_point_kind(inner[:1])
        key = re.sub(r"^[a-z]?", "", inner, count=1)
        el = inline_map.get(key)
        if el is None or key in used:
            # unknown id, or this id was already materialized -> drop the marker so
            # we never emit literal ⟦…⟧ or a duplicate id attribute. For a paired
            # open, push a phantom frame so its matching close is consumed instead
            # of leaking as a stray close; its inner text stays inline.
            if not is_point:
                stack.append((None, key, True))
            continue
        used.add(key)
        clone = deepcopy(el)
        clone.tail = None  # tails are carried as literal text in the token stream
        container_for_text().append(clone)
        if is_point:
            # point element: its own text/children came with the clone; following
            # text continues as this element's tail
            last["node"] = clone
        else:
            # paired open: clear the clone's copied content, nest into it
            _clear(clone)
            stack.append((clone, key, False))
            last["node"] = None  # next text is the clone's .text
    append_text(text[pos:])


@register
class XliffFilter(Filter):
    fmt = "xliff"
    exts = ("xlf",)
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        from app.filters import cat_xliff

        root = _parse_xml(data, capped=True)
        if cat_xliff.has_seg_source(root):
            # XLIFF 1.2 with <seg-source> segmentation: one unit per seg mrk, and the
            # target is written back mirroring that segmentation (cat_xliff).
            return cat_xliff.parse_units(data, "xliff")
        units: list[TextUnit] = []
        # #571 gate: 250 nested <g> around a million <x/> is a 4 MB file that asked for
        # 250 million element copies (minutes and tens of gigabytes). Budgeted before any
        # copy is made (ten copies per allowed segment); export of a stored document is
        # not budgeted.
        budget = 10 * config.MAX_DOCUMENT_SEGMENTS
        for idx, src in enumerate(_source_elements(root)):
            # Protected units (translate="no", approved="yes", a final/signed-off target,
            # vendor locks) are skipped but keep their index; build() never writes them.
            if _protected(src):
                continue
            budget -= _copy_work(src, budget)
            if budget < 0:
                raise InputLimitError(
                    "This XLIFF file has far more inline tags than MAX_DOCUMENT_SEGMENTS "
                    f"allows ({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into "
                    "smaller files."
                )
            text, inline_map = _tokenize_source(src)
            text = text.strip()
            # Skip a whitespace-only <source> (an empty <source/> etc.): it carries no
            # translatable content, and in FULL_TEXT mode a blank unit becomes a blank
            # "\n\n" part that desyncs the export count against the source body (honest-
            # failure delta review, 2026-08-13). SAFE for the build/merge round-trip:
            # `p_index` is the source element's DOCUMENT POSITION (this enumerate idx),
            # which build() re-derives the same way (`enumerate(_source_elements)`), so a
            # skipped blank source simply has no mapping entry and build() leaves its
            # <target> untouched — no renumbering of the surviving units, old or new.
            if not text:
                continue
            # A source that is ONLY inline codes (Storyline's empty paragraph blocks:
            # <bpt/><ph>&lt;Style /&gt;</ph><ept/>) has nothing to translate; sending it to
            # MT only invites text into an empty block. Same skip-by-position contract as
            # the blank source above: build() leaves its <target> as it is.
            if not strip_placeholders(text).strip():
                continue
            # translate="no" on the 1.2 <trans-unit> / 2.0 <unit> (both specs define the
            # attribute there): the unit is not for translation.
            if _translate_no(src):
                continue
            units.append(TextUnit(p_index=idx, text=text, inline_map=inline_map))
            check_segment_cap(len(units))  # #571
        skeleton = {
            "format": "xliff",
            "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        from app.filters import cat_xliff
        from app.filters.xml_text import xml_target_text

        root = _parse_xml(original)
        if cat_xliff.has_seg_source(root):
            return cat_xliff.build_units(original, paragraph_texts)
        for k, src in enumerate(_source_elements(root)):
            new = paragraph_texts.get(str(k))
            if new is None or _protected(src):
                # A protected unit is never overwritten, even for a document stored
                # before the parse learned to skip it.
                continue
            parent = src.getparent()
            ns = etree.QName(src).namespace
            tgt = next((c for c in parent if _local(c) == "target"), None)
            if tgt is None:
                tag = f"{{{ns}}}target" if ns else "target"
                tgt = etree.SubElement(parent, tag)
                src.addnext(tgt)
            # Re-derive the token->element map from the ORIGINAL <source> so we can
            # rebuild inline elements without persisting them through the DB.
            _, inline_map = _tokenize_source(src)
            _rebuild_target(tgt, xml_target_text(new), inline_map)
            if (tgt.get("state") or "").strip().lower() in ("new", "needs-translation"):
                tgt.set("state", "translated")  # the CAT tool shows it as translated
        # B-bytes: the original's declaration, BOM, line endings and trailing newline
        return xml_like(original, etree.tostring(root, xml_declaration=True, encoding="UTF-8"))

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the TARGET language: XLIFF 1.2 `target-language` on every
        <file>, XLIFF 2.0 `trgLang` on the root. Nothing else changes."""
        from app.filters import a11y

        out = self.build(original, paragraph_texts)
        tag = a11y.target_tag(target_locale)
        if not tag:
            return out
        root = _parse_xml(out)
        if (root.get("version") or "").startswith("2"):
            root.set("trgLang", tag)
        else:
            for el in root.iter():
                if isinstance(el.tag, str) and _local(el) == "file":
                    el.set("target-language", tag)
        return xml_like(original, etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
