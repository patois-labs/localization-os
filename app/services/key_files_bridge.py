"""key_files: the keys bridge (file-format Batch 9).

Every document filter that exposes a key view -- a `records(data)` method beside its
`parse`/`build` -- can be imported into and exported from a KEY-BASED project. Nothing
here hard-codes a format: `bridge_formats()` asks the filter registry which filters have
`records()`, so a resource filter that merges later (Android, RESX, stringsdict, ARB, PHP,
TOML, ...) lights up in `keys import/export`, the API, MCP and the git connector the moment
it is registered. JSON and gettext .po keep their own native key paths and are never
bridged.

THE FILTER STAYS THE ONLY PARSER. Import runs the filter's `records()` for key names,
comments, contexts and plural categories, and its `parse()` for the VALUES (the unit text a
translator sees, so a `.properties` value whose MessageFormat apostrophes the filter undoes
is stored the way the document pipeline would show it). Export runs the filter's own
`build()` (or `build_localized()` for a target locale) over a TEMPLATE: the source file
itself. Comments, ordering, escapes, encodings, BOMs and line endings are the filter's
fidelity, not a second renderer's. A value that equals the template's unit text is not
handed to `build()` at all, so a source-locale export with nothing changed is the template
byte for byte (the round-trip identity the Batch 9 tests pin).

THE TEMPLATE. A resource file is not a list of keys; it is a file with keys in it. A
base-locale import of a bridged format stores the uploaded bytes as the template for
(project, namespace, format) in the upload blob store (encrypted at rest like every other
upload). An export in that format and namespace fills that template. The git connector
passes the repository's own source file instead, so it never depends on a stored copy. A
key that is not in the template cannot be placed in the file and is reported; an entry of
the template with no exported value keeps its template (source) text, because a template
build cannot drop a line.

THE RECORD SHAPES the filters hand back differ (a `KeyValueRecord` dataclass, Batch 2's
`ResourceRecord`, plain dicts). `_scan` reads all of them by field name:
  key / name                -> the key name (a string-array `index` appends `::N`)
  context                   -> the key's context (its disambiguator, like gettext msgctxt)
  comment / description     -> the key's description
  plural (str)              -> this record is ONE form of a plural key (Android `<item>`)
  plural_forms (dict)       -> this record is a whole plural key; `plural_units` maps each
                               category to its unit index (without it the entry is not
                               imported, and a note says why)
  unit_index / p_index      -> the `parse()` unit carrying the value: it MUST equal that
                               unit's `p_index` (what `build()` keys on); a record type with
                               neither field is 1:1 with the filter's units by position
  translatable / duplicate  -> honoured when present
A record with no unit (a placeholder-only value, `translatable="false"`) is not imported
and keeps its template text on export.

KEY COLLISIONS ARE REFUSED, NEVER MERGED. Two entries of one file with the same (name,
context) raise `KeyCollisionError`, and so does an import whose key already exists in the
namespace from a file of a DIFFERENT format (an Android `strings.xml` and an iOS
`Localizable.strings` imported into one namespace): two resource files are two key sets.

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

from app.services.key_files_po_parse_render import (
    FORMATS,
    KeyCollisionError,
    KeyFileError,
)
from app.services.key_files_po_parse_render_leaf import NOT_IN_TEMPLATE
from app.services.keys import CLDR_CATEGORIES, NESTED_SEP

log = logging.getLogger(__name__)

#: Formats with their own native key parser/renderer; never bridged.
NATIVE = frozenset({"json", "po", "pot"})


# --- discovery -------------------------------------------------------------------------
def _registry() -> dict:
    import app.filters  # noqa: F401  (importing the package registers every filter)
    from app.filters import base

    return base._FILTERS


def bridge_formats() -> tuple[str, ...]:
    """Every registered filter fmt with a `records()` key view, minus the native formats."""
    out = []
    for fmt, flt in _registry().items():
        if fmt in NATIVE:
            continue
        if callable(getattr(flt, "records", None)):
            out.append(fmt)
    return tuple(sorted(out))


def all_formats() -> tuple[str, ...]:
    """The formats `keys import/export` accept: the native two, then every bridged one."""
    return tuple(FORMATS) + bridge_formats()


def resolve(fmt: str) -> str | None:
    """The canonical bridged fmt for a fmt name or file extension (`yml` -> `yaml`,
    `resw` -> `resx`), or None when it is native or not bridged."""
    f = (fmt or "").strip().lower().lstrip(".")
    if not f or f in NATIVE:
        return None
    from app.filters import base

    _registry()
    canon = base._ALIASES.get(f, f)
    flt = base._FILTERS.get(canon)
    if canon in NATIVE or flt is None or not callable(getattr(flt, "records", None)):
        return None
    return canon


def is_bridged(fmt: str) -> bool:
    return resolve(fmt) is not None


def export_ext(fmt: str) -> str:
    """The file extension a downloaded export carries: a filter's `download_ext` when it
    declares one (an `android` filter writes `.xml`), else its fmt."""
    f = (fmt or "json").strip().lower().lstrip(".")
    if f.startswith("po"):
        return "po"
    canon = resolve(f)
    if canon is None:
        return "json"
    from app.filters import base

    return str(getattr(base._FILTERS[canon], "download_ext", "") or canon)


def export_media_type(fmt: str) -> str:
    f = (fmt or "json").strip().lower().lstrip(".")
    if f.startswith("po"):
        return "text/plain; charset=utf-8"
    if resolve(f):
        # The bytes are the template's encoding (UTF-16 .strings, Latin-1 .properties), so
        # no charset is claimed.
        return "application/octet-stream"
    return "application/json"


def _filter(fmt: str):
    from app.filters import base

    canon = resolve(fmt)
    if canon is None:
        raise KeyFileError(
            "unsupported key file format {!r} (supported: {}).".format(
                fmt, ", ".join(all_formats())
            )
        )
    return canon, base._FILTERS[canon]


# --- the shared walk -------------------------------------------------------------------
_MISSING = object()


def _field(rec, name: str, default=_MISSING):
    if isinstance(rec, dict):
        return rec.get(name, None if default is _MISSING else default)
    return getattr(rec, name, None if default is _MISSING else default)


def _has(rec, name: str) -> bool:
    return (name in rec) if isinstance(rec, dict) else hasattr(rec, name)


def _unit_of(rec, position: int):
    for name in ("unit_index", "p_index"):
        if _has(rec, name):
            v = _field(rec, name)
            return v if isinstance(v, int) else None
    return position


class _Entry:
    __slots__ = ("context", "description", "forms", "is_plural", "name")

    def __init__(self, name: str, context: str, description: str, is_plural: bool):
        self.name, self.context, self.description = name, context, description
        self.is_plural = is_plural
        #: category ("" for a plain value) -> (unit index, key text, unit text, literals)
        self.forms: dict[str, tuple] = {}


# --- placeholder tokens ----------------------------------------------------------------
# A filter's parse() turns placeholders into the pipeline's ⟦xN⟧ / ⟦gN⟧..⟦/gN⟧ tokens
# (TOML {{.Name}}, Apple %d). A KEY value must carry the real text (the keys grid, MT and
# a JSON/PO export of the same keys read it), so import puts each token's literal back,
# taken from the record's own raw value, and export turns those literals back into the
# template unit's tokens before build(). A unit whose raw value cannot be aligned with its
# tokens keeps the unit text (the old behaviour).
_TOKEN_RE = re.compile("⟦/?[a-z]?\\d+⟧")


def _raw(rec):
    for name in ("value", "text"):
        v = _field(rec, name)
        if isinstance(v, str):
            return v
    return None


def _form(unit: int, unit_text: str, raw) -> tuple:
    """(unit, key text, unit text, {literal: [tokens]}) for one value slot."""
    toks = _TOKEN_RE.findall(unit_text)
    if not toks or not isinstance(raw, str):
        return (unit, unit_text, unit_text, {})
    pieces = _TOKEN_RE.split(unit_text)
    pattern = "\\s*" + "(.*?)".join(re.escape(x) for x in pieces) + "\\s*"
    m = re.fullmatch(pattern, raw, re.S)
    if m is None:
        return (unit, unit_text, unit_text, {})
    shown, lits = [pieces[0]], {}
    for tok, lit, after in zip(toks, m.groups(), pieces[1:], strict=True):
        if not lit or _TOKEN_RE.search(lit):
            return (unit, unit_text, unit_text, {})
        lits.setdefault(lit, []).append(tok)
        shown += [lit, after]
    return (unit, "".join(shown), unit_text, lits)


def _retokenize(new: str, lits: dict) -> str:
    """A key value with real placeholders -> the template unit's tokens (each literal
    becomes its tokens in source order; extra copies stay literal text)."""
    if not lits:
        return new
    alt = re.compile("|".join(re.escape(x) for x in sorted(lits, key=len, reverse=True)))
    used: dict[str, int] = {}

    def sub(m: re.Match) -> str:
        lit = m.group(0)
        n = used.get(lit, 0)
        used[lit] = n + 1
        return lits[lit][n] if n < len(lits[lit]) else lit

    return alt.sub(sub, new)


def _scan(fmt: str, data: bytes, notes: list | None = None) -> list[_Entry]:
    """The file as key entries, in file order. Shared by import (values) and export
    (where each value goes), so the two can never disagree about a unit."""
    canon, flt = _filter(fmt)
    try:
        units = {u.p_index: u.text for u in flt.parse(bytes(data)).units}
        records = list(flt.records(bytes(data)) or [])
    except KeyFileError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise KeyFileError(f"this {canon} file cannot be read: {exc}") from exc
    entries: list[_Entry] = []
    by_ident: dict[tuple[str, str], _Entry] = {}
    skipped = 0
    unmapped = 0  # plural entries whose filter names no unit per form
    for pos, rec in enumerate(records):
        name = str(_field(rec, "key") or _field(rec, "name") or "")
        if not name:
            skipped += 1
            continue
        index = _field(rec, "index")
        if isinstance(index, int):
            name = f"{name}{NESTED_SEP}{index}"
        context = str(_field(rec, "context") or "")
        desc = str(_field(rec, "comment") or _field(rec, "description") or "")
        ident = (name, context)
        if _field(rec, "duplicate"):
            raise KeyCollisionError(
                name, "an earlier entry of this file", "a later entry with the same key"
            )
        if _field(rec, "translatable", True) is False:
            skipped += 1
            continue
        unit = _unit_of(rec, pos)
        category = _field(rec, "plural")
        forms = _field(rec, "plural_forms")
        if isinstance(category, str) and category:
            # One form of a plural key (Android `<plurals><item quantity>`): group by key.
            if category not in CLDR_CATEGORIES:
                raise KeyFileError(
                    f"the key {name!r} has the plural form {category!r}, which is not a CLDR "
                    f"category ({', '.join(CLDR_CATEGORIES)})."
                )
            ent = by_ident.get(ident)
            if ent is None:
                ent = _Entry(name, context, desc, True)
                by_ident[ident] = ent
                entries.append(ent)
            elif not ent.is_plural or category in ent.forms:
                raise KeyCollisionError(
                    name, "an earlier entry of this file", f"a later {category!r} form"
                )
            if unit in units:
                ent.forms[category] = _form(unit, units[unit], _raw(rec))
            continue
        if ident in by_ident:
            raise KeyCollisionError(
                name, "an earlier entry of this file", "a later entry with the same key"
            )
        if isinstance(forms, dict) and forms:
            ent = _Entry(name, context, desc, True)
            # Each form needs the unit that carries it (`plural_units`); `unit_index` alone
            # is not trusted for a plural record (filters disagree on which form it names),
            # and a form with no unit could be imported but never exported.
            plural_units = _field(rec, "plural_units")
            plural_units = plural_units if isinstance(plural_units, dict) else {}
            for cat in forms:
                if cat not in CLDR_CATEGORIES:
                    continue  # a device/width variant, not a plural form: not a key value
                u = plural_units.get(cat)
                if u in units:
                    ent.forms[cat] = _form(u, units[u], forms.get(cat))
            if not ent.forms:
                unmapped += 1
                continue
        else:
            if unit not in units:
                skipped += 1  # placeholder-only or not translatable: kept as is on export
                continue
            ent = _Entry(name, context, desc, False)
            ent.forms[""] = _form(unit, units[unit], _raw(rec))
        by_ident[ident] = ent
        entries.append(ent)
    if skipped and notes is not None:
        notes.append(
            f"{skipped} {'entry' if skipped == 1 else 'entries'} of this {canon} file with "
            "nothing to translate (placeholders only, or marked not translatable) "
            f"{'was' if skipped == 1 else 'were'} not imported as keys; an export keeps "
            "them exactly as the template has them."
        )
    if unmapped and notes is not None:
        notes.append(
            f"{unmapped} plural {'entry' if unmapped == 1 else 'entries'} of this {canon} file "
            f"{'was' if unmapped == 1 else 'were'} not imported: the {canon} filter does not say "
            "which text carries each plural form (its records() gives no plural_units), so the "
            "forms could not be written back."
        )
    return entries


# --- import ----------------------------------------------------------------------------
def parse_resource_keys(data, fmt: str, *, notes: list | None = None) -> list[dict]:
    """A bridged resource file as key records (the `parse_keys` record shape)."""
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    out: list[dict] = []
    for ent in _scan(fmt, bytes(raw), notes):
        rec = {"name": ent.name, "context": ent.context, "description": ent.description}
        if ent.is_plural:
            rec["is_plural"] = True
            rec["plural_values"] = {c: f[1] for c, f in ent.forms.items()}
        else:
            rec["is_plural"] = False
            rec["value"] = ent.forms[""][1]
        out.append(rec)
    return out


def check_cross_format(
    session, project_id: int, namespace: str, fmt: str, filename: str, records: list[dict]
) -> None:
    """Refuse an import whose keys already exist in `namespace` from a file of another
    format. Called for a bridged format before anything is written."""
    from app.services import keys as keys_svc
    from app.services.key_files_po_parse_render import format_for_filename

    canon = resolve(fmt) or fmt
    wanted = {(r["name"], r.get("context") or "") for r in records}
    if not wanted:
        return
    for key in keys_svc.iter_keys(session, project_id, namespace=namespace):
        ident = (key.name, key.context or "")
        if ident not in wanted or not (key.source_file or "").strip():
            continue
        prior = format_for_filename(key.source_file)
        prior = resolve(prior) or prior
        if prior and prior != canon:
            raise KeyCollisionError(
                key.name,
                f"the {prior} file {key.source_file!r} already defines it in namespace "
                f"{namespace or '(default)'!r}",
                f"this {canon} file {os.path.basename(filename or '')!r} defines it again",
            )


# --- the template store ----------------------------------------------------------------
def template_key(project, namespace: str, fmt: str) -> str:
    """The blob key of a stored template. The project's creation time is part of the
    digest: SQLite reuses rowids, so a project that takes a deleted project's id must
    never find that project's source file (a cross-tenant leak if a delete ever left
    one behind)."""
    canon = resolve(fmt) or fmt
    born = getattr(project, "created_at", None)
    salt = born.replace(tzinfo=None).isoformat(timespec="seconds") if born else ""
    digest = hashlib.sha256(f"{salt}\x00{namespace}\x00{canon}".encode()).hexdigest()[:24]
    return f"keytpl_{int(project.id)}_{digest}.tpl"


def save_template(project, namespace: str, fmt: str, data: bytes) -> None:
    from app.ports import blobstore

    blobstore.get().put(template_key(project, namespace, fmt), bytes(data))


def load_template(project, namespace: str, fmt: str) -> bytes | None:
    from app.ports import blobstore

    try:
        return blobstore.get().get(template_key(project, namespace, fmt))
    except (OSError, ValueError):
        return None


def template_blob_keys(session, project) -> list[str]:
    """Every blob key a template of `project` can have: each namespace its keys use
    (live or soft-deleted) times each bridged format. Project delete removes these after
    its commit (the blob store has no listing, so the candidates are computed)."""
    from sqlmodel import select

    from app.models import StringKey

    namespaces = set(
        session.exec(
            select(StringKey.namespace).where(StringKey.project_id == project.id).distinct()
        ).all()
    )
    return [
        template_key(project, ns or "", fmt)
        for ns in sorted(n or "" for n in namespaces)
        for fmt in bridge_formats()
    ]


# --- export ----------------------------------------------------------------------------
def render_resource_keys(
    records: list[dict],
    template: bytes,
    fmt: str,
    *,
    locale: str = "",
    is_base: bool = False,
    report: list | None = None,
) -> bytes:
    """Fill `template` (a file of format `fmt`) with the exported key records.

    A value equal to the template's own unit text is left out of the build, so an
    unchanged source-locale export is the template byte for byte."""
    canon, flt = _filter(fmt)
    entries = _scan(canon, template)
    by_ident = {(r["name"], r.get("context") or ""): r for r in records}
    placed: set = set()
    missed: list[tuple[str, str]] = []  # (key name, plural category) with no slot
    texts: dict[str, str] = {}
    for ent in entries:
        rec = by_ident.get((ent.name, ent.context))
        if rec is None or bool(rec.get("is_plural")) != ent.is_plural:
            continue  # absent, or plural vs plain disagrees: reported as not placed below
        placed.add((ent.name, ent.context))
        if ent.is_plural:
            values = rec.get("plural_values") or {}
            for cat, new in values.items():
                unit, text, unit_text, lits = ent.forms.get(cat, (None, "", "", {}))
                if unit is None:
                    if (new or "").strip():
                        missed.append((rec["name"], cat))  # e.g. `few` in a one/other file
                    continue
                if new is not None and new not in (text, unit_text):
                    texts[str(unit)] = _retokenize(new, lits)
        else:
            new = rec.get("value")
            unit, text, unit_text, lits = ent.forms[""]
            if unit is None or new is None or new in (text, unit_text):
                continue
            texts[str(unit)] = _retokenize(new, lits)
    if report is not None:
        for rec in records:
            if (rec["name"], rec.get("context") or "") not in placed:
                missed.append((rec["name"], ""))
        for name, cat in missed:
            report.append(
                {
                    "unit": "key",
                    "index": name,
                    "kind": NOT_IN_TEMPLATE,
                    "locale": locale,
                    "plural_category": cat,
                }
            )
    if not texts and is_base:
        return bytes(template)
    try:
        if is_base:
            return flt.build(bytes(template), texts)
        return flt.build_localized(bytes(template), texts, target_locale=locale or None)
    except (ValueError, UnicodeError) as exc:
        raise KeyFileError(f"the {canon} file could not be written: {exc}") from exc
