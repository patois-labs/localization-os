"""key_files: json parse / render

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import io
import json

from sqlmodel import Session

from app import branding
from app.models import SegStatus
from app.services import keys as keys_svc
from app.services.key_files_po_parse_render import (
    _UNREPRESENTABLE_SEGMENT,
    JSON_META_KEY,
    JSON_META_OWNER,
    NS_MODE_QUALIFIED,
    PLURAL_I18NEXT,
    PLURAL_OBJECT,
    PO_LOCK_COMMENT_PREFIX,
    PO_META_COMMENT,
    KeyCollisionError,
    KeyFileError,
    _is_forced_segment,
    _is_plural_object,
    _key_label,
    _lock_from_transport,
    _note_skipped,
    _record_origin,
    _restates_source,
    _scalar,
    _take_json_meta,
    check_key_count,
    check_key_records,
    parse_po_keys,
)
from app.services.keys import (
    CLDR_CATEGORIES,
    NESTED_SEP,
)


def _is_ascii_digits(seg: str) -> bool:
    """True for a segment of ASCII digits only. `str.isdigit()` is also true for
    superscripts and circled digits, which the `:`-escape regex never matched (#508)."""
    return seg.isascii() and seg.isdigit()


def flatten_json(obj, *, sep: str = NESTED_SEP, notes: list | None = None) -> list[dict]:
    """Flatten a parsed JSON body into ordered key records.

    Each record is `{"name", "value" | "plural_values", "is_plural"}` in DOCUMENT
    ORDER (which is what `first_added` export ordering preserves). Arrays flatten to
    `path::0`, `path::1`; a numeric-looking OBJECT key is escaped as `path:::0` so the
    unflatten side rebuilds an object rather than an array.

    `notes` collects the facts a caller should be told but that are not errors: every
    lossy scalar coercion and every empty container that simply vanishes (an empty
    object or array holds no translatable string, so it cannot survive a round trip
    through a key list -- review B16)."""
    out: list[dict] = []
    seen: dict[str, str] = {}

    def _note(msg: str) -> None:
        if notes is not None:
            notes.append(msg)

    def _emit(path: str, origin: str, value=None, plural=None) -> None:
        if path in seen:
            raise KeyCollisionError(path, repr(seen[path]), repr(origin))
        seen[path] = origin
        rec = {"name": path, "is_plural": plural is not None}
        if plural is not None:
            rec["plural_values"] = {
                k: _scalar(v, lambda m, p=path: _note(f"{p}: {m}")) for k, v in plural.items()
            }
        else:
            rec["value"] = _scalar(value, lambda m, p=path: _note(f"{p}: {m}"))
        out.append(rec)
        check_key_count(len(out))  # #576: stop at the cap, not at the end of the file

    def _walk(node, path: str, origin: str) -> None:
        if _is_plural_object(node):
            _emit(path, origin, plural=node)
            return
        if isinstance(node, dict):
            if not node and path:
                _note(f"{path}: dropped an empty object (it holds no translatable string)")
            for k, v in node.items():
                seg = str(k)
                if _UNREPRESENTABLE_SEGMENT.match(seg):
                    raise KeyFileError(
                        f"the object key {seg!r} cannot be represented: a leading ':' before "
                        "digits is this format's escape for a numeric object key. "
                        "Rename it."
                    )
                # A numeric object key would re-import as an array index; ":" prefixes
                # it (yielding the ":::" triple-colon form) to pin it as an object.
                # Applied AT EVERY DEPTH including the top (review B8): gating it on a
                # non-empty path made {"0": "zero", "1": "one"} export as an ARRAY,
                # changing the file's top-level TYPE.
                escaped = (":" + seg) if _is_ascii_digits(seg) else seg
                child = (path + sep + escaped) if path else escaped
                _walk(v, child, origin + "/" + seg if origin else seg)
            return
        if isinstance(node, list):
            if not node and path:
                _note(f"{path}: dropped an empty array (it holds no translatable string)")
            for i, v in enumerate(node):
                child = (path + sep + str(i)) if path else str(i)
                _walk(v, child, "%s[%d]" % (origin, i))  # noqa: UP031
            return
        _emit(path, origin, value=node)

    if not isinstance(obj, (dict, list)):
        raise KeyFileError("a key file must contain a JSON object (or array) at the top level.")
    _walk(obj, "", "")
    return out


def unflatten_json(pairs, *, sep: str = NESTED_SEP):
    """Rebuild nested JSON from `::` paths.

    A run of numeric segments becomes an ARRAY only when it is COMPLETE and starts at
    0 (Lokalise's rule); a sparse or 1-based run degrades to an object keyed by the
    index strings, because an array with holes cannot represent it. A `:`-escaped
    segment (the `:::` form) is always an object key."""

    class _Node(dict):
        """A tree level we BUILT, as opposed to a dict that is itself a VALUE (a
        plural form map). Distinguishing them by type is what keeps `{"one": ...}`
        from being re-flattened into nesting on the way out."""

    root = _Node()
    for name, value in pairs:
        parts = [p for p in str(name).split(sep)]  # noqa: C416
        node = root
        for i, raw in enumerate(parts):
            forced = _is_forced_segment(raw)
            part = raw[1:] if forced else raw
            key = (part, forced)
            if i == len(parts) - 1:
                if key in node:
                    # The key is already a branch (`a::b` then `a`) or already set: a
                    # silent overwrite would drop a translation (#508).
                    raise KeyCollisionError(
                        str(name),
                        "an earlier key that already holds this path",
                        "a later key with the same path",
                    )
                node[key] = value
            else:
                node = node.setdefault(key, _Node())
                if not isinstance(node, _Node):  # a leaf and a branch share a path
                    raise KeyCollisionError(str(name), str(name), sep.join(parts[: i + 1]))

    def _collapse(node):
        if not isinstance(node, _Node):
            return node
        items = [(k, _collapse(v)) for k, v in node.items()]
        names = [k[0] for k in node]
        if len(set(names)) != len(names):
            # `a:::0` (object key "0") beside `a::0` (array index 0) collapse to one
            # member; refuse rather than keep the last (#508).
            dup = next(n for n in names if names.count(n) > 1)
            raise KeyCollisionError(
                dup,
                "an object key written with the ':::' escape",
                "a path segment with the same text",
            )
        forced = any(k[1] for k in node)
        if (
            items
            and not forced
            and all(_is_ascii_digits(n) for n in names)
            and sorted(int(n) for n in names) == list(range(len(names)))
        ):
            ordered = sorted(items, key=lambda kv: int(kv[0][0]))
            return [v for _, v in ordered]
        return {k[0]: v for k, v in items}

    return _collapse(root)


# --- JSON parse / render -----------------------------------------------------
def _no_duplicate_members(pairs):
    """`json.loads(object_pairs_hook=...)`: refuse a repeated member name inside ONE
    object instead of letting the parser collapse it (review B6).

    `{"a": "one", "a": "two"}` is legal JSON and every parser silently keeps the LAST
    value -- so a hand-edited or machine-merged file quietly lost a string BEFORE
    `flatten_json`'s collision guard could ever see two paths. This is the only place
    that duplication is still visible."""
    out: dict = {}
    for name, value in pairs:
        if name in out:
            raise KeyCollisionError(
                str(name),
                "an earlier member of the same JSON object",
                "a later one (JSON parsers keep only the last)",
            )
        out[name] = value
    return out


def parse_json_keys(
    data, *, locale: str = "", notes: list | None = None, meta: dict | None = None
) -> list[dict]:
    """Parse a JSON key file (flat or nested) into ordered key records."""
    text = data.decode("utf-8-sig") if isinstance(data, (bytes, bytearray)) else str(data)
    from app.filters.base import InputLimitError
    from app.filters.json_filter import check_json_budget

    # #576: the document-upload JSON budget (strings and non-string values) runs before
    # json.loads builds the whole tree; one helper for both doors.
    try:
        check_json_budget(text, what="this key file")
    except InputLimitError as exc:
        raise KeyFileError(str(exc)) from exc
    try:
        body = json.loads(text, object_pairs_hook=_no_duplicate_members)
    except KeyFileError:
        # Our own refusal, raised from inside object_pairs_hook. It is a ValueError
        # (every refusal in this module is), so it must be re-raised BEFORE the
        # not-valid-JSON handler swallows it and blames the JSON syntax.
        raise
    except ValueError as exc:
        raise KeyFileError(f"this file is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # A ~1500-deep body blows the C stack inside json itself. That is a property
        # of the FILE, so it belongs in this module's refusal vocabulary and not as a
        # 500 (review B12).
        raise KeyFileError("this file nests too deeply to parse safely.") from exc
    _take_json_meta(body, meta)
    try:
        return flatten_json(body, notes=notes)
    except RecursionError as exc:
        raise KeyFileError("this file nests too deeply to parse safely.") from exc


def render_json_keys(
    records,
    *,
    nested: bool = True,
    indent: int = 2,
    plural_style: str = PLURAL_OBJECT,
    meta: dict | None = None,
) -> bytes:
    """Render key records (already ordered + filtered) as a JSON file.

    `plural_style="i18next_v4"` emits CLDR-suffixed SIBLING keys (`items_one`,
    `items_other`) instead of a nested `{"one": ...}` object -- the two shapes i18next
    v4 and Lokalise's `json_string` actually consume.

    `meta` stamps the `$meta` marker (see the marker block); only a namespace-qualified
    export passes one."""
    pairs: list[tuple[str, object]] = []
    # Two keys that differ only by CONTEXT cannot both be represented in one JSON file
    # (JSON has no msgctxt). Refusing names the problem; letting the second silently
    # overwrite the first is how a translation disappears without a trace.
    seen: dict[str, str] = {}
    for rec in records:
        prior = seen.get(rec["name"])
        this = _record_origin(rec)
        if prior is not None:
            raise KeyCollisionError(rec["name"], prior, this)
        seen[rec["name"]] = this
    for rec in records:
        if rec.get("is_plural"):
            forms = rec.get("plural_values") or {}
            ordered = {c: forms[c] for c in CLDR_CATEGORIES if c in forms}
            if plural_style == PLURAL_I18NEXT:
                for cat, val in ordered.items():
                    pairs.append(("{}_{}".format(rec["name"], cat), val))
            else:
                pairs.append((rec["name"], ordered))
        else:
            pairs.append((rec["name"], rec.get("value", "")))
    # Names can collide only AFTER i18next suffixing (a plural `items` beside a plain
    # `items_one`); the record-level check above ran before it (#508).
    named: dict[str, int] = {}
    for name, _value in pairs:
        named[name] = named.get(name, 0) + 1
        if named[name] > 1:
            raise KeyCollisionError(
                name,
                "a key or plural form with this exact name",
                "another whose suffixed (i18next) name is the same",
            )
    if nested:
        body = unflatten_json(pairs)
    else:
        body = {}
        for name, value in pairs:
            # A flat file keeps the flattened path verbatim, minus the ":" escape,
            # which is an internal marker and must never reach a deliverable. The
            # test is the NARROW one (review B8) -- stripping every leading colon
            # mangled a user key genuinely named ":weird" into "weird".
            flat_name = NESTED_SEP.join(
                p[1:] if _is_forced_segment(p) else p for p in name.split(NESTED_SEP)
            )
            if flat_name in body:
                # `a:::0` (object key "0") and `a::0` (array index) both land here.
                raise KeyCollisionError(
                    flat_name,
                    "a key whose path has the ':::' escape",
                    "another that unescapes to the same flat name",
                )
            body[flat_name] = value
    if meta:
        # FIRST member, so a human opening the file sees what it is before the content.
        body = dict([(JSON_META_KEY, {JSON_META_OWNER: dict(meta)})] + list(body.items()))  # noqa: RUF005
    text = json.dumps(body, ensure_ascii=False, indent=indent)
    return (text + "\n").encode("utf-8")


def render_po_keys(
    records, *, locale: str = "", project_name: str = "", meta: dict | None = None
) -> bytes:
    """Render key records as a gettext catalog.

    The catalog is stamped with `locale`, so Babel writes the matching `Plural-Forms:`
    header -- which is the SAME expression `parse_po_keys` will read back.

    EXCEPT where Babel's expression is not the one the gettext world uses. Babel's
    Arabic rule sends `n%100 in 0..2` to slot 4 and everything else to slot 5, i.e. it
    puts CLDR `other` at index 4 and `many` at index 5 -- the reverse of the header in
    every ar.po in the wild. Our own round trip was self-consistent, so nothing noticed;
    but msgmerge against a GNU-header ar.po, or a runtime with the standard table
    compiled in, reads slot 4 as `many` and SILENTLY SWAPS TWO TRANSLATIONS. So exports
    use the GNU-canonical header and index order where one differs
    (`keys.gnu_plural_forms`), via a Catalog whose two plural properties are overridden
    -- the header we WRITE and the slots we FILL always come from the same expression
    (review B4). Import is unaffected: a file's own header stays authority for its own
    contents.

    DUPLICATES ARE REFUSED, exactly as in JSON (review B2). gettext keys on
    (msgid, msgctxt), so two records sharing both would have had the second silently
    overwrite the first inside Babel's catalog -- the Lokalise silent-merge trap this
    module's header claims to refuse, reproduced in our own exporter."""
    from babel.messages.catalog import Catalog
    from babel.messages.pofile import write_po

    babel_locale = keys_svc._babel_locale(locale)
    gnu = keys_svc.gnu_plural_forms(locale)

    class _Catalog(Catalog):
        """Catalog whose `Plural-Forms:` header is the GNU-canonical one. Both
        properties are overridden together so `num_plurals`, `plural_expr` and the
        rendered header can never disagree with the slots we filled."""

        @property
        def num_plurals(self):
            return gnu[0] if gnu else Catalog.num_plurals.fget(self)

        @property
        def plural_expr(self):
            return gnu[1] if gnu else Catalog.plural_expr.fget(self)

    catalog = (_Catalog if gnu else Catalog)(
        locale=babel_locale, project=project_name or branding.brand_name(), charset="utf-8"
    )
    # Babel marks a freshly-built header `#, fuzzy`, which some toolchains read as
    # "this whole catalog is untrusted" and skip. Ours is generated from reviewed
    # content, so say so. (Per-ENTRY fuzzy flags below are a different thing entirely.)
    catalog.fuzzy = False
    if meta and meta.get("namespaces") == NS_MODE_QUALIFIED:
        # A header COMMENT, not a header FIELD: gettext tools preserve it, msgmerge
        # carries it across, and Babel round trips it verbatim -- while an unknown
        # `X-` field can be dropped by a tool rewriting the header.
        catalog.header_comment = "{}\n{}".format(catalog.header_comment or "", PO_META_COMMENT)
    slot_cats = keys_svc.gettext_categories_for(
        locale,
        getattr(catalog, "plural_expr", "") or "",
        int(getattr(catalog, "num_plurals", 0) or 0),
    )
    index = keys_svc.gettext_index_map(
        locale,
        getattr(catalog, "plural_expr", "") or "",
        int(getattr(catalog, "num_plurals", 0) or 0),
    )
    num = int(getattr(catalog, "num_plurals", 0) or 2)
    seen: dict[tuple, str] = {}
    for rec in records:
        ident = (rec["name"], rec.get("context") or "")
        if ident in seen:
            raise KeyCollisionError(rec["name"], seen[ident], _record_origin(rec))
        seen[ident] = _record_origin(rec)
    for rec in records:
        comments = [c for c in str(rec.get("description") or "").split("\n") if c]
        # TM lock (§8, D-1 export): one transport `#.` comment PER LOCKED FORM, in XLIFF
        # vocabulary, e.g. `#. loc-studio-lock: leveraged-tm translate=no` (non-plural) or
        # `#. loc-studio-lock[one]: leveraged-tm` (a plural form). rec["lock"] is a
        # {slot: lockdict} map (empty/absent on a build file), so per-form hardness never
        # collapses. parse_po_keys validates + strips these back out.
        for _slot, _lk in sorted((rec.get("lock") or {}).items()):
            _tr = " translate=no" if _lk.get("translate") == "no" else ""
            _br = (f"[{_slot}]") if _slot else ""
            comments.append(
                "{}{}: {}{}".format(PO_LOCK_COMMENT_PREFIX, _br, _lk.get("state", ""), _tr)
            )
        # `#, fuzzy` on an entry means "not reviewed" -- the same thing our
        # needs-review state means. Emitting it is the export half of review B9;
        # without it an unreviewed string shipped looking final.
        flags = ["fuzzy"] if rec.get("fuzzy") else []
        if rec.get("is_plural"):
            forms = rec.get("plural_values") or {}
            strings, filled = [""] * num, [False] * num
            for cat, value in forms.items():
                slot = index.get(cat)
                if slot is None or slot >= num:
                    continue
                strings[slot], filled[slot] = value, True
            # A gettext slot that NO stored category maps to falls back rather than
            # shipping a hole -- FIRST to its OWN category, then to `other`. The own-
            # category step is what covers a slot whose category a lower index already
            # claimed (`index` keeps the first, re-review R1/R8): id's msgstr[1] and
            # Breton's msgstr[1] both mean a category the map points at another slot,
            # and backfilling them from `other` shipped a form's text under a rule that
            # does not describe it. `ru`'s fraction-only `other`, which claims no slot
            # at all, still lands on the `other` step. A slot that WAS claimed keeps its
            # value even when empty -- an untranslated form must stay visibly
            # untranslated, never silently backfilled with a different form's text.
            for i, done in enumerate(filled):
                if not done:
                    own = slot_cats[i] if i < len(slot_cats) else "other"
                    strings[i] = forms.get(own, forms.get("other", "")) or ""
            catalog.add(
                (rec["name"], rec.get("plural_source") or rec["name"]),
                tuple(strings),
                context=(rec.get("context") or None),
                auto_comments=comments,
                flags=flags,
            )
        else:
            catalog.add(
                rec["name"],
                rec.get("value", ""),
                context=(rec.get("context") or None),
                auto_comments=comments,
                flags=flags,
            )
    buf = io.BytesIO()
    write_po(buf, catalog, width=0, sort_output=False, sort_by_file=False)
    return buf.getvalue()


def parse_keys(
    data, fmt: str, *, locale: str = "", notes: list | None = None, meta: dict | None = None
) -> list[dict]:
    """Format dispatch: the two native parsers, then the keys bridge (Batch 9)."""
    fmt = (fmt or "").strip().lower().lstrip(".")
    if fmt in ("json",):
        records = parse_json_keys(data, locale=locale, notes=notes, meta=meta)
        check_key_records(records)  # #576
        return records
    if fmt in ("po", "pot"):
        records = parse_po_keys(data, locale=locale, notes=notes, meta=meta)
        check_key_records(records)  # #576
        return records
    from app.services import key_files_bridge as bridge

    if bridge.is_bridged(fmt):
        # Batch 9: any resource filter with a records() key view (discovered, not listed).
        records = bridge.parse_resource_keys(data, fmt, notes=notes)
        check_key_records(records)  # #576
        return records
    raise KeyFileError(
        "unsupported key file format {!r} (supported: {}).".format(
            fmt, ", ".join(bridge.all_formats())
        )
    )


def _apply_record(
    session: Session,
    key,
    rec: dict,
    locale: str,
    is_base: bool,
    actor: str,
    notes: list | None = None,
    skipped: list | None = None,
) -> bool:
    """Write ONE record's value(s). Returns True when something actually changed.

    ONLY the categories the FILE CARRIED are written (review B3). A gettext catalog
    physically cannot express a CLDR category its Plural-Forms header has no slot for
    -- Russian's `other` is fraction-only -- so treating "absent from the file" as
    "empty" wrote "" over a perfectly good stored translation and reported it as an
    update. Absent means unmentioned.

    A `#, fuzzy` entry lands in the needs-review state rather than as a final value
    (review B9).

    SOURCE TEXT IN A TARGET LOCALE IS NOT A TRANSLATION (live-QA K1c). An incoming
    value that restates that form's OWN base text is, by construction, untranslated
    English sitting in a de/ru/es slot. ONE rule: a file restating the source is never
    a write. The row -- translated or empty -- keeps exactly what it had, and the
    report NAMES the key.

    WHAT "RESTATES" MEANS, and why it is not byte identity (fix-round F3/F7). The first
    version compared bytes, and a single trailing newline walked straight past it:
    importing `"Settings\\r\\n"` over an APPROVED Russian translation destroyed it and
    landed the English as `mt` with the review flag gone -- and a trailing newline is
    what half the export tools and every hand-edited file in the world emit. The
    comparison is therefore on a NORMALIZED form: NFC and stripped, so a
    whitespace-only difference from the source -- or the same word spelled with
    combining accents rather than composed ones -- is not a translation either.

    CASE IS CONTENT, NOT NOISE (final round). F3 also casefolded, and that made every
    case-only difference read as source text: `Email`, `Internet`, `iPhone`, `SAVE`
    refused into an empty row. Worse, `casefold("ss-with-eszett") == "ss"`, and Swiss
    German differs from German ONLY by that substitution -- so a de-DE -> de-CH project
    could not import its target locale at all. `_fold` does NFC and strip and stops.

    AND AN EMPTY ROW IS NOT A LICENCE (fix-round F7). The first version let an empty
    row accept the source text as NEEDS_REVIEW. That reads as caution and is not: it
    fills an untranslated locale with English, drops the key out of
    `--untranslated-in` (which tests EMPTINESS), and the next build file ships the
    English -- permanently, since the second round trip is stable. `--empty-as base`
    plus a re-import did exactly that to three rows in the fix round's probe. An
    untranslated row stays untranslated and stays in the translator's queue.

    THIS IS THE FLOOR UNDER THE K1 FIX, NOT THE FIX. The transport file no longer
    carries the substitution; this makes a file we did NOT write -- a hand-made file, a
    translation kit pre-filled with English, an `--empty-as base` build file fed back
    in -- unable to do the same damage. It is not a guarantee that no file can ever
    reduce a translation's status: an import that carries a genuinely DIFFERENT value
    is still an ordinary write, which is what an import is for.

    Text IDENTICAL to what is stored is untouched anyway (`set_value` no-ops), so a
    locale whose translation genuinely equals the source ("OK", a brand name, "%d")
    keeps its status and an export -> import round trip stays a fixed point.

    THE TRADE, recorded so it is a choice and not an oversight: a translator who
    deliberately sets a value TO its source string cannot do it through an import --
    the write is skipped and NAMED in the report. That direction is rare, recoverable
    and announced; the direction it blocks is silent and destroys text."""
    if rec.get("is_plural"):
        values = {c: v for c, v in (rec.get("plural_values") or {}).items() if c in CLDR_CATEGORIES}
    else:
        values = {"": rec.get("value", "")}
    if is_base:
        before = keys_svc.base_values(session, key)
        keys_svc.set_base_value(
            session, key, values if rec.get("is_plural") else values[""], actor=actor
        )
        return keys_svc.base_values(session, key) != before
    changed = False
    allowed = keys_svc.plural_categories_for(locale)
    rows = keys_svc.values_for(session, key, locale)
    before = {(s.plural_category or ""): (s.target_text or "") for s in rows}
    sources = {(s.plural_category or ""): (s.source_text or "") for s in rows}
    for cat, text in values.items():
        if rec.get("is_plural") and cat not in allowed:
            # The FILE carries a form this locale does not use (a shared source file
            # imported for ja). Dropping it silently is the wrong answer only if it
            # had content; an empty one is genuinely nothing.
            if (text or "").strip():
                raise keys_svc.UnknownPluralCategory(cat, locale, allowed)
            continue
        incoming = text or ""
        slot = cat or ""
        stored, base_text = before.get(slot, ""), sources.get(slot, "")
        status = SegStatus.NEEDS_REVIEW if (rec.get("fuzzy") and incoming.strip()) else None
        if status is not None and rec.get("is_plural") and incoming == stored:
            # gettext's `#, fuzzy` is per-ENTRY, never per `msgstr[N]` (fix-round F5).
            # A plural key with one unreviewed form therefore exports ONE fuzzy flag
            # for the whole entry, and re-importing it flipped every OTHER form --
            # approved and mt alike -- to needs-review: a reviewer's sign-off destroyed
            # by a loop the product calls safe. A form whose text the file merely
            # RESTATES carries no per-form verdict, so it keeps its own status. A form
            # whose text actually CHANGED is a new suggestion and still lands
            # needs-review.
            status = None
        if incoming.strip() and _restates_source(incoming, base_text) and incoming != stored:
            # See the docstring: this is SOURCE TEXT, not a translation. Never a write,
            # whether the row holds a translation or nothing at all.
            form = (f" [{slot}]") if slot else ""
            label = f"{_key_label(key)}{form}"
            if stored.strip():
                _note_skipped(
                    notes,
                    skipped,
                    label,
                    f"{label}: kept the stored {locale} translation -- the file "
                    "carries the SOURCE text for this locale, which is not a "
                    "translation and must not overwrite one.",
                )
            else:
                _note_skipped(
                    notes,
                    skipped,
                    label,
                    f"{label}: left {locale} untranslated -- the file carries the "
                    "SOURCE text, which is not a translation, so the key "
                    "stays in the untranslated queue.",
                )
            continue
        # TM HARD-LOCK on import (§8, D-1). A hard lock is an authoritative accepted
        # translation: a machine import must never overwrite it. Predict the refusal here
        # (the seam would refuse it too) so we (MED-8) REPORT it with its own kind -- never
        # as an `updated` -- and do NOT apply the caller's status to the untouched value,
        # and (K1) do NOT re-lock a row the import did not write.
        from app.services import quality as _quality

        seg_before = next((s for s in rows if (s.plural_category or "") == slot), None)
        if seg_before is not None and _quality.is_hard_locked(seg_before) and incoming != stored:
            form = (f" [{slot}]") if slot else ""
            label = f"{_key_label(key)}{form}"
            _note_skipped(
                notes,
                skipped,
                label,
                f"{label}: kept the TM-hard-locked {locale} translation -- an import may not "
                "overwrite a hard lock (unlock it first).",
            )
            continue
        # STATUS PROTECTION on import: a human-APPROVED or -EDITED value is authoritative, and a
        # machine import must never overwrite it. Predict the refusal here (the seam would refuse
        # it too) so we REPORT it with its own kind -- never as an `updated` (the `changed`
        # accounting at the bottom of this loop would otherwise count a write that did not
        # happen) -- and never re-stamp the caller's status onto the untouched value. Escape
        # hatch: clear the value first (the grid save resets it to NEW).
        #
        # F5 (adversarial review): the seam refuses BOTH a text overwrite (incoming != stored)
        # AND a bare STATUS re-flag on UNCHANGED text -- a `#, fuzzy` on an unchanged approved
        # SIMPLE entry asks to demote the sign-off to needs-review, which the seam silently
        # drops. Predict BOTH so the refusal is REPORTED, never a silent unchanged row. A true
        # no-op (unchanged text with no status change -- status is None, or already equals the
        # stored status) is NOT reported. (The plural per-form rule above already reset an
        # entry-level fuzzy to status=None for an unchanged form, so a plural restatement stays
        # a clean no-op and never reaches this report.)
        _human_final = seg_before is not None and getattr(seg_before, "status", None) in (
            SegStatus.APPROVED,
            SegStatus.EDITED,
        )
        if _human_final and (
            incoming != stored or (status is not None and status != seg_before.status)
        ):
            _human = "approved" if seg_before.status == SegStatus.APPROVED else "edited"
            form = (f" [{slot}]") if slot else ""
            label = f"{_key_label(key)}{form}"
            _note_skipped(
                notes,
                skipped,
                label,
                f"{label}: kept the human-{_human} {locale} translation -- an import may not overwrite "  # noqa: E501
                "or re-flag an approved or edited value (clear it first).",
            )
            continue
        # ORIGIN_IMPORT routes the write through the refusal choke point: a file
        # whose msgstr/value is an LLM hedge is quarantined on the way in, exactly as
        # the same text arriving on a document would be.
        keys_svc.set_value(
            session,
            key,
            locale,
            incoming,
            origin=keys_svc.ORIGIN_IMPORT,
            plural_category=(cat or None) if rec.get("is_plural") else None,
            actor=actor,
            status=status,
        )
        # MED-6: CONSUME the lock envelope. A transport file that carries a lock for this
        # form re-applies it to the value the import just WROTE (stored != incoming) --
        # never on a restatement no-op (the row keeps whatever lock it had) and never on a
        # refused row (skipped above). The lock flag itself never drives a write (K1): it
        # only rides onto a value the value map already wrote.
        _rl = (rec.get("lock") or {}).get(slot)
        _bh = _lock_from_transport(_rl) if _rl is not None else None
        if _bh is not None and stored != incoming:
            seg_after = next(
                (
                    s
                    for s in keys_svc.values_for(session, key, locale)
                    if (s.plural_category or "") == slot
                ),
                None,
            )
            if seg_after is not None:
                from app.services import tm_lock

                tm_lock._stamp_lock(seg_after, band=_bh[0], hard=_bh[1], tm_id=None, entry_id=None)
                session.add(seg_after)
                session.commit()
        changed = changed or stored != incoming
    return changed
