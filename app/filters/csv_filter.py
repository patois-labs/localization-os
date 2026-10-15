"""CSV / TSV as a translatable document (Batch 6).

A product catalogue, CMS export, game string table or a multilingual sheet (one column
per language) arrives as CSV. The column mapping (app/filters/csv_filter_common.py) says which
column is the source, which column each target language writes into, and which columns
are shown to the translator as notes (key, context, max length). With no mapping the
header row is read: a `source`/`text` or source-locale column becomes the source and a
`target`/`translation` or target-locale column the target. With neither, every text cell
is translated where it stands (the XLSX default).

Byte fidelity: the file is read record by record (Python's csv module is the parser;
each record's raw text is kept), and the build writes back the raw text of every record
it does not change. In a changed record only the written cell's bytes change: the other
fields keep their exact quoting. Delimiter (`,` `;` TAB `|`), quote doubling and each
record's line ending are kept.

Encoding (#509): UTF-8 (with or without BOM) and UTF-16 with a BOM are read and written
back the same way (a UTF-8 BOM stays, UTF-16 LE/BE stays UTF-16 with its BOM, which is
what Excel's "Unicode Text" saves). A Windows-1252 file is converted (the shared #509
rule) and written back in Windows-1252 when every character fits (B-bytes), else as UTF-8
with a BOM, so Excel still opens it correctly; anything else is refused with the shared
re-save-as-UTF-8 message.

Formula injection: every cell this filter WRITES goes through output_safety.csv_safe_cell
(a leading = + - @, TAB or CR is prefixed with an apostrophe, OWASP). Source cells that
are formulas (`=SUM(A1)`, Excel's `="0012"`) are never units and never touched.

Target columns: a target cell that already holds text keeps it when the segment has no
translation of its own (the export fell back to the source), so a half-filled `fr`
column in a multilingual sheet is not overwritten with English.
"""

from __future__ import annotations

import codecs
import csv
import re

from app.filters import csv_filter_common as tabular
from app.filters.base import (
    Filter,
    InputLimitError,
    ParseResult,
    TextUnit,
    check_segment_cap,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.output_safety import csv_safe_cell

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")
_CANDIDATES = (",", ";", "\t", "|")


class _Dialect(csv.Dialect):
    delimiter = ","
    quotechar = '"'
    doublequote = True
    skipinitialspace = False
    lineterminator = "\r\n"
    quoting = csv.QUOTE_MINIMAL
    strict = False


def _dialect(delim: str):
    return type("_D", (_Dialect,), {"delimiter": delim})


def _decode(data: bytes) -> tuple[str, dict]:
    """(text, how) where `how` records exactly how to write the file back."""
    data = bytes(data)
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        le = data.startswith(codecs.BOM_UTF16_LE)
        try:
            text = data[2:].decode("utf-16-le" if le else "utf-16-be")
        except UnicodeDecodeError:
            text = None
        if text is not None:
            from app.filters.base import check_line_budget

            check_line_budget(text)
            return text, {"enc": "utf-16-le" if le else "utf-16-be", "bom": True}
    text, enc = read_text_upload(data, sig=True)
    bom = data.startswith(codecs.BOM_UTF8)
    if enc != "utf-8":
        # Converted (#509): written back as UTF-8 with a BOM so Excel reads it as UTF-8.
        return text, {"enc": "utf-8", "bom": True, "converted": enc}
    return text, {"enc": "utf-8", "bom": bom}


def _encode(text: str, how: dict) -> bytes:
    if how.get("converted"):
        # B-bytes: a converted file goes back in its own code page when every character
        # fits (no BOM, as uploaded); only a translation that does not fit becomes UTF-8.
        try:
            return text.encode(how["converted"])
        except (UnicodeEncodeError, LookupError):
            pass
    enc = how.get("enc", "utf-8")
    body = text.encode(enc, "surrogateescape" if enc == "utf-8" else "strict")
    if not how.get("bom"):
        return body
    bom = {
        "utf-8": codecs.BOM_UTF8,
        "utf-16-le": codecs.BOM_UTF16_LE,
        "utf-16-be": codecs.BOM_UTF16_BE,
    }[enc]
    return bom + body


def _sniff_delimiter(text: str, default: str) -> str:
    """The delimiter that splits the first lines most consistently (outside quotes)."""
    lines = [ln for ln in text[:65536].splitlines()[:20] if ln.strip()]
    if not lines:
        return default
    best, best_score = default, -1
    for d in _CANDIDATES:
        try:
            widths = [len(r) for r in csv.reader(lines, _dialect(d))]
        except csv.Error:
            continue
        if not widths or max(widths) < 2:
            continue
        # most rows agreeing on one width > 1, then wider
        common = max(set(widths), key=widths.count)
        score = widths.count(common) * 1000 + min(common, 999) if common > 1 else 0
        if score > best_score:
            best, best_score = d, score
    return best


class _Lines:
    """Feed csv.reader physical lines while remembering the raw text of each record."""

    def __init__(self, text: str):
        self._it = (m.group(0) for m in _LINE_RE.finditer(text))
        self.buf: list[str] = []

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = next(self._it)
        self.buf.append(line)
        return line


def _records(text: str, delim: str):
    """Yield (values, raw) per record; raw includes the record's own line ending."""
    src = _Lines(text)
    reader = csv.reader(src, _dialect(delim))
    try:
        for row in reader:
            raw = "".join(src.buf)
            src.buf.clear()
            if len(row) > tabular.MAX_COLUMNS:
                raise InputLimitError(
                    f"This CSV file has a row with more than {tabular.MAX_COLUMNS} columns."
                )
            yield row, raw
    except csv.Error as exc:
        raise InputLimitError(f"This CSV file could not be read: {exc}.") from exc


def _split_ending(raw: str) -> tuple[str, str]:
    body = raw.rstrip("\r\n")
    return body, raw[len(body) :]


def _spans(body: str, delim: str) -> list[tuple[int, int]]:
    """Field spans of one record's text (RFC 4180 with quote doubling)."""
    out = []
    i, n = 0, len(body)
    while True:
        start = i
        if i < n and body[i] == '"':
            i += 1
            while i < n:
                if body[i] == '"':
                    if i + 1 < n and body[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        while i < n and body[i] != delim:
            i += 1
        out.append((start, i))
        if i < n and body[i] == delim:
            i += 1
            continue
        return out


def _quote(value: str, delim: str, force: bool) -> str:
    if force or any(c in value for c in (delim, '"', "\r", "\n")):
        return '"' + value.replace('"', '""') + '"'
    return value


def _rewrite(raw: str, row: list[str], delim: str, writes: dict[int, str]) -> str:
    """The record with only the cells in `writes` changed (other fields byte-identical).
    A target column past the end of a short row is padded with empty fields."""
    body, ending = _split_ending(raw)
    spans = _spans(body, delim)
    fields = [body[a:b] for a, b in spans]
    if len(fields) != len(row):  # parser disagreement: never guess, re-serialise the row
        fields = [_quote(v, delim, False) for v in row]
    width = max([len(fields), *[i + 1 for i in writes]])
    fields += [""] * (width - len(fields))
    for i, value in writes.items():
        was_quoted = fields[i].startswith('"')
        fields[i] = _quote(str(csv_safe_cell(value)), delim, was_quoted)
    return delim.join(fields) + ending


def _plan_and_rows(text: str, table: dict, source_locale: str, targets: list[str]):
    spec = table.get("spec") or {}
    delim = (
        spec.get("delimiter")
        or table.get("delimiter")
        or _sniff_delimiter(text, table.get("default_delimiter", ","))
    )
    recs = _records(text, delim)
    first = None
    peek = []
    for row, raw in recs:
        peek.append((row, raw))
        if any(c.strip() for c in row):
            first = row
            break
    plan = table.get("plan")
    if plan is None:
        width = len(first or [])
        plan = tabular.resolve(spec, first or [], width, source_locale, targets)
    return delim, plan, peek, recs


def _walk(text: str, table: dict, source_locale: str = "", targets=()):
    """Yield (row_no, values, raw, unit_cols, header) in file order; unit_cols are the
    columns of this record that are units, in the order parse and build share."""
    delim, plan, peek, recs = _plan_and_rows(text, table, source_locale, list(targets))

    def _all():
        yield from peek
        yield from recs

    seen_header = False
    for n, (row, raw) in enumerate(_all()):
        is_header = False
        if plan["header"] and not seen_header and any(c.strip() for c in row):
            seen_header = is_header = True
        cols: list[int] = []
        if not is_header:
            if plan["inplace"]:
                note_cols = {i for _, i in plan.get("notes") or []}
                cols = [
                    i
                    for i, v in enumerate(row)
                    if i not in note_cols and tabular.is_translatable(v)
                ]
            else:
                s = plan["source"]
                if s < len(row) and tabular.is_translatable(row[s]):
                    cols = [s]
        yield n, row, raw, cols, is_header, delim, plan


@register
class CsvFilter(Filter):
    fmt = "csv"
    default_delimiter = ","
    sniff = True  # TSV is always TAB unless the mapping says delimiter=
    takes_columns = True

    def parse(
        self,
        data: bytes,
        provider: str | None = None,
        columns: str | dict | None = None,
        source_locale: str = "",
        target_locales: list[str] | None = None,
    ) -> ParseResult:
        text, how = _decode(data)
        spec = columns if isinstance(columns, dict) else tabular.parse_spec(columns)
        table = {"spec": spec, "default_delimiter": self.default_delimiter}
        if not self.sniff:
            table["delimiter"] = self.default_delimiter
        units: list[TextUnit] = []
        out_units: list[dict] = []
        plan = delim = None
        for row_no, row, _raw, cols, _hdr, row_delim, plan in _walk(
            text, table, source_locale, target_locales or []
        ):
            delim = row_delim
            note = tabular.unit_note(plan, row)
            for c in cols:
                idx = len(units)
                units.append(TextUnit(p_index=idx, text=row[c]))
                unit = {"p_index": idx, "text": row[c], "style": "", "row": row_no, "col": c}
                if note:
                    unit["note"] = note
                out_units.append(unit)
                check_segment_cap(idx + 1)  # #571
        skeleton = {
            "format": self.fmt,
            "units": out_units,
            "table": {
                "spec": spec,
                "plan": plan,
                "delimiter": delim or self.default_delimiter,
                "encoding": how,
            },
        }
        return ParseResult(
            skeleton=note_source_encoding(skeleton, how.get("converted") or "utf-8"), units=units
        )

    def build(self, original: bytes, paragraph_texts: dict, table: dict | None = None) -> bytes:
        return self.build_localized(original, paragraph_texts, None, table=table)

    def build_localized(
        self,
        original: bytes,
        paragraph_texts: dict,
        target_locale: str | None = None,
        table: dict | None = None,
        fallback_keys: set | None = None,
    ) -> bytes:
        """Rebuild for one target language. `table` is the skeleton's stamp (spec, plan,
        delimiter) so the rebuild walks exactly the units ingest made; without it (a
        stored document from a caller that has no skeleton) the file is re-planned the
        same deterministic way. `fallback_keys` (from export) names the units whose text is
        the source standing in for a missing translation: their target cell is left as it
        is (empty stays empty), so a target column never fills up with source text."""
        text, how = _decode(original)
        table = dict(table or {"default_delimiter": self.default_delimiter})
        if not self.sniff and not table.get("delimiter"):
            table["delimiter"] = self.default_delimiter  # TSV is TAB, as parse() reads it
        if table.get("encoding"):
            how = {**how, **{k: v for k, v in table["encoding"].items() if k in ("enc", "bom")}}
        out: list[str] = []
        k = 0
        for _n, row, raw, cols, _hdr, delim, plan in _walk(text, table):
            writes: dict[int, str] = {}
            tcol = None if plan["inplace"] else tabular.target_col(plan, target_locale)
            for c in cols:
                tr = paragraph_texts.get(str(k))
                k += 1
                if tr is None:
                    continue
                if plan["inplace"]:
                    if tr != row[c]:
                        writes[c] = tr
                elif tcol is None:
                    if tr != row[c]:
                        writes[c] = tr  # no target column for this locale: replace source
                else:
                    existing = row[tcol] if tcol < len(row) else ""
                    if fallback_keys is not None and str(k - 1) in fallback_keys:
                        continue
                    if tr == row[c] and existing.strip():
                        continue  # untranslated: keep a target someone already filled
                    if tr != existing:
                        writes[tcol] = tr
            out.append(_rewrite(raw, row, delim, writes) if writes else raw)
        return _encode("".join(out), how)


@register
class TsvFilter(CsvFilter):
    fmt = "tsv"
    default_delimiter = "\t"
    exts = ("tab",)
    sniff = False
