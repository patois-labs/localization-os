"""Trados TagEditor TTX (TRADOStag): the bilingual format of Trados 2007 and earlier.

A TTX file is XML, usually UTF-16 LE with a BOM:

    <TRADOStag Version="2.0">
      <FrontMatter>
        <ToolSettings CreationTool="TRADOS TagEditor" .../>
        <UserSettings SourceLanguage="EN-US" TargetLanguage="DE-DE" DataType="HTML" .../>
      </FrontMatter>
      <Body><Raw>
        <ut Type="start" Style="external" DisplayText="p">&lt;p&gt;</ut>
        <Tu MatchPercent="0"><Tuv Lang="EN-US">Click <ut Type="start" DisplayText="b">&lt;b&gt;</ut>
          Save<ut Type="end" DisplayText="b">&lt;/b&gt;</ut>.</Tuv><Tuv Lang="DE-DE"></Tuv></Tu>
        <ut Type="end" Style="external" DisplayText="p">&lt;/p&gt;</ut>
      </Raw></Body>
    </TRADOStag>

Rules (Okapi TTX filter behaviour, Apache-2.0 docs, used as the spec since RWS never
published one):

- Only a PRE-SEGMENTED file is translatable: each `<Tu>` is one unit. A target cannot be
  written into text that has no `<Tu>`, and segmenting it here would not match Trados's
  own segmentation, so a file with no `<Tu>` at all is refused with a plain message (run
  Pre-translate with "Segment unknown sentences" first). Text outside `<Tu>` in a
  segmented file is left exactly as it is (Okapi's "existing segments only" mode) and
  counted on the skeleton as `unsegmented_text`.
- A `<Tu>` whose target `<Tuv>` already has text is kept as it is (a TM match or someone's
  translation) and never parsed or overwritten. An empty or missing target is translated.
- `<ut>` elements (the original document's tags, carried as escaped text) are protected
  tokens: a `Type="start"` / `Type="end"` pair among the same siblings becomes a paired
  token, any other `<ut>` a point token. `<df>` (display formatting) wraps its content
  and becomes a paired token. Inline markup is written back with the source's own bytes.
- A written `<Tu>` gets `MatchPercent` and `Origin` from TTX_WRITTEN_MATCH / ORIGIN.

build() is a byte splice over the decoded text (scanner + edit list shared with
cat_xliff): only written target `<Tuv>` contents, new target `<Tuv>` elements and the
written `<Tu>` start tags change. A build with nothing to write returns the original
bytes. The file is written back in its own encoding (UTF-16 LE/BE, UTF-8, BOM or not).

Unit keys (p_index): position of the `<Tu>` among all outermost `<Tu>` elements in the
file, protected ones counted (skip-but-count).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import config
from app.filters.base import (
    EmptyDocumentError,
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
)
from app.filters.cat_xliff import _edges, _Edits, _render, _with_attr, scan
from app.services.segment import _PLACEHOLDER_RE, ph_close, ph_open, ph_point

# Written on every <Tu> this pipeline translated (module constants so a deployment
# decision is one line). MatchPercent 0 = not a TM match (a higher value would make
# Trados treat the text as a fuzzy/exact TM hit); Origin "mt" = machine translation.
TTX_WRITTEN_MATCH = "0"
TTX_WRITTEN_ORIGIN = "mt"

_LETTER = re.compile(r"[^\W\d_]")
_TU_OPEN = re.compile(r"<Tu[\s>/]")


class UnsegmentedTtxError(EmptyDocumentError):
    """A TTX file with no <Tu> segments (plain message, 422 like an empty document)."""


def _local(el) -> str:
    if not isinstance(el.tag, str):
        return ""
    tag = el.tag
    return tag.rsplit("}", 1)[-1].lower()


# --------------------------------------------------------------------------- decoding


def _decode(raw: bytes) -> tuple[str, str, bytes]:
    """(text, codec, bom). TTX is usually UTF-16 LE with a BOM; UTF-16 without a BOM and
    UTF-8 (BOM or not) are read too. The build writes the same codec and BOM back."""
    if raw.startswith(b"\xef\xbb\xbf"):
        codec, bom = "utf-8", raw[:3]
    elif raw[:2] == b"\xff\xfe":
        codec, bom = "utf-16-le", raw[:2]
    elif raw[:2] == b"\xfe\xff":
        codec, bom = "utf-16-be", raw[:2]
    elif raw[:2] == b"<\x00":
        codec, bom = "utf-16-le", b""
    elif raw[:2] == b"\x00<":
        codec, bom = "utf-16-be", b""
    else:
        codec, bom = "utf-8", b""
        head = raw[:200].decode("ascii", "replace")
        m = re.search(r"""encoding\s*=\s*["']([A-Za-z0-9_.-]+)""", head)
        if m and m.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
            raise InputLimitError(
                f"This TTX file is encoded as {m.group(1)}; only UTF-16 and UTF-8 are "
                "supported. Re-save it from TagEditor (TTX files are normally UTF-16)."
            )
    try:
        return raw[len(bom) :].decode(codec), codec, bom
    except UnicodeDecodeError as exc:
        raise InputLimitError(
            f"This TTX file is not valid {codec.upper()}. Re-save it from TagEditor."
        ) from exc


def _budget(text: str) -> None:
    """Pre-parse budget (#571 pattern): bound the tree before lxml builds it."""
    cap = config.MAX_DOCUMENT_SEGMENTS
    if len(_TU_OPEN.findall(text)) > 4 * cap or text.count("<") > 40 * cap:
        raise InputLimitError(
            "This TTX file has far more segments or tags than MAX_DOCUMENT_SEGMENTS "
            f"allows ({cap} segments). Split it into smaller files."
        )


class TtxParseError(ValueError):
    """The upload is not a readable TTX file (every upload path maps a ValueError from a
    parse to a plain 422). Defined here so this always-on filter never imports the
    feature-gated vendors service (verify_features check 11)."""


_DOCTYPE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _parse(raw: bytes, text: str | None = None):
    """Hardened parse (app.filters.xml_guard): DOCTYPE/ENTITY refused on the DECODED text
    (so a UTF-16 DOCTYPE is caught too) and again from docinfo after the parse; no
    entities, DTD, network or huge tree."""
    from lxml import etree

    from app.filters.xml_guard import make_hardened_parser

    if text is None:
        text = _decode(raw)[0]
    if _DOCTYPE_RE.search(text):
        raise TtxParseError("This TTX file declares a DOCTYPE or entity; TTX never needs one.")
    try:
        root = etree.fromstring(raw, parser=make_hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise TtxParseError(f"This TTX file is not well-formed XML: {exc}") from exc
    info = root.getroottree().docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise TtxParseError("This TTX file declares a DOCTYPE or entity; TTX never needs one.")
    if _local(root) != "tradostag":
        raise TtxParseError("This is not a Trados TagEditor TTX file (no <TRADOStag> root).")
    return root


# --------------------------------------------------------------------------- the walk


@dataclass
class TuSlot:
    key: int
    tu: object
    src: object = None  # source <Tuv>
    tgt: object = None  # target <Tuv> (None: missing)
    skip: str = ""  # why this unit is protected ("" = translatable)


@dataclass
class Langs:
    source: str = ""
    target: str = ""
    settings: object = field(default=None, repr=False)  # the <UserSettings> element


def _langs(root) -> Langs:
    us = next((e for e in root.iter() if _local(e) == "usersettings"), None)
    if us is None:
        return Langs()
    return Langs(
        (us.get("SourceLanguage") or "").strip(), (us.get("TargetLanguage") or "").strip(), us
    )


def _tus(root) -> list:
    """Outermost <Tu> elements in document order (a <Tu> nested in a <Tuv> is inline)."""
    out = []
    for el in root.iter():
        if _local(el) != "tu":
            continue
        p = el.getparent()
        while p is not None and _local(p) != "tu":
            p = p.getparent()
        if p is None:
            out.append(el)
    return out


def _pick_tuvs(tu, langs: Langs):
    tuvs = [c for c in tu if _local(c) == "tuv"]
    if not tuvs:
        return None, None

    def lang(t):
        return (t.get("Lang") or "").strip().lower()

    src = next((t for t in tuvs if langs.source and lang(t) == langs.source.lower()), tuvs[0])
    others = [t for t in tuvs if t is not src]
    tgt = next((t for t in others if langs.target and lang(t) == langs.target.lower()), None)
    if tgt is None and others:
        tgt = others[0]
    return src, tgt


def walk(root, langs: Langs) -> list[TuSlot]:
    """Every <Tu> slot in document order, protected ones included. parse() and build()
    both walk this list, so keys always line up."""
    slots: list[TuSlot] = []
    for key, tu in enumerate(_tus(root)):
        src, tgt = _pick_tuvs(tu, langs)
        slot = TuSlot(key=key, tu=tu, src=src, tgt=tgt)
        if src is None:
            slot.skip = "empty"
        elif tgt is not None and _plain(tokenize(tgt)[0]).strip():
            slot.skip = "existing-target"
        slots.append(slot)
    return slots


def _plain(tokenized: str) -> str:
    return _PLACEHOLDER_RE.sub("", tokenized)


def _unsegmented_runs(root) -> int:
    """Text runs with a letter that sit outside every <Tu> and every <ut> in the body."""
    body = next((e for e in root if _local(e) == "body"), None)
    if body is None:
        return 0
    n = 0

    def visit(el):
        nonlocal n
        for c in el:
            name = _local(c)
            if name not in ("tu", "ut") and isinstance(c.tag, str):
                if c.text and _LETTER.search(c.text):
                    n += 1
                visit(c)
            if c.tail and _LETTER.search(c.tail):
                n += 1

    if body.text and _LETTER.search(body.text):
        n += 1
    visit(body)
    return n


# ---------------------------------------------------------------------- tokenizing


def tokenize(tuv) -> tuple[str, dict]:
    """(tokenized text, items) for a <Tuv>. items[str(n)] describes token n:
    ("point", el) | ("pair", start_ut, end_ut) | ("wrap", df). The SAME walk numbers the
    tokens at parse and at build, so token n always names the same source element.
    Comments and processing instructions inside a Tuv are not carried over."""
    items: dict = {}
    counter = [0]

    def nxt() -> int:
        counter[0] += 1
        return counter[0]

    def visit(el) -> str:
        parts = [el.text or ""]
        kids = list(el)
        pair: dict[int, int] = {}
        stack: list[int] = []
        for i, c in enumerate(kids):
            if _local(c) == "ut":
                t = (c.get("Type") or "").strip().lower()
                if t == "start":
                    stack.append(i)
                elif t == "end" and stack:
                    j = stack.pop()
                    pair[i], pair[j] = j, i
        num_of: dict[int, int] = {}
        for i, c in enumerate(kids):
            name = _local(c)
            if not name:  # comment / PI
                pass
            elif name == "ut" and i in pair and pair[i] > i:
                n = nxt()
                num_of[i] = n
                items[str(n)] = ("pair", c, kids[pair[i]])
                parts.append(ph_open("g", n))
            elif name == "ut" and i in pair:
                parts.append(ph_close("g", num_of[pair[i]]))
            elif name == "df" and (len(c) or c.text):
                n = nxt()
                items[str(n)] = ("wrap", c)
                parts.append(ph_open("g", n) + visit(c) + ph_close("g", n))
            else:
                n = nxt()
                items[str(n)] = ("point", c)
                parts.append(ph_point("x", n))
            parts.append(c.tail or "")
        return "".join(parts)

    return visit(tuv), items


def _literals(items: dict, text: str, spans: dict) -> dict:
    """Token number -> the source's own markup (point literal or (open, close))."""
    lits: dict = {}
    for k, item in items.items():
        kind, el = item[0], item[1]
        sp = spans[el]
        if kind == "point":
            lits[k] = text[sp.start : sp.end]
        elif kind == "pair":
            end = spans[item[2]]
            lits[k] = (text[sp.start : sp.end], text[end.start : end.end])
        else:  # wrap
            lits[k] = (text[sp.start : sp.start_end], text[sp.end_start : sp.end])
    return lits


def _spans(root, text: str) -> tuple[list, dict]:
    els = [e for e in root.iter() if isinstance(e.tag, str)]
    raw_spans = scan(text)
    if len(raw_spans) != len(els):
        raise ValueError("TTX markup scan did not match the parsed tree")
    return els, dict(zip(els, raw_spans, strict=True))


# ---------------------------------------------------------------------- parse / build


def parse_ttx(data: bytes) -> ParseResult:
    raw = bytes(data)
    text, _codec, _bom = _decode(raw)
    _budget(text)
    root = _parse(raw, text)
    langs = _langs(root)
    slots = walk(root, langs)
    if not slots:
        raise UnsegmentedTtxError(
            "This TTX file is not segmented (it has no <Tu> segments), so it cannot be "
            "translated here. Pre-translate it in Trados (Translator's Workbench, with "
            '"Segment unknown sentences" on) and upload the segmented TTX again.'
        )
    _els, spans = _spans(root, text)
    units: list[TextUnit] = []
    skipped: dict[str, int] = {}
    for slot in slots:
        if slot.skip:
            skipped[slot.skip] = skipped.get(slot.skip, 0) + 1
            continue
        tok, items = tokenize(slot.src)
        tok = tok.strip()
        if not _plain(tok).strip():
            skipped["no-text"] = skipped.get("no-text", 0) + 1
            continue
        lits = _literals(items, text, spans)
        units.append(TextUnit(p_index=slot.key, text=tok, inline_map=lits))
        check_segment_cap(len(units))
    if not units and skipped.get("existing-target"):
        raise EmptyDocumentError(
            "Every segment in this TTX file already has a translation, so there is "
            "nothing left to translate."
        )
    skeleton: dict = {
        "format": "ttx",
        "units": [{"p_index": u.p_index, "text": u.text, "style": ""} for u in units],
    }
    if skipped:
        skeleton["protected_units"] = skipped
    loose = _unsegmented_runs(root)
    if loose:
        skeleton["unsegmented_text"] = loose
    return ParseResult(skeleton=skeleton, units=units)


def _ttx_lang(locale) -> str:
    """TTX writes languages as upper-case ll-CC (EN-US, DE-DE)."""
    from app.filters import a11y

    return a11y.target_tag(locale).upper() if locale else ""


def build_ttx(
    original: bytes,
    paragraph_texts: dict,
    *,
    fallback_keys=None,
    target_locale: str | None = None,
) -> bytes:
    from app.filters.xml_text import xml_target_text

    raw = bytes(original)
    texts = {k: v for k, v in paragraph_texts.items() if k not in (fallback_keys or ())}
    text, codec, bom = _decode(raw)
    _budget(text)
    root = _parse(raw, text)
    langs = _langs(root)
    _els, spans = _spans(root, text)
    edits = _Edits(text)
    tgt_lang = langs.target or _ttx_lang(target_locale)

    for slot in walk(root, langs):
        new = texts.get(str(slot.key))
        if new is None or slot.skip:
            continue
        src_tok, items = tokenize(slot.src)
        if not _plain(src_tok).strip():
            continue
        lead, trail = _edges(src_tok)
        body = xml_target_text(new).strip()
        inner = _render(lead + body + trail, _literals(items, text, spans))
        if slot.tgt is not None:
            sp = spans[slot.tgt]
            if sp.empty:
                open_tag = text[sp.start : sp.start_end][:-2].rstrip() + ">"
                edits.add(sp.start, sp.end, open_tag + inner + f"</{sp.name}>")
            else:
                edits.add(sp.start_end, sp.end_start, inner)
        else:
            if not tgt_lang:
                raise ValueError(
                    "This TTX file names no target language (UserSettings TargetLanguage), "
                    "so a target segment cannot be written."
                )
            ssp = spans[slot.src]
            name = ssp.name
            lang_attr = _with_attr(f"<{name}>", "Lang", tgt_lang)
            edits.add(ssp.end, ssp.end, f"{lang_attr}{inner}</{name}>")
        tsp = spans[slot.tu]
        tag = text[tsp.start : tsp.start_end]
        tag = _with_attr(tag, "MatchPercent", TTX_WRITTEN_MATCH)
        tag = _with_attr(tag, "Origin", TTX_WRITTEN_ORIGIN)
        edits.add(tsp.start, tsp.start_end, tag)

    us = langs.settings
    if target_locale and us is not None and not langs.target and _ttx_lang(target_locale):
        sp = spans[us]
        start = _with_attr(text[sp.start : sp.start_end], "TargetLanguage", tgt_lang)
        edits.add(sp.start, sp.end if sp.empty else sp.start_end, start)

    if not edits.items:
        return raw
    out = bom + edits.apply().encode(codec)
    _parse(out)  # the spliced file must still be a well-formed TTX
    return out


@register
class TtxFilter(Filter):
    fmt = "ttx"
    exts = ()
    writes_targets = True  # B-bytes: a target equal to its source is a real write

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        return parse_ttx(data)

    def build(self, original: bytes, paragraph_texts: dict, *, fallback_keys=None) -> bytes:
        return build_ttx(original, paragraph_texts, fallback_keys=fallback_keys)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        *,
        fallback_keys=None,
    ) -> bytes:
        """build() plus UserSettings TargetLanguage when the file has none (a file's own
        target language is never changed: Trados matches it to the project)."""
        return build_ttx(
            original, paragraph_texts, fallback_keys=fallback_keys, target_locale=target_locale
        )
