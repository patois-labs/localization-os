"""Trados Studio file-based translation memory (.sdltm) import.

An .sdltm file is a SQLite database (RWS: "File-based TMs have the extension *.sdltm, and are
based on SQLite"). Each translation unit is one row of `translation_units`, whose
`source_segment` / `target_segment` columns hold the segment serialized as XML:

    <Segment><Elements><Text><Value>Click </Value></Text>
      <Tag><Type>Start</Type><Anchor>1</Anchor><AlignmentAnchor>1</AlignmentAnchor>
           <TagID>1</TagID></Tag><Text><Value>Save</Value></Text>
      <Tag><Type>End</Type><Anchor>1</Anchor>...</Tag></Elements>
      <CultureName>en-US</CultureName></Segment>

`translation_memories` carries the TM's `source_language` / `target_language`. Tag types follow
Sdl.LanguagePlatform.Core.TagType (Start, End, Standalone, TextPlaceholder, LockedContent,
Undefined, UnmatchedStart, UnmatchedEnd).

DESIGN: this module only READS the SQLite file and turns every readable unit into a TMX <tu>;
the TMX it builds goes through `tm.import_tmx`, so dedupe (project, locales, source hash), the
refusal gate, the #567 integrity gate (a unit with inline tags is withheld as `tagged_source`,
exactly as for a TMX), locale reconciliation, the chunked writes and the report are the SAME
code. Units this module cannot read are counted in `skipped_unreadable` with a code per unit.

UNTRUSTED INPUT: the bytes are copied to a private temp file and opened with the SQLite URI
`mode=ro&immutable=1` (no journal, no WAL, no writes), extension loading off, SQLite defensive
mode, `cell_size_check` on, no mmap, `trusted_schema` off, `query_only` on, an authorizer
that allows only reads of the two known tables (plus the schema lookups this module issues),
and a progress handler that aborts a runaway query. Only
real TABLES named `translation_units` / `translation_memories` are read (a hostile file can
define a VIEW under either name). Each segment's XML is size-capped and parsed by lxml with
entities, DTDs and the network off; a DOCTYPE is refused outright.

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from lxml import etree

SQLITE_MAGIC = b"SQLite format 3\x00"

# Per-segment XML ceiling (characters). A real Trados segment is a few KB at most; the unit
# text itself is capped again by config.MAX_SEGMENT_CHARS after parsing.
SDLTM_MAX_SEGMENT_XML = 400_000
# SQLite VM steps between progress-handler calls, and the most calls one query may take
# before it is aborted (about 2 billion steps: far beyond a 500k-row TM read).
_PROGRESS_STEP = 10_000
_PROGRESS_MAX_CALLS = 200_000
# How many unreadable units the report lists row by row (the counts are exact).
SDLTM_REPORT_ROWS = 20

SKIP_BAD_XML = "sdltm_bad_xml"
SKIP_EMPTY = "sdltm_empty"
SKIP_TOO_LARGE = "sdltm_too_large"
SKIP_UNSUPPORTED = "sdltm_unsupported_element"
SKIP_BAD_CHARS = "sdltm_invalid_chars"
SKIP_LANGUAGE = "sdltm_language"

_KNOWN_TABLES = ("translation_units", "translation_memories")
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.DOTALL)
_DOCTYPE = re.compile(r"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
# XML 1.0 Char production: anything outside it cannot be written to the TMX.
_BAD_XML_CHARS = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
_PAIRED_TYPES = {"start": "start", "end": "end", "unmatchedstart": "start", "unmatchedend": "end"}
_POINT_TYPES = {"standalone", "textplaceholder", "lockedcontent", "undefined"}


class SdltmError(ValueError):
    """The file is not a readable Trados .sdltm (shown to the user as a refusal)."""


def is_sdltm(data: bytes) -> bool:
    """True when `data` is a SQLite database (the only container an .sdltm uses)."""
    return bytes(data[:16]) == SQLITE_MAGIC


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _child_text(el, name: str) -> str:
    for c in el:
        if _local(c) == name:
            return (c.text or "").strip()
    return ""


class _Skip(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _parse_segment_xml(raw) -> etree._Element:
    if raw is None:
        raise _Skip(SKIP_EMPTY, "segment is empty")
    if isinstance(raw, bytes | bytearray | memoryview):
        raw = bytes(raw)
        if len(raw) > SDLTM_MAX_SEGMENT_XML * 4:
            raise _Skip(SKIP_TOO_LARGE, "segment XML is over the size limit")
        for enc in ("utf-8", "utf-16"):
            try:
                raw = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise _Skip(SKIP_BAD_XML, "segment is not UTF-8 or UTF-16 text")
    if not isinstance(raw, str):
        raise _Skip(SKIP_BAD_XML, "segment is not text")
    if len(raw) > SDLTM_MAX_SEGMENT_XML:
        raise _Skip(SKIP_TOO_LARGE, "segment XML is over the size limit")
    raw = raw.lstrip("﻿")
    if _DOCTYPE.search(raw):
        raise _Skip(SKIP_BAD_XML, "segment XML declares a DOCTYPE or entity")
    # Trados writes `<?xml ... encoding="utf-16"?>` on a string column; lxml refuses an
    # encoding declaration on a str, so the declaration is dropped (the text is decoded).
    raw = _XML_DECL.sub("", raw, count=1)
    if not raw.strip():
        raise _Skip(SKIP_EMPTY, "segment is empty")
    try:
        root = etree.fromstring(raw, parser=_xml_parser())  # entities, DTD and network off
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise _Skip(SKIP_BAD_XML, f"segment XML is malformed ({str(exc)[:80]})") from None
    if _local(root) != "Segment":
        raise _Skip(SKIP_BAD_XML, "segment XML has no <Segment> root")
    return root


class _TagNumbers:
    """Maps Trados tags to the segment token scheme (services/segment: g = paired, x = point).

    Source tags are numbered in order (one shared counter, like the XLIFF filter). A paired
    End takes its Start's number through `Anchor`. Target tags reuse the SOURCE number of
    the tag with the same `AlignmentAnchor` (Trados' source/target tag link), else the same
    `TagID`, else a fresh number, which the TM integrity gate then reports as damage."""

    def __init__(self, source_keys: dict | None = None, start_at: int = 0):
        self.source_keys = source_keys
        self.keys: dict = {}
        self.anchor: dict[str, int] = {}
        self.n = start_at

    def _fresh(self) -> int:
        self.n += 1
        return self.n

    def _for_key(self, align: str, tag_id: str) -> int:
        if self.source_keys is not None:
            for key in (("a", align), ("i", tag_id)):
                if key[1] and key in self.source_keys:
                    return self.source_keys[key]
            return self._fresh()
        n = self._fresh()
        for key in (("a", align), ("i", tag_id)):
            if key[1]:
                self.keys.setdefault(key, n)
        return n

    def token(self, tag_el) -> str:
        from app.services.segment import ph_close, ph_open, ph_point

        kind = _child_text(tag_el, "Type").lower()
        anchor = _child_text(tag_el, "Anchor")
        align = _child_text(tag_el, "AlignmentAnchor")
        tag_id = _child_text(tag_el, "TagID")
        paired = _PAIRED_TYPES.get(kind)
        if paired == "start":
            n = self._for_key(align, tag_id)
            if anchor:
                self.anchor[anchor] = n
            return ph_open("g", n)
        if paired == "end":
            n = self.anchor.get(anchor) if anchor else None
            if n is None:
                n = self._for_key(align, tag_id)
            return ph_close("g", n)
        if kind in _POINT_TYPES or not kind:
            return ph_point("x", self._for_key(align, tag_id))
        raise _Skip(SKIP_UNSUPPORTED, f"unknown tag type {kind[:40]!r}")


def _segment_text(root, numbers: _TagNumbers) -> tuple[str, str]:
    """(text with inline tokens, CultureName) for one <Segment>."""
    elements = None
    for c in root:
        if _local(c) == "Elements":
            elements = c
            break
    parts: list[str] = []
    if elements is not None:
        for el in elements:
            name = _local(el)
            if not name:
                continue  # comment / PI already removed; defensive
            if name == "Text":
                for v in el:
                    if _local(v) == "Value":
                        parts.append(v.text or "")
            elif name == "Tag":
                parts.append(numbers.token(el))
            else:
                raise _Skip(SKIP_UNSUPPORTED, f"unsupported segment element <{name[:40]}>")
    return "".join(parts), _child_text(root, "CultureName")


def _unit_pair(src_raw, tgt_raw) -> tuple[str, str, str, str]:
    """(source text, target text, source culture, target culture) or raises _Skip."""
    from app import config

    src_root = _parse_segment_xml(src_raw)
    tgt_root = _parse_segment_xml(tgt_raw)
    src_nums = _TagNumbers()
    src, src_lang = _segment_text(src_root, src_nums)
    tgt_nums = _TagNumbers(source_keys=src_nums.keys, start_at=src_nums.n)
    tgt, tgt_lang = _segment_text(tgt_root, tgt_nums)
    if not src.strip() or not tgt.strip():
        raise _Skip(SKIP_EMPTY, "source or target text is empty")
    if len(src) > config.MAX_SEGMENT_CHARS or len(tgt) > config.MAX_SEGMENT_CHARS:
        raise _Skip(SKIP_TOO_LARGE, "unit text is longer than MAX_SEGMENT_CHARS")
    if _BAD_XML_CHARS.search(src) or _BAD_XML_CHARS.search(tgt):
        raise _Skip(SKIP_BAD_CHARS, "unit text holds characters XML cannot carry")
    return src, tgt, src_lang, tgt_lang


# --- the SQLite side -----------------------------------------------------------------------
_ALLOWED_READ_TABLES = {
    "translation_units",
    "translation_memories",
    "sqlite_master",
    "sqlite_schema",
}


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return (
            sqlite3.SQLITE_OK
            if (arg1 or "").lower() in _ALLOWED_READ_TABLES
            else sqlite3.SQLITE_DENY
        )
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in (
        "table_info",
        "query_only",
        "trusted_schema",
    ):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("count", "lower"):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _open_readonly(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True)
    try:
        if hasattr(conn, "enable_load_extension"):
            conn.enable_load_extension(False)
        # SQLite's "defense against the dark arts" settings for an untrusted database file:
        # defensive mode (Python 3.12+), b-tree cell checks against crafted pages, no mmap.
        if hasattr(conn, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        conn.execute("PRAGMA cell_size_check=ON")
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        calls = [0]

        def _progress() -> int:
            calls[0] += 1
            return 1 if calls[0] > _PROGRESS_MAX_CALLS else 0

        conn.set_progress_handler(_progress, _PROGRESS_STEP)
        conn.set_authorizer(_authorizer)
    except Exception:
        conn.close()
        raise
    return conn


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)", _KNOWN_TABLES
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn, table: str) -> set[str]:
    # `table` is one of _KNOWN_TABLES (never user text), so the f-string is safe.
    return {str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info({table})")}


def read_sdltm_units(data: bytes) -> dict:
    """Read every unit of an .sdltm. Returns {"units": [(row_no, tu_id, src, tgt, src_lang,
    tgt_lang)], "tm_languages": (src, tgt) or None, "total": rows, "skipped": n,
    "skipped_by_code": {...}, "skipped_rows": [...]}. Raises SdltmError for a file that is
    not a readable Trados TM, InputLimitError over the row cap."""
    from app import config
    from app.services import seed_caps

    if len(data) > config.MAX_UPLOAD_BYTES:
        raise SdltmError(
            f"the SDLTM file is {len(data)} bytes, over the "
            f"{config.MAX_UPLOAD_BYTES}-byte upload limit."
        )
    if not is_sdltm(data):
        raise SdltmError("this is not an SDLTM file (a Trados file-based TM is a SQLite database).")
    fd, path = tempfile.mkstemp(prefix="loc-sdltm-", suffix=".sdltm")
    conn = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            conn = _open_readonly(path)
            return _read_units(conn, seed_caps)
        except sqlite3.DatabaseError as exc:
            raise SdltmError(f"could not read the SDLTM database ({str(exc)[:120]}).") from None
    finally:
        if conn is not None:
            conn.close()
        with contextlib.suppress(OSError):
            os.unlink(path)


def _read_units(conn, seed_caps) -> dict:
    tables = _tables(conn)
    if "translation_units" not in tables:
        raise SdltmError(
            "this SQLite file has no translation_units table, so it is not a Trados TM."
        )
    tu_cols = _columns(conn, "translation_units")
    if not {"source_segment", "target_segment"} <= tu_cols:
        raise SdltmError(
            "the translation_units table has no source_segment/target_segment columns; "
            "this SDLTM layout is not supported."
        )
    tm_langs = None
    if "translation_memories" in tables:
        tm_cols = _columns(conn, "translation_memories")
        if {"source_language", "target_language"} <= tm_cols:
            row = conn.execute(
                "SELECT source_language, target_language FROM translation_memories LIMIT 1"
            ).fetchone()
            if row and row[0] and row[1]:
                tm_langs = (str(row[0])[:35], str(row[1])[:35])

    total = conn.execute("SELECT count(*) FROM translation_units").fetchone()[0] or 0
    seed_caps.check_import_rows(int(total), what="This SDLTM file")
    id_col = "id" if "id" in tu_cols else "rowid"
    order = "ORDER BY id" if id_col == "id" else ""
    cur = conn.execute(
        f"SELECT {id_col}, source_segment, target_segment FROM translation_units {order}"  # noqa: S608 -- fixed identifiers
    )
    units: list[tuple] = []
    skipped = 0
    by_code: dict[str, int] = {}
    rows: list[dict] = []
    walked = 0
    for tu_id, src_raw, tgt_raw in cur:
        walked += 1
        seed_caps.check_import_rows(walked, what="This SDLTM file")
        try:
            src, tgt, src_lang, tgt_lang = _unit_pair(src_raw, tgt_raw)
        except _Skip as sk:
            skipped += 1
            by_code[sk.code] = by_code.get(sk.code, 0) + 1
            if len(rows) < SDLTM_REPORT_ROWS:
                rows.append(
                    {
                        "row": walked,
                        "tu_id": _int_or_none(tu_id),
                        "code": sk.code,
                        "reason": sk.reason,
                        "source": "",
                    }
                )
            continue
        s_lang = src_lang or (tm_langs[0] if tm_langs else "")
        t_lang = tgt_lang or (tm_langs[1] if tm_langs else "")
        units.append((walked, _int_or_none(tu_id), src, tgt, s_lang[:35], t_lang[:35]))
    return {
        "units": units,
        "tm_languages": tm_langs,
        "total": walked,
        "skipped": skipped,
        "skipped_by_code": by_code,
        "skipped_rows": rows,
    }


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def units_to_tmx(units, source_locale: str, target_locale: str) -> tuple[bytes, list[int], dict]:
    """A TMX 1.4 document for `units` (only those whose languages match the requested pair,
    in either direction). Returns (tmx bytes, sdltm row number per <tu>, language skips)."""
    from app.services.tm_common import _lang_match

    root = etree.Element("tmx", version="1.4")
    etree.SubElement(
        root,
        "header",
        creationtool="sdltm-import",
        creationtoolversion="1",
        segtype="sentence",
        adminlang="en",
        srclang=source_locale,
        datatype="plaintext",
        **{"o-tmf": "sdltm"},
    )
    body = etree.SubElement(root, "body")
    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    row_map: list[int] = []
    lang_skips: list[tuple[int, int | None, str, str]] = []
    for row_no, tu_id, src, tgt, s_lang, t_lang in units:
        fwd = _lang_match(s_lang, source_locale) and _lang_match(t_lang, target_locale)
        rev = _lang_match(t_lang, source_locale) and _lang_match(s_lang, target_locale)
        if not (fwd or rev):
            lang_skips.append((row_no, tu_id, s_lang, t_lang))
            continue
        if not fwd:  # reversed direction: the file's target is the requested source
            src, tgt = tgt, src
        # Label each tuv with the REQUESTED locale it stands for, so import_tmx's picker
        # never confuses two variants of one language (en-US -> en-GB).
        tu = etree.SubElement(body, "tu")
        for lang, text in ((source_locale, src), (target_locale, tgt)):
            tuv = etree.SubElement(tu, "tuv")
            tuv.set(xml_lang, lang)
            etree.SubElement(tuv, "seg").text = text
        row_map.append(row_no)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8"), row_map, lang_skips


def import_sdltm(
    session,
    project_id: int | None,
    data: bytes,
    source_locale: str,
    target_locale: str,
    *,
    acknowledge_placeholder_loss: bool = False,
    progress=None,
) -> dict:
    """Import a Trados .sdltm into the project's segment TM through `tm.import_tmx`.

    The reply is import_tmx's (imported / updated = merged into an existing entry /
    skipped = unchanged / skipped_refusal / skipped_integrity / skipped_by_code /
    skipped_rows / write_chunks ...) plus `format: "sdltm"`, `sdltm_languages`, `sdltm_units`
    (rows read) and `skipped_unreadable` (units this reader could not turn into text, or whose
    languages are not the requested pair), whose codes are merged into `skipped_by_code` and
    whose rows lead `skipped_rows`. Row numbers are positions in the SDLTM (by unit id), with
    the Trados `tu_id` alongside."""
    from app import config
    from app.services import tm, tm_common

    read = read_sdltm_units(data)
    langs = read["tm_languages"]
    if langs:
        ok = (
            tm_common._lang_match(langs[0], source_locale)
            and tm_common._lang_match(langs[1], target_locale)
        ) or (
            tm_common._lang_match(langs[1], source_locale)
            and tm_common._lang_match(langs[0], target_locale)
        )
        if not ok:
            raise SdltmError(
                f"this SDLTM is a {langs[0]} -> {langs[1]} memory; it holds no "
                f"{source_locale} -> {target_locale} units."
            )
    tmx, row_map, lang_skips = units_to_tmx(read["units"], source_locale, target_locale)
    if len(tmx) > config.MAX_UPLOAD_BYTES:
        raise SdltmError(
            "the SDLTM's text is too large to import in one go "
            f"(over {config.MAX_UPLOAD_BYTES} bytes); split the memory in Trados first."
        )
    res = tm.import_tmx(
        session,
        project_id,
        tmx,
        source_locale,
        target_locale,
        acknowledge_placeholder_loss=acknowledge_placeholder_loss,
        progress=progress,
    )

    unread_rows = list(read["skipped_rows"])
    by_code = dict(read["skipped_by_code"])
    for row_no, tu_id, s_lang, t_lang in lang_skips:
        by_code[SKIP_LANGUAGE] = by_code.get(SKIP_LANGUAGE, 0) + 1
        if len(unread_rows) < SDLTM_REPORT_ROWS:
            unread_rows.append(
                {
                    "row": row_no,
                    "tu_id": tu_id,
                    "code": SKIP_LANGUAGE,
                    "reason": f"unit languages {s_lang or '?'} -> {t_lang or '?'} "
                    "are not the requested pair",
                    "source": "",
                }
            )
    unreadable = read["skipped"] + len(lang_skips)

    # import_tmx numbers its rows by position in the generated TMX; map them back to the
    # unit's position in the SDLTM so every surface names the same unit.
    gate_rows = []
    for r in res.get("skipped_rows") or []:
        r = dict(r)
        pos = int(r.get("row") or 0)
        if 1 <= pos <= len(row_map):
            r["row"] = row_map[pos - 1]
        gate_rows.append(r)
    merged_codes = dict(res.get("skipped_by_code") or {})
    for k, v in by_code.items():
        merged_codes[k] = merged_codes.get(k, 0) + v
    all_rows = sorted(unread_rows + gate_rows, key=lambda r: int(r.get("row") or 0))
    res.update(
        {
            "format": "sdltm",
            "sdltm_languages": list(langs) if langs else None,
            "sdltm_units": read["total"],
            "skipped_unreadable": unreadable,
            "skipped_by_code": merged_codes,
            "skipped_rows": all_rows[: max(SDLTM_REPORT_ROWS, tm.TMX_IMPORT_REPORT_ROWS)],
        }
    )
    return res
