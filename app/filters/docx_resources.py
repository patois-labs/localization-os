"""Offline DOCX external-resource inventory, recorded in the ingest skeleton.

Never resolves relationships or returns clickable attacker-controlled targets.
Package contents remain unchanged; users can remove links in Word and re-upload.

Documented in: docs/architecture/translation-pipeline.md
"""

import re
import zlib
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from lxml import etree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_STRICT_HYPERLINK = "http://purl.oclc.org/ooxml/officeDocument/relationships/hyperlink"
_COMMAND = re.compile(r"^\s*(INCLUDEPICTURE|INCLUDETEXT|LINK|DDEAUTO|DDE)\b", re.I)
_MAX_PART_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
# Content types of the parts the DOCX walk translates (docx_filter walk 4).
_WALKED_PARTS = (
    ".main+xml",
    ".header+xml",
    ".footer+xml",
    ".footnotes+xml",
    ".endnotes+xml",
    ".comments+xml",
)


def _block_level(sdt) -> bool:
    """True for a content control that holds blocks (the walk translates it): its nearest
    enclosing paragraph or text box is a text box, or there is no paragraph above it."""
    for a in sdt.iterancestors():
        if a.tag == _W + "p":
            return False
        if a.tag == _W + "txbxContent":
            return True
    return True


# Field types Word recomputes by itself (when the file opens or prints), so their cached
# result is no text anyone translates.
_SELF_UPDATING_FIELDS = frozenset(
    {
        "PAGE",
        "NUMPAGES",
        "SECTIONPAGES",
        "SECTION",
        "DATE",
        "TIME",
        "CREATEDATE",
        "SAVEDATE",
        "PRINTDATE",
        "EDITTIME",
        "NUMWORDS",
        "NUMCHARS",
    }
)


def _field_result_carried(fld) -> bool:
    """True for a w:fldSimple whose cached result has words and is not a field Word
    recomputes on its own (page numbers, dates). The walk carries that result as source
    text: Word regenerates it on a field update, so translating the cache would be undone,
    and a cache nobody updates would be left in the source language."""
    instr = (fld.get(_W + "instr") or "").split(None, 1)
    if instr and instr[0].upper() in _SELF_UPDATING_FIELDS:
        return False
    return any(c.isalpha() for t in fld.iter(_W + "t") for c in (t.text or ""))


def _table_depth(table) -> int:
    """How many tables enclose `table`, counting up to the nearest text box (the walk
    restarts its nesting count inside a text box)."""
    depth = 0
    for a in table.iterancestors():
        if a.tag == _W + "txbxContent":
            break
        if a.tag == _W + "tbl":
            depth += 1
    return depth


def external_resource_warnings(data: bytes) -> list[dict]:
    """Find non-hyperlink external rels and fetching fields in every XML part."""
    warnings = []

    def add(part, kind):
        warnings.append(
            {
                "code": "docx_external_resource",
                "part": part,
                "kind": kind,
                "message": (
                    "This DOCX contains external resources. Opening the source or translation "
                    "in Word may contact another computer and expose network credentials. "
                    "Remove external links in Word and re-upload before sharing."
                ),
            }
        )

    def field(part, instruction):
        match = _COMMAND.match(instruction)
        if match:
            add(part, match[1].upper())

    with ZipFile(BytesIO(data)) as archive:
        consumed = 0

        def read_part(name):
            nonlocal consumed
            try:
                info = archive.getinfo(name)
                limit = min(_MAX_PART_BYTES, _MAX_TOTAL_BYTES - consumed)
                if info.file_size > limit:
                    raise ValueError("scan size limit exceeded")
                # Charge failed reads too, so corrupt parts cannot reset the budget.
                consumed += info.file_size
                with archive.open(info) as stream:
                    raw = stream.read(limit + 1)
                consumed += max(0, len(raw) - info.file_size)
                if len(raw) > limit:
                    raise ValueError("scan size limit exceeded")
                return etree.fromstring(
                    raw,
                    parser=etree.XMLParser(resolve_entities=False, no_network=True),
                )
            except (
                etree.XMLSyntaxError,
                BadZipFile,
                KeyError,
                ValueError,
                OSError,
                RuntimeError,
                NotImplementedError,
                zlib.error,
            ) as exc:
                warnings.append(
                    {
                        "code": "docx_unscanned_part",
                        "part": name,
                        "kind": "unscanned",
                        "message": (
                            f"DOCX part {name} could not be checked for external resources "
                            f"({type(exc).__name__}). Review the file in Word before sharing."
                        ),
                    }
                )
                return None

        types = read_part("[Content_Types].xml")
        defaults, overrides = {}, {}
        if types is not None:
            for item in types:
                content_type = (item.get("ContentType") or "").lower()
                if item.get("PartName"):
                    overrides[item.get("PartName").lstrip("/").lower()] = content_type
                elif item.get("Extension"):
                    defaults[item.get("Extension").lower()] = content_type
        for name in sorted(archive.namelist()):
            if name == "[Content_Types].xml":
                continue
            lower = name.lower()
            content_type = overrides.get(lower, defaults.get(lower.rsplit(".", 1)[-1], ""))
            if not (content_type.endswith("xml") or lower.endswith((".rels", ".xml"))):
                continue
            root = read_part(name)
            if root is None:
                continue
            regions = set()
            # #270 (walk 4): the main part, headers, footers, footnotes, endnotes and
            # comments are walked, so their block and cell content controls, text boxes,
            # tracked insertions and moves and tables nested up to MAX_TABLE_DEPTH are
            # translated. Walk 6 (#686) also reads tracked insertions, moves, smart tags
            # and text content controls inside a paragraph, nested in each other, in
            # hyperlinks or in inline custom XML (`walk6_reads`). Only such an element
            # held by something else (a field, a deletion, a list / date / check box
            # control) and a table nested deeper than the cap stay untranslated there. Any
            # other part with text (the glossary) keeps the whole-part rule.
            from app.filters.docx_filter import MAX_TABLE_DEPTH, walk6_reads

            walked = "wordprocessingml" in content_type and content_type.endswith(_WALKED_PARTS)
            if any(t.text for t in root.iter(_W + "t")):
                if not walked:
                    for stem in ("header", "footer", "footnotes", "endnotes", "comments"):
                        if lower.startswith("word/" + stem):
                            regions.add(stem)
                for tag, label in (
                    ("txbxContent", "text boxes"),
                    ("sdt", "content controls (including TOC)"),
                    ("ins", "tracked insertions"),
                    ("smartTag", "smart tags"),
                ):
                    if walked and tag == "txbxContent":
                        continue
                    if tag == "smartTag":
                        for e in root.iter(_W + tag):
                            if not (walked and walk6_reads(e)) and any(
                                t.text for t in e.iter(_W + "t")
                            ):
                                regions.add(label)
                                break
                        continue
                    tags = (_W + "ins", _W + "moveTo") if walked and tag == "ins" else (_W + tag,)
                    for e in root.iter(*tags):
                        if walked and ((tag == "sdt" and _block_level(e)) or walk6_reads(e)):
                            continue
                        if any(t.text for t in e.iter(_W + "t")):
                            regions.add(label)
                            break
                if any(_field_result_carried(f) for f in root.iter(_W + "fldSimple")):
                    regions.add("simple field results")
                for table in root.iter(_W + "tbl"):
                    depth = _table_depth(table)
                    if depth >= (MAX_TABLE_DEPTH if walked else 1):
                        regions.add("nested tables")
            if regions:
                warnings.append(
                    {
                        "code": "docx_untranslated_regions",
                        "part": name,
                        "kind": "untranslated",
                        "message": "Untranslated DOCX regions: "
                        + ", ".join(sorted(regions))
                        + ". Review these in Word before delivery.",
                    }
                )
            # Only where the walk writes a translation for Word to put the source back over:
            # a part nobody translates (the glossary's building blocks) is named above.
            if walked and any(
                any(t.text for t in sdt.iter(_W + "t"))
                for sdt in root.iter(_W + "sdt")
                if sdt.find(_W + "sdtPr/" + _W + "dataBinding") is not None
            ):
                warnings.append(
                    {
                        "code": "docx_data_bound_controls",
                        "part": name,
                        "kind": "data_bound",
                        "message": (
                            "This DOCX has content controls bound to custom XML data. Word "
                            "can refill them from that data when the file opens, which would "
                            "put the source text back over the translation. Check them in "
                            "Word, or unbind them, before delivery."
                        ),
                    }
                )
            if lower.endswith(".rels"):
                for rel in root.iter(_REL):
                    if (rel.get("TargetMode") or "").lower() == "external" and rel.get(
                        "Type"
                    ) not in (_HYPERLINK, _STRICT_HYPERLINK):
                        add(name, "relationship")
                continue
            # Field instructions can span runs and paragraphs. Track nested fields
            # separately and stop collecting at the result separator.
            stack = []
            for el in root.iter():
                if el.tag == _W + "fldSimple":
                    field(name, el.get(_W + "instr", ""))
                elif el.tag == _W + "fldChar":
                    kind = el.get(_W + "fldCharType")
                    if kind == "begin":
                        stack.append([])
                    elif kind in ("separate", "end") and stack:
                        instruction = stack[-1]
                        if instruction is not None:
                            field(name, "".join(instruction))
                            stack[-1] = None
                        if kind == "end":
                            stack.pop()
                elif el.tag == _W + "instrText":
                    if stack:
                        if stack[-1] is not None:
                            stack[-1].append(el.text or "")
                    else:
                        field(name, el.text or "")
            for instruction in stack:
                if instruction:
                    field(name, "".join(instruction))
    return warnings
