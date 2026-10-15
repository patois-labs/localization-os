"""Shared tabular helpers (Batch 6): the column-mapping spec, header detection and
the locale match that the CSV/TSV document filter, the XLSX filter's column mode and
the spreadsheet glossary/TM importers all use. ONE definition, so a mapping means the
same thing on every surface (web upload field, CLI `--columns`, /api `columns`).

Column-mapping spec (a short string the user types; comma-separated `role=column`):

    source=English, target.de=German, target.fr=fr, key=id, context=notes, maxlen=limit

* `source=COL`          the column holding the source text (required unless detected).
* `target=COL`          one column every target language writes into.
* `target.<loc>=COL`    the column for one target language (multilingual sheets).
* `key=COL`, `context=COL` (repeatable), `maxlen=COL`: shown to the translator as the
  segment note; never translated, never changed.
* `header=yes|no|auto`  whether row 1 is a header (default auto).
* `delimiter=comma|semicolon|tab|pipe` (CSV/TSV only; default detected).

COL is a header name (case-insensitive) or a 1-based column number. Every column the
spec does not name is left exactly as it was, and so is every row: a CSV export
changes only the bytes of the cells it writes.
"""

from __future__ import annotations

import re

from app.filters.base import InputLimitError

# A real spreadsheet has tens of columns; this bounds the per-row work on a hostile file.
MAX_COLUMNS = 1000
SPEC_MAX_CHARS = 2000


class ColumnMappingError(InputLimitError):
    """The column mapping does not fit the file (an unknown column, no source column, a
    malformed spec). Subclasses InputLimitError so every surface already shows the
    message; the API answers 422 for it. The message is plain and safe to show."""


_ROLE_NAMES = {
    "source": {
        "source",
        "source text",
        "source_text",
        "source string",
        "text",
        "string",
        "original",
        "default content",
        "source language",
    },
    "target": {
        "target",
        "target text",
        "target_text",
        "translation",
        "translated",
        "translated content",
        "target language",
    },
    "key": {"key", "id", "string id", "string_id", "identifier", "msgid", "identification"},
    "context": {"context", "notes", "note", "comment", "comments", "description", "field"},
    "maxlen": {"max length", "max_length", "maxlength", "maxlen", "char limit", "length limit"},
}

_DELIMS = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|", ",": ",", ";": ";", "|": "|"}


def _norm(name: str) -> str:
    return " ".join(str(name or "").strip().lower().replace("﻿", "").split())


def norm_locale(code: str) -> str:
    return _norm(code).replace("_", "-")


def parse_spec(spec: str | None) -> dict:
    """Parse a column-mapping spec into a plain dict (stored on the skeleton so export
    replays exactly what ingest used). Raises ColumnMappingError on a malformed spec."""
    out: dict = {
        "header": "auto",
        "source": None,
        "target": None,
        "targets": {},
        "key": None,
        "context": [],
        "maxlen": None,
        "delimiter": None,
    }
    spec = (spec or "").strip()
    if not spec:
        return out
    if len(spec) > SPEC_MAX_CHARS:
        raise ColumnMappingError(f"The column mapping is too long (limit {SPEC_MAX_CHARS}).")
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ColumnMappingError(
                f"Column mapping entry '{part[:60]}' needs the form role=column "
                "(for example source=English)."
            )
        role, col = (p.strip() for p in part.split("=", 1))
        role_l = role.lower()
        if not col:
            raise ColumnMappingError(f"Column mapping entry '{role[:40]}' names no column.")
        if role_l == "header":
            if col.lower() not in ("yes", "no", "auto", "true", "false"):
                raise ColumnMappingError("header= takes yes, no or auto.")
            out["header"] = {"true": "yes", "false": "no"}.get(col.lower(), col.lower())
        elif role_l == "delimiter":
            d = _DELIMS.get(col.lower())
            if d is None:
                raise ColumnMappingError("delimiter= takes comma, semicolon, tab or pipe.")
            out["delimiter"] = d
        elif role_l in ("source", "target", "key", "maxlen"):
            out[role_l] = col
        elif role_l == "context":
            out["context"].append(col)
        elif role_l.startswith(("target.", "target:")):
            loc = norm_locale(role[7:])
            if not loc:
                raise ColumnMappingError("target.<locale>= needs a locale, e.g. target.de=German.")
            out["targets"][loc] = col
        else:
            raise ColumnMappingError(
                f"Unknown column role '{role[:40]}'. Use source, target, target.<locale>, "
                "key, context, maxlen, header or delimiter."
            )
    return out


def has_mapping(spec: dict | None) -> bool:
    s = spec or {}
    return bool(s.get("source") or s.get("target") or s.get("targets"))


def _role_of(name: str) -> str | None:
    n = _norm(name)
    for role, names in _ROLE_NAMES.items():
        if n in names:
            return role
    return None


def locale_match(name: str, locales: list[str]) -> str | None:
    """The project locale a header names (`de`, `de-DE`, `de_de`), else None. An exact
    match wins; a bare language (`de`) matches a regional locale only when it is the
    only one with that language."""
    n = norm_locale(name)
    if not n or len(n) > 20:
        return None
    by_norm = {norm_locale(x): x for x in locales if x}
    if n in by_norm:
        return by_norm[n]
    lang = n.split("-")[0]
    hits = [x for k, x in by_norm.items() if k.split("-")[0] == lang and ("-" not in n)]
    return hits[0] if len(hits) == 1 else None


def looks_like_header(first_row: list[str], source_locale: str, targets: list[str]) -> bool:
    """Auto header detection: row 1 is a header when any cell names a role
    (source/target/key/context/max length) or a project locale."""
    locs = [x for x in (source_locale, *targets) if x]
    return any(_role_of(cell) or locale_match(cell, locs) for cell in first_row)


def _col_index(ref: str, header: list[str] | None, width: int, *, new_ok: bool = False) -> int:
    """`new_ok` (target columns only): a column number past the last column is allowed,
    so `target=3` on a two-column file appends the translation as a new column."""
    ref = str(ref).strip()
    if ref.isdigit():
        i = int(ref) - 1
        if i < 0 or i >= MAX_COLUMNS or (i >= max(width, 1) and not new_ok):
            raise ColumnMappingError(f"Column {ref} does not exist (the file has {width}).")
        return i
    if header is None:
        raise ColumnMappingError(
            f"Column '{ref[:60]}' is named but the file has no header row; use a column number."
        )
    want = _norm(ref)
    for i, h in enumerate(header):
        if _norm(h) == want:
            return i
    found = ", ".join(h for h in header if h)[:200] or "(blank header)"
    raise ColumnMappingError(f"No column named '{ref[:60]}'. Found: {found}.")


def resolve(
    spec: dict,
    first_row: list[str],
    width: int,
    source_locale: str = "",
    target_locales: list[str] | None = None,
    *,
    auto: bool = True,
) -> dict:
    """Turn a spec (possibly empty) plus the file's first row into a concrete plan:

        {"header": bool, "source": int|None, "targets": {locale|"*": int},
         "notes": [(label, int)], "inplace": bool}

    `inplace` (no source column found and none named): every text cell is translated
    where it stands, the XLSX default. `auto=False` (XLSX without a spec) never guesses."""
    targets = list(target_locales or [])
    hdr_mode = spec.get("header", "auto")
    if hdr_mode == "yes":
        has_header = True
    elif hdr_mode == "no":
        has_header = False
    else:
        refs = [spec.get("source"), spec.get("target"), spec.get("key"), spec.get("maxlen")]
        refs += list((spec.get("targets") or {}).values()) + list(spec.get("context") or [])
        # A column named (not numbered) in the mapping implies row 1 is the header.
        has_header = any(r and not str(r).strip().isdigit() for r in refs) or looks_like_header(
            first_row, source_locale, targets
        )
    header = list(first_row) if has_header else None
    plan: dict = {
        "header": has_header,
        "source": None,
        "targets": {},
        "notes": [],
        "inplace": False,
    }
    named = has_mapping(spec) or spec.get("key") or spec.get("context") or spec.get("maxlen")
    if named:
        if spec.get("source"):
            plan["source"] = _col_index(spec["source"], header, width)
        if spec.get("target"):
            plan["targets"]["*"] = _col_index(spec["target"], header, width, new_ok=True)
        for loc, ref in (spec.get("targets") or {}).items():
            match = locale_match(loc, targets) or loc
            plan["targets"][match] = _col_index(ref, header, width, new_ok=True)
        if spec.get("key"):
            plan["notes"].append(("Key", _col_index(spec["key"], header, width)))
        for ref in spec.get("context") or []:
            label = header[_col_index(ref, header, width)] if header else "Context"
            plan["notes"].append((label or "Context", _col_index(ref, header, width)))
        if spec.get("maxlen"):
            plan["notes"].append(("Max length", _col_index(spec["maxlen"], header, width)))
        if plan["source"] is None:
            if plan["targets"]:
                raise ColumnMappingError("The column mapping names targets but no source=.")
            plan["inplace"] = True
    elif auto and has_header:
        for i, h in enumerate(first_row[:MAX_COLUMNS]):
            role = _role_of(h)
            if role == "source" and plan["source"] is None:
                plan["source"] = i
            elif role == "target" and "*" not in plan["targets"]:
                plan["targets"]["*"] = i
            elif role == "key":
                plan["notes"].append(("Key", i))
            elif role == "context":
                plan["notes"].append((str(h).strip() or "Context", i))
            elif role == "maxlen":
                plan["notes"].append(("Max length", i))
            elif source_locale and locale_match(h, [source_locale]) and plan["source"] is None:
                plan["source"] = i
            else:
                loc = locale_match(h, targets)
                if loc and loc not in plan["targets"]:
                    plan["targets"][loc] = i
        if plan["source"] is None:
            plan["targets"] = {}
            plan["inplace"] = True
    else:
        plan["inplace"] = True
    if plan["source"] is not None and plan["source"] in plan["targets"].values():
        raise ColumnMappingError("The source column cannot also be a target column.")
    return plan


def target_col(plan: dict, locale: str | None) -> int | None:
    """The column a build for `locale` writes into, or None (in-place / no column)."""
    t = plan.get("targets") or {}
    if locale:
        for k, v in t.items():
            if k != "*" and norm_locale(k) == norm_locale(locale):
                return v
        hit = locale_match(locale, [k for k in t if k != "*"])
        if hit:
            return t[hit]
    return t.get("*")


def unit_note(plan: dict, row: list[str]) -> str:
    parts = []
    for label, i in plan.get("notes") or []:
        v = row[i].strip() if i < len(row) else ""
        if v:
            parts.append(f"{label}: {v[:500]}")
    return " | ".join(parts)


_FORMULA_RE = re.compile(r"^\s*=")


def is_translatable(value: str) -> bool:
    """A source cell worth a segment: has a letter or digit and is not a formula
    (`=SUM(...)`, Excel's `="0012"` text-number trick)."""
    if not isinstance(value, str) or _FORMULA_RE.match(value):
        return False
    return any(c.isalpha() for c in value)
