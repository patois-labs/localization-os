"""In-place SpreadsheetML writer for the XLSX filter (#685).

The export used to load the workbook into openpyxl, set the cell values and save it again.
openpyxl regenerates every part from its own model and silently drops what it does not
model: text boxes and shapes, EMF/WMF pictures, printer settings, the calc chain. This
module writes the translated text straight into the ORIGINAL package instead, as the Word
and PowerPoint filters do: only the parts that carry new text (the worksheets, the shared
string table, the drawings, docProps/core.xml) are re-serialized, and every other member
keeps its bytes, order, compression and timestamps.

The XLSX filter still enumerates the units with openpyxl (the unit order IS the segment
numbering, and existing projects must keep theirs); this module only locates the same
cells in the XML. Cell addressing mirrors openpyxl's WorkSheetParser exactly: every `row`
element in document order (`r` attribute, else the previous row + 1), every element child
of a row is a cell (`r` coordinate, else the previous column + 1), and a later duplicate of
a coordinate wins. Sheets are the workbook's `sheet` entries with a relationship target in
the package, chartsheets left out, in workbook order (openpyxl's `wb.worksheets`).

Writing a cell (ECMA-376 Part 1, 18.3.1.4 c, 18.4 shared strings):
  - a shared-string cell (t="s") keeps pointing into the shared string table. When every
    cell that uses that string gets the same translation, the `si` item is rewritten in
    place; otherwise a new `si` is appended for this translation and the cell repointed,
    so a string shared with an untranslated cell (column mode) is never changed under it.
  - any other cell becomes an inline string (t="inlineStr", `is`), its formula and cached
    value removed. A translation is always a literal string, never a formula.
  - rich text (`r` runs): a translation keeps the run formatting when every text run has
    the same `rPr`; mixed runs become one plain `t` (the cell style still applies), which
    is what the openpyxl rebuild produced for every rich string. Phonetic runs (`rPh`,
    furigana of the SOURCE text) are dropped; `phoneticPr` is kept.

Drawing text (walk 2 of the filter): the `a:p` paragraphs of the text boxes and shapes in
each worksheet's drawing part (xl/drawings/drawingN.xml), in sheet order and XML order,
mc:Fallback copies skipped (the PowerPoint filter's rule). Chart text and legacy VML
(comments, form controls) are not walked.

References: ECMA-376 5th ed. Part 1 sections 18.2.20 (sheets), 18.3.1.73 (row), 18.3.1.4
(c), 18.3.1.53 (is), 18.4.8 (si), 18.4.12 (t), 20.5 (SpreadsheetML drawing); Part 2 (OPC)
9.3 for relationship target resolution.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import copy
import io
import posixpath
import re
import zipfile
from collections import Counter, defaultdict

from app.filters.base import InputLimitError

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_PKG_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CORE_TYPE = "application/vnd.openxmlformats-package.core-properties+xml"
_CORE_REL = _PKG_RELS_NS + "/metadata/core-properties"

LABEL = "XLSX file"
_COORD = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def _invalid(name: str) -> InputLimitError:
    return InputLimitError(
        f"This {LABEL} has invalid spreadsheet XML in {name}. Save it again in Excel."
    )


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_letters(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _coord(ref: str, name: str) -> tuple[int, int]:
    m = _COORD.match(ref.strip())
    if not m:
        raise _invalid(name)
    return int(m.group(2)), _col_number(m.group(1))


def _row_number(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError:
        try:
            f = float(value)
        except ValueError as exc:
            raise _invalid(name) from exc
    if not f.is_integer():
        raise _invalid(name)
    return int(f)


def _parse_xml(raw: bytes, name: str):
    from lxml import etree

    from app.filters.xml_guard import make_hardened_parser

    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
        raise InputLimitError(f"This {LABEL} contains an unsupported XML document type.")
    try:
        root = etree.fromstring(raw, make_hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise _invalid(name) from exc
    if root.getroottree().docinfo.doctype:
        raise InputLimitError(f"This {LABEL} contains an unsupported XML document type.")
    return root


def _rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def _resolve(part: str, target: str) -> str | None:
    """Package path of a relationship target relative to `part` (OPC 9.3), or None when it
    escapes the package root."""
    if target.startswith("/"):
        path = posixpath.normpath(target.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
    if path.startswith("..") or path.startswith("/") or path == ".":
        return None
    return path


def _needs_preserve(text: str) -> bool:
    return text != text.strip() or any(ch in text for ch in "\n\r\t") or "  " in text


def _string_item(tag: str, text: str, template):
    """A new `si` / `is` element holding `text`, formatted from `template` (the item it
    replaces, or None): one run with the shared rPr when every text run of the template
    has the same rPr, else a plain `t`. The template's phoneticPr is kept."""
    from lxml import etree

    item = etree.Element(tag)
    rpr = None
    if template is not None:
        text_runs = [r for r in template.findall(_M + "r") if r.findtext(_M + "t")]
        rprs = [r.find(_M + "rPr") for r in text_runs]
        shapes = {b"" if x is None else etree.tostring(x) for x in rprs}
        if rprs and len(shapes) == 1 and rprs[0] is not None:
            rpr = rprs[0]
    if rpr is not None:
        run = etree.SubElement(item, _M + "r")
        run.append(copy.deepcopy(rpr))
        t = etree.SubElement(run, _M + "t")
    else:
        t = etree.SubElement(item, _M + "t")
    t.text = text
    if _needs_preserve(text):
        t.set(_XML_SPACE, "preserve")
    if template is not None:
        phonetic = template.find(_M + "phoneticPr")
        if phonetic is not None:
            item.append(copy.deepcopy(phonetic))
    return item


def has_text(p) -> bool:
    """True when a DrawingML a:p has a text run with visible text."""
    return any((r.findtext(_A + "t") or "").strip() for r in p.iter(_A + "r"))


class Package:
    """The workbook package, read with zipfile (the caller has run xlsx_guard.check on
    it), with parsed XML parts cached and the parts it changed tracked."""

    def __init__(self, data: bytes):
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise InputLimitError(
                f"This {LABEL} is not a valid Excel package. Save it again in Excel."
            ) from exc
        self.names = set(self.zf.namelist())
        self.roots: dict = {}
        self.dirty: set = set()
        self.added: dict = {}
        self._cells: dict = {}
        self._explicit: set = set()
        self._rows: dict = {}
        self._shared: list = []
        self.removed: set = set()
        self._formula_removed = False
        self.workbook = self._main_part()
        wrels = self.rels(self.workbook)
        self.shared_strings = next(
            (path for kind, path in wrels.values() if kind == "sharedStrings"), None
        )
        self.sheets: list[str] = []
        sheets = self.xml(self.workbook).find(_M + "sheets")
        for sh in [] if sheets is None else sheets.iter(_M + "sheet"):
            rid = sh.get(_R + "id")
            if not rid:
                continue
            kind, path = wrels.get(rid, ("", ""))
            if path not in self.names or kind == "chartsheet":
                continue
            self.sheets.append(path)

    # ------------------------------------------------------------- reading

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
                out[rel.get("Id")] = ((rel.get("Type") or "").rsplit("/", 1)[-1], path)
        return out

    def _main_part(self) -> str:
        for _rid, (kind, path) in sorted(self.rels("").items()):
            if kind == "officeDocument" and path in self.names:
                root = self.xml(path)
                if root is not None and root.tag == _M + "workbook":
                    return path
        raise InputLimitError(f"This {LABEL} is not an Excel workbook (no workbook part).")

    def _index(self, path: str) -> dict:
        """(row, column) -> c element, addressed exactly as openpyxl reads the sheet."""
        if path not in self._cells:
            cells: dict = {}
            row_n = 0
            for row in self.xml(path).iter(_M + "row"):
                r = row.get("r")
                row_n = _row_number(r, path) if r is not None else row_n + 1
                col_n = 0
                for c in row:
                    if not isinstance(c.tag, str):
                        continue  # comments / processing instructions are not cells
                    ref = c.get("r")
                    if ref:
                        key = _coord(ref, path)
                        col_n = key[1]
                    else:
                        col_n += 1
                        key = (row_n, col_n)
                    cells[key] = c
            self._cells[path] = cells
        return self._cells[path]

    def drawing_paragraphs(self) -> list[tuple[int, str, object]]:
        """(sheet index, drawing part, a:p) of every text-bearing paragraph in the
        worksheets' drawings, in sheet order then XML order; mc:Fallback copies skipped."""
        out: list = []
        seen: set = set()
        for n, path in enumerate(self.sheets):
            root = self.xml(path)
            srels = None
            for d in root.iter(_M + "drawing"):
                if srels is None:
                    srels = self.rels(path)
                kind, dpath = srels.get(d.get(_R + "id"), ("", ""))
                if kind != "drawing" or dpath in seen or dpath not in self.names:
                    continue
                seen.add(dpath)
                droot = self.xml(dpath)
                for p in droot.iter(_A + "p"):
                    if any(a.tag == _MC_FALLBACK for a in p.iterancestors()):
                        continue
                    if has_text(p):
                        out.append((n, dpath, p))
        return out

    # ------------------------------------------------------------- writing

    def touch(self, part: str) -> None:
        self.dirty.add(part)

    def set_cell_text(
        self, sheet: int, row: int, col: int, text: str, create: bool = False
    ) -> None:
        """Write `text` into the cell as a literal string (never a formula)."""
        path = self.sheets[sheet]
        c = self._index(path).get((row, col))
        if c is None:
            if not create:
                raise ValueError(f"Excel cell {_col_letters(col)}{row} was not found.")
            c = self._create_cell(path, row, col)
        self.dirty.add(path)
        v = c.find(_M + "v")
        shared = c.get("t") == "s" and v is not None and (v.text or "").strip().isdigit()
        if shared and c.find(_M + "f") is None:
            self._shared.append((c, int(v.text.strip()), text))
            return
        template = c.find(_M + "is") if c.get("t") == "inlineStr" else None
        new = _string_item(_M + "is", text, template)
        f = c.find(_M + "f")
        if f is not None:
            # gate: a formula overwritten by text (column mode, a target cell that held a
            # formula). A shared-formula master takes its formula text with it, so its
            # dependents get their own copy first; the calc chain would name a cell that
            # no longer has a formula (Excel repairs the file), so it is dropped and
            # Excel rebuilds it, as the openpyxl rebuild did.
            self._unshare(path, c, f)
            self._formula_removed = True
        for child in list(c):
            if child.tag in (_M + "f", _M + "v", _M + "is"):
                c.remove(child)
        for attr in ("cm", "vm"):  # formula / value metadata of the value just removed
            c.attrib.pop(attr, None)
        c.set("t", "inlineStr")
        ext = c.find(_M + "extLst")
        if ext is not None:
            ext.addprevious(new)
        else:
            c.append(new)

    def _unshare(self, path: str, c, f) -> None:
        """Before the shared-formula master `c` loses its formula, give every dependent
        of the same shared group (f t="shared" si=N, no text) its own formula, translated
        from the master's position (ECMA-376 Part 1, 18.3.1.40 f)."""
        if f.get("t") != "shared" or not (f.text or "").strip() or f.get("si") is None:
            return
        from openpyxl.formula.translate import Translator

        si = f.get("si")
        where = {id(cell): key for key, cell in self._index(path).items()}
        origin = where.get(id(c))
        if origin is None:
            return
        origin_ref = f"{_col_letters(origin[1])}{origin[0]}"
        formula = "=" + f.text.strip()
        for other in self.xml(path).iter(_M + "f"):
            if other is f or other.get("t") != "shared" or other.get("si") != si:
                continue
            key = where.get(id(other.getparent()))
            if key is None or (other.text or "").strip():
                continue
            dest = f"{_col_letters(key[1])}{key[0]}"
            other.text = Translator(formula, origin=origin_ref).translate_formula(dest)[1:]
            for attr in ("t", "si", "ref"):
                other.attrib.pop(attr, None)

    def _drop_calc_chain(self) -> None:
        """Remove xl/calcChain.xml (its part, content-type override and workbook
        relationship); Excel rebuilds the chain on open."""
        rels_name = _rels_name(self.workbook)
        rels = self.xml(rels_name)
        if rels is None:
            return
        for rel in list(rels.iter(_PKG_REL)):
            if (rel.get("Type") or "").rsplit("/", 1)[-1] != "calcChain":
                continue
            path = _resolve(self.workbook, rel.get("Target") or "")
            rel.getparent().remove(rel)
            self.dirty.add(rels_name)
            if path and path in self.names:
                self.removed.add(path)
                types = self.xml("[Content_Types].xml")
                for o in [] if types is None else list(types.iter(_CT + "Override")):
                    if (o.get("PartName") or "").lstrip("/") == path:
                        types.remove(o)
                        self.dirty.add("[Content_Types].xml")

    def _make_explicit(self, path: str) -> None:
        """Give every row and cell of the sheet its `r` address, so inserting one cannot
        shift the implicit position of a later row or cell. Also indexes the rows by
        number (row number -> element, and the sorted numbers) so inserting stays linear."""
        if path in self._explicit:
            return
        for (row, col), c in self._index(path).items():
            c.set("r", f"{_col_letters(col)}{row}")
        row_n = 0
        by_number: dict = {}
        for row in self.xml(path).iter(_M + "row"):
            r = row.get("r")
            row_n = _row_number(r, path) if r is not None else row_n + 1
            row.set("r", str(row_n))
            by_number[row_n] = row  # a later duplicate wins, as for cells
        self._rows[path] = (by_number, sorted(by_number))
        self._explicit.add(path)

    def _create_cell(self, path: str, row: int, col: int):
        import bisect

        from lxml import etree

        root = self.xml(path)
        data = root.find(_M + "sheetData")
        if data is None:
            raise ValueError("This Excel sheet has no cell table.")
        self._make_explicit(path)
        by_number, numbers = self._rows[path]
        row_el = by_number.get(row)
        if row_el is None:
            row_el = etree.Element(_M + "row")
            row_el.set("r", str(row))
            at = bisect.bisect_right(numbers, row)
            if at < len(numbers):
                by_number[numbers[at]].addprevious(row_el)
            else:
                data.append(row_el)
            numbers.insert(at, row)
            by_number[row] = row_el
        row_el.attrib.pop("spans", None)  # an optional hint the new cell may fall outside
        c = etree.Element(_M + "c")
        c.set("r", f"{_col_letters(col)}{row}")
        nxt = None
        for other in row_el:
            ref = other.get("r") if isinstance(other.tag, str) else None
            if ref and _coord(ref, path)[1] > col:
                nxt = other
                break
        if nxt is not None:
            nxt.addprevious(c)
        else:
            row_el.append(c)
        self._index(path)[(row, col)] = c
        dim = root.find(_M + "dimension")
        if dim is not None:
            self._grow_dimension(dim, row, col, path)
        return c

    @staticmethod
    def _grow_dimension(dim, row: int, col: int, path: str) -> None:
        ref = dim.get("ref") or ""
        try:
            corners = [_coord(x, path) for x in ref.split(":")]
        except InputLimitError:
            return
        rows = [r for r, _c in corners] + [row]
        cols = [c for _r, c in corners] + [col]
        lo = f"{_col_letters(min(cols))}{min(rows)}"
        hi = f"{_col_letters(max(cols))}{max(rows)}"
        dim.set("ref", lo if lo == hi else f"{lo}:{hi}")

    def _resolve_shared(self) -> None:
        if not self._shared:
            return
        sst = self.xml(self.shared_strings) if self.shared_strings else None
        if sst is None:
            raise ValueError("This Excel file has no shared string table.")
        items = sst.findall(_M + "si")
        refs: Counter = Counter()
        for path in self.sheets:
            for c in self.xml(path).iter(_M + "c"):
                if c.get("t") == "s":
                    v = (c.findtext(_M + "v") or "").strip()
                    if v.isdigit():
                        refs[int(v)] += 1
        by_index: dict = defaultdict(list)
        for c, i, text in self._shared:
            by_index[i].append((c, text))
        appended: dict = {}
        ext = sst.find(_M + "extLst")
        for i, edits in by_index.items():
            if not 0 <= i < len(items):
                raise ValueError(f"Shared string {i} is missing from this Excel file.")
            texts = {text for _c, text in edits}
            if len(texts) == 1 and len(edits) >= refs[i]:
                items[i][:] = list(_string_item(_M + "si", edits[0][1], items[i]))
                continue
            for c, text in edits:
                if (i, text) not in appended:
                    new = _string_item(_M + "si", text, items[i])
                    if ext is not None:
                        ext.addprevious(new)
                    else:
                        sst.append(new)
                    appended[(i, text)] = len(items) + len(appended)
                c.find(_M + "v").text = str(appended[(i, text)])
        if appended and sst.get("uniqueCount") is not None:
            sst.set("uniqueCount", str(len(items) + len(appended)))
        self.dirty.add(self.shared_strings)
        self._shared = []

    def set_sheets_rtl(self) -> None:
        """Every worksheet view right to left (sheetView rightToLeft, ECMA-376 Part 1,
        18.3.1.87): column A sits on the right. A worksheet with no sheetViews gets one
        view, placed after sheetPr / dimension as CT_Worksheet orders it."""
        from lxml import etree

        for path in self.sheets:
            root = self.xml(path)
            views = list(root.iter(_M + "sheetView"))
            if not views:
                box = etree.Element(_M + "sheetViews")
                view = etree.SubElement(box, _M + "sheetView")
                view.set("workbookViewId", "0")
                after = [ch for ch in root if ch.tag in (_M + "sheetPr", _M + "dimension")]
                if after:
                    after[-1].addnext(box)
                else:
                    root.insert(0, box)
                views = [view]
            for view in views:
                if view.get("rightToLeft") not in ("1", "true"):
                    view.set("rightToLeft", "1")
                    self.dirty.add(path)

    def stamp_language(self, tag: str) -> None:
        """dc:language in docProps/core.xml, the only workbook-level language declaration
        SpreadsheetML has; the part is created when the package has none."""
        from lxml import etree

        core = next((p for k, p in self.rels("").values() if k == "core-properties"), None)
        root = self.xml(core) if core else None
        if root is None:
            core = "docProps/core.xml"
            n = 1
            while core in self.names or core in self.added:
                n += 1
                core = f"docProps/core{n}.xml"
            root = etree.Element(
                f"{{{_CP_NS}}}coreProperties", nsmap={"cp": _CP_NS, "dc": _DC[1:-1]}
            )
            self.roots[core] = root
            self.added[core] = True
            rels = self.xml("_rels/.rels")
            ids = {r.get("Id") for r in rels.iter(_PKG_REL)}
            rid = next(f"rIdCore{k}" for k in range(1, 10_000) if f"rIdCore{k}" not in ids)
            rel = etree.SubElement(rels, _PKG_REL)
            rel.set("Id", rid)
            rel.set("Type", _CORE_REL)
            rel.set("Target", core)
            self.dirty.add("_rels/.rels")
            types = self.xml("[Content_Types].xml")
            if types is not None:
                o = etree.SubElement(types, _CT + "Override")
                o.set("PartName", "/" + core)
                o.set("ContentType", _CORE_TYPE)
                self.dirty.add("[Content_Types].xml")
        lang = root.find(_DC + "language")
        if lang is None:
            lang = etree.SubElement(root, _DC + "language")
        lang.text = tag
        self.dirty.add(core)

    def save(self) -> bytes:
        """A copy of the package with the changed parts re-serialized; every other member
        keeps its bytes, order, compression and timestamps."""
        from lxml import etree

        self._resolve_shared()
        if self._formula_removed:
            self._drop_calc_chain()
        changed: dict = {}
        for name in self.dirty:
            raw = self.zf.read(name) if name in self.names else b""
            standalone = b'standalone="yes"' in raw[:200] or None
            changed[name] = etree.tostring(
                self.roots[name], xml_declaration=True, encoding="UTF-8", standalone=standalone
            )
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as dst:
            for info in self.zf.infolist():
                if info.filename in self.removed:
                    continue
                if info.filename in changed:
                    zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = info.external_attr
                    dst.writestr(zi, changed[info.filename])
                else:
                    dst.writestr(info, self.zf.read(info))
            for name in self.added:
                dst.writestr(name, changed[name], compress_type=zipfile.ZIP_DEFLATED)
        return out.getvalue()
