"""Salesforce Translation Workbench .stf filter (Source, Outdated and untranslated, Bilingual).

An STF file is UTF-8 plain text, tab-separated, one translatable item per line, exported
from Setup > Translation Workbench > Export and re-imported through Translation Workbench
> Import. Its layout:

  # comment lines (instructions; ignored by the import)
  Language code: de
  Type: Bilingual                       (Source | Outdated and untranslated | Bilingual)
  Translation type: Metadata            (optional)
  ------------------TRANSLATED-------------------          (Bilingual; dashed section marker)
  # KEY<TAB>LABEL<TAB>TRANSLATION<TAB>OUT OF DATE           (column header, a comment)
  CustomField.Account.Region.FieldLabel<TAB>Region<TAB>Region<TAB>-
  ------------------UNTRANSLATED-----------------
  # KEY<TAB>LABEL
  CustomLabel.Welcome<TAB>Welcome back, {0}!

parse(): every data row whose source label has text becomes one unit, in file order. The
source is the LABEL column. KEY is never a unit and is never changed.

build(): writes the Salesforce import shape in place:
  - a row under a column header that names TRANSLATION (the Bilingual TRANSLATED section)
    gets the translation in its TRANSLATION column; LABEL and OUT OF DATE are kept;
  - every other row (Source, Outdated and untranslated, the Bilingual UNTRANSLATED
    section) gets the translation in place of its LABEL, as the Workbench instructions say.
A unit whose text did not change is not touched (an existing reviewed translation stays).
Every byte outside changed cells is identical; an unchanged file comes back byte for byte.

Escapes: Salesforce writes tab, newline and carriage return inside a label as the two
characters `\\t`, `\\n`, `\\r` (and a backslash as `\\\\`). Those, `{0}` style
String.format / MessageFormat arguments and `{!...}` merge fields become point tokens
(⟦x1⟧, services/segment) rebuilt from the original text. A raw tab, newline or carriage
return in a translation is written back escaped, so a row can never split.

build_localized(): also sets the `Language code:` header to the target in Salesforce's
code form (de, fr, es_MX, pt_BR, zh_CN, zh_TW, nl_NL, iw, in, no, ...): a Source file
carries the org's default language and must name the target to import.

Max lengths: STF carries no length column. Salesforce's own label limits for the common
key types (field label 40, relationship label 80, help text 510, custom label 1000,
picklist value 255, validation error message 255, record type name 80) are reported per
unit by `records()` and `over_limit()`; nothing is truncated (truncation can split a
placeholder).

Encoding: Salesforce exports UTF-8 only. A UTF-8 BOM is kept. A UTF-16 file with a BOM (Excel
"Unicode Text") or a Windows-1252 file is read and exported as UTF-8, the encoding the
import expects.

Specs: help.salesforce.com articles sf.workbench_translation_file_bilingual.htm,
sf.workbench_translation_file_source.htm, sf.workbench_translation_file_outdated.htm,
sf.workbench_import.htm, sf.translation_file_description.htm.

Documented in: docs/architecture/translation-pipeline.md
"""

import re

from app.filters.base import (
    Filter,
    ParseResult,
    TextUnit,
    UnsupportedEncodingError,
    check_segment_cap,
    check_unit_texts,
    note_source_encoding,
    read_text_upload,
    register,
)
from app.services.segment import _PLACEHOLDER_RE, ph_point

_BOM = "\ufeff"
_HEADER_RE = re.compile(r"^(Language code|Type|Translation type)[ \t]*:[ \t]*(.*?)[ \t]*$", re.I)
_SECTION_RE = re.compile(r"^-{3,}[ \t]*([A-Za-z][A-Za-z ]{0,60}?)[ \t]*-{3,}[ \t]*$")
_COLUMNS_RE = re.compile(r"^#[ \t]*KEY\t", re.I)
_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
# Point-token spans inside a label: STF escapes, {0} arguments, {!merge.fields}.
_TOKEN_RE = re.compile(r"\\[ntr\\]|\{\d{1,3}(?:,[^{}\r\n]{0,60})?\}|\{![^{}\r\n]{1,200}\}")

# Salesforce label limits by key component type (see module docstring; never truncated).
_LIMITS = (
    (re.compile(r"^CustomField\..*\.FieldLabel$"), 40),
    (re.compile(r"^CustomField\..*\.RelationshipLabel$"), 80),
    (re.compile(r"^CustomField\..*\.HelpText$"), 510),
    (re.compile(r"^CustomLabel\."), 1000),
    (re.compile(r"^PicklistValue\."), 255),
    (re.compile(r"^ValidationFormula\..*\.ErrorMessage$"), 255),
    (re.compile(r"^RecordType\..*\.Name$"), 80),
)

# BCP 47 -> Salesforce language code, where the plain `ll` / `ll_RR` rule is wrong.
_SF_LANG = {
    "he": "iw",
    "id": "in",
    "yi": "ji",
    "nb": "no",
    "nn": "no",
    "nl": "nl_NL",
    "zh": "zh_CN",
    "zh_hans": "zh_CN",
    "zh_sg": "zh_CN",
    "zh_hant": "zh_TW",
    "zh_mo": "zh_TW",
}


def max_length_for_key(key: str) -> int | None:
    for rx, n in _LIMITS:
        if rx.search(key):
            return n
    return None


def salesforce_language_code(locale: str) -> str:
    """BCP 47 tag (de-DE, zh-Hant, pt-BR) -> Salesforce Translation Workbench code."""
    parts = [p for p in re.split(r"[-_]", (locale or "").strip()) if p]
    if not parts:
        return ""
    lang = parts[0].lower()
    script = next((p for p in parts[1:] if len(p) == 4 and p.isalpha()), "")
    region = next((p for p in parts[1:] if len(p) in (2, 3) and p.isalnum()), "")
    if lang == "zh" and script:
        key = "zh_" + script.lower()
        if key in _SF_LANG:
            return _SF_LANG[key]
    if region:
        mapped = _SF_LANG.get(f"{lang}_{region.lower()}")
        if mapped:
            return mapped
        base = _SF_LANG.get(lang, lang)
        base = base.split("_")[0]
        return f"{base}_{region.upper()}"
    return _SF_LANG.get(lang, lang)


class _Row:
    __slots__ = ("cells", "dst", "imap", "key", "line_end", "section", "src", "start", "text")


def _tokenize(cell: str) -> tuple[str, dict, bool]:
    parts, imap, pos, has_text = [], {}, 0, False
    for n, m in enumerate(_TOKEN_RE.finditer(cell), 1):
        chunk = cell[pos : m.start()]
        parts.append(chunk)
        has_text = has_text or bool(chunk.strip())
        imap[str(n)] = {"point": m.group()}
        parts.append(ph_point("x", n))
        pos = m.end()
    tail = cell[pos:]
    parts.append(tail)
    has_text = has_text or bool(tail.strip())
    return "".join(parts), imap, has_text


def _escape(s: str) -> str:
    return s.replace("\r\n", "\\n").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _render(text: str, imap: dict) -> str:
    out, pos = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(_escape(text[pos : m.start()]))
        pos = m.end()
        inner = m.group(0)[1:-1]
        if inner.startswith("x"):
            info = imap.get(inner[1:])
            if info:
                out.append(info["point"])
        # unknown or paired tokens are dropped (same tolerance as the other filters)
    out.append(_escape(text[pos:]))
    return "".join(out)


class _Stf:
    """One walk over the decoded text: header spans and data rows, in file order."""

    def __init__(self, text: str):
        self.text = text
        self.headers: dict[str, tuple[int, int, str]] = {}  # name -> (value start, end, value)
        self.type = ""
        self.rows: list[_Row] = []
        self.units: list[_Row] = []
        self._walk()

    def _walk(self):
        text = self.text
        pos = 1 if text.startswith(_BOM) else 0
        section = ""
        columns: list[str] | None = None
        seen_data = False
        # Rows end at CR/LF only: str.splitlines() would also break a label at U+2028,
        # U+0085, form feed or vertical tab, splitting one row into two (gate fix).
        for lm in _LINE_RE.finditer(text, pos):
            raw = lm.group(0)
            start = pos
            pos += len(raw)
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            if line.startswith("#"):
                if _COLUMNS_RE.match(line):
                    columns = [c.strip().upper() for c in line[1:].split("\t")]
                continue
            m = _SECTION_RE.match(line)
            if m:
                section = re.sub(r"\s+", " ", m.group(1).strip().lower())
                columns = None
                continue
            h = _HEADER_RE.match(line) if not seen_data else None
            if h:
                # Checked before the tab test: `Language code:<TAB>de` is a header, not a
                # data row whose label "de" would be translated (gate fix).
                name = h.group(1).lower()
                span = (start + h.start(2), start + h.end(2), h.group(2))
                self.headers.setdefault(name, span)
                if name == "type":
                    self.type = h.group(2).strip()
                continue
            if "\t" not in line:
                continue  # stray line: kept byte for byte, never a unit
            seen_data = True
            cells = line.split("\t")
            src, dst = 1, 1
            if columns and "TRANSLATION" in columns and section != "untranslated":
                src = columns.index("LABEL") if "LABEL" in columns else 1
                dst = columns.index("TRANSLATION")
            elif section == "translated" and len(cells) >= 3:
                src, dst = 1, 2
            if src >= len(cells) or not cells[0].strip():
                continue
            row = _Row()
            row.key = cells[0]
            row.section = section or (self.type.lower() or "source")
            row.src, row.dst, row.start, row.cells = src, dst, start, cells
            row.line_end = start + len(line)
            row.text, row.imap, has_text = _tokenize(cells[src])
            self.rows.append(row)
            if has_text:
                self.units.append(row)
                check_segment_cap(len(self.units))

    def cell_span(self, row: _Row, idx: int) -> tuple[int, int]:
        """Absolute (start, end) of cell `idx`; a missing cell is an empty span at line end."""
        off = row.start
        for i, c in enumerate(row.cells):
            if i == idx:
                return off, off + len(c)
            off += len(c) + 1
        return row.line_end, row.line_end


def _load(data: bytes) -> tuple[_Stf, str, bool]:
    raw = bytes(data)
    text, enc = read_text_upload(raw)  # UTF-8 (BOM kept as U+FEFF), UTF-16 BOM, cp1252
    if "\x00" in text:
        raise UnsupportedEncodingError(
            "This .stf file contains NUL bytes, so it is not a Translation Workbench text "
            "export. Re-export it from Translation Workbench or re-save it as UTF-8."
        )
    bom = text.startswith(_BOM)
    if enc == "utf-16" and bom:
        text = text[1:]
        bom = False
    return _Stf(text), enc, bom


@register
class StfFilter(Filter):
    fmt = "stf"
    exts = ()

    def parse(self, data: bytes, provider: str | None = None) -> ParseResult:
        stf, enc, _bom = _load(data)
        units = [
            TextUnit(p_index=i, text=r.text, style="stf:" + r.section, inline_map=r.imap)
            for i, r in enumerate(stf.units)
        ]
        check_unit_texts(u.text for u in units)
        skeleton = {
            "format": "stf",
            "type": stf.type,
            "language_code": (stf.headers.get("language code") or (0, 0, ""))[2],
            "units": [
                {
                    "p_index": i,
                    "text": r.text,
                    "key": r.key,
                    "style": "stf:" + r.section,
                    **({"max_length": n} if (n := max_length_for_key(r.key)) else {}),
                }
                for i, r in enumerate(stf.units)
            ],
        }
        return ParseResult(skeleton=note_source_encoding(skeleton, enc), units=units)

    def _rebuild(self, original: bytes, paragraph_texts: dict, target_locale=None) -> bytes:
        stf, enc, _bom = _load(original)
        edits = []
        for i, r in enumerate(stf.units):
            new = paragraph_texts.get(str(i))
            if new is None or new == r.text:
                continue
            a, b = stf.cell_span(r, r.dst)
            val = _render(new, r.imap)
            if r.dst >= len(r.cells):  # TRANSLATION cell missing: pad the row out to it
                val = "\t" * (r.dst - len(r.cells) + 1) + val
            edits.append((a, b, val))
        if target_locale:
            hdr = stf.headers.get("language code")
            code = salesforce_language_code(target_locale)
            if hdr and code and hdr[2] != code:
                edits.append((hdr[0], hdr[1], code))
        if not edits and enc == "utf-8":
            return bytes(original)
        text = stf.text
        for a, b, val in sorted(edits, reverse=True):
            text = text[:a] + val + text[b:]
        return text.encode("utf-8")

    def build(self, original: bytes, paragraph_texts: dict) -> bytes:
        return self._rebuild(original, paragraph_texts)

    def build_localized(
        self, original: bytes, paragraph_texts: dict, target_locale: str | None = None
    ) -> bytes:
        return self._rebuild(original, paragraph_texts, target_locale or "")


def records(data: bytes) -> list[dict]:
    """Key view for the keys bridge (Batch 9): one dict per data row, units and blanks alike:
    key, value (the source LABEL), translation (the TRANSLATION cell, Bilingual only),
    out_of_date (True for `*`), section, max_length (Salesforce label limit or None)."""
    stf, _enc, _bom = _load(data)
    out = []
    for r in stf.rows:
        tr = r.cells[r.dst] if r.dst != r.src and r.dst < len(r.cells) else None
        ood = r.cells[r.dst + 1].strip() if r.dst != r.src and r.dst + 1 < len(r.cells) else ""
        out.append(
            {
                "key": r.key,
                "value": r.cells[r.src],
                "translation": tr,
                "out_of_date": ood == "*",
                "section": r.section,
                "max_length": max_length_for_key(r.key),
            }
        )
    return out


def over_limit(original: bytes, paragraph_texts: dict) -> list[dict]:
    """Units whose rendered translation is longer than the Salesforce limit for its key."""
    stf, _enc, _bom = _load(original)
    out = []
    for i, r in enumerate(stf.units):
        new = paragraph_texts.get(str(i))
        limit = max_length_for_key(r.key)
        if new is None or limit is None:
            continue
        n = len(_render(new, r.imap))
        if n > limit:
            out.append({"p_index": i, "key": r.key, "length": n, "max_length": limit})
    return out
