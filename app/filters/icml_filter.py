"""ICML filter: Adobe InCopy Markup Language (.icml) and WoodWing WCML (.wcml).

An ICML file is ONE plain XML file (not a package): an InDesign snippet whose
`<?aid SnippetType="InCopyInterchange"?>` root `<Document>` holds the styles, colours,
conditions, languages and hyperlinks the text needs, plus the text itself as one
`<Story>` with exactly the IDML story model:

    Story > ParagraphStyleRange > CharacterStyleRange > Content | Br | ...

WoodWing Smart Connection articles (.wcml) use the same snippet model and can hold
several stories, each wrapped in WoodWing's own namespaced elements. Two layouts open:
a `<Document>` root, and the Smart Connection v3 layout whose root is
`<ea:Stories xmlns:ea="urn:SmartConnection_v3">` with one InCopy snippet `<Document>`
inside each `<ea:Story>` (next to its `<ea:StoryInfo>` metadata, which is never offered:
Smart Connection regenerates it). Every plain `<Story>` inside a snippet `<Document>` and
not inside another story is walked, in document order; hidden conditions are honoured
per snippet document; the wrapper bytes are kept.

The text walk, inline protection and rebuild are the IDML filter's (app/filters/
idml_filter.py), unchanged: one TextUnit per visible paragraph (body first, then table
cells, footnotes and endnotes breadth first); a paragraph styled across several ranges is
one unit with ``⟦rN⟧`` run tokens; hyperlink sources, inline XML tags and tracked
insertions are paired ``⟦gN⟧``; tabs, forced line breaks, `<?ACE n?>` special characters,
index markers, notes, tables, footnotes, anchored frames and hidden text are ``⟦xN⟧``
points that build() moves back. Text under a condition with `Visible="false"` (declared in
the same file), notes, hidden text and tracked deletions are never offered.

build(): with no translated paragraph the original bytes come back unchanged. Otherwise
only the `<Story>` elements that hold a translated paragraph are re-serialized and spliced
into the original bytes at their own spans: the XML declaration, the `<?aid?>` processing
instructions, every style, swatch, hyperlink and untouched story keep their bytes. If the
spans cannot be located safely (a comment or CDATA that mentions a story tag), the whole
document is re-serialized under the original declaration instead.

build_localized(): build() plus AppliedLanguage on every rebuilt range, set to the InDesign
language the file itself declares for the target (never an invented one), and for a
right-to-left target paragraph direction plus the Adobe World-Ready composer on every
translated paragraph (the IDML filter's _stamp_rtl).

Security: hardened lxml (no DTD, no entity resolution, no network, no huge tree) plus a
DOCTYPE/ENTITY byte prescan and a post-parse docinfo check; a markup budget (forty `<`
per allowed segment) runs before the tree is built; the segment cap runs during the walk.
UTF-8 only (the format's encoding); another declared encoding is refused.

Spec: ICML is described in Adobe's "IDML File Format Specification" version 8.0 (InDesign
CS6, 2012), now withdrawn; Adobe points to the InDesign SDK docs and
`app.generateIDMLSchema()` (https://community.adobe.com/t5/indesign-discussions/where-is-the-
idml-specification/m-p/13172633). Single-file nature: O'Reilly "A Brief Note about IDML and
ICML" (https://oreilly.com/library/view/xml-publishing-with/9781449397234/ar01s12.html).
WCML: WoodWing Smart Connection article format (WCML2/WCML3,
https://helpcenter.woodwing.com/hc/en-us/articles/360016107692).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

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
    newline_like,
    register,
)
from app.filters.idml_filter import (
    _NEWLINES,
    _has_text,
    _indesign_language,
    _local,
    _normalize,
    _rebuild,
    _split_ws,
    _stamp,
    _stamp_rtl,
    _style,
    _tokenize,
    _Walker,
)

_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_DECL_RE = re.compile(rb"^(\xef\xbb\xbf)?<\?xml[^>]*\?>")
# Byte spans of plain <Story> elements (never <ea:Story> or <StoryPreference>).
_STORY_OPEN = re.compile(rb"<Story[\s>/]")
_STORY_CLOSE = re.compile(rb"</Story\s*>")


class IcmlError(ValueError):
    """The upload is not a readable ICML/WCML file. Every door reports a ValueError
    from a parse as an unreadable file (422)."""


def _parse(data: bytes):
    raw = bytes(data)
    if _DOCTYPE_RE.search(raw):
        raise IcmlError("This InCopy file declares a DOCTYPE or entity; ICML never needs one.")
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This InCopy file has far more markup than MAX_DOCUMENT_SEGMENTS allows "
            f"({config.MAX_DOCUMENT_SEGMENTS} segments). Split the document."
        )
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
        remove_pis=False,
    )
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise IcmlError(f"This file is not well-formed InCopy XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise IcmlError("This InCopy file declares a DOCTYPE; ICML never needs one.")
    if (info.encoding or "UTF-8").upper().replace("_", "-") not in ("UTF-8", "UTF8"):
        raise IcmlError(
            f"This InCopy file is encoded as {info.encoding}; ICML is UTF-8. "
            "Re-save it from InCopy."
        )
    if _local(root) != "Document" and not _is_woodwing(root):
        raise IcmlError(
            "This XML file is not an InCopy or WoodWing document (its root is neither "
            "<Document> nor WoodWing's <ea:Stories>)."
        )
    stories = _stories(root)
    if not stories:
        raise IcmlError("This InCopy document holds no <Story>.")
    # Conditions belong to the snippet <Document> that declares them; a WoodWing article
    # can carry several snippets, so each story only honours its own document's.
    hidden = [_hidden(_document_of(story, root)) for story in stories]
    return root, stories, hidden


def _is_woodwing(root) -> bool:
    """A WoodWing Smart Connection article: `<ea:Stories xmlns:ea="urn:SmartConnection_v3">`
    whose `<ea:Story>` elements each wrap an InCopy snippet `<Document>`."""
    ns = etree.QName(root).namespace or ""
    return _local(root) == "Stories" and ns.startswith("urn:SmartConnection")


def _document_of(story, root):
    """The nearest enclosing snippet <Document> (the root for a plain ICML file)."""
    for anc in story.iterancestors():
        if anc.tag == "Document":
            return anc
    return root


def _hidden(doc) -> set:
    return {
        el.get("Self")
        for el in doc.iter()
        if _local(el) == "Condition"
        and (el.get("Visible") or "").lower() == "false"
        and el.get("Self")
    }


def _stories(root) -> list:
    """Every plain <Story> not nested in another one, in document order. Inside a WoodWing
    wrapper only stories inside a snippet <Document> count."""
    wrapped = root.tag != "Document"
    out = []
    for el in root.iter("Story"):
        ancestors = [a.tag for a in el.iterancestors()]
        if "Story" in ancestors:
            continue
        if wrapped and "Document" not in ancestors:
            continue
        out.append(el)
    return out


def _spans(raw: bytes, stories: list) -> list | None:
    """Byte span of each story in the original file, or None when they cannot be
    matched one to one (a story tag inside a comment or CDATA, a self-closed story)."""
    opens = [m.start() for m in _STORY_OPEN.finditer(raw)]
    closes = [m.end() for m in _STORY_CLOSE.finditer(raw)]
    if len(opens) != len(stories) or len(closes) != len(stories):
        return None
    spans = list(zip(opens, closes, strict=True))
    prev = -1
    for start, end in spans:
        if not (prev <= start < end):
            return None
        prev = end
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    for (start, end), story in zip(spans, stories, strict=True):
        try:
            alone = etree.fromstring(raw[start:end], parser=parser)
        except etree.XMLSyntaxError:
            return None
        if alone.tag != "Story" or alone.get("Self") != story.get("Self"):
            return None
    return spans


def _serialize(raw: bytes, root, stories: list, changed: set) -> bytes:
    spans = _spans(raw, stories)
    if spans is not None:
        out = bytearray()
        pos = 0
        for k, (start, end) in enumerate(spans):
            if k not in changed:
                continue
            out += raw[pos:start]
            # A detached copy would repeat every ancestor's xmlns declaration (WoodWing's
            # ea: namespace); drop the ones the story itself never uses.
            alone = deepcopy(stories[k])
            alone.tail = None
            etree.cleanup_namespaces(alone)
            # B-bytes: the rebuilt story keeps the file's line endings (CRLF from InCopy)
            out += newline_like(raw, etree.tostring(alone, encoding="UTF-8", xml_declaration=False))
            pos = end
        out += raw[pos:]
        return bytes(out)
    m = _DECL_RE.match(raw)
    head = m.group(0) + b"\n" if m else b""
    return newline_like(
        raw, head + etree.tostring(root.getroottree(), encoding="UTF-8", xml_declaration=False)
    )


@register
class IcmlFilter(Filter):
    fmt = "icml"
    exts = ("wcml",)

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        _root, stories, hidden = _parse(data)
        units: list[TextUnit] = []
        idx = 0
        for story, story_hidden in zip(stories, hidden, strict=True):
            for para in _Walker(story_hidden).run(story):
                tok = _tokenize(para)
                if _has_text(tok.text):
                    _lead, core, _trail = _split_ws(tok.text)
                    inline = {k: {"kind": v[0]} for k, v in tok.reg.items()}
                    units.append(
                        TextUnit(p_index=idx, text=core, style=_style(para), inline_map=inline)
                    )
                    check_segment_cap(len(units))
                idx += 1
        skeleton = {
            "format": "icml",
            "stories": [s.get("Self") or "" for s in stories],
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(original, paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the target language on every rebuilt range, only when the file
        declares a matching InDesign <Language>; a right-to-left target also turns every
        translated paragraph right to left with the World-Ready composer (IDML _stamp_rtl)."""
        from app.filters import a11y

        return self._build(original, paragraph_texts, a11y.target_tag(target_locale) or None)

    def _build(self, original: bytes, paragraph_texts: dict, tag: str | None) -> bytes:
        from app.filters import a11y
        from app.filters.xml_text import xml_target_text

        raw = bytes(original)
        root, stories, hidden = _parse(raw)
        language = _indesign_language(root, tag) if tag else None
        rtl = bool(tag) and a11y.target_direction(tag) == "rtl"
        changed: set = set()
        idx = 0
        for k_story, story in enumerate(stories):
            paras = _Walker(hidden[k_story]).run(story)
            keys = [str(idx + k) for k in range(len(paras))]
            wanted = {k: paragraph_texts[k] for k in keys if paragraph_texts.get(k) is not None}
            if wanted:
                _normalize(paras)
                paras2 = _Walker(hidden[k_story]).run(story)
                if len(paras2) != len(paras):  # pragma: no cover - defensive
                    raise IcmlError("Story paragraph structure changed on rebuild.")
                plans = []
                for k, para in enumerate(paras2):
                    new = wanted.get(str(idx + k))
                    if new is None:
                        continue
                    tok = _tokenize(para)
                    if not _has_text(tok.text):
                        continue
                    lead, _core, trail = _split_ws(tok.text)
                    # A newline is InDesign's forced line break, never a paragraph break
                    # (that would shift every later paragraph).
                    text = _NEWLINES.sub(" ", xml_target_text(str(new)))
                    if not text.startswith(lead):
                        text = lead + text.lstrip()
                    if not text.endswith(trail):
                        text = text.rstrip() + trail
                    plans.append((para, tok, text))
                for para, tok, text in plans:
                    ranges = _rebuild(para, tok, text)
                    if language:
                        _stamp(ranges, language)
                    if rtl:
                        _stamp_rtl(ranges)
                if plans:
                    changed.add(k_story)
            idx += len(paras)
        if not changed:
            return raw
        return _serialize(raw, root, stories, changed)
