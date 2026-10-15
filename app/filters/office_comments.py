"""Comment units shared by the Office filters (#690, the maintainer's decision 7b, 2026-10-11).

Whether comments are units at all is the per-project option in
app/services/comment_option.py; the filters take it as `parse(..., comments=True)`.

Numbering. Comment units are APPENDED at fixed indices COMMENT_BASE + j (j = the
comment's position in the format's comment walk), far above every body, alt-text or
other appended index a walk can reach (the segment cap bounds those). So:
  * a document imported without comments keeps exactly the indices it had, and its
    export never touches a comment (no comment key is in the mapping);
  * a future change to a format's body walk (a new walk stamp, a new appended unit
    kind) can never collide with a comment index, and vice versa;
  * build() needs no flag: it walks the comments of the ORIGINAL and writes the keys
    that are present.
Word is the exception: its comments part is part of the frozen DOCX walk (stamp 4+), so
the DOCX filter only leaves those units out of the parse when the option is off.

Never translated: authors, initials, dates, person lists, the "[Threaded comment]"
placeholder Excel writes into the legacy comment of a threaded one, and Excel's bold
"Author:" label run at the start of a classic note.

Excel lives here (not in xlsx_filter) on purpose: the workbook export is being rewritten
to patch the original package (B-xlsx, #685). The comment pieces are a self-contained
read (`xlsx_comment_slots`) and a post-build patch of the finished package
(`xlsx_apply`), so either export can call them unchanged: the patch finds each comment
by sheet name + cell (classic notes) or by its id (threaded comments) in whatever
package the export produced.

Specs: ECMA-376 Part 1 18.7 (Comments), [MS-XLSX] 2.6.207 ff. (threaded comments,
http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments).

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile

from app.filters.base import InputLimitError

COMMENT_BASE = 1_000_000
COMMENT_STYLE = "comment"

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def is_comment_index(i) -> bool:
    try:
        return int(i) >= COMMENT_BASE
    except (TypeError, ValueError):
        return False


def has_comment_keys(paragraph_texts) -> bool:
    return any(is_comment_index(k) for k in (paragraph_texts or {}))


def translatable(text: str) -> bool:
    return any(c.isalnum() for c in text or "")


def local(el) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.rsplit("}", 1)[-1]


def parse_part(raw: bytes, label: str):
    """Parse one package part with the hardened parser (no DTD, entities or network)."""
    from lxml import etree

    from app.filters.xml_guard import make_hardened_parser

    if _DOCTYPE_RE.search(raw[:4096]) or b"<!ENTITY" in raw:
        raise InputLimitError(f"This {label} contains an unsupported XML document type.")
    try:
        return etree.fromstring(raw, make_hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise InputLimitError(f"This {label} has a comments part that is not valid XML.") from exc


def serialize(root, original: bytes) -> bytes:
    from lxml import etree

    standalone = True if b'standalone="yes"' in original[:200] else None
    return etree.tostring(
        root.getroottree(), xml_declaration=True, encoding="UTF-8", standalone=standalone
    )


def repack(data: bytes, changed: dict) -> bytes:
    """A copy of the package with the `changed` parts replaced; every other member keeps
    its bytes, order, compression and timestamps."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename in changed:
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                dst.writestr(zi, changed[info.filename])
            else:
                dst.writestr(info, src.read(info))
    return out.getvalue()


def rels_name(part: str) -> str:
    d, f = posixpath.split(part)
    return posixpath.join(d, "_rels", f + ".rels")


def resolve(part: str, target: str) -> str | None:
    if target.startswith("/"):
        path = posixpath.normpath(target.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
    if path.startswith("..") or path.startswith("/") or path == ".":
        return None
    return path


class _Pkg:
    """Read-only, lazily parsed view of an OPC package (names, rels, XML parts)."""

    def __init__(self, data: bytes, label: str):
        self.label = label
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise InputLimitError(f"This {label} is not a valid package.") from exc
        self.names = set(self.zf.namelist())
        self.roots: dict = {}

    def xml(self, name: str):
        if name not in self.names:
            return None
        if name not in self.roots:
            self.roots[name] = parse_part(self.zf.read(name), self.label)
        return self.roots[name]

    def rels(self, part: str) -> list[tuple[str, str, str]]:
        """[(rId, type suffix, resolved path)] of the internal relationships of `part`."""
        root = self.xml(rels_name(part))
        out = []
        if root is None:
            return out
        for rel in root:
            if local(rel) != "Relationship":
                continue
            if (rel.get("TargetMode") or "").lower() == "external":
                continue
            path = resolve(part, rel.get("Target") or "")
            if path is not None:
                kind = (rel.get("Type") or "").rsplit("/", 1)[-1]
                out.append((rel.get("Id") or "", kind, path))
        return out


# ------------------------------------------------------------------------- Excel


def _xlsx_sheets(pkg: _Pkg) -> list[tuple[str, str]]:
    """[(sheet name, worksheet part)] in workbook order (chartsheets have no cells)."""
    wb_part = next((p for _r, k, p in pkg.rels("") if k == "officeDocument"), None)
    wb = pkg.xml(wb_part) if wb_part else None
    if wb is None:
        return []
    by_id = {rid: (kind, path) for rid, kind, path in pkg.rels(wb_part)}
    out = []
    for el in wb.iter():
        if local(el) != "sheet":
            continue
        rid = next((v for k, v in el.attrib.items() if k.endswith("}id")), None)
        kind, path = by_id.get(rid, ("", ""))
        if kind == "worksheet" and path in pkg.names:
            out.append((el.get("name") or "", path))
    return out


def _note_nodes(comment_el) -> tuple[list, int]:
    """(the comment's text nodes in order, phonetic runs excluded; how many leading
    nodes are Excel's bold "Author:" label)."""
    text_el = next((c for c in comment_el if local(c) == "text"), None)
    if text_el is None:
        return [], 0
    nodes = []
    for el in text_el.iter():
        if local(el) != "t":
            continue
        parent = el.getparent()
        if parent is not None and local(parent) == "rPh":
            continue
        nodes.append(el)
    skip = 0
    if len(nodes) >= 2:
        first = nodes[0]
        run = first.getparent()
        bold = (
            run is not None
            and local(run) == "r"
            and any(local(x) == "b" for rpr in run if local(rpr) == "rPr" for x in rpr)
        )
        if bold and (first.text or "").strip().endswith(":"):
            skip = 1
    return nodes, skip


def _xlsx_walk(pkg: _Pkg) -> list[dict]:
    """Every comment slot of a workbook, in the order the units are numbered:
    for each sheet in workbook order, its threaded comments, then its classic notes
    (a note shadowed by a threaded comment on the same cell is Excel's placeholder and
    is skipped). Slot: {"sheet", "ref", "kind": "thread"|"note", "id", "el", "part",
    "text"}."""
    slots: list[dict] = []
    for name, sheet_part in _xlsx_sheets(pkg):
        rels = pkg.rels(sheet_part)
        threaded_refs: set = set()
        for _rid, kind, path in rels:
            if kind != "threadedComment":
                continue
            root = pkg.xml(path)
            if root is None:
                continue
            for tc in root:
                if local(tc) != "threadedComment":
                    continue
                threaded_refs.add((tc.get("ref") or "").upper())
                text_el = next((c for c in tc if local(c) == "text"), None)
                if text_el is None:
                    continue
                slots.append(
                    {
                        "sheet": name,
                        "ref": tc.get("ref") or "",
                        "kind": "thread",
                        "id": tc.get("id") or "",
                        "el": text_el,
                        "part": path,
                        "text": text_el.text or "",
                    }
                )
        for _rid, kind, path in rels:
            if kind != "comments":
                continue
            root = pkg.xml(path)
            if root is None:
                continue
            for cm in root.iter():
                if local(cm) != "comment":
                    continue
                ref = cm.get("ref") or ""
                if ref.upper() in threaded_refs:
                    continue
                nodes, skip = _note_nodes(cm)
                body = "".join(n.text or "" for n in nodes[skip:])
                slots.append(
                    {
                        "sheet": name,
                        "ref": ref,
                        "kind": "note",
                        "id": "",
                        "el": cm,
                        "part": path,
                        "text": body,
                    }
                )
    return slots


def xlsx_comment_slots(data: bytes) -> list[tuple[int, str, dict]]:
    """[(p_index, text, slot meta)] for the translatable comments of a workbook."""
    pkg = _Pkg(data, "XLSX file")
    out = []
    for j, slot in enumerate(_xlsx_walk(pkg)):
        core = slot["text"].strip()
        if translatable(core):
            meta = {
                "comment_sheet": slot["sheet"],
                "comment_ref": slot["ref"],
                "comment_kind": slot["kind"],
            }
            out.append((COMMENT_BASE + j, core, meta))
    return out


def _replace_span(nodes: list, start: int, end: int, new: str) -> None:
    """Replace characters [start, end) of the concatenated text of `nodes` with `new`:
    the first node the span touches takes the new text, later ones lose their part of
    the span, text outside the span stays where it was."""
    pos = 0
    placed = False
    for node in nodes:
        text = node.text or ""
        a, b = pos, pos + len(text)
        pos = b
        if not (a < end and b > start):
            continue  # this node holds no character of the span
        lo = max(start, a) - a
        hi = min(end, b) - a
        if not placed:
            node.text = text[:lo] + new + text[hi:]
            placed = True
        else:
            node.text = text[:lo] + text[hi:]
        node.set(_XML_SPACE, "preserve")


def _write_note(cm, source_core: str, new: str) -> bool:
    nodes, skip = _note_nodes(cm)
    if not nodes:
        return False
    full = "".join(n.text or "" for n in nodes)
    after = sum(len(n.text or "") for n in nodes[:skip])
    i = full.find(source_core, after)
    if i < 0:
        return False
    _replace_span(nodes, i, i + len(source_core), new)
    return True


def xlsx_apply(built: bytes, original: bytes, paragraph_texts: dict) -> bytes:
    """Write the comment translations of `paragraph_texts` into a finished workbook
    package `built` (the export of `original`). Comments are found in `built` by sheet
    name + cell (notes) and by id (threaded comments), so the patch works on any export
    that keeps the sheets and their comments. A comment the built package does not have
    stays as it is. Returns `built` unchanged when there is nothing to write."""
    if not has_comment_keys(paragraph_texts):
        return built
    from app.filters.xml_text import xml_target_text

    src = _xlsx_walk(_Pkg(original, "XLSX file"))
    wanted = {}
    for key, new in paragraph_texts.items():
        if new is None or not is_comment_index(key):
            continue
        j = int(key) - COMMENT_BASE
        if 0 <= j < len(src):
            wanted[j] = xml_target_text(str(new))
    if not wanted:
        return built
    out = _Pkg(built, "XLSX file")
    dst = _xlsx_walk(out)
    notes = {(s["sheet"], s["ref"].upper()): s for s in dst if s["kind"] == "note"}
    threads = {s["id"]: s for s in dst if s["kind"] == "thread" and s["id"]}
    # A threaded comment's placeholder note is skipped by the walk; the built package
    # may have dropped the threaded part and kept only that note (openpyxl does), so the
    # note is looked up separately for a fallback that leaves it alone.
    touched: set = set()
    for j, new in wanted.items():
        s = src[j]
        core = s["text"].strip()
        if not core or new == core:
            continue
        if s["kind"] == "thread":
            d = threads.get(s["id"])
            if d is None:
                continue
            text = d["el"].text or ""
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()) :]
            d["el"].text = lead + new + trail
            touched.add(d["part"])
        else:
            d = notes.get((s["sheet"], s["ref"].upper()))
            if d is not None and _write_note(d["el"], core, new):
                touched.add(d["part"])
    if not touched:
        return built
    changed = {name: serialize(out.roots[name], out.zf.read(name)) for name in touched}
    return repack(built, changed)
