"""Sanity write strategies (both localization models) and the shapes they share.

Documented in: docs/architecture/connectors.md

Sanity has NO native localization: the project picks a plugin, and the two common plugins store a
translation in two different places. This connector supports both, selected per connection by the
`i18n_model` setting, because a customer's choice of plugin is not ours to make.

FIELD model (T1) - sanity-plugin-internationalized-array. One document; each translatable field is
an array of language items:
    "title": [{"_key": "k1", "_type": "internationalizedArrayStringValue", "language": "en",
               "value": "Hello"}, ...]
(v5 and later keep the language in a `language` field; v4 and earlier kept it in `_key`, so both
are read, and a new item copies the style of the existing ones.) Localizing language X = adding X's
item to each translated field of that ONE document. SanityFieldStrategy writes only the delta, as
Sanity patch operations (`set` on an existing item by `_key`, `insert` after the last item for a
new one), guarded by `ifRevisionID`. It never sends the whole document, so other languages and
every untouched field keep their stored values by construction.

DOCUMENT model (T3) - @sanity/document-internationalization. One document per language, each with a
`language` field, linked to its siblings by a `translation.metadata` document. SanityDocStrategy is
the stock DocPerLocaleStrategy (reserve-then-fill, id-map reconciliation) with two Sanity-specific
rules. (1) The target id is DETERMINISTIC (a hash of source id and language), so a create is
`createIfNotExists` and a crash between create and reserve cannot mint a second document. (2) Rule 6
(a translation this connection did not create) is stateless: the metadata document is consulted on
every run before any machine translation is spent, and a translation the id map does not own is
left untouched and reported; no needs_link row is persisted, so deleting the foreign translation in
the Studio is enough for the next run to proceed.

BOTH models read and write the DRAFT (`drafts.<id>`), creating it from the published document first
when only that exists. Neither ever publishes: publishing is the editor's explicit action. When the
translation is already published, the new text is a draft overlay (what the Studio shows editors);
the live version is unchanged and the pair is reported under `live_kept` in the run result. The one
non-draft write is the document model's `translation.metadata` link, which the plugin itself keeps
as a live-edit document; the connector only ever INSERTS a reference there (no text, no replace).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
from dataclasses import dataclass

from app.services.connectors import base
from app.services.connectors.strategy import (
    ABSENT,
    DocPerLocaleStrategy,
    LocaleMappingStrategy,
    WriteResult,
    _record_write_lock_for,
)

log = logging.getLogger("loc_tms.connectors")

API_VERSION = "v2025-02-19"

HELD_BACK_PREFIX = "held-back:"
HELD_BACK = (
    HELD_BACK_PREFIX + " every translatable field is awaiting review "
    "(NEEDS_REVIEW); nothing written, existing translation (if any) kept"
)
EXISTING_PREFIX = "existing-translation:"

# Reserved payload key carrying the source document id to the document strategy's fill step
# (fill_target receives only the target id). Sanity field names cannot start with an underscore,
# so it can never collide with a real field.
SOURCE_KEY = "_source"

MAX_RETRIES = 4
MAX_CONFLICT_WAIT = 30.0

KEY_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
SYSTEM_KEYS = frozenset({"_id", "_rev", "_createdAt", "_updatedAt"})


# ------------------------------------------------------------------ shared shapes
def item_lang(item) -> str | None:
    """The language of an internationalized-array item: the `language` field (plugin v5+), else
    the `_key` (v4 and earlier)."""
    if not isinstance(item, dict):
        return None
    lang = item.get("language")
    if isinstance(lang, str) and lang:
        return lang
    key = item.get("_key")
    return key if isinstance(key, str) and key else None


def is_i18n_array(value) -> bool:
    """True for an internationalized-array field value: a non-empty list of language items, each
    a typed object with a `value` slot. The `_type` prefix is the plugin's own naming."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(i, dict)
            and "value" in i
            and str(i.get("_type", "")).startswith("internationalizedArray")
            and item_lang(i) is not None
            for i in value
        )
    )


def find_item(arr, lang: str):
    """The item of `arr` whose language is exactly `lang`, or None."""
    if not isinstance(arr, list):
        return None
    for i in arr:
        if item_lang(i) == lang:
            return i
    return None


def published_id(doc_id: str) -> str:
    return doc_id[len("drafts.") :] if doc_id.startswith("drafts.") else doc_id


def draft_id(doc_id: str) -> str:
    return "drafts." + published_id(doc_id)


def target_doc_id(source_id: str, lang: str) -> str:
    """The deterministic PUBLISHED id of the translation document for (source, language). No
    dots: a dotted id is a private document path in Sanity."""
    h = hashlib.sha256(f"{published_id(source_id)}|{lang}".encode()).hexdigest()[:24]
    return f"l10n-{h}"


def metadata_doc_id(source_id: str) -> str:
    return "l10n-meta-" + hashlib.sha256(published_id(source_id).encode()).hexdigest()[:24]


def copy_content(doc: dict) -> dict:
    """A document's content without system fields (`_id`, `_rev`, `_createdAt`, `_updatedAt`)."""
    return {k: copy.deepcopy(v) for k, v in doc.items() if k not in SYSTEM_KEYS}


@dataclass
class Outcome:
    """Result of one mutate call: ok (2xx), conflict (revision mismatch) or a terminal failure."""

    ok: bool
    conflict: bool = False
    message: str = ""
    status: int | None = None


def _wait_conflict(attempt: int, waited: float) -> float:
    """Bounded backoff between conflict retries; returns the new cumulative wait."""
    if attempt >= MAX_RETRIES - 1 or waited >= MAX_CONFLICT_WAIT:
        return waited
    wait = min(base.BACKOFF_BASE * (2**attempt), base.MAX_SLEEP, MAX_CONFLICT_WAIT - waited)
    base._sleep(wait)
    return waited + wait


# ------------------------------------------------------------------ FIELD model (T1)
def plan_field_write(doc: dict, lang: str, fields: dict, *, hint: dict | None = None):
    """Pure planner: (ops, dropped, unchanged) for writing `fields` (field name -> translated
    value) as language `lang` into `doc`'s internationalized arrays.

    ops are patch operations ({"set": {...}} or {"insert": {...}}); dropped is {field: reason}
    for a field that cannot be written safely; unchanged counts fields already holding the value.
    Remote-supplied `_key`s are validated before they enter a path expression (path injection)."""
    ops_set: dict = {}
    ops_ins: list = []
    dropped: dict = {}
    unchanged = 0
    for f, v in fields.items():
        arr = doc.get(f)
        if not FIELD_RE.fullmatch(f) or not is_i18n_array(arr):
            dropped[f] = "not an internationalized array field on this document"
            continue
        item = find_item(arr, lang)
        if item is not None:
            if item.get("value") == v:
                unchanged += 1
                continue
            k = item.get("_key")
            if not isinstance(k, str) or not KEY_RE.fullmatch(k):
                dropped[f] = "the existing language item has an unsafe _key"
                continue
            ops_set[f'{f}[_key=="{k}"].value'] = v
            continue
        tmpl = find_item(arr, (hint or {}).get("source", "")) or arr[0]
        new = {"_type": tmpl["_type"], "value": v}
        taken = {i.get("_key") for i in arr if isinstance(i, dict)}
        if "language" in tmpl:
            new["language"] = lang
            key = hashlib.sha256(f"{f}|{lang}".encode()).hexdigest()[:12]
            while key in taken:
                key += "x"
            new["_key"] = key
        else:  # plugin v4 and earlier: the language IS the _key
            if not KEY_RE.fullmatch(lang) or lang in taken:
                dropped[f] = "the language id cannot be used as an item _key"
                continue
            new["_key"] = lang
        ops_ins.append({"insert": {"after": f"{f}[-1]", "items": [new]}})
    ops: list = []
    if ops_set:
        ops.append({"set": ops_set})
    ops.extend(ops_ins)
    return ops, dropped, unchanged


class SanityFieldStrategy(LocaleMappingStrategy):
    """T1 write-back for internationalized-array fields. The connector supplies (all called
    ACROSS the strategy/connector boundary):

        connection_id                       keys the shared record lock
        resolve_locale(locale) -> exact id  (lower-cased flow locale -> the Studio's language id)
        source_language() -> id
        snapshot(id) -> {"doc", "has_draft", "draft_rev", "type"} | ABSENT   (effective document)
        translated_field_ids(doc) -> set    (the fields THIS connection translates)
        ensure_draft(id)                    (createIfNotExists drafts.<id> from the published doc)
        mutate(mutations) -> Outcome
    """

    topology = "T1"

    def exists_target(self, client, record_id: str, target_locale: str) -> bool:
        snap = client.snapshot(record_id)
        if snap is ABSENT:
            return False
        lang = client.resolve_locale(target_locale)
        doc = snap["doc"]
        return any(
            find_item(doc.get(f), lang) is not None for f in client.translated_field_ids(doc)
        )

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        # `draft` is accepted for the framework signature and ignored: this connector never
        # publishes, so every write is a draft write.
        fields = {k: v for k, v in (fields or {}).items() if k != SOURCE_KEY}
        if not fields:
            return WriteResult(False, False, message=HELD_BACK)
        lang = client.resolve_locale(target_locale)
        if lang == client.source_language():
            return WriteResult(True, False, message="skipped: target equals the source language")
        with _record_write_lock_for(getattr(client, "connection_id", None), record_id):
            last = ""
            waited = 0.0
            for attempt in range(MAX_RETRIES):
                snap = client.snapshot(record_id)
                if snap is ABSENT:
                    return WriteResult(False, False, message=f"document {record_id} not found")
                if not snap["has_draft"]:
                    client.ensure_draft(record_id)
                    snap = client.snapshot(record_id)
                    if snap is ABSENT or not snap["has_draft"]:
                        return WriteResult(False, False, message="could not create the draft")
                doc = snap["doc"]
                scope = client.translated_field_ids(doc) | set(fields)
                existed = any(find_item(doc.get(f), lang) is not None for f in scope)
                ops, dropped, unchanged = plan_field_write(
                    doc, lang, fields, hint={"source": client.source_language()}
                )
                if dropped:
                    # Fail closed: writing the other fields and recording the content hash would
                    # hide the dropped one for good (a silent partial write).
                    why = "; ".join(f"{k} ({v})" for k, v in sorted(dropped.items()))
                    return WriteResult(False, False, message="cannot write safely - " + why)
                if not ops and not unchanged:
                    why = "; ".join(f"{k} ({v})" for k, v in sorted(dropped.items()))
                    return WriteResult(False, not existed, message="no writable fields - " + why)
                if not ops:
                    return WriteResult(True, not existed, target_id=record_id, message="unchanged")
                muts = []
                for n, op in enumerate(ops):
                    patch = {"id": snap["draft_id"], **op}
                    if n == 0:
                        patch["ifRevisionID"] = snap["draft_rev"]
                    muts.append({"patch": patch})
                out = client.mutate(muts)
                if out.ok:
                    pub = snap.get("published")
                    if pub and any(find_item(pub.get(f), lang) is not None for f in scope):
                        # The language is already LIVE: the new text sits on the draft only and
                        # the published item is unchanged until an editor publishes (reported).
                        note = getattr(client, "note_live_kept", None)
                        if callable(note):
                            note(record_id, target_locale)
                    return WriteResult(
                        True,
                        not existed,
                        target_id=record_id,
                        message="created" if not existed else "updated",
                    )
                if out.conflict:
                    last = out.message
                    waited = _wait_conflict(attempt, waited)
                    continue  # refetch the fresh revision and re-plan the delta
                return WriteResult(False, not existed, message=out.message)
            return WriteResult(
                False,
                False,
                message=f"revision conflict after {MAX_RETRIES} attempts (concurrent edit); {last}",
            )


# ------------------------------------------------------------------ DOCUMENT model (T3)
class SanityDocStrategy(DocPerLocaleStrategy):
    """T3 write-back for document-internationalization. See the module docstring for the two
    Sanity-specific rules layered on the stock reserve-then-fill reconciliation."""

    def write_target(
        self, client, record_id: str, target_locale: str, fields: dict, *, draft: bool = True
    ) -> WriteResult:
        real = {k: v for k, v in (fields or {}).items() if k != SOURCE_KEY}
        if not real:
            return WriteResult(False, False, message=HELD_BACK)
        # Rule 6, stateless: a translation Sanity already links for this (source, language) that
        # this connection does not own is never touched (it may be a human's work).
        foreign = client.foreign_translation(record_id, target_locale)
        if foreign:
            return WriteResult(
                False,
                False,
                target_id=foreign,
                message=f"{EXISTING_PREFIX} {record_id} already has a {target_locale} translation "
                f"in Sanity ({foreign}) that this connection did not create; left untouched",
            )
        return super().write_target(client, record_id, target_locale, fields, draft=draft)
