"""key_files_po_parse_render: shared leaf helpers (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import os
import re

from app.services.keys import (
    CLDR_CATEGORIES,
    DEFAULT_NAMESPACE_LABEL,
    NESTED_SEP,
)

FORMATS = ("json", "po")


EMPTY_BASE, EMPTY_EMPTY, EMPTY_SKIP = "base", "empty", "skip"


EMPTY_MODES = (EMPTY_BASE, EMPTY_EMPTY, EMPTY_SKIP)


MODE_MERGE, MODE_CLEANUP = "merge", "cleanup"


MODES = (MODE_MERGE, MODE_CLEANUP)


SORT_AZ, SORT_FIRST_ADDED = "a_z", "first_added"


SORTS = (SORT_AZ, SORT_FIRST_ADDED)


PLURAL_OBJECT, PLURAL_I18NEXT = "object", "i18next_v4"


PLURAL_STYLES = (PLURAL_OBJECT, PLURAL_I18NEXT)


JSON_META_KEY = "$meta"


JSON_META_OWNER = "loc_studio"


PO_META_COMMENT = "# loc-studio-namespaces: qualified"


PO_LOCK_COMMENT_PREFIX = "loc-studio-lock"


_PO_LOCK_RE = re.compile(
    r"^"
    + re.escape(PO_LOCK_COMMENT_PREFIX)
    + r"(?:\[(?P<form>[a-z]+)\])?:\s*(?P<state>\S+)(?P<tr>\s+translate=no)?\s*$"
)


NS_MODE_QUALIFIED = "qualified"


SUB_KEPT_FOR_TRANSPORT = "kept_for_transport"


SUB_TM_LOCKED = "tm_locked"


_LOCK_STATE_VOCAB = {"ice": "exact-match", "100": "leveraged-tm", "fuzzy": "fuzzy-match"}


_LOCK_STATE_FROM_VOCAB = {v: k for k, v in _LOCK_STATE_VOCAB.items()}


# --- constants ---------------------------------------------------------------
#: `empty_as` (Lokalise's export_empty_as, minus `null`): what an untranslated value
#: becomes in the exported file.
#: Import modes. `merge` never deletes; `cleanup` soft-deletes (WITH tombstones) the
#: keys the file does not mention -- the destructive one, gated by an explicit flag.
#: Export ordering.
#: Plural rendering in JSON: a CLDR-named object, or i18next-v4 suffixed sibling keys.
# --- the namespace-qualification MARKER (re-review R2) -----------------------
# A qualified export writes `namespace::name` for every key; a scoped or unqualified
# one writes bare names. THE TWO FILES ARE INDISTINGUISHABLE BY INSPECTION -- an
# ordinary nested JSON file's names contain `::` too -- so `NS_FROM_KEY`, which reads
# the namespace off the front of a name, had no way to tell whether it was pointed at a
# file that meant it. Fed a bare export it silently FORKED the key space: exporting
# namespace `common` alone and re-importing produced a second copy of every key under a
# namespace named after its first path segment, with a clean report.
#
# So a qualified export SAYS SO, in the file, and NS_FROM_KEY refuses a file that does
# not. The marker costs nothing where it lands: a qualified export is a TRANSPORT file
# by construction (its names carry `ns::` prefixes, so no runtime can consume it as a
# build file anyway), while the namespace-scoped export -- the one that IS a build file
# -- stays bare and marker-free.
#: JSON: a top-level member. `$` cannot start a JSON identifier in any i18n convention
#: we target, and the nested `loc_studio` envelope means a foreign file that happens to
#: have a `$meta` member of its own is left completely alone.
#: .po: a line in the header COMMENT block, which Babel round trips verbatim and every
#: gettext tool ignores.
#: .po: the PER-FORM extracted-comment carrying a value's TM lock on a TRANSPORT file
#: (PROPOSAL-tm-matching §8, D-1). A DISTINCTIVE, namespaced prefix (MED-9) so a user's
#: own `#.` description can never be mis-parsed as a lock: `#. loc-studio-lock: <state>`
#: (non-plural) or `#. loc-studio-lock[<form>]: <state>` (a plural form), optionally
#: ` translate=no` for a hard lock. Emitted ONLY on transport exports; parse_po_keys
#: strips it back out into rec["lock"] AND validates the state against the known XLIFF
#: vocabulary, so a coincidental user comment that fails the grammar stays a description.
#: The exact grammar of a lock comment (MED-9), so nothing else in the `#.` channel is
#: parsed as a lock: `loc-studio-lock[form]: <state>[ translate=no]` (form optional).
#: The value both formats carry.
# --- what the export `report` channel can say (live-QA K2) -------------------
#: A tag-damaged value that SHIPPED ITS SOURCE instead. Deliberately the SAME kind
#: constant `app/services/export.py` uses for the document path, so
#: `export.substitution_detail` renders a key export and a document export identically
#: and one audit `detail` vocabulary covers both.
#: (imported lazily below as `export_svc.SUB_AUTOMATIC` -- no import cycle here.)
#: A tag-damaged value the TRANSPORT file kept VERBATIM. Not a substitution -- nothing
#: was replaced -- but it is still a fact the operator has to be told, because the file
#: they just downloaded contains internal mask notation.
#: A TM-LOCKED value the TRANSPORT file carries a lock representation for (PROPOSAL-tm-
#: matching §8, D-1 export). Reported so the lock is visible on every surface; the file
#: itself also carries it (JSON $meta.locks / a PO comment). BUILD deliverables never
#: carry it -- lock state is transport-only, like every other transport-only fact here.
# XLIFF-standard vocabulary for the lock STATE (settled export design, this fix round):
# the transport carries these names so a future NATIVE XLIFF exporter is a rename, not a
# redesign. lock_hard=True adds XLIFF's translate="no" semantics alongside the state.
# Batch 15 T1 (M3): the "fuzzy" band (a below-100 fuzzy fill's soft/policy lock) maps to
# XLIFF's own "fuzzy-match" state, so a fuzzy lock ROUND-TRIPS through the JSON/PO transport
# (without it _lock_transport emitted the non-XLIFF token "fuzzy" and _lock_from_transport
# then dropped the lock on re-import). lock_hard adds translate="no" alongside, as for 100.
def _lock_transport(seg) -> dict | None:
    """The transport lock representation for a segment, or None when unlocked. Values use
    XLIFF's own standard vocabulary (exact-match / leveraged-tm; translate=no for a hard
    lock) so the JSON/PO transport shapes and a future native XLIFF exporter agree."""
    band = getattr(seg, "lock_band", None)
    if not band:
        return None
    d = {"state": _LOCK_STATE_VOCAB.get(band, band)}
    if getattr(seg, "lock_hard", False):
        d["translate"] = "no"
    return d


SUB_EMPTY_AS_BASE = "empty_as_base"


CHAR_OVER_LIMIT = "char_limit"


FILTERED_OUT = "filtered_out"


#: A key a bridged resource export (Batch 9) had no place for in its template.
NOT_IN_TEMPLATE = "not_in_template"


NOTICE_NAME_CAP = 8


#: A WITHHELD value (empty, but flagged needs-review -- a quarantined refusal, an MT
#: output the gate refused to commit) whose slot `empty_as=base` filled with the SOURCE
#: text (fix-round K12). Filling an untouched-but-EMPTY row is the documented, silent
#: point of `empty_as=base`; filling a row whose status says "somebody looked at this
#: and withheld it" is the export quietly answering a question the product had refused
#: to answer, so it is counted and named like any other substitution.
#: K8 (spec §7): a value that SHIPPED but exceeds its key's character limit. NOT a
#: substitution and NOT a filter -- the value went out unchanged; this is the third,
#: distinct export outcome ("it shipped and it is too long"), so a release manager can
#: tell it apart from a value dropped by a status filter or replaced by source text. The
#: report entries carry `length`/`limit` for the --json payload; the notice/header name
#: the keys.
#: K9 (live-QA F3): a key the `status=` export filter DROPPED whole. The FOURTH export
#: outcome, and the last one that had no channel -- "substituted", "kept for transport"
#: and "over limit" all announced themselves while "filtered out" left only a smaller
#: (or empty) file behind. Report-only: the key is already omitted; this exists so the
#: omission is legible, and so a valid-but-empty file (a bogus filter value) can be told
#: apart from a genuinely empty project. Entries carry the `status` that dropped them.
#: How many key names a human-readable notice names before it says "and N more".
class _NamespaceFromKey:
    """Sentinel for `import_keys(namespace=...)`: read each key's namespace off the
    FRONT of its own name (`common::error.generic` -> namespace "common", name
    "error.generic"), the shape a NAMESPACE-QUALIFIED export writes.

    This is the other half of making export -> import a fixed point (review B2). It is
    accepted ONLY on a file that SAYS it is namespace-qualified (re-review R2): an
    ordinary nested-JSON file's names contain `::` too, and this sentinel would happily
    read the first path segment as a namespace. See the marker block above."""

    def __repr__(self):
        return "NS_FROM_KEY"


# --- JSON: flatten / unflatten ----------------------------------------------
def _is_plural_object(value) -> bool:
    """True for a `{"one": ..., "other": ...}` leaf: a non-empty mapping whose keys are
    ALL CLDR category names and which carries the mandatory `other`. Anything else --
    including a nested object that merely happens to contain an "other" child -- is
    ordinary nesting, so the check is deliberately strict in both directions."""
    if not isinstance(value, dict) or not value:
        return False
    if not all(k in CLDR_CATEGORIES for k in value):
        return False
    if "other" not in value:
        return False
    return all(isinstance(v, (str, int, float, bool)) for v in value.values())


# ASCII digits only (#508): `\d` and str.isdigit() disagree on characters such as the
# superscript two, so one side escaped a segment the other never un-escaped.
_FORCED_NUMERIC_SEGMENT = re.compile(r"^:[0-9]+$")


_UNREPRESENTABLE_SEGMENT = re.compile(r"^:+[0-9]+$")


#: A path segment of the form ":0" -- the ESCAPE that pins a numeric object key as an
#: object key rather than an array index (the `:::` form once the `::` separator is
#: joined in). It is deliberately NARROW (review B8): the old test was "starts with a
#: colon", so a user key genuinely named ":weird" was silently exported as "weird" --
#: an internal marker leaking into, and corrupting, user key space. A leading colon
#: followed by anything but digits now means nothing at all and round trips verbatim.
#: The one shape this escape genuinely CANNOT represent: a literal object key that is
#: itself ":0" / "::0" / ":::7". Encoding it would collide with the escape, so it is
#: refused loudly rather than mangled -- the module's rule everywhere else too.
def _is_forced_segment(seg: str) -> bool:
    return bool(_FORCED_NUMERIC_SEGMENT.match(seg))


def _scalar(value, note=None) -> str:
    """A leaf as text. Non-string scalars are coerced (a JSON `42` or `true` is a
    translatable string once it reaches a translator); None is not a value at all.

    The coercion is LOSSY and used to be documented only in this docstring: a `null`
    that becomes "" and a `42` that becomes "42" never come back as JSON scalars.
    `note` is the callback that surfaces each coercion in the import report so the
    fact rides alongside the data (review B16)."""
    if isinstance(value, str):
        return value
    if note is not None and value is not None:
        note(f"coerced {type(value).__name__} {value!r} to text")
    elif note is not None:
        note("coerced null to an empty string")
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _record_origin(rec) -> str:
    """How to DESCRIBE a record when two of them collide on one exported name. Names
    the real cause -- namespace and/or context -- rather than always blaming context
    (review B2): before namespace qualification, a clash between `common::error` and
    `admin::error` was reported as "no context and no context", which points the
    reader at a disambiguator that had nothing to do with it."""
    bits = []
    if rec.get("namespace"):
        bits.append("namespace {!r}".format(rec["namespace"]))
    bits.append("context {!r}".format(rec["context"]) if rec.get("context") else "no context")
    return " / ".join(bits)


def _note_merged_slot(notes, name, idx, cat, kept, dropped) -> None:
    """Report an msgstr index whose CLDR category an EARLIER index already claimed.

    Silence is only honest when nothing is lost, so an empty slot, or one carrying the
    same text as the kept one, says nothing -- otherwise a locale whose two gettext
    slots genuinely mean the same CLDR category (id, ms, my...) would put a note on
    every plural key in the file, and a report nobody can read is not a report."""
    if notes is None or not (dropped or "").strip() or dropped == kept:
        return
    notes.append(
        "%s: msgstr[%d] dropped -- this locale maps it to the CLDR category %r, which "  # noqa: UP031
        "an earlier slot already filled (gettext has more forms here than CLDR does)"
        % (name, idx, cat)
    )


_MSGSTR_SLOT = re.compile(r'^msgstr\[(\d+)\]\s+"(.*)"\s*$', re.M)


_MSGID_LINE = re.compile(r'^msgid\s+"(.*)"', re.M)


_ENTRY_START = re.compile(r"(#|msgctxt\b|msgid\s)")


def _po_entry_blocks(text: str) -> list[str]:
    """Split catalog text into one block per ENTRY.

    A blank line ends an entry, but gettext does not require one: a comment, `msgctxt` or
    `msgid` that follows a completed `msgstr` begins the next entry just as well (GitHub
    #507; Babel, which parses the entries themselves, already accepts this). Splitting on
    blank lines alone merged such entries into one block, so a dropped-slot note named the
    wrong entry. A `msgid_plural` or a continuation string never starts a block."""
    blocks: list[list[str]] = [[]]
    done = False  # the current block already carries a msgstr line
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            blocks.append([])
            done = False
            continue
        if done and _ENTRY_START.match(stripped):
            blocks.append([])
            done = False
        blocks[-1].append(line)
        if stripped.startswith("msgstr"):
            done = True
    return ["\n".join(b) for b in blocks if b]


#: One `msgstr[N] "..."` line, non-empty value.
def _note_dropped_msgstr_slots(raw: bytes, cats, notes) -> None:
    """Report msgstr INDICES the file's own header has no slot for (review B7).

    Read from the RAW TEXT rather than from the parsed catalog because Babel silently
    TRUNCATES an over-long plural entry to `num_plurals` before we ever see it (it logs
    a warning to stderr, which is not a report a caller can act on). A dropped index is
    a translation that will not survive the import, so it belongs in the import
    report's `notes` -- previously the whole thing was `errors: []`, i.e. a file that
    lost half its content read as a clean import."""
    if notes is None:
        return
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return
    for block in _po_entry_blocks(text):
        if "msgstr[" not in block or block.lstrip().startswith("#~"):
            continue
        over = sorted(
            {
                int(m.group(1))
                for m in _MSGSTR_SLOT.finditer(block)
                if int(m.group(1)) >= len(cats) and m.group(2).strip()
            }
        )
        if not over:
            continue
        found = _MSGID_LINE.search(block)
        notes.append(
            "%s: msgstr[%s] dropped -- this file's Plural-Forms header declares only "  # noqa: UP031
            "%d form(s)"
            % (
                found.group(1) if found else "(entry)",
                "], msgstr[".join(str(i) for i in over),
                len(cats),
            )
        )


def split_namespace(name: str) -> tuple[str, str]:
    """Split a NAMESPACE-QUALIFIED exported name back into (namespace, name).

    The inverse of `qualified_name`: `common::error::generic` -> ("common",
    "error::generic"), `__default__::hello` -> ("", "hello"). A name with no separator
    at all belongs to the default namespace. Only meaningful on a qualified export --
    see `NS_FROM_KEY`."""
    if NESTED_SEP not in name:
        return "", name
    head, rest = name.split(NESTED_SEP, 1)
    head = "" if head == DEFAULT_NAMESPACE_LABEL else head
    return head, rest


def qualified_name(namespace: str, name: str) -> str:
    """`namespace::name`, with the DEFAULT (empty) namespace spelled out as
    `__default__` so the prefix is always present and always splittable."""
    return (namespace or DEFAULT_NAMESPACE_LABEL) + NESTED_SEP + name


def format_for_filename(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    return "po" if ext in ("po", "pot") else ext


# --- what an export has to SAY about itself (live-QA K2) ---------------------
def _report_names(report, kind: str) -> list[str]:
    """The key names in `report` carrying `kind`, de-duplicated, first-seen order.

    De-duplicated because a PLURAL key contributes one entry per damaged FORM, and
    "cart::total, cart::total, cart::total" is not a more informative report than
    naming the key once."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in report or []:
        if not isinstance(entry, dict) or (entry.get("kind") or "") != kind:
            continue
        name = str(entry.get("index") or "")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out
