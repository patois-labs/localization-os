"""key_files: export

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.models import SegStatus
from app.services import keys as keys_svc
from app.services.key_files_json_parse_render import render_json_keys, render_po_keys
from app.services.key_files_po_parse_render import (
    CHAR_OVER_LIMIT,
    EMPTY_BASE,
    EMPTY_EMPTY,
    EMPTY_MODES,
    EMPTY_SKIP,
    FILTERED_OUT,
    NS_MODE_QUALIFIED,
    PLURAL_OBJECT,
    SORT_AZ,
    SORT_FIRST_ADDED,
    SORTS,
    SUB_EMPTY_AS_BASE,
    SUB_KEPT_FOR_TRANSPORT,
    SUB_TM_LOCKED,
    KeyFileError,
    _lock_transport,
    _resolve_locale,
    qualified_name,
)

log = logging.getLogger(__name__)

#: Report kind for a key `omit_unfinished` held out of the file (see export_locale).
WITHHELD_UNFINISHED = "withheld_unfinished"


def _is_unfinished(seg, policy: str) -> bool:
    """A value that must not ship as a translation: needs-review (a withheld or flagged MT
    output), blank while its source is not, or tag-damaged under a policy that would
    substitute or omit it. The same predicates the exporter applies value by value."""
    from app.services import export as export_svc

    if (seg.status or "") == SegStatus.NEEDS_REVIEW:
        return True
    if not (seg.target_text or "").strip():
        return bool((seg.source_text or "").strip())
    return bool(policy != "ship" and export_svc.segment_tag_damage(seg))


# --- export ------------------------------------------------------------------
def export_locale(
    session: Session,
    project,
    locale: str,
    *,
    fmt: str = "json",
    namespace=None,
    tag=None,
    status=None,
    empty_as: str = EMPTY_EMPTY,
    nested: bool = True,
    sort: str = SORT_AZ,
    plural_style: str = PLURAL_OBJECT,
    indent: int = 2,
    include_hidden: bool = True,
    include_archived: bool = False,
    qualify_namespaces=None,
    report: list | None = None,
    exported_keys: list | None = None,
    omit_unfinished: bool = False,
    keep_existing: dict | None = None,
    template: bytes | None = None,
) -> bytes:
    """The Batch-4 headline deliverable: ONE file per locale across the WHOLE project.

    `report` is the substitution channel, the same `{unit, index, kind}` list
    `export.build_translated_file` fills and `export.substitution_detail` renders --
    because the tag guard below can hand back bytes that look perfectly well-formed
    while one value silently shipped its SOURCE, and that fact has to ride alongside
    the file. `unit` is "key" and `index` is the key's full name.

    The tag guard itself is `export.segment_tag_damage`, unchanged and uncopied: key
    values are Segments, so the guard that protects a DOCX round trip protects a
    strings file for free. Policy follows the same `export_tag_damage_policy` setting
    ("source" substitutes, "ship" opts out); a key file's analogue of "quarantine" is
    to omit the damaged value rather than the whole file, since damage is local to one
    key exactly as it is local to one segment.

    IT RUNS ON THE BUILD FILE (live-QA K1). A TRANSPORT file carries the true stored
    state instead, because substituting there launders a damaged translation into
    confident-looking English on the way back IN. Damaged transport values are reported
    with `kind=SUB_KEPT_FOR_TRANSPORT` so the fact still reaches the operator; see the
    module header. `export_notices()` renders both kinds for CLI/web/audit.

    NAMESPACES SURVIVE THE TRIP (review B2). A namespace is part of a key's identity --
    and it DEFAULTS to the file stem, so two uploaded files routinely define the same
    name -- but a whole-project export used to emit bare names. JSON then refused the
    whole export on a collision, .po SILENTLY kept one namespace's value and dropped
    the other (the exact Lokalise silent-merge trap this module's header says it
    refuses), and even with no clash at all an export could not be re-imported into the
    namespaces it came from.

    WHICH SHAPE YOU GET IS A PROPERTY OF THE CALL, NEVER OF THE DATA (re-review R2/R12).
    A WHOLE-PROJECT export (`namespace=None`) always qualifies every name as
    `namespace::name`, the default namespace spelled `__default__` -- in nested JSON,
    one top-level section per namespace -- and stamps the marker that says so.
    `namespace=<x>` scopes the export to that one namespace and always keeps bare
    names. `qualify_namespaces=True/False` forces either answer explicitly.

    The first version derived it from the ROWS -- qualify iff the result spanned more
    than one namespace -- and that is a shape that FLIPS UNDER THE DATA. Adding one key
    in a second namespace rewrote every name in the next export, which for .po means
    every msgid, i.e. every runtime lookup key in the product, silently, in a diff that
    looks like an unrelated feature branch. Worse, `qualify` was computed AFTER
    filtering, so `include_archived` or a `tag` could flip the shape of an export whose
    content had not changed at all. A build pipeline cannot depend on that. Feed the
    result back with `import_keys(namespace=NS_FROM_KEY)` and the round trip is a fixed
    point -- and only a marked file is accepted there, so a bare export can no longer
    be mistaken for a qualified one.

    `locale` DEFAULTS TO THE SOURCE LOCALE, exactly as `import_keys` does (re-review
    R9): the two seams are a pair, and one defaulting while the other refused was a
    difference with no reason behind it.

    `locale` is validated UP FRONT against the source locale and the ACTIVE targets
    (review B11/B18) -- exporting a locale that was removed from the project used to
    hand back its stale value rows, and exporting the SOURCE locale used to return an
    empty file because value rows exist only per target. The source locale now serves
    the keys' BASE values, which is what makes a base-file round trip possible at all
    (review B5).

    ARCHIVED keys are excluded by default (`include_archived=False`, review B13):
    archived means "retired from the product", so shipping them in a build file
    resurrects strings somebody deliberately retired. HIDDEN keys are still exported by
    default -- hidden means hidden from TRANSLATORS, not absent from the build.

    `omit_unfinished` (git connectors, 2026-10-10) is the HELD-BACK rule for a file that is
    committed into someone else's repository: a key with ANY value for this locale that is
    needs-review, blank (while its source is not), or tag-damaged is OMITTED instead of
    shipping a placeholder, an empty string or source text. `keep_existing` maps an output
    key name to the record already published in the repository's current file (a parsed
    `parse_keys` record); an omitted key that has one keeps THAT value in its place, so a
    withheld run never deletes a translation that is already shipped. Each omission is
    appended to `report` as `{unit, index, kind: "withheld_unfinished", locale,
    kept_existing}`. Both default off: every other caller is byte-identical.

    A BRIDGED resource format (Batch 9, `key_files_bridge`: .properties, .strings, YAML and
    every other filter with a `records()` key view) is one file per namespace, rendered by
    that format's own filter over a TEMPLATE: `template` when given (the git connector
    passes the repository's source file), else the source file the last base-locale import
    of that namespace stored. A source-locale export with nothing changed is the template
    byte for byte. Exported keys the template has no place for are reported as
    `not_in_template`."""
    from app.services import export as export_svc
    from app.services import settings

    proj = keys_svc._project(session, project)
    fmt = (fmt or "json").lower().lstrip(".")
    from app.services import key_files_bridge as bridge

    bridged = bridge.resolve(fmt)
    if fmt not in ("json", "po", "pot") and not bridged:
        raise KeyFileError(
            "unsupported key file format {!r} (supported: {}).".format(
                fmt, ", ".join(bridge.all_formats())
            )
        )
    if bridged:
        if namespace is None or qualify_namespaces:
            raise KeyFileError(
                f"a {bridged} export is one file per namespace: pass the namespace to export "
                "(a whole-project transport file is JSON or .po)."
            )
        if template is None:
            template = bridge.load_template(proj, namespace, bridged)
        if template is None:
            raise KeyFileError(
                f"there is no {bridged} source file for namespace {namespace or '(default)'!r} "
                f"to fill: import the {proj.source_locale} {bridged} file into it first "
                "(that import is kept as the template the export writes)."
            )
    if empty_as not in EMPTY_MODES:
        raise KeyFileError(
            "unknown empty_as {!r} (expected {}).".format(empty_as, ", ".join(EMPTY_MODES))
        )
    if sort not in SORTS:
        raise KeyFileError("unknown sort {!r} (expected {}).".format(sort, ", ".join(SORTS)))
    locale = _resolve_locale(session, proj, locale or proj.source_locale, "export")
    # Batch 15 T2 (§9.2/N1): the KEY/project-mode TM-only empty gate COMPANION. This
    # whole-project key export is the canonical TM-only delivery. A HOLD refuses until a
    # human approves (keys deliver-approve); a RELEASE_EMPTIES delivers (the gate is
    # PERMISSION, `empty_as` is RENDERING -- §9.4); a PASS is unchanged. tm_delivery loads
    # keys machinery LAZILY, so a pure-document install never arms it (P10 inertness).
    from app.services import tm_delivery

    # ISSUE #95 (gate C round 1, H1): the QUALITY-GATE hold COMPANION in KEY mode. A key
    # project's values are Segments in its internal key documents and run through the same
    # segmented loop, so a held correction loop flags them the same way -- without this the
    # canonical TM-only delivery would ship the text the document export refuses.
    _qgate = export_svc.gate_held_reason_for_project(session, proj.id, locale)
    if _qgate is not None:
        raise export_svc.ExportHeldLocale(_qgate, locale=locale)

    # A-M3/B-MED-3: pass this export's ACTUAL include_archived/include_hidden so the gate's
    # key universe matches what will ship -- a hardcoded narrower set would let an archived
    # empty key ship "" un-approved (fail-open).
    _gate = tm_delivery.empty_gate_decision(
        session,
        project=proj,
        locale=locale,
        include_archived=include_archived,
        include_hidden=include_hidden,
    )
    if _gate == tm_delivery.HOLD:
        # A2-H1: the 409 render is a REVIEW surface -> show the WIDEST empty set (the default),
        # matching what `approve_keys` covers, so a `--archived` export's empties are visible +
        # coverable (never a permanent 409). The gate DECISION above still uses the export's own
        # flags (it must hold exactly what THIS export would ship).
        _empties = tm_delivery.key_empties(session, proj, locale)
        raise export_svc.ExportEmptyTargetsHeld(
            # #105: shared diagnosis seam (names an in-flight run) + the keys way-out.
            tm_delivery.held_message(
                session,
                project_id=proj.id,
                locale=locale,
                count=len(_empties),
                unit="key",
                way_out="Use keys deliver-approve. This is not bypassable.",
            ),
            project_id=proj.id,
            locale=locale,
            empties=_empties,
        )
    is_base = locale == proj.source_locale
    policy = (settings.get("export_tag_damage_policy") or "source").strip().lower()

    rows = keys_svc.list_keys(
        session,
        proj.id,
        namespace=namespace,
        tag=tag,
        include_hidden=include_hidden,
        include_archived=include_archived,
    )
    if sort == SORT_FIRST_ADDED:
        rows = sorted(rows, key=lambda k: k.id)
    # Derived from the CALL only -- not from `rows`, which every filter above has
    # already narrowed (re-review R2/R12).
    qualify = (namespace is None) if qualify_namespaces is None else bool(qualify_namespaces)
    meta = {"namespaces": NS_MODE_QUALIFIED} if qualify else None
    # THE GUARD IS A DELIVERY GUARD (live-QA K1, and the module header). `qualify` is
    # already the build/transport discriminator, so it is also the one that decides
    # whether a damaged value may be replaced by its source: a build file must never
    # ship damaged markup, and a transport file must never lie about what is stored.
    substitute = not qualify

    records: list[dict] = []
    locks: dict[str, dict] = {}  # out_name -> {state, translate}; transport only (§8)
    for key in rows:
        out_name = qualified_name(key.namespace or "", key.name) if qualify else key.name
        common = {
            "name": out_name,
            "namespace": key.namespace or "",
            "context": key.context or "",
            "description": key.description or "",
        }
        if is_base:
            # The SOURCE locale has no value rows of its own -- a key's base value is
            # the `source_text` its per-target rows carry (spec 2.5) -- so it is served
            # from there. Without this the source locale exported as `{}` (review B5).
            base = keys_svc.base_values(session, key)
            forms = {c: (v or "") for c, v in base.items()}
            if empty_as == EMPTY_SKIP:
                forms = {c: v for c, v in forms.items() if v.strip()}
            if not forms:
                continue
        else:
            segs = [s for s in keys_svc.values_for(session, key, locale)]  # noqa: C416
            if not segs:
                continue
            if omit_unfinished and any(_is_unfinished(seg, policy) for seg in segs):
                old = (keep_existing or {}).get(out_name)
                kept = isinstance(old, dict) and bool(old.get("is_plural")) == bool(key.is_plural)
                if report is not None:
                    report.append(
                        {
                            "unit": "key",
                            "index": out_name,
                            "kind": WITHHELD_UNFINISHED,
                            "locale": locale,
                            "kept_existing": kept,
                        }
                    )
                if kept:
                    keep = dict(common, fuzzy=False, is_plural=bool(key.is_plural))
                    if key.is_plural:
                        keep["plural_values"] = dict(old.get("plural_values") or {})
                    else:
                        keep["value"] = old.get("value", "")
                    records.append(keep)
                    if exported_keys is not None:
                        exported_keys.append(key)
                continue
            if status and not any((s.status or "") == status for s in segs):
                # K9 (live-QA F3): "filtered out by status" is the THIRD export outcome
                # alongside substituted and reported-over-limit, and it was the only one
                # with no report channel -- a bogus `--status` yields a VALID EMPTY file
                # (a full .po header, zero entries) indistinguishable from "this project
                # has no strings". Announce every key the filter drops so the emptiness
                # is legible on every surface. Report-only; the key is already omitted.
                if report is not None:
                    report.append(
                        {
                            "unit": "key",
                            "index": out_name,
                            "kind": FILTERED_OUT,
                            "locale": locale,
                            "status": status,
                        }
                    )
                continue
            forms = {}
            fuzzy = False
            # K8 (spec §7): an over-limit value needs its OWN export signal, distinct from
            # the status filter -- a release manager cannot express "ship everything except
            # values that physically will not fit" via `--status` (which conflates "too
            # long" with "unreviewed"). REPORT-ONLY: nothing substituted, nothing omitted,
            # no status touched -- the value ships and the report says it is too long, so
            # the three export outcomes stay distinct (substituted / filtered-out /
            # reported). `key.char_limit` is coerced so a dirty legacy 0/negative reads as
            # unset and can never raise here.
            from app.services import quality as _quality

            _cl = _quality.coerce_char_limit(key.char_limit)
            _rec_locks: dict[str, dict] = {}  # slot -> lockdict; filtered + emitted below
            for seg in segs:
                value = seg.target_text or ""
                if value.strip() and (seg.status or "") == SegStatus.NEEDS_REVIEW:
                    fuzzy = True
                if value.strip() and policy != "ship" and export_svc.segment_tag_damage(seg):
                    if substitute:
                        value = "" if policy == "quarantine" else (seg.source_text or "")
                        kind = export_svc.SUB_AUTOMATIC
                    else:
                        # Transport: the stored text rides out untouched so it can ride
                        # back in. Announced, never silent.
                        kind = SUB_KEPT_FOR_TRANSPORT
                    if report is not None:
                        report.append(
                            {
                                "unit": "key",
                                "index": out_name,
                                "kind": kind,
                                "locale": locale,
                                "plural_category": seg.plural_category or "",
                            }
                        )
                if not value.strip():
                    if empty_as == EMPTY_SKIP:
                        value = None
                    elif empty_as == EMPTY_BASE:
                        value = seg.source_text or ""
                        if value.strip() and (seg.status or "") == SegStatus.NEEDS_REVIEW:
                            # A WITHHELD value, not an untouched one (fix-round K12).
                            # `fuzzy` was computed from the RAW text above, so an empty
                            # needs-review row -- a quarantined refusal, an MT output
                            # the gate declined to commit -- shipped its source text as
                            # a FINAL translation: `"cta.start": "Start now"` in JSON,
                            # and in .po without the `#, fuzzy` that is gettext's whole
                            # word for "suggested, not reviewed". Filling an untouched
                            # empty row is what `empty_as=base` is FOR and stays
                            # silent; filling one the product deliberately withheld is
                            # announced on every surface.
                            fuzzy = True
                            if report is not None:
                                report.append(
                                    {
                                        "unit": "key",
                                        "index": out_name,
                                        "kind": SUB_EMPTY_AS_BASE,
                                        "locale": locale,
                                        "plural_category": seg.plural_category or "",
                                    }
                                )
                    else:
                        value = ""
                forms[seg.plural_category or ""] = value
                # K8: judge the value that ACTUALLY SHIPS (after any substitution/fill),
                # per plural form. Report-only; the value is already in `forms`.
                if _cl is not None and isinstance(value, str) and value.strip():
                    _n = _quality.char_count(value)
                    if _n > _cl and report is not None:
                        report.append(
                            {
                                "unit": "key",
                                "index": out_name,
                                "kind": CHAR_OVER_LIMIT,
                                "locale": locale,
                                "plural_category": seg.plural_category or "",
                                "length": _n,
                                "limit": _cl,
                            }
                        )
                # TM lock (§8, D-1 export): collect PER FORM (LOW-13 -- a plural key's
                # forms may lock differently; the earlier code collapsed to the first).
                # Only EMITTED forms survive the filter below (MED-7 -- no orphan lock for
                # a form that empty_as=skip dropped from the file), and it rides the file
                # only on the TRANSPORT shape.
                _lk = _lock_transport(seg)
                if _lk is not None:
                    _rec_locks[seg.plural_category or ""] = _lk
            # A needs-review value is a SUGGESTION, and gettext has a word for that.
            common["fuzzy"] = fuzzy
        if key.is_plural:
            present = {c: v for c, v in forms.items() if v is not None and c}
            if not present:
                continue
            rec = dict(common, is_plural=True, plural_values=present)
        else:
            value = forms.get("")
            if value is None:
                continue
            rec = dict(common, is_plural=False, value=value)
        # TM-lock transport representation (§8, D-1), attached ONLY for slots this record
        # actually EMITTED (MED-7) and ONLY on a TRANSPORT export (LOW-13 -- a build
        # deliverable never carries lock state, and the report kind is transport-only too).
        if not is_base and qualify:
            _emitted = set(rec.get("plural_values", {})) if rec.get("is_plural") else {""}
            _kept = {slot: lk for slot, lk in _rec_locks.items() if slot in _emitted}
            if _kept:
                locks[out_name] = _kept
                rec["lock"] = _kept  # PO renderer emits per-form comments from it
                if report is not None:
                    for slot, lk in _kept.items():
                        report.append(
                            {
                                "unit": "key",
                                "index": out_name,
                                "kind": SUB_TM_LOCKED,
                                "locale": locale,
                                "plural_category": slot,
                                "lock": lk,
                            }
                        )
        records.append(rec)
        # Batch 14 Phase 1.5: the include_string_images bundle needs the ACTUAL exported
        # StringKey rows (the ones that survived every filter above) so it can attach only
        # THESE keys' images -- appended here, at the single inclusion point, so the out-param
        # can never disagree with `records`. Inert to the returned file bytes (M: an out-param
        # like `report`), so a caller that does not pass it gets today's behaviour exactly.
        if exported_keys is not None:
            exported_keys.append(key)

    # TM locks ride the TRANSPORT file's $meta envelope (JSON) so the value map stays a
    # clean key->value map: a build deliverable (meta is None) NEVER carries lock state.
    # On import, _take_json_meta lifts $meta.locks into `meta`, import_keys distributes it
    # onto each record, and _apply_record CONSUMES it (re-applying band/hard to the values
    # it writes, subject to K1) -- the lock rides both OUT and back IN.
    if locks and meta is not None:
        meta = dict(meta, locks=locks)
    if _gate == tm_delivery.RELEASE_EMPTIES:
        # The approved-empty KEY delivery actually assembled -> audit it (§9.7). A system
        # event (the human decision was audited at approval time); customer derived from the
        # PROJECT (P7 -- there is no single document). Best-effort.
        try:
            from app.services import audit as _audit

            _audit.log(
                actor="system",
                action="delivery.empty_delivered",
                target="project %d %s" % (proj.id, locale),  # noqa: UP031
                detail="approved-empty key delivery assembled",
                customer_id=_audit.customer_for_project(proj.id),
            )
        except Exception:  # an audit failure must never break a delivery
            log.warning(
                "audit write failed for delivery.empty_delivered (project %s %s)",
                proj.id,
                locale,
                exc_info=True,
            )
    if bridged:
        return bridge.render_resource_keys(
            records, template, bridged, locale=locale, is_base=is_base, report=report
        )
    if fmt == "json":
        return render_json_keys(
            records, nested=nested, indent=indent, plural_style=plural_style, meta=meta
        )
    return render_po_keys(records, locale=locale, project_name=proj.name, meta=meta)
