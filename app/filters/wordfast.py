"""Wordfast bilingual files: Wordfast Pro TXML (.txml, also GlobalLink TransStudio) and
Wordfast Pro TXLF (.txlf, XLIFF 1.2 with the gs4tr extension namespace).

TXLF is XLIFF 1.2, so it runs on the cat_xliff engine (one walk shared by parse and
build, a byte-splice build, untranslated units stay empty). The gs4tr rules live in
cat_xliff._unit_skip so they apply to any XLIFF carrying the namespace: `gs4tr:locked=
"true"` and a non-empty target the translator already moved past "needs-*" are never
parsed or overwritten.

TXML has no public specification. The structure here follows Okapi's TXML filter
(Apache-2.0, used as the behaviour spec only) and Wordfast's own help:

    <txml locale=".." targetlocale="..">
      <translatable blockId="1">
        <segment segmentId="1" [locked="true"] [modified="true"] [gtmt="true"]>
          <ws> </ws>?  <source>..<ut ..>..</ut>..</source>  <ws> </ws>?  <target>..</target>?
          <comments>..</comments>?  <revisions>..</revisions>?
        </segment>
      </translatable>
      <localizable ..>..</localizable>     (not extracted, like Okapi)
    </txml>

- One unit per `<segment>` under a `<translatable>` block (key = position among all
  `<segment>` elements under translatable/localizable blocks, protected ones counted).
  `<source>`/`<target>` inside `<revisions>` are history and never read.
- Inline elements in a source (`<ut>` and any other) are protected tokens carrying the
  element's own bytes. A `<ut type="start" x="n">` / `<ut type="end" x="n">` pair becomes
  a paired token so the translation can move the text it wraps; every other element is
  a point token.
- Protected (never parsed, never overwritten): `locked="true"` on the segment or its
  block; a segment in a `<localizable>` block; a non-empty target that is not raw MT
  (`gtmt="true"` and not `modified="true"`) and not a fuzzy match (`score` < 100 and not
  modified). That is someone's confirmed work the pipeline cannot preload for review.
- build() is a byte splice: only the inside of written `<target>` elements changes (a
  missing target is created after the source and its trailing `<ws>`, in the order
  Okapi writes them). Segment attributes are left as they were. A build with nothing to
  write returns the original bytes.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

from dataclasses import dataclass

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    register,
)
from app.filters.cat_xliff import (
    _CatXliff,
    _decode,
    _edges,
    _Edits,
    _has_content,
    _local,
    _render,
    _with_attr,
    scan,
)
from app.services.segment import ph_close, ph_open, ph_point

_BLOCKS = ("translatable", "localizable")


@register
class TxlfFilter(_CatXliff):
    """Wordfast Pro TXLF: XLIFF 1.2 plus gs4tr attributes. No namespace is required (the
    gs4tr URI is detected by name), so a TXLF from any Wordfast Pro version reads."""

    fmt = "txlf"
    exts = ()
    namespace = None


# ------------------------------------------------------------------------- TXML


def _txml_error(exc: Exception) -> Exception:
    msg = str(exc).replace("XLIFF", "TXML")
    return InputLimitError(msg) if isinstance(exc, InputLimitError) else ValueError(msg)


def _parse_txml(raw: bytes, capped: bool = False):
    """The hardened XLIFF parser (no DTD, no entities, no network, DOCTYPE refused) plus
    the TXML root check. Refusals are ValueError / InputLimitError with TXML wording."""
    from app.filters.xliff_filter import _parse_xml, check_markup_budget

    try:
        if capped:
            check_markup_budget(raw, "This TXML file")
        root = _parse_xml(raw)
    except (ValueError, InputLimitError) as exc:
        raise _txml_error(exc) from exc
    if _local(root) != "txml":
        raise ValueError("This is not a Wordfast TXML file (the root element is not <txml>).")
    return root


def _decode_txml(raw: bytes):
    try:
        return _decode(raw)
    except InputLimitError as exc:
        raise _txml_error(exc) from exc


def _true(v) -> bool:
    return (v or "").strip().lower() == "true"


def _kid(el, name: str):
    return next((c for c in el if _local(c) == name), None)


@dataclass
class TSlot:
    key: int
    seg: object
    src: object
    target: object
    skip: str


def _skip(seg, block, target) -> str:
    if _local(block) == "localizable":
        return "localizable"
    if _true(seg.get("locked")) or _true(block.get("locked")):
        return "locked"
    if not _has_content(target):
        return ""
    modified = _true(seg.get("modified"))
    if _true(seg.get("gtmt")) and not modified:
        return ""  # raw machine translation: re-translate
    score = target.get("score") if target.get("score") is not None else seg.get("score")
    if score is not None and not modified:
        try:
            if float(score) < 100:
                return ""  # a fuzzy match: re-translate
        except ValueError:
            pass
    return "confirmed"


def walk_txml(root) -> list[TSlot]:
    slots: list[TSlot] = []
    for seg in root.iter():
        if _local(seg) != "segment":
            continue
        block = seg.getparent()
        if block is None or _local(block) not in _BLOCKS:
            continue  # e.g. history inside <revisions>
        src = _kid(seg, "source")
        if src is None:
            continue
        target = _kid(seg, "target")
        slots.append(TSlot(len(slots), seg, src, target, _skip(seg, block, target)))
    return slots


def _tokenize(src, raw_of=None) -> tuple[str, dict]:
    """(tokenized text, literals). Element children become tokens; comments and PIs in a
    source are dropped (their tail text is kept). `raw_of(el)` returns an element's own
    bytes for build(); parse() stores a serialisation for reference only."""
    from lxml import etree

    kids = [c for c in src if isinstance(c.tag, str)]
    # pair <ut type="start" x="n"> with the next <ut type="end" x="n">
    pair_of: dict[int, int] = {}
    open_x: dict[str, int] = {}
    for i, c in enumerate(kids):
        if _local(c) != "ut":
            continue
        kind, x = (c.get("type") or "").lower(), c.get("x")
        if x is None:
            continue
        if kind == "start":
            open_x[x] = i
        elif kind == "end" and x in open_x:
            j = open_x.pop(x)
            pair_of[i], pair_of[j] = j, i

    def lit(c) -> str:
        return raw_of(c) if raw_of else etree.tostring(c, with_tail=False, encoding="unicode")

    out = [src.text or ""]
    lits: dict = {}
    num_of: dict[int, int] = {}
    n = 0
    i = -1
    for c in src:
        if isinstance(c.tag, str):
            i += 1
            if i in pair_of and pair_of[i] > i:
                n += 1
                num_of[i] = n
                lits[str(n)] = (lit(c), lit(kids[pair_of[i]]))
                out.append(ph_open("g", n))
            elif i in pair_of:
                out.append(ph_close("g", num_of[pair_of[i]]))
            else:
                n += 1
                lits[str(n)] = lit(c)
                out.append(ph_point("x", n))
        out.append(c.tail or "")
    return "".join(out), lits


def parse_txml(data: bytes) -> ParseResult:
    raw = bytes(data)
    _decode_txml(raw)  # refuse an encoding the build could not write back
    root = _parse_txml(raw, capped=True)
    units: list[TextUnit] = []
    skipped: dict[str, int] = {}
    budget = 10 * config.MAX_DOCUMENT_SEGMENTS
    for slot in walk_txml(root):
        if slot.skip:
            skipped[slot.skip] = skipped.get(slot.skip, 0) + 1
            continue
        budget -= 1 + sum(1 for _ in slot.src.iter())
        if budget < 0:
            raise InputLimitError(
                "This TXML file has far more inline tags than MAX_DOCUMENT_SEGMENTS allows "
                f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into smaller files."
            )
        text, lits = _tokenize(slot.src)
        text = text.strip()
        if not text:
            continue
        units.append(TextUnit(p_index=slot.key, text=text, inline_map=lits))
        check_segment_cap(len(units))
    check_unit_texts(u.text for u in units)
    skeleton = {
        "format": "txml",
        "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
    }
    if skipped:
        skeleton["protected_units"] = skipped
    return ParseResult(skeleton=skeleton, units=units)


def build_txml(
    original: bytes,
    paragraph_texts: dict,
    *,
    fallback_keys=None,
    target_locale: str | None = None,
) -> bytes:
    from app.filters.xml_text import xml_target_text

    raw = bytes(original)
    texts = {k: v for k, v in paragraph_texts.items() if k not in (fallback_keys or ())}
    text, codec, bom = _decode_txml(raw)
    root = _parse_txml(raw)
    els = [e for e in root.iter() if isinstance(e.tag, str)]
    raw_spans = scan(text)
    if len(raw_spans) != len(els):
        raise ValueError("TXML markup scan did not match the parsed tree")
    spans = dict(zip(els, raw_spans, strict=True))
    edits = _Edits(text)

    def raw_of(el) -> str:
        sp = spans[el]
        return text[sp.start : sp.end]

    for slot in walk_txml(root):
        new = texts.get(str(slot.key))
        if new is None or slot.skip:
            continue
        src_text, lits = _tokenize(slot.src, raw_of)
        lead, trail = _edges(src_text)
        inner = _render(lead + xml_target_text(new).strip() + trail, lits)
        if slot.target is not None:
            sp = spans[slot.target]
            if sp.empty:
                open_tag = text[sp.start : sp.start_end][:-2].rstrip() + ">"
                edits.add(sp.start, sp.end, open_tag + inner + f"</{sp.name}>")
            else:
                edits.add(sp.start_end, sp.end_start, inner)
            continue
        # No target yet: <source>, then a trailing <ws> if any, then the new <target>.
        anchor = slot.src
        nxt = anchor.getnext()
        while nxt is not None and not isinstance(nxt.tag, str):
            nxt = nxt.getnext()
        if nxt is not None and _local(nxt) == "ws":
            anchor = nxt
        name = spans[slot.src].name
        tname = name[: -len("source")] + "target"
        edits.add(spans[anchor].end, spans[anchor].end, f"<{tname}>{inner}</{tname}>")

    if target_locale and not (root.get("targetlocale") or "").strip():
        from app.filters import a11y

        tag = a11y.target_tag(target_locale)
        if tag and "_" in (root.get("locale") or ""):
            tag = tag.replace("-", "_")  # match the file's own style (en_US)
        if tag:
            sp = spans[root]
            start_tag = _with_attr(text[sp.start : sp.start_end], "targetlocale", tag)
            edits.add(sp.start, sp.end if sp.empty else sp.start_end, start_tag)
    if not edits.items:
        return raw
    out = bom + edits.apply().encode(codec)
    _parse_txml(out)  # the spliced file must still be well-formed TXML
    return out


@register
class TxmlFilter(Filter):
    fmt = "txml"
    exts = ()
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return parse_txml(data)

    def build(self, original: bytes, paragraph_texts: dict, *, fallback_keys=None) -> bytes:
        return build_txml(original, paragraph_texts, fallback_keys=fallback_keys)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys=None,
    ) -> bytes:
        """build() plus `targetlocale` on a <txml> root that has none (an existing one is
        never changed: Wordfast matches it to the project)."""
        return build_txml(
            original, paragraph_texts, fallback_keys=fallback_keys, target_locale=target_locale
        )
