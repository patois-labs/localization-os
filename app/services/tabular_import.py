"""Spreadsheet glossaries and TMs (Batch 6).

LSPs migrate termbases and translation memories as Excel sheets or tab-separated text.
Neither gets its own importer: each spreadsheet is converted, in memory, into the format
the existing importer already reads, so every rule those importers enforce (dedupe,
#615 chunked writes, the #567 integrity gate, refusal screening, locale reconciliation)
applies unchanged:

* glossary XLSX / TSV  ->  the glossary CSV `glossary_hub.import_bytes` reads
  (header `source_term,target_term,...`);
* TM XLSX / CSV / TSV (two columns: source, target)  ->  a TMX body for `tm.import_tmx`.

Rows are read by the same readers the document filters use: XLSX through
`xlsx_filter._load_checked` (xlsx_guard zip-bomb and XML checks first), CSV/TSV through
`csv_filter` (UTF-8 / UTF-8 BOM / UTF-16 BOM; Windows-1252 converted, anything else
refused, #509). Cells we write into the intermediate CSV go through
`app.output_csv.writer` (csv_safe_cell), which the glossary reader's
`csv_original_cell` undoes exactly, so a term that starts with `=` or `-` arrives as typed.

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import io
import re

from app.filters import csv_filter_common as tabular
from app.filters.base import InputLimitError

ZIP_MAGIC = b"PK\x03\x04"
MAX_ROWS = 1_000_000
_GLOSSARY_COLUMNS = {
    "source_term",
    "target_term",
    "dnt",
    "sense_label",
    "context_cue",
    "part_of_speech",
    "definition",
    "usage_example",
    "subject_field",
    "status",
    "term_type",
    "audience",
    "grammatical_gender",
    "grammatical_number",
    "geographical_usage",
}
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _ext(filename: str | None) -> str:
    name = (filename or "").strip().lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def is_spreadsheet(data: bytes, filename: str | None, *, csv_too: bool) -> bool:
    """True for an XLSX (by name or zip magic), a TSV/TAB, and (csv_too) a CSV."""
    ext = _ext(filename)
    if ext in ("xlsx", "tsv", "tab") or (csv_too and ext == "csv"):
        return True
    if ext:
        return False
    head = bytes(data[:4])
    if head == ZIP_MAGIC:
        return True
    if not csv_too:
        return False
    stripped = bytes(data[:64]).lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n")
    return bool(stripped) and stripped[:1] not in (b"<", b"{")


def rows(data: bytes, filename: str | None = None) -> list[list[str]]:
    """Every row of the first sheet / the whole CSV, as strings ("" for empty cells)."""
    out: list[list[str]] = []
    if bytes(data[:4]) == ZIP_MAGIC or _ext(filename) == "xlsx":
        from app.filters.xlsx_filter import _cell_str, _load_checked

        wb = _load_checked(bytes(data))
        if not wb.worksheets:
            return out
        for row in wb.worksheets[0].iter_rows(values_only=True):
            out.append([_cell_str(v) for v in row[: tabular.MAX_COLUMNS]])
            if len(out) > MAX_ROWS:
                raise InputLimitError(f"This spreadsheet has more than {MAX_ROWS} rows.")
        return out
    from app.filters import csv_filter

    text, _how = csv_filter._decode(bytes(data))
    tsv = _ext(filename) in ("tsv", "tab")
    delim = "\t" if tsv else csv_filter._sniff_delimiter(text, ",")
    for row, _raw in csv_filter._records(text, delim):
        out.append(row)
        if len(out) > MAX_ROWS:
            raise InputLimitError(f"This file has more than {MAX_ROWS} rows.")
    return out


def _pair_plan(first: list[str], source_locale: str, target_locale: str) -> tuple[bool, int, int]:
    """(has_header, source col, target col) for a two-language sheet: header names
    `source`/`target` (and synonyms) or the two locale codes; no header -> columns 1, 2."""
    s_col = t_col = None
    for i, h in enumerate(first):
        n = tabular._norm(h)
        if n in ("source_term", *tabular._ROLE_NAMES["source"]) or (
            source_locale and tabular.locale_match(h, [source_locale])
        ):
            s_col = i if s_col is None else s_col
        elif n in ("target_term", *tabular._ROLE_NAMES["target"]) or (
            target_locale and tabular.locale_match(h, [target_locale])
        ):
            t_col = i if t_col is None else t_col
    if s_col is not None and t_col is not None:
        return True, s_col, t_col
    return False, 0, 1


# --- glossary ------------------------------------------------------------------
def glossary_csv(
    data: bytes, filename: str | None, source_locale: str = "", target_locale: str = ""
) -> bytes:
    """A glossary XLSX/TSV as the glossary CSV the existing importer reads. Header cells
    that already are glossary columns keep their name; `source`/`target` (or the two
    locale codes) become source_term/target_term; a sheet with no header is read as
    source, target."""
    table = rows(data, filename)
    while table and not any(c.strip() for c in table[0]):
        table.pop(0)
    if not table:
        return b"source_term,target_term\r\n"
    first = table[0]
    names = [tabular._norm(h).replace(" ", "_") for h in first]
    if "source_term" in names:
        header, body = names, table[1:]
    else:
        has_header, s_col, t_col = _pair_plan(first, source_locale, target_locale)
        header = []
        for i, n in enumerate(names):
            if i == s_col:
                header.append("source_term")
            elif i == t_col:
                header.append("target_term")
            else:
                header.append(n if (has_header and n in _GLOSSARY_COLUMNS) else f"_col{i + 1}")
        body = table[1:] if has_header else table
    from app import output_csv

    buf = io.StringIO()
    w = output_csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(body)
    return buf.getvalue().encode("utf-8")


def glossary_payload(data: bytes, filename: str | None, source_locale: str, target_locale: str):
    """(data, filename) for glossary_hub.import_bytes: a spreadsheet is converted to the
    glossary CSV; anything else (CSV, TBX, .glossary.json) passes through unchanged."""
    if not is_spreadsheet(data, filename, csv_too=False):
        return data, filename
    stem = (filename or "glossary").rsplit(".", 1)[0]
    return glossary_csv(data, filename, source_locale, target_locale), stem + ".csv"


# --- translation memory --------------------------------------------------------
def looks_like_tmx(data: bytes) -> bool:
    from app.services.glossary_common import _looks_like_xml

    return _looks_like_xml(bytes(data or b""))


def tm_tmx(data: bytes, filename: str | None, source_locale: str, target_locale: str) -> bytes:
    """A two-column TM spreadsheet (XLSX / CSV / TSV) as a TMX body. Rows with an empty
    source or target are dropped (nothing to remember); characters XML cannot carry are
    removed. The TMX importer then validates, dedupes and writes as for any TMX."""
    table = rows(data, filename)
    while table and not any(c.strip() for c in table[0]):
        table.pop(0)
    if not table:
        raise InputLimitError("This translation memory file has no rows.")
    has_header, s_col, t_col = _pair_plan(table[0], source_locale, target_locale)
    if not has_header and max(len(r) for r in table[:50]) < 2:
        raise InputLimitError(
            "A spreadsheet translation memory needs two columns: source, then target "
            "(or a header row naming source and target, or the two language codes)."
        )
    from xml.sax.saxutils import escape, quoteattr

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n<tmx version="1.4"><header '
        f'srclang={quoteattr(source_locale)} datatype="plaintext" segtype="sentence" '
        'adminlang="en" o-tmf="spreadsheet" creationtool="localization-os"/><body>'
    ]
    for r in table[1:] if has_header else table:
        src = _XML_ILLEGAL.sub("", r[s_col] if s_col < len(r) else "")
        tgt = _XML_ILLEGAL.sub("", r[t_col] if t_col < len(r) else "")
        if not src.strip() or not tgt.strip():
            continue
        out.append(
            f"<tu><tuv xml:lang={quoteattr(source_locale)}><seg>{escape(src)}</seg></tuv>"
            f"<tuv xml:lang={quoteattr(target_locale)}><seg>{escape(tgt)}</seg></tuv></tu>"
        )
    out.append("</body></tmx>\n")
    return "".join(out).encode("utf-8")
