"""Strict Open XML packages (ISO/IEC 29500 Strict) for the xlsx, docx and pptx filters (#669).

Office can save a workbook, document or presentation as "Strict Open XML". The package
layout and content types are the same as the usual Transitional files; what changes is
the namespace of every part (http://purl.oclc.org/ooxml/... instead of
http://schemas.openxmlformats.org/...) and the relationship types. openpyxl, python-docx
and the PowerPoint filter only know the Transitional names, so a Strict file used to read
as empty (openpyxl: zero sheets, then "At least one sheet must be visible" on rebuild).

`to_transitional()` rewrites the namespace URIs of every XML part to their Transitional
twins (ISO/IEC 29500-1 Strict and -4 Transitional name the same elements and
attributes), and `to_strict()` maps them back. The swap only touches quoted attribute
values (xmlns declarations, relationship Type attributes), never element text. A Strict
URI this table does not know is refused with a plain message instead of being half
read: an unknown variant must never parse to "no text" or crash on export.

Which way each filter goes:
  - pptx, docx, xlsx: read Transitional, write back Strict. All three edit the original
    parts in place (xlsx since #685; before it openpyxl regenerated the workbook as
    Transitional), so the rebuilt package is the customer's Strict file with the new text.
"""

import io
import re
import zipfile

from app.filters.base import InputLimitError

STRICT_PREFIX = "http://purl.oclc.org/ooxml/"
_T = "http://schemas.openxmlformats.org/"

# Strict namespace -> Transitional namespace (ISO/IEC 29500-1:2016 Annex A; the Open XML
# SDK uses the same pairs). Relationship types are the relationships namespace plus a
# name and are mapped through the prefix below.
NAMESPACES = {
    STRICT_PREFIX + "spreadsheetml/main": _T + "spreadsheetml/2006/main",
    STRICT_PREFIX + "wordprocessingml/main": _T + "wordprocessingml/2006/main",
    STRICT_PREFIX + "presentationml/main": _T + "presentationml/2006/main",
    STRICT_PREFIX + "drawingml/main": _T + "drawingml/2006/main",
    STRICT_PREFIX + "drawingml/chart": _T + "drawingml/2006/chart",
    STRICT_PREFIX + "drawingml/chartDrawing": _T + "drawingml/2006/chartDrawing",
    STRICT_PREFIX + "drawingml/diagram": _T + "drawingml/2006/diagram",
    STRICT_PREFIX + "drawingml/picture": _T + "drawingml/2006/picture",
    STRICT_PREFIX + "drawingml/spreadsheetDrawing": _T + "drawingml/2006/spreadsheetDrawing",
    STRICT_PREFIX + "drawingml/wordprocessingDrawing": _T + "drawingml/2006/wordprocessingDrawing",
    STRICT_PREFIX + "drawingml/lockedCanvas": _T + "drawingml/2006/lockedCanvas",
    STRICT_PREFIX + "drawingml/compatibility": _T + "drawingml/2006/compatibility",
    STRICT_PREFIX + "drawingml/table": _T + "drawingml/2006/table",
    STRICT_PREFIX + "officeDocument/math": _T + "officeDocument/2006/math",
    STRICT_PREFIX + "officeDocument/bibliography": _T + "officeDocument/2006/bibliography",
    STRICT_PREFIX + "officeDocument/characteristics": _T + "officeDocument/2006/characteristics",
    STRICT_PREFIX + "officeDocument/customXml": _T + "officeDocument/2006/customXml",
    STRICT_PREFIX + "officeDocument/docPropsVTypes": _T + "officeDocument/2006/docPropsVTypes",
    STRICT_PREFIX + "officeDocument/extendedProperties": _T
    + "officeDocument/2006/extended-properties",
    STRICT_PREFIX + "officeDocument/customProperties": _T + "officeDocument/2006/custom-properties",
    STRICT_PREFIX + "officeDocument/sharedTypes": _T + "officeDocument/2006/sharedTypes",
    STRICT_PREFIX + "schemaLibrary/main": _T + "schemaLibrary/2006/main",
}
_STRICT_RELS = STRICT_PREFIX + "officeDocument/relationships"
_TRANS_RELS = _T + "officeDocument/2006/relationships"
NAMESPACES[_STRICT_RELS] = _TRANS_RELS
# relationship type names that differ between the two (the rest are spelled the same)
_REL_RENAMES = {
    "extendedProperties": "extended-properties",
    "customProperties": "custom-properties",
}

_TO_STRICT = {t: s for s, t in NAMESPACES.items()}
_REL_RENAMES_BACK = {t: s for s, t in _REL_RENAMES.items()}

# a namespace or relationship-type URI standing alone in a quoted attribute value
_STRICT_ATTR = re.compile(rb"""(["'])(http://purl\.oclc\.org/ooxml/[^"'<>\s]*)\1""")
_TRANS_ATTR = re.compile(
    rb"""(["'])(http://schemas\.openxmlformats\.org/(?!package/|markup-compatibility/)"""
    rb"""[^"'<>\s]*)\1"""
)
_XML_SUFFIXES = (".xml", ".rels", ".vml")


def refusal(label: str) -> InputLimitError:
    return InputLimitError(
        f"This {label} is saved as Strict Open XML with parts this version cannot read. "
        "Save it in the standard Office format (for example Excel Workbook, Word Document "
        "or PowerPoint Presentation) and upload it again."
    )


def _map_strict(uri: str, label: str) -> str:
    if uri in NAMESPACES:
        return NAMESPACES[uri]
    if uri.startswith(_STRICT_RELS + "/"):
        name = uri[len(_STRICT_RELS) + 1 :]
        return _TRANS_RELS + "/" + _REL_RENAMES.get(name, name)
    raise refusal(label)


def _map_transitional(uri: str) -> str:
    if uri in _TO_STRICT:
        return _TO_STRICT[uri]
    if uri.startswith(_TRANS_RELS + "/"):
        name = uri[len(_TRANS_RELS) + 1 :]
        return _STRICT_RELS + "/" + _REL_RENAMES_BACK.get(name, name)
    return uri  # Transitional-only names (e.g. VML-era URIs) stay as they are


def part_to_transitional(xml: bytes, label: str) -> bytes:
    """One XML part with every Strict namespace / relationship type made Transitional."""
    if b"purl.oclc.org/ooxml/" not in xml:
        return xml

    def sub(m):
        q = m.group(1)
        return q + _map_strict(m.group(2).decode("ascii", "replace"), label).encode() + q

    out = _STRICT_ATTR.sub(sub, xml)
    if b'"http://purl.oclc.org/ooxml/' in out or b"'http://purl.oclc.org/ooxml/" in out:
        raise refusal(label)  # a Strict URI in a form the swap did not recognise
    return out


def part_to_strict(xml: bytes) -> bytes:
    """The inverse of part_to_transitional, for a part a filter re-serialized."""
    if b"schemas.openxmlformats.org/" not in xml:
        return xml

    def sub(m):
        q = m.group(1)
        return q + _map_transitional(m.group(2).decode("ascii", "replace")).encode() + q

    return _TRANS_ATTR.sub(sub, xml)


def is_strict(data: bytes) -> bool:
    """True when the package's root relationships name a Strict main part. Cheap: one
    small member read. A blob that is not a zip is left to the filter's own checks."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            try:
                rels = zf.read("_rels/.rels")
            except KeyError:
                return False
    except (zipfile.BadZipFile, ValueError, OSError, NotImplementedError, RuntimeError):
        return False
    return (_STRICT_RELS + "/officeDocument").encode() in rels


def _rewrite(data: bytes, fn) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            raw = src.read(info)
            name = info.filename.lower()
            if name.endswith(_XML_SUFFIXES) or info.filename == "[Content_Types].xml":
                new = fn(raw)
                if new != raw:
                    zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = info.external_attr
                    dst.writestr(zi, new)
                    continue
            dst.writestr(info, raw)
    return out.getvalue()


def to_transitional(data: bytes, label: str) -> tuple[bytes, bool]:
    """(package, was_strict): a Strict package rewritten with Transitional names, or the
    original bytes untouched when it is not Strict. The caller has already run its zip
    guard on `data`; the rewrite only swaps attribute values, so part sizes barely move."""
    if not is_strict(data):
        return data, False
    try:
        return _rewrite(data, lambda raw: part_to_transitional(raw, label)), True
    except InputLimitError:
        raise
    except Exception as exc:  # a member that cannot be read: refuse, never crash
        raise refusal(label) from exc


def to_strict(original: bytes, normalized: bytes, rebuilt: bytes) -> bytes:
    """A rebuilt package mapped back to Strict names.

    `original` is the uploaded Strict package, `normalized` what to_transitional made of
    it and `rebuilt` the filter's output. A member the filter left byte-identical to its
    normalized copy gets the ORIGINAL Strict bytes back (so untouched parts round-trip
    exactly); a member it rewrote or added has its Transitional names mapped to Strict."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(original)) as orig,
        zipfile.ZipFile(io.BytesIO(normalized)) as norm,
        zipfile.ZipFile(io.BytesIO(rebuilt)) as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        orig_names = set(orig.namelist())
        norm_names = set(norm.namelist())
        for info in src.infolist():
            raw = src.read(info)
            name = info.filename
            if name in norm_names and name in orig_names and raw == norm.read(name):
                new = orig.read(name)
            elif name.lower().endswith(_XML_SUFFIXES) or name == "[Content_Types].xml":
                new = part_to_strict(raw)
            else:
                new = raw
            if new is raw:
                dst.writestr(info, raw)
            else:
                zi = zipfile.ZipInfo(name, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                dst.writestr(zi, new)
    return out.getvalue()
