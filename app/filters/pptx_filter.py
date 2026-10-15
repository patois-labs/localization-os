"""PPTX filter: PowerPoint presentations (PresentationML + DrawingML), lxml only.

Formats: .pptx and its macro / template / show siblings .pptm .potx .potm .ppsx .ppsm.
They share one package layout (ECMA-376 Part 1, 13 "PresentationML"); only the main
part's content type in [Content_Types].xml differs, and that part is never rewritten,
so a .pptm stays a macro presentation and its ppt/vbaProject.bin is copied byte for
byte. No python-pptx: the package is read with zipfile (behind zip_guard) and only the
XML parts that hold translated text are re-serialized; every other member, including
[Content_Types].xml, keeps its exact bytes.

WHAT IS TRANSLATED, in this order (the order IS the unit numbering, so parse and build
walk exactly the same paragraphs; stamped as skeleton["pptx_walk"]):
  for each slide in presentation.xml p:sldIdLst order:
    1. the slide's own text: every a:p of shapes, group shapes and table cells, in XML
       order (merged-away table cells, hMerge / vMerge, are skipped);
    2. SmartArt text (diagramData parts, dgm:pt/dgm:t) and chart rich text (titles,
       axis titles, text boxes: c:rich and c:txPr-free a:p) in the order the slide
       references them;
    3. the speaker notes (notesSlide), included by default and marked "notes" in the
       unit style (Smartcat excludes notes by default and users miss them).
  Hidden slides (p:sld show="0") are translated too, and marked "hidden" in the style.
  Then the alt text (p:cNvPr descr / title) of every shape on the slides, as the DOCX
  filter does, styled a11y:img-alt / a11y:img-title.

COMMENTS (#690) are units only when the project's "translate comments" option is on
(parse(comments=True)): per slide in the same order, classic comments (p:cm/p:text,
ppt/comments/commentN.xml) and modern comments with their replies (PowerPoint 365,
p188:txBody/a:p), styled "slide N comment" at office_comments.COMMENT_BASE + j. Authors
(commentAuthors.xml / authors.xml), dates and positions never change. build() writes a
comment key whenever the mapping has one, so no stamp is needed.

NOT translated: slide layouts and masters (their placeholder prompt text is not shown
in a slide show and a slide's own placeholder text overrides it), handout and notes
masters, chart category / series caches (they mirror the embedded workbook),
mc:Fallback copies (the Choice copy is the one every current reader shows), field
results (a:fld: slide number, date) which PowerPoint recomputes.

INLINE RUNS mirror the DOCX design (docx_filter): a paragraph with one text run is plain
text; with more, each a:r becomes ⟦rN⟧text⟦/rN⟧ and build() re-creates one a:r per span
with a deep copy of that run's a:rPr (bold, colour, a:hlinkClick hyperlink). Line breaks
(a:br) and fields (a:fld) are point tokens ⟦xN⟧ put back as the original elements; a
point the translation dropped is appended at the end, never deleted.

SmartArt has a second, cached rendering in ppt/diagrams/drawing*.xml (dsp:sp with the
data point's modelId). After a diagramData part is rewritten, each cached shape whose
paragraphs read exactly like the source point gets the same translation, so readers that
draw from the cache show the translation too.

References: ECMA-376 5th ed. Part 1 (https://ecma-international.org/publications-and-
standards/standards/ecma-376/), sections 13.3.8 (Presentation), 13.3.10 (Slide, show
attribute), 19.3.1.43 (sld), 21.1.2.2.7 (a:p), 21.1.2.3.8 (a:r), 21.1.2.2.4 (a:fld),
21.1.2.3.9 (a:rPr lang); Microsoft Learn "Structure of a PresentationML document"
(https://learn.microsoft.com/en-us/office/open-xml/presentation/structure-of-a-
presentationml-document) and "Working with presentation slides"
(https://learn.microsoft.com/en-us/office/open-xml/presentation/working-with-
presentation-slides); [MS-OFFMACRO2] for the macro-enabled content types
(https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-offmacro2/).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import copy
import io
import posixpath
import zipfile

from app.filters import ooxml_strict, zip_guard
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    register,
)
from app.services.segment import ph_close, ph_open, ph_point

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
_DSP = "{http://schemas.microsoft.com/office/drawing/2008/diagram}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"

_A_P, _A_R, _A_T, _A_BR, _A_FLD = _A + "p", _A + "r", _A + "t", _A + "br", _A + "fld"
_A_RPR, _A_PPR, _A_END = _A + "rPr", _A + "pPr", _A + "endParaRPr"
_A_TC = _A + "tc"
_CNVPR = _P + "cNvPr"

# Walk stamp (see the module docstring). A stored skeleton is exported under the walk it
# was numbered by; a stamp this build does not know is refused rather than guessed.
PPTX_WALK_VERSION = 1
_KNOWN_WALKS = (1,)

# Main-part content types of the PresentationML family (ECMA-376 Part 1 13.3.6 and
# [MS-OFFMACRO2] 2.2.1.x). The package is accepted only when its main part is one of these.
PRESENTATION_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml": "ppsx",
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml": "potx",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml": "pptm",
    "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml": "ppsm",
    "application/vnd.ms-powerpoint.template.macroEnabled.main+xml": "potm",
}

_ALT_ATTRS = (("descr", "a11y:img-alt"), ("title", "a11y:img-title"))


def _xml_parser():
    from lxml import etree

    # Same posture as xliff_filter / glossary_tbx: no entities, no DTD, no network,
    # no huge-tree escape hatch (XXE and billion laughs).
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_blank_text=False,
    )


def _label() -> str:
    return "PowerPoint file"


def _parse_xml(raw: bytes, name: str):
    from lxml import etree

    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
        raise InputLimitError(f"This {_label()} contains an unsupported XML document type.")
    try:
        root = etree.fromstring(raw, _xml_parser())
    except etree.XMLSyntaxError as exc:
        raise InputLimitError(
            f"This {_label()} has invalid XML in {name}. Save it again in PowerPoint."
        ) from exc
    if root.getroottree().docinfo.doctype:
        raise InputLimitError(f"This {_label()} contains an unsupported XML document type.")
    return root


def _rel_type(rel) -> str:
    """The last path segment of a relationship type: transitional and strict
    (http://purl.oclc.org/ooxml/...) packages name the same relationship."""
    return (rel.get("Type") or "").rsplit("/", 1)[-1]


def _rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def _resolve(part: str, target: str) -> str | None:
    """Package path of a relationship target relative to `part` (ECMA-376 Part 2, 9.3),
    or None when it escapes the package root."""
    if target.startswith("/"):
        path = posixpath.normpath(target.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
    if path.startswith("..") or path.startswith("/") or path == ".":
        return None
    return path


class _Package:
    """Read-only view of the zip with cached, parsed XML parts."""

    def __init__(self, data: bytes):
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise InputLimitError(
                f"This {_label()} is not a valid PowerPoint package. Save it again in PowerPoint."
            ) from exc
        self.names = set(self.zf.namelist())
        self.roots: dict = {}

    def xml(self, name: str):
        if name not in self.roots:
            if name not in self.names:
                return None
            self.roots[name] = _parse_xml(self.zf.read(name), name)
        return self.roots[name]

    def rels(self, part: str) -> dict:
        """rId -> (type suffix, resolved path) for the internal relationships of `part`."""
        root = self.xml(_rels_name(part))
        out: dict = {}
        if root is None:
            return out
        for rel in root.iter(_PKG_REL):
            if (rel.get("TargetMode") or "").lower() == "external":
                continue
            path = _resolve(part, rel.get("Target") or "")
            if path is not None:
                out[rel.get("Id")] = (_rel_type(rel), path)
        return out


def _main_part(pkg: _Package) -> str:
    rels = pkg.rels("")  # _rels/.rels
    for _rid, (kind, path) in sorted(rels.items()):
        if kind == "officeDocument" and path in pkg.names:
            root = pkg.xml(path)
            if root is not None and root.tag == _P + "presentation":
                return path
    raise InputLimitError(
        f"This {_label()} is not a PowerPoint presentation (no presentation part)."
    )


def check_markup_budget(data: bytes) -> None:
    """Refuse a package whose PresentationML / DrawingML parts carry far more runs or
    markup than MAX_DOCUMENT_SEGMENTS allows, before lxml builds any tree (#571
    pattern, same ratios as the DOCX filter's _check_markup_budget). zip_guard has
    already bounded the inflated sizes."""
    from app import config

    runs = elements = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not (name.startswith("ppt/") and name.endswith(".xml")):
                continue
            xml = zf.read(info)
            runs += xml.count(b"<a:r>") + xml.count(b"<a:r ")
            elements += xml.count(b"<")
    if runs > 2 * config.MAX_DOCUMENT_SEGMENTS or elements > 40 * config.MAX_DOCUMENT_SEGMENTS:
        raise InputLimitError(
            "This PowerPoint file has far more formatting runs or markup than "
            f"MAX_DOCUMENT_SEGMENTS allows ({config.MAX_DOCUMENT_SEGMENTS} segments). "
            "Split it into smaller files."
        )


def _in_fallback_or_merged(p) -> bool:
    for a in p.iterancestors():
        if a.tag == _MC_FALLBACK:
            return True
        if a.tag == _A_TC and (
            a.get("hMerge") in ("1", "true") or a.get("vMerge") in ("1", "true")
        ):
            return True
    return False


def _paragraphs(root) -> list:
    return [p for p in root.iter(_A_P) if not _in_fallback_or_merged(p)]


class _Walk:
    """The ordered paragraphs (part, a:p, style) and alt-text slots of one package."""

    def __init__(self, pkg: _Package):
        self.paras: list[tuple[str, object, str]] = []
        self.alts: list[tuple[str, object, str, str, str]] = []
        self.parts: list[str] = []  # parts holding walked text, first-visit order
        self.drawings: dict[str, str] = {}  # diagramData part -> its cached drawing part
        self.slides: list[tuple[str, str, dict]] = []  # (slide part, label, rels): #690
        main = _main_part(pkg)
        pres = pkg.xml(main)
        prels = pkg.rels(main)
        seen: set = set()
        lst = pres.find(_P + "sldIdLst")
        slide_ids = [] if lst is None else list(lst.iter(_P + "sldId"))
        for n, sld in enumerate(slide_ids, 1):
            kind, path = prels.get(sld.get(_R + "id"), ("", ""))
            if kind != "slide" or path in seen or path not in pkg.names:
                continue
            seen.add(path)
            root = pkg.xml(path)
            hidden = root.get("show") in ("0", "false")
            label = f"slide {n}" + (" hidden" if hidden else "")
            self._add(path, root, label)
            srels = pkg.rels(path)
            self.slides.append((path, label, srels))
            # SmartArt and charts, in the order the slide references them
            for el in root.iter():
                rids = []
                if el.tag == _DGM + "relIds":
                    rids = [(el.get(_R + "dm"), "SmartArt")]
                elif el.tag == "{http://schemas.openxmlformats.org/drawingml/2006/chart}chart":
                    rids = [(el.get(_R + "id"), "chart")]
                for rid, what in rids:
                    kind2, sub = srels.get(rid, ("", ""))
                    if not sub or sub in seen or sub not in pkg.names:
                        continue
                    if kind2 not in ("diagramData", "chart"):
                        continue
                    seen.add(sub)
                    subroot = pkg.xml(sub)
                    self._add(sub, subroot, f"{label} {what}")
                    if kind2 == "diagramData":
                        drawing = self._diagram_drawing(pkg, subroot, srels)
                        if drawing:
                            self.drawings[sub] = drawing
            for kind2, sub in srels.values():
                if kind2 == "notesSlide" and sub not in seen and sub in pkg.names:
                    seen.add(sub)
                    self._add(sub, pkg.xml(sub), f"{label} notes")
                    break
            for cnv in root.iter(_CNVPR):
                if any(a.tag == _MC_FALLBACK for a in cnv.iterancestors()):
                    continue  # the Fallback copy of a shape is not walked (module docstring)
                for attr, style in _ALT_ATTRS:
                    value = (cnv.get(attr) or "").strip()
                    if value and any(c.isalnum() for c in value):
                        self.alts.append((path, cnv, attr, value, style))

    @staticmethod
    def _diagram_drawing(pkg: _Package, data_root, srels: dict) -> str | None:
        """The cached drawing part of a SmartArt data part: dsp:dataModelExt relId names
        a relationship of the SLIDE (MS-ODRAWXML 2.5.x, diagramDrawing)."""
        for ext in data_root.iter(_DSP + "dataModelExt"):
            kind, path = srels.get(ext.get("relId"), ("", ""))
            if kind == "diagramDrawing" and path in pkg.names:
                return path
        return None

    def _add(self, part: str, root, style: str) -> None:
        if part not in self.parts:
            self.parts.append(part)
        for p in _paragraphs(root):
            self.paras.append((part, p, style))
            check_segment_cap(len(self.paras))  # paragraphs, empty ones included


_P188 = "{http://schemas.microsoft.com/office/powerpoint/2018/8/main}"


def _comment_slots(pkg: _Package, walk: _Walk) -> list[tuple[str, str, object, str]]:
    """#690: every comment slot, numbered from office_comments.COMMENT_BASE: (part, kind,
    element, style) with kind "classic" (a p:text element, plain text) or "modern" (an
    a:p of a modern comment or reply). Parsed only when comments are wanted (parse with
    comments=True, or a build whose mapping has a comment key), so a broken comments part
    never stops a presentation imported without them."""
    out: list = []
    seen: set = set()
    for _path, label, srels in walk.slides:
        for kind, sub in srels.values():
            if kind != "comments" or sub in seen or sub not in pkg.names:
                continue
            seen.add(sub)
            root = pkg.xml(sub)
            if root is None:
                continue
            style = f"{label} comment"
            if root.tag == _P + "cmLst":
                for cm in root.iter(_P + "cm"):
                    text_el = cm.find(_P + "text")
                    if text_el is not None:
                        out.append((sub, "classic", text_el, style))
            else:
                for body in root.iter(_P188 + "txBody"):
                    for p in body.iter(_A_P):
                        out.append((sub, "modern", p, style))
    return out


# ----------------------------------------------------------------- tokenizing


def _items(p) -> list:
    """The content children of an a:p, in order: ("r", el) text runs, ("x", el) breaks
    and fields. a:pPr / a:endParaRPr and unknown extension elements are not content."""
    out = []
    for child in p:
        if child.tag == _A_R:
            out.append(("r", child))
        elif child.tag in (_A_BR, _A_FLD):
            out.append(("x", child))
    return out


def _run_text(r) -> str:
    t = r.find(_A_T)
    return "" if t is None or t.text is None else t.text


def _has_text(p) -> bool:
    return any(k == "r" and _run_text(el).strip() for k, el in _items(p))


def _serialize(el) -> str:
    from lxml import etree

    return etree.tostring(el).decode("utf-8")


def tokenize(p) -> tuple[str, dict]:
    """(text, inline_map) of one a:p. One run and nothing else: plain text. Otherwise
    each run is ⟦rN⟧…⟦/rN⟧ (N = 1..runs) and each break/field a point ⟦xM⟧ numbered
    after the runs, as in the DOCX filter."""
    items = _items(p)
    runs = [el for k, el in items if k == "r"]
    if len(items) == 1 and runs:
        return _run_text(runs[0]), {}
    if not items:
        return "", {}
    inline_map: dict = {}
    parts: list[str] = []
    rn = 0
    xn = len(runs)
    for kind, el in items:
        if kind == "r":
            rn += 1
            rpr = el.find(_A_RPR)
            inline_map[str(rn)] = None if rpr is None else _serialize(rpr)
            parts.append(ph_open("r", rn) + _run_text(el) + ph_close("r", rn))
        else:
            xn += 1
            inline_map["x" + str(xn)] = "object"
            parts.append(ph_point("x", xn))
    return "".join(parts), inline_map


def _spans(text: str, n_runs: int):
    """Translated text -> [("t", run_index_or_None, s) | ("x", point_index)]. Flat like
    docx_filter._split_on_run_tokens: a close returns to "no run"; unknown tokens are
    kept as literal text so nothing the translator wrote disappears."""
    from app.services.segment import _PLACEHOLDER_RE

    out: list = []
    cur = None
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.start() > pos:
            out.append(("t", cur, text[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        closing = inner.startswith("/")
        kind, num = inner.lstrip("/")[:1], inner.lstrip("/")[1:]
        if kind == "r" and num.isdigit() and 1 <= int(num) <= n_runs:
            cur = None if closing else int(num) - 1
        elif kind == "x" and num.isdigit() and not closing:
            out.append(("x", int(num) - n_runs - 1))
        else:
            out.append(("t", cur, m.group(0)))
    if pos < len(text):
        out.append(("t", cur, text[pos:]))
    return out


def _new_run(text: str, rpr):
    from lxml import etree

    r = etree.Element(_A_R)
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = etree.SubElement(r, _A_T)
    t.text = text
    return r


def _new_br(rpr):
    from lxml import etree

    br = etree.Element(_A_BR)
    if rpr is not None:
        br.append(copy.deepcopy(rpr))
    return br


def write_paragraph(p, text: str) -> None:
    """Replace the content of one a:p with `text` (tokenized like `tokenize`)."""
    items = _items(p)
    runs = [el for k, el in items if k == "r"]
    points = [el for k, el in items if k == "x"]
    rprs = [r.find(_A_RPR) for r in runs]
    fallback = rprs[0] if rprs else None
    used: set = set()
    new: list = []
    for span in _spans(text, len(runs)):
        if span[0] == "x":
            i = span[1]
            if 0 <= i < len(points) and i not in used:
                used.add(i)
                new.append(copy.deepcopy(points[i]))
            continue
        _, ri, s = span
        if not s:
            continue
        rpr = rprs[ri] if ri is not None else fallback
        lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for j, line in enumerate(lines):
            if j:
                new.append(_new_br(rpr))
            if line:
                new.append(_new_run(line, rpr))
    for i, el in enumerate(points):  # a dropped break or field is kept, never deleted
        if i not in used:
            new.append(copy.deepcopy(el))
    if not any(el.tag == _A_R for el in new):
        new.insert(0, _new_run("", fallback))
    for _k, el in items:
        p.remove(el)
    end = p.find(_A_END)
    for el in new:
        if end is not None:
            end.addprevious(el)
        else:
            p.append(el)


# ----------------------------------------------------------------- SmartArt cache


def _point_paragraphs(data_root) -> dict:
    """modelId -> [a:p] of every dgm:pt that carries text."""
    out: dict = {}
    for pt in data_root.iter(_DGM + "pt"):
        t = pt.find(_DGM + "t")
        if t is not None and pt.get("modelId"):
            out[pt.get("modelId")] = list(t.iter(_A_P))
    return out


def _mirror_drawing(source_texts: dict, data_root, drawing_root) -> bool:
    """Copy each translated SmartArt point into the cached dsp:sp with the same modelId
    when that shape still reads exactly like the source point. True if anything moved."""
    changed = False
    translated = _point_paragraphs(data_root)
    for sp in drawing_root.iter(_DSP + "sp"):
        mid = sp.get("modelId")
        if mid not in source_texts:
            continue
        body = sp.find(_DSP + "txBody")
        if body is None:
            continue
        cached = list(body.iter(_A_P))
        src = source_texts[mid]
        if [tokenize(p)[0] for p in cached] != src or len(cached) != len(translated[mid]):
            continue
        for cp, tp in zip(cached, translated[mid], strict=True):
            write_paragraph(cp, tokenize(tp)[0])
        changed = True
    return changed


# ----------------------------------------------------------------- language


def _stamp_language(p, tag: str, rtl: bool) -> None:
    from lxml import etree

    for r in p:
        if r.tag in (_A_R, _A_FLD, _A_BR):
            rpr = r.find(_A_RPR)
            if rpr is None:
                rpr = etree.Element(_A_RPR)
                r.insert(0, rpr)
            rpr.set("lang", tag)
    end = p.find(_A_END)
    if end is not None:
        end.set("lang", tag)
    if rtl:
        ppr = p.find(_A_PPR)
        if ppr is None:
            ppr = etree.Element(_A_PPR)
            p.insert(0, ppr)
        ppr.set("rtl", "1")


# ----------------------------------------------------------------- filter


def walk_version(skeleton) -> int:
    stamp = (skeleton or {}).get("pptx_walk", PPTX_WALK_VERSION)
    if type(stamp) is int and stamp in _KNOWN_WALKS:
        return stamp
    raise ValueError(
        f"This PowerPoint file was imported with walk {stamp!r}, which this version does "
        "not know. Re-import the document to export it."
    )


def _open(data: bytes) -> tuple[_Package, _Walk, tuple | None]:
    """(package, walk, strict). #669: a Strict Open XML presentation is read through its
    Transitional names; strict is (original, normalized) for the rebuild to map back."""
    zip_guard.check(data, _label())
    normalized, is_strict = ooxml_strict.to_transitional(data, _label())
    pkg = _Package(normalized)  # a blob that is not a zip is refused here, with a plain message
    check_markup_budget(normalized)
    return pkg, _Walk(pkg), ((data, normalized) if is_strict else None)


def _serialize_part(root, original: bytes) -> bytes:
    from lxml import etree

    standalone = b'standalone="yes"' in original[:200] or None
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=standalone)


def _repack(pkg: _Package, data: bytes, changed: dict) -> bytes:
    """A copy of the package with `changed` parts replaced; every other member keeps its
    bytes, order, compression and timestamps."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in pkg.zf.infolist():
            if info.filename in changed:
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                dst.writestr(zi, changed[info.filename])
            else:
                dst.writestr(info, pkg.zf.read(info))
    return out.getvalue()


@register
class PptxFilter(Filter):
    fmt = "pptx"
    exts = ("pptm", "potx", "potm", "ppsx", "ppsm")

    takes_comments = True  # #690: the per-project "translate comments" option

    def parse(
        self, data: bytes, provider: str | None = None, comments: bool = False
    ) -> ParseResult:
        _pkg, walk, _strict = _open(data)
        units: list[TextUnit] = []
        rows = []
        for i, (part, p, style) in enumerate(walk.paras):
            if not _has_text(p):
                continue
            text, inline_map = tokenize(p)
            units.append(TextUnit(p_index=i, text=text, style=style, inline_map=inline_map))
            rows.append(part)
            check_segment_cap(len(units))
        base = len(walk.paras)
        for j, (part, _cnv, _attr, value, style) in enumerate(walk.alts):
            units.append(TextUnit(p_index=base + j, text=value, style=style))
            rows.append(part)
            check_segment_cap(len(units))
        if comments:
            from app.filters.office_comments import COMMENT_BASE

            for j, (part, kind, el, style) in enumerate(_comment_slots(_pkg, walk)):
                if kind == "classic":
                    text, inline_map = (el.text or "").strip(), {}
                    if not any(c.isalnum() for c in text):
                        continue
                elif _has_text(el):
                    text, inline_map = tokenize(el)
                else:
                    continue
                units.append(
                    TextUnit(
                        p_index=COMMENT_BASE + j, text=text, style=style, inline_map=inline_map
                    )
                )
                rows.append(part)
                check_segment_cap(len(units))
        skeleton = {
            "format": "pptx",
            "pptx_walk": PPTX_WALK_VERSION,
            "units": [
                {"p_index": u.p_index, "text": u.text, "style": u.style, "part": rows[k]}
                for k, u in enumerate(units)
            ],
        }
        if comments:
            skeleton["comments"] = True
        return ParseResult(skeleton=skeleton, units=units)

    def _rebuild(self, original: bytes, paragraph_texts: dict, target_locale=None) -> bytes:
        from app.filters.xml_text import XmlTargetError, xml_target_text

        pkg, walk, strict = _open(original)
        tag = rtl = None
        if target_locale is not None:
            from app.filters import a11y

            tag = a11y.target_tag(target_locale)
            rtl = a11y.target_direction(target_locale) == "rtl"
        # SmartArt source text per point, read BEFORE any paragraph is rewritten
        smart_src = {
            data: {
                mid: [tokenize(p)[0] for p in ps]
                for mid, ps in _point_paragraphs(pkg.xml(data)).items()
            }
            for data in walk.drawings
        }
        touched: set = set()
        base = len(walk.paras)
        from app.filters.office_comments import COMMENT_BASE, has_comment_keys

        cslots = _comment_slots(pkg, walk) if has_comment_keys(paragraph_texts) else []
        for key, new_text in paragraph_texts.items():
            if new_text is None:
                continue
            try:
                i = int(key)
            except (TypeError, ValueError):
                continue
            new_text = xml_target_text(str(new_text))
            if 0 <= i < base:
                part, p, _style = walk.paras[i]
                try:
                    write_paragraph(p, new_text)
                except ValueError as exc:
                    raise XmlTargetError(
                        f"PowerPoint paragraph {i + 1} could not be written. "
                        "Review its target text."
                    ) from exc
                if tag:
                    _stamp_language(p, tag, rtl)
                touched.add(part)
            elif base <= i < base + len(walk.alts):
                part, cnv, attr, _value, _style = walk.alts[i - base]
                try:
                    cnv.set(attr, new_text)
                except ValueError as exc:
                    raise XmlTargetError(
                        f"PowerPoint alt text unit {i + 1} could not be written. "
                        "Review its target text."
                    ) from exc
                touched.add(part)
            elif 0 <= i - COMMENT_BASE < len(cslots):
                part, kind, el, _style = cslots[i - COMMENT_BASE]
                try:
                    if kind == "classic":
                        old = el.text or ""
                        lead = old[: len(old) - len(old.lstrip())]
                        trail = old[len(old.rstrip()) :]
                        el.text = lead + new_text + trail
                    else:
                        write_paragraph(el, new_text)
                        if tag:
                            _stamp_language(el, tag, rtl)
                except ValueError as exc:
                    raise XmlTargetError(
                        f"PowerPoint comment {i - COMMENT_BASE + 1} could not be written. "
                        "Review its target text."
                    ) from exc
                touched.add(part)
        for data, drawing in walk.drawings.items():
            if data in touched:
                droot = pkg.xml(drawing)
                if droot is not None and _mirror_drawing(smart_src[data], pkg.xml(data), droot):
                    if tag:
                        mids = set(smart_src[data])
                        for sp in droot.iter(_DSP + "sp"):
                            if sp.get("modelId") in mids:
                                for p in sp.iter(_A_P):
                                    _stamp_language(p, tag, rtl)
                    touched.add(drawing)
        changed = {name: _serialize_part(pkg.roots[name], pkg.zf.read(name)) for name in touched}
        out = _repack(pkg, original, changed)
        if strict is not None:  # #669: Strict in, Strict out
            out = ooxml_strict.to_strict(strict[0], strict[1], out)
        return out

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        """build() plus the TARGET language on every translated run (a:rPr lang,
        ECMA-376 21.1.2.3.9) and right-to-left paragraphs (a:pPr rtl) for an RTL
        target. Untranslated paragraphs keep their source language."""
        return self._rebuild(original, paragraph_texts, target_locale or None)
