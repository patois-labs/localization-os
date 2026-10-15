"""Bilingual CAT XLIFF variants: Trados SDLXLIFF, memoQ MQXLIFF, Phrase MXLIFF, and the
XLIFF 1.2 segmentation (`<seg-source>`) and protection rules the plain XLIFF filter shares.

All three vendor formats are XLIFF 1.2 with a vendor namespace. What makes them different
from a plain .xlf is that the vendor tool reads the file back:

- Segmentation is the vendor's. SDLXLIFF keeps it in `<seg-source>` as
  `<mrk mtype="seg" mid="n">`; the `<target>` must mirror it (`<target><mrk mtype="seg"
  mid="n">`), or Trados refuses the returned file. Each seg `mrk` is its own unit here.
- Locked and approved content is never overwritten: XLIFF 1.2 `translate="no"` (inherited
  from a `<group>`), `approved="yes"`, a target `state` of `final` or `signed-off`;
  SDLXLIFF `sdl:seg locked="true"` or `conf` ApprovedTranslation / ApprovedSignOff; memoQ
  `mq:locked="locked"` or a reviewer status; Phrase `m:locked="true"`; WorldServer
  `iws:status lock_status="locked"` or `translation_status="finished"`. A segment the vendor
  tool already shows as confirmed (SDL conf Translated, memoQ ManuallyConfirmed, Phrase
  `m:confirmed` > 0) that already has a target is also left alone: that is someone's
  confirmed work, and this pipeline cannot preload it for review.
- Written segments are marked translated in the vendor's own vocabulary: SDL `conf=
  "Translated"` (an `sdl:seg` is added when missing), memoQ `mq:status=
  "ManuallyConfirmed"`, WorldServer `translation_status="finished"` (on an existing
  iws:status, spliced builds only), an XLIFF 1.2 target `state` of `new`/`needs-translation` becomes
  `translated`. Phrase `m:confirmed` is a workflow LEVEL this file does not know, so it is
  left for the linguist to confirm in Phrase.

build() is a byte splice, not a re-serialisation: lxml validates the file and makes every
decision, a small markup scanner finds the byte ranges of the same elements, and only the
target contents and the status attributes change. Every other byte (XML declaration, BOM,
namespace prefixes, attribute quoting, line endings, character references) is the
original's. A build with nothing to write returns the original bytes unchanged.

Unit keys (p_index): a whole-source unit keeps its plain XLIFF key (its position among
`<source>` elements, as xliff_filter numbers them); a seg `mrk` unit is SEG_BASE plus its
position among all seg mrks in the file. Skipped units are counted, never renumbered.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

from app import config
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
)
from app.services.segment import is_point_kind, ph_close, ph_open, ph_point

SEG_BASE = 1_000_000_000
SDL_NS = "http://sdl.com/FileTypes/SdlXliff/1.0"
MQ_NS = "MQXliff"
M_NS = "http://www.memsource.com/mxlf/2.0"

# Written into a segment this pipeline translated (module constants so a deployment
# decision is one line).
SDL_WRITTEN_CONF = "Translated"
MQ_WRITTEN_STATUS = "ManuallyConfirmed"
# WorldServer (Idiom) XLIFF: `<iws:segment-metadata><iws:status translation_status=..
# lock_status=..>` inside the trans-unit. Okapi's IWS writer default is "finished".
IWS_WRITTEN_STATUS = "finished"
_IWS_FINAL = {"finished"}
_IWS_LOCKED = {"locked"}
_SDL_APPROVED = {"approvedtranslation", "approvedsignoff"}
_SDL_CONFIRMED = {"translated"}
_MQ_APPROVED = {"proofread", "reviewer1confirmed", "reviewer2confirmed"}
_MQ_CONFIRMED = {"manuallyconfirmed"}
_FINAL_STATES = {"final", "signed-off"}
_RESET_STATES = {"new", "needs-translation"}


def _local(el) -> str:
    return etree.QName(el).localname.lower() if isinstance(el.tag, str) else ""


def _child(el, name: str):
    return next((c for c in el if _local(c) == name), None)


def _has_content(el) -> bool:
    return el is not None and (bool("".join(el.itertext()).strip()) or len(el) > 0)


# --------------------------------------------------------------------------- the walk


@dataclass
class Slot:
    key: int
    tu: object  # the trans-unit (1.2) or segment (2.0)
    src: object  # the element whose content is the source text (<source> or seg <mrk>)
    mrk: object = None  # the seg-source <mrk mtype="seg"> (None: a whole-source unit)
    skip: str = ""  # why this unit is protected ("" = translatable)
    unit_key: int = -1  # the trans-unit's whole-source key (its <source> position)
    unit_skip: str = ""  # why the whole trans-unit is protected
    source: object = None  # the trans-unit's <source>


def _translate_no(tu) -> bool:
    """XLIFF 1.2 `translate` on the trans-unit, else the nearest group/file that says."""
    el = tu
    while el is not None:
        if _local(el) in ("trans-unit", "group", "unit", "file"):
            v = el.get("translate")
            if v is not None:
                return v.strip().lower() == "no"
        el = el.getparent()
    return False


def _unit_skip(tu, target) -> str:
    """Why a whole trans-unit is protected, or ''. Shared with xliff_filter."""
    if _translate_no(tu):
        return "translate-no"
    if (tu.get("approved") or "").strip().lower() == "yes":
        return "approved"
    if target is not None and (target.get("state") or "").strip().lower() in _FINAL_STATES:
        return "final"
    if _local(tu) == "segment" and (tu.get("state") or "").strip().lower() == "final":
        return "final"  # XLIFF 2.0 segment state
    mq_locked = (tu.get(f"{{{MQ_NS}}}locked") or "").strip().lower()
    if mq_locked in ("locked", "true"):
        return "locked"
    if (tu.get(f"{{{M_NS}}}locked") or "").strip().lower() == "true":
        return "locked"
    if _is_gs4tr(tu):
        if _gs4tr_attr(tu, "locked") == "true" or _gs4tr_attr(target, "locked") == "true":
            return "locked"
        state = (target.get("state") or "").strip().lower() if target is not None else ""
        if _has_content(target) and state != "new" and not state.startswith("needs-"):
            return "confirmed"  # Wordfast: a target past "needs-*" is the translator's
    mq_status = (tu.get(f"{{{MQ_NS}}}status") or "").strip().lower()
    if mq_status in _MQ_APPROVED:
        return "approved"
    if mq_status in _MQ_CONFIRMED and _has_content(target):
        return "confirmed"
    confirmed = (tu.get(f"{{{M_NS}}}confirmed") or "").strip()
    if confirmed not in ("", "0") and _has_content(target):
        return "confirmed"
    iws = iws_status(tu)
    if iws is not None:
        if (iws.get("lock_status") or "").strip().lower() in _IWS_LOCKED:
            return "locked"
        if (iws.get("translation_status") or "").strip().lower() in _IWS_FINAL:
            return "final"
    return ""


def _is_gs4tr(el) -> bool:
    """Wordfast TXLF: the file declares the gs4tr extension namespace."""
    return any("gs4tr" in (uri or "") for uri in (el.nsmap or {}).values())


def _gs4tr_attr(el, local: str) -> str:
    if el is None:
        return ""
    for k, v in el.attrib.items():
        q = etree.QName(k)
        if q.localname == local and "gs4tr" in (q.namespace or ""):
            return (v or "").strip().lower()
    return ""


def _resets(tu, target) -> bool:
    """A written target's state becomes `translated` (Wordfast: any needs-* state)."""
    state = (target.get("state") or "").strip().lower()
    return state in _RESET_STATES or (_is_gs4tr(tu) and state.startswith("needs-"))


def _is_iws(el) -> bool:
    """A WorldServer element: the `iws` prefix (what Okapi keys on) or an Idiom URI."""
    if not isinstance(el.tag, str):
        return False
    ns = etree.QName(el).namespace or ""
    return el.prefix == "iws" or "idiominc.com" in ns


def iws_status(tu):
    """The trans-unit's `<iws:segment-metadata><iws:status>` element, or None."""
    meta = next((c for c in tu if _is_iws(c) and _local(c) == "segment-metadata"), None)
    if meta is None:
        return None
    return next((c for c in meta if _is_iws(c) and _local(c) == "status"), None)


def _sdl_seg(tu, mid: str):
    defs = next((c for c in tu if c.tag == f"{{{SDL_NS}}}seg-defs"), None)
    if defs is None:
        return None, None
    return defs, next((s for s in defs if s.tag == f"{{{SDL_NS}}}seg" and s.get("id") == mid), None)


def _seg_skip(tu, mid: str, tgt_mrk) -> str:
    _defs, seg = _sdl_seg(tu, mid)
    if seg is None:
        return ""
    if (seg.get("locked") or "").strip().lower() == "true":
        return "locked"
    conf = (seg.get("conf") or "").strip().lower()
    if conf in _SDL_APPROVED:
        return "approved"
    if conf in _SDL_CONFIRMED and _has_content(tgt_mrk):
        return "confirmed"
    return ""


def _seg_mrks(container) -> list:
    if container is None:
        return []
    return [m for m in container.iter() if _local(m) == "mrk" and m.get("mtype") == "seg"]


def has_seg_source(root) -> bool:
    """True when any 1.2 trans-unit carries `<seg-source>` with seg mrks (the files that
    need this engine's mirrored-segmentation build)."""
    return any(_local(el) == "seg-source" and _seg_mrks(el) for el in root.iter())


def walk(root) -> list[Slot]:
    """Every unit slot in document order, protected ones included (skip-but-count).
    parse() and build() both walk this list, so keys always line up."""
    from app.filters.xliff_filter import _source_elements

    slots: list[Slot] = []
    seg_n = 0
    for idx, src in enumerate(_source_elements(root)):
        tu = src.getparent()
        target = _child(tu, "target")
        unit_skip = _unit_skip(tu, target)
        mrks = _seg_mrks(_child(tu, "seg-source")) if _local(tu) == "trans-unit" else []
        if not mrks:
            slots.append(Slot(key=idx, tu=tu, src=src, skip=unit_skip))
            continue
        tgt_mrks = {m.get("mid"): m for m in _seg_mrks(target)}
        for m in mrks:
            mid = m.get("mid") or ""
            skip = unit_skip or _seg_skip(tu, mid, tgt_mrks.get(mid))
            slots.append(
                Slot(
                    key=SEG_BASE + seg_n,
                    tu=tu,
                    src=m,
                    mrk=m,
                    skip=skip,
                    unit_key=idx,
                    unit_skip=unit_skip,
                    source=src,
                )
            )
            seg_n += 1
    return slots


# ---------------------------------------------------------------------- tokenizing

# Phrase MXLIFF writes inline marks as text: {1} and {2>inner<2}, {b>bold<b}.
_TEXT_TAG = re.compile(r"\{([A-Za-z0-9]{1,12})\}|\{([A-Za-z0-9]{1,12})>|<([A-Za-z0-9]{1,12})\}")


def _text_tags(text: str, start: int) -> tuple[str, dict]:
    """Replace MXLIFF text marks with protected tokens numbered from `start` + 1.
    Returns (text, {n: literal | (open_literal, close_literal)})."""
    matches = list(_TEXT_TAG.finditer(text))
    if not matches:
        return text, {}
    pair_of: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    for i, m in enumerate(matches):
        if m.group(2):
            stack.append((m.group(2), i))
        elif m.group(3):
            for j in range(len(stack) - 1, -1, -1):
                if stack[j][0] == m.group(3):
                    pair_of[stack[j][1]] = i
                    pair_of[i] = stack[j][1]
                    del stack[j:]
                    break
    lits: dict = {}
    num_of: dict[int, int] = {}
    out: list[str] = []
    pos = 0
    n = start
    for i, m in enumerate(matches):
        out.append(text[pos : m.start()])
        pos = m.end()
        if i in pair_of and m.group(2):
            n += 1
            num_of[i] = n
            lits[str(n)] = (m.group(0), matches[pair_of[i]].group(0))
            out.append(ph_open("g", n))
        elif i in pair_of:
            out.append(ph_close("g", num_of[pair_of[i]]))
        else:
            n += 1
            lits[str(n)] = m.group(0)
            out.append(ph_point("x", n))
    out.append(text[pos:])
    return "".join(out), lits


def tokenize(el, text_tags: bool) -> tuple[str, dict, list]:
    """(tokenized text, inline_map, inline elements in token order) for a source element
    or seg mrk. inline_map values are lxml clones (element tokens) or literal strings
    (MXLIFF text marks)."""
    from app.filters.xliff_filter import _tokenize_source

    text, inline_map = _tokenize_source(el)
    if text_tags:
        text, lits = _text_tags(text, len(inline_map))
        inline_map.update(lits)
    return text, inline_map


def _inline_elements(el) -> list:
    """The inline elements of `el` in the SAME order _tokenize_source numbers them (a
    pre-order walk that does not descend into point elements)."""
    from app.filters.xliff_filter import _POINT

    out: list = []

    def visit(e):
        for c in e:  # comments/PIs count too: _tokenize_source numbers every child
            out.append(c)
            if isinstance(c.tag, str) and _local(c) not in _POINT:
                visit(c)

    visit(el)
    return out


# --------------------------------------------------------------------- byte scanner

_MARKUP = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<![^>]*>"
    r"|</([^\s>]+)\s*>"
    r"|<([^\s/>!?]+)((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>",
    re.S,
)


@dataclass
class Span:
    name: str  # qualified name as written, e.g. "sdl:seg"
    start: int  # "<" of the start tag
    start_end: int  # just after the start tag's ">"
    end_start: int  # "<" of the end tag (== start_end for a self-closing element)
    end: int  # just after the end tag (== start_end for a self-closing element)
    empty: bool  # written as <x/>


def scan(text: str) -> list[Span]:
    """Element spans in document (start-tag) order. The file is already known to be
    well-formed (lxml parsed it), so a tag-level scan is exact."""
    spans: list[Span] = []
    stack: list[Span] = []
    for m in _MARKUP.finditer(text):
        if m.group(1):
            sp = stack.pop()
            sp.end_start, sp.end = m.start(), m.end()
        elif m.group(2):
            empty = bool(m.group(4))
            sp = Span(m.group(2), m.start(), m.end(), m.end(), m.end(), empty)
            spans.append(sp)
            if not empty:
                stack.append(sp)
    return spans


def _esc_text(s: str) -> str:
    # ">" needs escaping only in "]]>"; leaving it bare keeps MXLIFF marks ({1>) as written.
    return s.replace("&", "&amp;").replace("<", "&lt;").replace("]]>", "]]&gt;")


def _esc_attr(s: str) -> str:
    return _esc_text(s).replace('"', "&quot;")


def _with_attr(tag: str, qname: str, value: str) -> str:
    """Set one attribute in a start tag's text, keeping its quoting and every other byte."""
    pat = re.compile(r"(\s" + re.escape(qname) + r"\s*=\s*)(\"[^\"]*\"|'[^']*')")
    m = pat.search(tag)
    if m:
        q = m.group(2)[0]
        val = _esc_attr(value) if q == '"' else _esc_attr(value).replace("'", "&apos;")
        return tag[: m.start(2)] + q + val + q + tag[m.end(2) :]
    close = len(tag) - (2 if tag.endswith("/>") else 1)
    while close > 0 and tag[close - 1].isspace():
        close -= 1
    return tag[:close] + f' {qname}="{_esc_attr(value)}"' + tag[close:]


def _prefix_for(el, ns: str) -> str | None:
    for p, uri in (el.nsmap or {}).items():
        if uri == ns:
            return p
    return None


def _qname(el, ns: str, local: str) -> str | None:
    if ns is None:
        return local
    p = _prefix_for(el, ns)
    if p is None and ns not in (el.nsmap or {}).values():
        return None
    return f"{p}:{local}" if p else local


# ---------------------------------------------------------------------- the engine


def _decode(raw: bytes) -> tuple[str, str, bytes]:
    """(text, codec, bom). UTF-8 (with or without BOM) and UTF-16 with a BOM only: the
    splice must write the file back in its own encoding."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8", b"\xef\xbb\xbf"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        codec = "utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-16-be"
        return raw[2:].decode(codec), codec, raw[:2]
    head = raw[:200].decode("ascii", "replace")
    m = re.search(r"""encoding\s*=\s*["']([A-Za-z0-9_.-]+)""", head)
    if m and m.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
        raise InputLimitError(
            f"This XLIFF file is encoded as {m.group(1)}; only UTF-8 and UTF-16 are "
            "supported. Re-save it as UTF-8 in the CAT tool."
        )
    try:
        return raw.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError as exc:
        raise InputLimitError("This XLIFF file is not valid UTF-8. Re-save it as UTF-8.") from exc


def parse_units(data: bytes, fmt: str, *, require_ns: str | None = None) -> ParseResult:
    from app.filters.xliff_filter import _copy_work, _parse_xml

    raw = bytes(data)
    _decode(raw)  # refuse an encoding the build could not write back
    root = _parse_xml(raw, capped=True)
    _check_version(root, fmt, require_ns)
    text_tags = M_NS in (root.nsmap or {}).values()
    units: list[TextUnit] = []
    skipped: dict[str, int] = {}
    budget = 10 * config.MAX_DOCUMENT_SEGMENTS
    for slot in walk(root):
        if slot.skip:
            skipped[slot.skip] = skipped.get(slot.skip, 0) + 1
            continue
        budget -= _copy_work(slot.src, budget)
        if budget < 0:
            raise InputLimitError(
                "This XLIFF file has far more inline tags than MAX_DOCUMENT_SEGMENTS "
                f"allows ({config.MAX_DOCUMENT_SEGMENTS} segments). Split it into "
                "smaller files."
            )
        text, inline_map = tokenize(slot.src, text_tags)
        text = text.strip()
        if not text:
            continue
        units.append(TextUnit(p_index=slot.key, text=text, inline_map=inline_map))
        check_segment_cap(len(units))
    skeleton = {
        "format": fmt,
        "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
    }
    if skipped:
        skeleton["protected_units"] = skipped
    return ParseResult(skeleton=skeleton, units=units)


class CatXliffFormatError(ValueError):
    """The upload is not the XLIFF 1.2 vendor file its extension names. A ValueError like
    xliff_filter's parse refusals, defined here so this always-on filter does not import
    the feature-gated vendors service (verify_features check 11)."""


def _check_version(root, fmt: str, require_ns: str | None) -> None:
    if _local(root) != "xliff" or (root.get("version") or "1.2").startswith("2"):
        raise CatXliffFormatError(f"This is not an XLIFF 1.2 file, so it cannot be read as .{fmt}.")
    if require_ns and require_ns not in (root.nsmap or {}).values():
        raise CatXliffFormatError(
            f"This file does not declare the {fmt.upper()} namespace ({require_ns}); "
            "upload a plain XLIFF as .xlf."
        )


class _Edits:
    def __init__(self, text: str):
        self.text = text
        self.items: list[tuple[int, int, int, str]] = []

    def add(self, start: int, end: int, new: str) -> None:
        self.items.append((start, end, len(self.items), new))

    def apply(self) -> str:
        out: list[str] = []
        pos = 0
        for start, end, _seq, new in sorted(self.items):
            if start < pos:
                raise ValueError("overlapping XLIFF edits")
            out.append(self.text[pos:start])
            out.append(new)
            pos = end
        out.append(self.text[pos:])
        return "".join(out)


def _render(text: str, lits: dict) -> str:
    """Tokenized translation -> XML content using the source's own tag bytes.
    lits[n] = point literal or (open, close). Unknown or repeated tokens are dropped and
    stray closes ignored (same rules as xliff_filter._rebuild_target); anything left open
    is closed so the result is always well-formed."""
    from app.services.segment import _PLACEHOLDER_RE

    out: list[str] = []
    used: set[str] = set()
    stack: list[tuple[str, str | None]] = []  # (key, close literal or None for phantom)
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_esc_text(text[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("/"):
            key = re.sub(r"^/[a-z]?", "", inner)
            if stack and stack[-1][0] == key:
                _k, close = stack.pop()
                if close is not None:
                    out.append(close)
            continue
        key = re.sub(r"^[a-z]?", "", inner, count=1)
        lit = lits.get(key)
        is_point = is_point_kind(inner[:1])  # ⟦pN⟧: an <x id>/<ph id> (#693)
        if lit is None or key in used:
            if not is_point:
                stack.append((key, None))
            continue
        used.add(key)
        if isinstance(lit, tuple):
            if is_point:  # a paired element referenced as a point: emit it empty
                out.append(lit[0] + lit[1])
            else:
                out.append(lit[0])
                stack.append((key, lit[1]))
        else:
            out.append(lit)
    out.append(_esc_text(text[pos:]))
    for _k, close in reversed(stack):
        if close is not None:
            out.append(close)
    return "".join(out)


def _literals(src_el, text_tags: bool, text: str, spans: dict) -> dict:
    """Token number -> the source's own markup bytes for that inline element."""
    from app.filters.xliff_filter import _POINT

    lits: dict = {}
    for n, el in enumerate(_inline_elements(src_el), 1):
        sp = spans.get(el) if isinstance(el.tag, str) else None
        if sp is None:
            lits[str(n)] = ""  # a comment or PI inside the source is not carried over
        elif _local(el) in _POINT:
            lits[str(n)] = text[sp.start : sp.end]
        elif sp.empty:
            tag = text[sp.start : sp.start_end]
            lits[str(n)] = (tag[:-2].rstrip() + ">", f"</{sp.name}>")
        else:
            lits[str(n)] = (text[sp.start : sp.start_end], text[sp.end_start : sp.end])
    if text_tags:
        _tok, inline_map = tokenize(src_el, True)
        for k, v in inline_map.items():
            # MXLIFF text marks are character data: escape them like any other text.
            if isinstance(v, str):
                lits[k] = _esc_text(v)
            elif isinstance(v, tuple):
                lits[k] = (_esc_text(v[0]), _esc_text(v[1]))
    return lits


def _edges(s: str) -> tuple[str, str]:
    body = s.strip()
    if not body:
        return "", ""
    i = s.find(body)
    return s[:i], s[i + len(body) :]


def build_units(
    original: bytes,
    paragraph_texts: dict,
    *,
    fallback_keys=None,
    target_locale: str | None = None,
) -> bytes:
    from app.filters import a11y
    from app.filters.xliff_filter import _parse_xml
    from app.filters.xml_text import xml_target_text

    raw = bytes(original)
    texts = {k: v for k, v in paragraph_texts.items() if k not in (fallback_keys or ())}
    text, codec, bom = _decode(raw)
    root = _parse_xml(raw)
    text_tags = M_NS in (root.nsmap or {}).values()
    els = [e for e in root.iter() if isinstance(e.tag, str)]
    raw_spans = scan(text)
    if len(raw_spans) != len(els):
        raise ValueError("XLIFF markup scan did not match the parsed tree")
    spans = dict(zip(els, raw_spans, strict=True))
    edits = _Edits(text)

    def content_for(slot: Slot, new: str) -> str:
        src_text, _map = tokenize(slot.src, text_tags)
        lead, trail = _edges(src_text)
        body = xml_target_text(new).strip()
        lits = _literals(slot.src, text_tags, text, spans)
        return _render(lead + body + trail, lits)

    def set_inner(el, inner: str, tag_text: str | None = None) -> None:
        sp = spans[el]
        start_tag = tag_text if tag_text is not None else text[sp.start : sp.start_end]
        if sp.empty:
            open_tag = start_tag[:-2].rstrip() + ">"
            edits.add(sp.start, sp.end, open_tag + inner + f"</{sp.name}>")
        else:
            if tag_text is not None:
                edits.add(sp.start, sp.start_end, tag_text)
            edits.add(sp.start_end, sp.end_start, inner)

    # Group writable seg slots per trans-unit (a missing <target> is created once).
    by_tu: dict = {}
    for slot in walk(root):
        legacy = texts.get(str(slot.unit_key)) if slot.mrk is not None else None
        if legacy is not None and not slot.unit_skip and slot.tu not in by_tu:
            # A document stored before seg-source support keyed the whole trans-unit by
            # its <source> position: keep writing it whole rather than dropping it.
            whole = Slot(key=slot.unit_key, tu=slot.tu, src=slot.source)
            by_tu[slot.tu] = [(whole, legacy)]
            continue
        if slot.tu in by_tu and by_tu[slot.tu][0][0].mrk is None:
            continue
        new = texts.get(str(slot.key))
        if new is None or slot.skip:
            continue
        by_tu.setdefault(slot.tu, []).append((slot, new))

    for tu, items in by_tu.items():
        target = _child(tu, "target")
        tu_sp = spans[tu]
        if items[0][0].mrk is None:  # whole-source unit (one per trans-unit)
            slot, new = items[0]
            inner = content_for(slot, new)
            if target is not None:
                tag_text = None
                if _resets(tu, target):
                    tag_text = _with_attr(
                        text[spans[target].start : spans[target].start_end], "state", "translated"
                    )
                set_inner(target, inner, tag_text)
            else:
                after = _child(tu, "seg-source")
                anchor = spans[after if after is not None else slot.src]
                name = spans[slot.src].name
                tname = name[: -len("source")] + "target"
                edits.add(anchor.end, anchor.end, f"<{tname}>{inner}</{tname}>")
            if f"{{{MQ_NS}}}status" in tu.attrib:
                q = _qname(tu, MQ_NS, "status")
                edits.add(
                    tu_sp.start,
                    tu_sp.start_end,
                    _with_attr(text[tu_sp.start : tu_sp.start_end], q, MQ_WRITTEN_STATUS),
                )
            _mark_iws(tu, text, spans, edits)
            continue
        # seg-mrk units
        written = {slot.mrk.get("mid"): content_for(slot, new) for slot, new in items}
        if target is None:
            seg_source = _child(tu, "seg-source")
            ss = spans[seg_source]
            # Mirror <seg-source> byte for byte; each seg mrk carries its translation, or
            # nothing when this unit was not written (never the source text).
            pieces: list[str] = []
            pos = ss.start_end
            for m in _seg_mrks(seg_source):
                msp = spans[m]
                inner = written.get(m.get("mid"), "")
                if msp.empty:
                    open_tag = text[msp.start : msp.start_end][:-2].rstrip() + ">"
                    pieces += [text[pos : msp.start], open_tag, inner, f"</{msp.name}>"]
                    pos = msp.end
                else:
                    pieces += [text[pos : msp.start_end], inner]
                    pos = msp.end_start
            pieces.append(text[pos : ss.end_start])
            mirrored = "".join(pieces)
            tname = ss.name[: -len("seg-source")] + "target"
            edits.add(ss.end, ss.end, f"<{tname}>{mirrored}</{tname}>")
        else:
            tgt_mrks = {m.get("mid"): m for m in _seg_mrks(target)}
            for mid, inner in written.items():
                tm = tgt_mrks.get(mid)
                if tm is None:
                    raise ValueError(
                        f"The target of trans-unit {tu.get('id')!r} has no segment {mid!r}; "
                        "its segmentation does not match <seg-source>."
                    )
                set_inner(tm, inner)
        _mark_sdl(tu, list(written), text, spans, edits)
        _mark_iws(tu, text, spans, edits)

    if target_locale:
        tag = a11y.target_tag(target_locale)
        for el in els:
            if tag and _local(el) == "file" and not (el.get("target-language") or "").strip():
                sp = spans[el]
                start_tag = _with_attr(text[sp.start : sp.start_end], "target-language", tag)
                if sp.empty:
                    edits.add(sp.start, sp.end, start_tag)
                else:
                    edits.add(sp.start, sp.start_end, start_tag)
    if not edits.items:
        return raw
    out = bom + edits.apply().encode(codec)
    _parse_xml(out)  # the spliced file must still be well-formed XLIFF
    return out


def _mark_iws(tu, text: str, spans: dict, edits: _Edits) -> None:
    """translation_status="finished" on a written WorldServer unit's existing iws:status
    (never added when absent; tm_origin / translation_type are left as they were)."""
    st = iws_status(tu)
    if st is None or "translation_status" not in st.attrib:
        return
    sp = spans[st]
    tag = _with_attr(text[sp.start : sp.start_end], "translation_status", IWS_WRITTEN_STATUS)
    edits.add(sp.start, sp.start_end, tag)


def _mark_sdl(tu, mids: list, text: str, spans: dict, edits: _Edits) -> None:
    """conf="Translated" on each written segment's sdl:seg (added when missing)."""
    prefix = _prefix_for(tu, SDL_NS)
    if prefix is None:
        return
    defs, _ = _sdl_seg(tu, "")
    new_segs: list[str] = []
    for mid in mids:
        _d, seg = _sdl_seg(tu, mid)
        if seg is not None:
            sp = spans[seg]
            tag = _with_attr(text[sp.start : sp.start_end], "conf", SDL_WRITTEN_CONF)
            edits.add(sp.start, sp.start_end, tag)
        else:
            new_segs.append(f'<{prefix}:seg id="{_esc_attr(mid)}" conf="{SDL_WRITTEN_CONF}"/>')
    if not new_segs:
        return
    added = "".join(new_segs)
    if defs is not None:
        sp = spans[defs]
        if sp.empty:
            edits.add(
                sp.start,
                sp.end,
                text[sp.start : sp.start_end][:-2].rstrip() + ">" + added + f"</{sp.name}>",
            )
        else:
            edits.add(sp.end_start, sp.end_start, added)
    else:
        sp = spans[tu]
        edits.add(sp.end_start, sp.end_start, f"<{prefix}:seg-defs>{added}</{prefix}:seg-defs>")


class _CatXliff(Filter):
    namespace: str | None = None
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return parse_units(data, self.fmt, require_ns=self.namespace)

    def build(self, original: bytes, paragraph_texts: dict, *, fallback_keys=None) -> bytes:
        return build_units(original, paragraph_texts, fallback_keys=fallback_keys)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys=None,
    ) -> bytes:
        """build() plus `target-language` on a <file> that has none. A vendor file's own
        target-language is never changed (the vendor tool matches it to the project)."""
        return build_units(
            original, paragraph_texts, fallback_keys=fallback_keys, target_locale=target_locale
        )


@register
class SdlxliffFilter(_CatXliff):
    fmt = "sdlxliff"
    exts = ("sdlxlf",)
    namespace = SDL_NS


@register
class MqxliffFilter(_CatXliff):
    fmt = "mqxliff"
    exts = ()
    namespace = MQ_NS


@register
class MxliffFilter(_CatXliff):
    fmt = "mxliff"
    exts = ()
    namespace = M_NS
