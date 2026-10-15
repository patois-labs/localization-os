"""EPUB filter: EPUB 2 and EPUB 3 publications (.epub), Batch 15.

An EPUB is an OCF ZIP container (W3C EPUB 3.3, section 4): a stored `mimetype` member
first, `META-INF/container.xml` naming the package document (OPF), and the publication
resources the OPF manifest lists. This filter:

  * opens the container only after `zip_guard.check` (bomb, ratio, member count, real
    stream sizes) and refuses a container whose `mimetype` is not `application/epub+zip`;
  * refuses an encrypted EPUB: any `META-INF/encryption.xml` entry that is not font
    obfuscation (IDPF `http://www.idpf.org/2008/embedding` or the legacy Adobe
    `http://ns.adobe.com/pdf/enc#RC`) of a resource we do not translate. Obfuscated fonts
    are fine: they are copied untouched and the unique identifier (the key) never changes;
  * reads `container.xml` -> the first `rootfile` (the default rendition) -> the OPF, with
    lxml hardened (no entities, no network, no DTD load, no huge tree, and any
    `<!ENTITY` declaration refused before parsing);
  * extracts, in this fixed order (parse and build walk the same order):
      1. OPF metadata: every `dc:title` and `dc:description` (style `epub:title` /
         `epub:description`);
      2. every XHTML content document in spine order, through `html_filter` (block units
         with inline-tag tokens, then that document's alt/title/aria-label units);
      3. the EPUB 3 navigation document (manifest `properties="nav"`) when it is not
         already in the spine, also through `html_filter`;
      4. the EPUB 2 NCX (`spine@toc` or the NCX manifest item): `docTitle`, every
         `navLabel` and `navInfo` text (style `epub:toc-label`);
  * leaves every other member (CSS, fonts, images, audio, scripts, META-INF files)
    byte-for-byte as it was.

build() returns the ORIGINAL bytes when no unit changed (a full byte-identical round
trip). Otherwise it rewrites the container: `mimetype` first, stored, no extra field
(EPUB 3.3 section 4.3.3), every other member in its original order, name, date,
attributes and compression method, with only the changed members' content replaced.

build_localized() also declares the target language: the first OPF `dc:language`, the
`package` xml:lang when present, spine `page-progression-direction` for a right-to-left
target (an existing `rtl` is set back to `ltr` for a left-to-right target), `lang` and
`xml:lang` (and `dir` for RTL) on every translated XHTML document's `html` element, and
the NCX `xml:lang` when present.

Not covered (listed in the handoff): SVG content documents in the spine, multiple
renditions beyond the default one, media overlays (SMIL), and `dc:subject` / creator
names (deliberately not translated).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import html.entities as _html_entities
import posixpath
import re
import zipfile
from io import BytesIO
from urllib.parse import unquote, urlsplit

from app import config
from app.filters import zip_guard
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    check_unit_texts,
    register,
)
from app.filters.xml_text import xml_target_text
from app.services.segment import strip_placeholders

MIMETYPE = b"application/epub+zip"
_CONTAINER = "META-INF/container.xml"
_ENCRYPTION = "META-INF/encryption.xml"
_OPF_MEDIA = "application/oebps-package+xml"
_XHTML_MEDIA = "application/xhtml+xml"
_NCX_MEDIA = "application/x-dtbncx+xml"

_NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
_NS_OPF = "http://www.idpf.org/2007/opf"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
_NS_ENC = "http://www.w3.org/2001/04/xmlenc#"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

# Font obfuscation, not DRM (EPUB 3.3 section 4.4; the Adobe one is the legacy variant).
_OBFUSCATION = frozenset({"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"})

_TRANSLATED_DC = (f"{{{_NS_DC}}}title", f"{{{_NS_DC}}}description")
_NCX_LABEL_PARENTS = frozenset(
    {f"{{{_NS_NCX}}}docTitle", f"{{{_NS_NCX}}}navLabel", f"{{{_NS_NCX}}}navInfo"}
)


def _refuse(msg: str) -> InputLimitError:
    return InputLimitError(f"This EPUB file {msg}. It was not processed.")


# --- hardened XML ---------------------------------------------------------------------
def _check_xml_budget(raw: bytes, what: str) -> None:
    """Pre-parse budget for one package XML part (OPF, NCX, container, encryption):
    forty markup characters per allowed segment bounds the lxml tree before it exists."""
    if raw.count(b"<") > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise _refuse(f"has far more markup in {what} than MAX_DOCUMENT_SEGMENTS allows")


def _parse_xml(raw: bytes, what: str):
    """Parse an untrusted package XML part. External DOCTYPEs (the EPUB 2 NCX carries
    one) are tolerated because nothing is ever loaded or resolved; an internal entity
    declaration in any encoding is refused before lxml sees it."""
    from lxml import etree

    _check_xml_budget(raw, what)
    for needle in ("<!ENTITY",):
        for enc in ("utf-8", "utf-16-le", "utf-16-be"):
            if needle.encode(enc) in raw:
                raise _refuse(f"declares XML entities in {what}")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
    )
    try:
        tree = etree.parse(BytesIO(raw), parser)
    except etree.XMLSyntaxError as exc:
        raise _refuse(f"has a damaged {what} ({str(exc)[:120]})") from exc
    # Charset-independent second check: lxml reports any entity declaration it saw
    # (the byte prescan above cannot read every encoding).
    dtd = tree.docinfo.internalDTD
    if dtd is not None and any(True for _ in dtd.iterentities()):
        raise _refuse(f"declares XML entities in {what}")
    return tree


def _serialize_xml(tree, original: bytes) -> bytes:
    """Serialize a changed package XML part, keeping the original XML declaration when it
    is a UTF-8 one (lxml writes its own otherwise) so only real content changes show."""
    from lxml import etree

    body = etree.tostring(tree, encoding="utf-8", xml_declaration=False)
    raw = original[3:] if original.startswith(b"\xef\xbb\xbf") else original
    if raw.startswith(b"<?xml"):
        end = raw.find(b"?>")
        decl = raw[: end + 2] if end != -1 else b""
        low = decl.lower()
        if decl and (b"encoding" not in low or b"utf-8" in low):
            return decl + b"\n" + body
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body


# --- container walk -----------------------------------------------------------------
def _href_path(base_dir: str, href: str) -> str | None:
    """Resolve a manifest href (a URL relative to the package document, EPUB 3.3 5.2) to
    a container member name, or None for a remote, absolute or escaping reference."""
    if not href:
        return None
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or parts.path.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(base_dir, unquote(parts.path)))
    if path in (".", "..") or path.startswith("../"):
        return None
    return path


class _Package:
    """Everything parse and build need, read the same way from the same bytes."""

    def __init__(self, data: bytes):
        zip_guard.check(data, "EPUB file")  # bomb / ratio / members / real stream sizes
        try:
            self.zf = zipfile.ZipFile(BytesIO(data))
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise _refuse("is not a valid ZIP container") from exc
        self.names = {i.filename: i for i in self.zf.infolist() if not i.is_dir()}
        if self._read("mimetype", required=True).strip() != MIMETYPE:
            raise _refuse("has no application/epub+zip mimetype")
        self.opf_path = self._rootfile()
        self.opf_raw = self._read(self.opf_path, required=True)
        self.opf = _parse_xml(self.opf_raw, "its package document")
        root = self.opf.getroot()
        if root.tag != f"{{{_NS_OPF}}}package":
            raise _refuse("has a package document that is not an OPF package")
        self.version = (root.get("version") or "").strip()
        base = posixpath.dirname(self.opf_path)
        manifest = {}
        for item in root.iter(f"{{{_NS_OPF}}}item"):
            path = _href_path(base, item.get("href", ""))
            manifest[item.get("id", "")] = {
                "path": path,
                "media": (item.get("media-type") or "").strip().lower(),
                "props": (item.get("properties") or "").split(),
            }
        spine = root.find(f"{{{_NS_OPF}}}spine")
        self.spine_el = spine
        # Spine XHTML content documents, in reading order, each once.
        self.xhtml: list[str] = []
        seen: set[str] = set()
        if spine is not None:
            for ref in spine.iter(f"{{{_NS_OPF}}}itemref"):
                item = manifest.get(ref.get("idref", ""))
                if not item or item["media"] != _XHTML_MEDIA or not item["path"]:
                    continue
                if item["path"] in seen or item["path"] not in self.names:
                    continue
                seen.add(item["path"])
                self.xhtml.append(item["path"])
        self.nav = None
        for item in manifest.values():
            if "nav" in item["props"] and item["path"] in self.names:
                if item["path"] not in seen and item["media"] == _XHTML_MEDIA:
                    self.nav = item["path"]
                break
        self.ncx = None
        toc_id = spine.get("toc") if spine is not None else None
        cand = manifest.get(toc_id or "")
        if not cand or cand["media"] != _NCX_MEDIA:
            cand = next((i for i in manifest.values() if i["media"] == _NCX_MEDIA), None)
        if cand and cand["path"] in self.names and cand["path"] not in seen:
            self.ncx = cand["path"]
        self._check_encryption()

    def _read(self, name: str, required: bool = False) -> bytes:
        info = self.names.get(name)
        if info is None:
            if required:
                raise _refuse(f"is missing {name}")
            return b""
        return self.zf.read(info)

    def _rootfile(self) -> str:
        tree = _parse_xml(self._read(_CONTAINER, required=True), "META-INF/container.xml")
        for rf in tree.getroot().iter(f"{{{_NS_CONTAINER}}}rootfile"):
            media = (rf.get("media-type") or "").strip().lower()
            path = _href_path("", rf.get("full-path", ""))
            if media == _OPF_MEDIA and path:
                return path  # the first one is the default rendition
        raise _refuse("names no package document in META-INF/container.xml")

    def translated_paths(self) -> set[str]:
        out = {self.opf_path, *self.xhtml}
        out.update(p for p in (self.nav, self.ncx) if p)
        return out

    def _check_encryption(self) -> None:
        """Refuse DRM: anything encrypted that is not font obfuscation of a resource we do
        not translate (EPUB 3.3 4.2.6.3.2 and 4.4)."""
        raw = self._read(_ENCRYPTION)
        if not raw:
            return
        tree = _parse_xml(raw, "META-INF/encryption.xml")
        ours = self.translated_paths()
        for ed in tree.getroot().iter(f"{{{_NS_ENC}}}EncryptedData"):
            method = ed.find(f"{{{_NS_ENC}}}EncryptionMethod")
            alg = (method.get("Algorithm") if method is not None else "") or ""
            ref = ed.find(f"{{{_NS_ENC}}}CipherData/{{{_NS_ENC}}}CipherReference")
            target = _href_path("", ref.get("URI", "")) if ref is not None else None
            if alg.strip() not in _OBFUSCATION or not target or target in ours:
                raise _refuse(
                    "is encrypted (DRM protected). Only font obfuscation is supported; "
                    "export a DRM-free EPUB from the source"
                )


# --- unit walk ------------------------------------------------------------------------
def _dc_elements(opf_tree) -> list:
    """The OPF metadata elements we translate, in document order: text-only dc:title and
    dc:description with something to translate."""
    out = []
    meta = opf_tree.getroot().find(f"{{{_NS_OPF}}}metadata")
    if meta is None:
        return out
    for el in meta.iter(*_TRANSLATED_DC):
        if len(el) == 0 and el.text and any(ch.isalnum() for ch in el.text):
            out.append(el)
    return out


def _ncx_elements(ncx_tree) -> list:
    out = []
    for el in ncx_tree.getroot().iter(f"{{{_NS_NCX}}}text"):
        parent = el.getparent()
        if parent is None or parent.tag not in _NCX_LABEL_PARENTS:
            continue
        if len(el) == 0 and el.text and any(ch.isalnum() for ch in el.text):
            out.append(el)
    return out


def _split_ws(s: str) -> tuple[str, str, str]:
    core = s.strip()
    lead = s[: len(s) - len(s.lstrip())]
    trail = s[len(s.rstrip()) :] if core else ""
    return lead, core, trail


def _set_text(el, new: str) -> bool:
    lead, core, trail = _split_ws(el.text or "")
    clean = xml_target_text(strip_placeholders(str(new)))
    if clean == core:
        return False
    el.text = lead + clean + trail
    return True


def _html():
    from app.filters.html_filter import HtmlFilter

    # #733: no per-chapter page metadata units: chapters are concatenated, so a unit
    # added in one chapter would move every later chapter's stored indices.
    return HtmlFilter(page_meta=False)


def _sub_texts(paragraph_texts: dict, base: int, count: int) -> dict:
    out = {}
    for k in range(count):
        v = paragraph_texts.get(str(base + k))
        if v is not None:
            out[str(k)] = v
    return out


_NAMED_REF_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_PREDEFINED = frozenset({"amp", "lt", "gt", "quot", "apos"})


def _xml_named_refs(text: str) -> str:
    """XHTML chapters are XML read without a DTD: an HTML-only named reference such as
    &nbsp; would make the chapter invalid. Rewrite each one as numeric character
    references; an unknown name becomes literal text (&amp;name;)."""

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_PREDEFINED:
            return m.group(0)
        chars = _html_entities.html5.get(name + ";")
        if chars is None:
            return "&amp;" + name + ";"
        return "".join(f"&#{ord(ch)};" for ch in chars)

    return _NAMED_REF_RE.sub(_sub, text)


def _xhtml_safe(sub: dict, chapter_units: list) -> dict:
    """XML 1.0 characters only in every chapter target; body units (entity-escaped text,
    style "") also get XML-safe character references. Attribute units are plain text
    the HTML writer escapes in full, so their entities stay literal and valid."""
    out = {}
    for k, v in sub.items():
        unit = chapter_units[int(k)]
        if v == unit.text:
            out[k] = v  # unchanged: html_filter keeps the original bytes
            continue
        v = xml_target_text(str(v))
        out[k] = v if unit.style else _xml_named_refs(v)
    return out


def _stamp_xhtml(raw: bytes, tag: str, direction: str) -> bytes:
    """html_filter's own declaration (lang, existing xml:lang, dir, content-language meta),
    plus xml:lang on the html element: in XHTML it is the attribute that counts."""
    from app.filters import html_filter as h

    tokens = h._tokenize(h.decode_text_lossless(raw, declared=True))
    h._declare_language(tokens, tag, direction)
    for ti, name in h._iter_start_tags(tokens):
        if name == "html":
            tokens[ti] = h._set_attr(tokens[ti], "xml:lang", tag)
            break
    return h.encode_text_lossless("".join(tokens))


@register
class EpubFilter(Filter):
    # #703: raw <, > and & in a target are escaped on export (quality entity gate).
    escapes_raw_text = True
    fmt = "epub"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        pkg = _Package(bytes(data))
        units: list[TextUnit] = []
        parts: list[dict] = []
        images = 0
        missing: list[dict] = []

        def add(text: str, style: str = "", inline_map: dict | None = None) -> None:
            units.append(
                TextUnit(p_index=len(units), text=text, style=style, inline_map=inline_map or {})
            )
            check_segment_cap(len(units))

        start = len(units)
        for el in _dc_elements(pkg.opf):
            add(el.text.strip(), "epub:" + el.tag.rsplit("}", 1)[-1])
        parts.append({"path": pkg.opf_path, "kind": "opf", "count": len(units) - start})

        html = _html()
        for path, kind in [(p, "xhtml") for p in pkg.xhtml] + (
            [(pkg.nav, "nav")] if pkg.nav else []
        ):
            base = len(units)
            res = html.parse(pkg._read(path))
            for u in res.units:
                add(u.text, u.style, u.inline_map)
            parts.append({"path": path, "kind": kind, "count": len(units) - base})
            a11y_info = res.skeleton.get("a11y_images")
            if a11y_info:
                images += a11y_info.get("images", 0)
                for f in a11y_info.get("missing_alt", []):
                    anchor = f.get("unit")
                    missing.append({**f, "unit": None if anchor is None else anchor + base})

        if pkg.ncx:
            start = len(units)
            ncx = _parse_xml(pkg._read(pkg.ncx), "its NCX table of contents")
            for el in _ncx_elements(ncx):
                add(el.text.strip(), "epub:toc-label")
            parts.append({"path": pkg.ncx, "kind": "ncx", "count": len(units) - start})

        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "epub",
            "epub_version": pkg.version,
            "package": pkg.opf_path,
            "parts": parts,
            "units": [{"p_index": u.p_index, "text": u.text, "style": u.style} for u in units],
        }
        if images:
            skeleton["a11y_images"] = {"images": images, "missing_alt": missing}
        return ParseResult(skeleton=skeleton, units=units)

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._build(bytes(original), paragraph_texts, None)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        from app.filters import a11y

        tag = a11y.target_tag(target_locale)
        lang = (tag, a11y.target_direction(target_locale)) if tag else None
        return self._build(bytes(original), paragraph_texts, lang)

    def _build(self, original: bytes, paragraph_texts: dict, lang) -> bytes:
        pkg = _Package(original)
        replaced: dict[str, bytes] = {}
        pos = 0

        # 1. OPF metadata (+ language declaration)
        changed = False
        for el in _dc_elements(pkg.opf):
            new = paragraph_texts.get(str(pos))
            pos += 1
            if new is not None:
                changed = _set_text(el, new) or changed
        if lang:
            changed = _declare_opf_language(pkg, *lang) or changed
        if changed:
            replaced[pkg.opf_path] = _serialize_xml(pkg.opf, pkg.opf_raw)

        # 2-3. XHTML content documents in spine order, then a non-spine nav document
        html = _html()
        for path in pkg.xhtml + ([pkg.nav] if pkg.nav else []):
            raw = pkg._read(path)
            chapter_units = html.parse(raw, enforce_caps=False).units
            count = len(chapter_units)
            sub = _xhtml_safe(_sub_texts(paragraph_texts, pos, count), chapter_units)
            pos += count
            if not sub and not lang:
                continue
            src = _stamp_xhtml(raw, *lang) if lang else raw
            out = html.build(src, sub) if sub else src
            if out != raw:
                replaced[path] = out

        # 4. NCX
        if pkg.ncx:
            raw = pkg._read(pkg.ncx)
            ncx = _parse_xml(raw, "its NCX table of contents")
            changed = False
            for el in _ncx_elements(ncx):
                new = paragraph_texts.get(str(pos))
                pos += 1
                if new is not None:
                    changed = _set_text(el, new) or changed
            root = ncx.getroot()
            if lang and root.get(_XML_LANG) is not None and root.get(_XML_LANG) != lang[0]:
                root.set(_XML_LANG, lang[0])
                changed = True
            if changed:
                replaced[pkg.ncx] = _serialize_xml(ncx, raw)

        if not replaced:
            return original
        return _rezip(pkg.zf, replaced)


def _declare_opf_language(pkg: _Package, tag: str, direction: str) -> bool:
    root = pkg.opf.getroot()
    changed = False
    meta = root.find(f"{{{_NS_OPF}}}metadata")
    first = meta.find(f".//{{{_NS_DC}}}language") if meta is not None else None
    if first is not None and (first.text or "").strip() != tag:
        lead, _core, trail = _split_ws(first.text or "")
        first.text = lead + tag + trail
        changed = True
    if root.get(_XML_LANG) is not None and root.get(_XML_LANG) != tag:
        root.set(_XML_LANG, tag)
        changed = True
    spine = pkg.spine_el
    if spine is not None:
        ppd = spine.get("page-progression-direction")
        if direction == "rtl" and ppd != "rtl":
            spine.set("page-progression-direction", "rtl")
            changed = True
        elif direction == "ltr" and ppd == "rtl":
            spine.set("page-progression-direction", "ltr")
            changed = True
    return changed


def _rezip(zf: zipfile.ZipFile, replaced: dict[str, bytes]) -> bytes:
    """Rewrite the container: `mimetype` first, stored, no extra field (EPUB 3.3 4.3.3);
    every other member in its original order with its original metadata and compression
    method, content replaced only where `replaced` says so."""
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        src = zf.getinfo("mimetype")
        mi = zipfile.ZipInfo("mimetype", date_time=src.date_time)
        mi.compress_type = zipfile.ZIP_STORED
        mi.external_attr = src.external_attr
        z.writestr(mi, MIMETYPE)
        for info in zf.infolist():
            if info.filename == "mimetype":
                continue
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = (
                zipfile.ZIP_DEFLATED
                if info.compress_type == zipfile.ZIP_DEFLATED
                else zipfile.ZIP_STORED
            )
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zi.comment = info.comment
            data = b"" if info.is_dir() else replaced.get(info.filename, zf.read(info))
            z.writestr(zi, data)
    return out.getvalue()
