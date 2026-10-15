"""store: part 4 (layered pure move)

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app.db import engine
from app.models import (
    ConnStatus,
    Segment,
    SegStatus,
)
from app.services import audit, providers, seed_caps
from app.services.connectors import egress_guard, harvest
from app.services.connectors.base import content_hash
from app.services.connectors.store_common import (
    WriteBudget,
    _airtable_scope_target,
    _jira_harvest_fields,
    _jira_translate_attachments,
    _log,
    _set_status,
    effective_locales,
    get,
)
from app.services.connectors.store_part2 import (
    _new_connector_doc,
    _refuse_archived_sync,
    _refuse_disabled_sync,
    build_fresh,
    patch_config_merge,
)

log = logging.getLogger("loc_tms.connectors")


def airtable_delete_records(
    cid: int,
    record_ids: list,
    base_id: str | None = None,
    table: str | None = None,
    actor: str = "",
    transport=None,
) -> tuple[bool, str, dict]:
    """Delete Airtable record(s) by id. Returns (ok, message, summary). Never raises
    for an HTTP failure. Runs under the WriteBudget (one delete = one write)."""
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "No such Airtable connection.", {}
    cfg = conn.config or {}
    if not cfg.get("base_id"):
        return False, "Configure a base on the connection before deleting records.", {}
    base, tbl, scope_err = _airtable_scope_target(cfg, base_id, table)
    if scope_err:
        return False, scope_err, {}
    if not base or not tbl:
        return False, "base_id and table are required to delete records.", {}
    ids = [str(r).strip() for r in (record_ids or []) if str(r).strip()]
    if not ids:
        return False, "No record ids provided to delete.", {}
    budget = WriteBudget()
    allowed = budget.trim(len(ids))
    to_delete = ids[:allowed]
    if not to_delete:
        return (
            False,
            (f"Write ceiling ({budget.ceiling}) is 0 — nothing deleted."),
            {"deleted": [], "ceiling_hit": True, "write_ceiling": budget.ceiling},
        )
    at = build_fresh(cid, transport)
    try:
        deleted, failed, msg = at.delete_records(base, tbl, to_delete)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    ok = bool(deleted)
    summary = {
        "deleted_ids": deleted,
        "deleted": len(deleted),
        "failed": len(failed),
        "failed_ids": failed,
        "ceiling_hit": budget.ceiling_hit,
        "write_ceiling": budget.ceiling,
    }
    if budget.ceiling_hit:
        msg += (
            f" (write ceiling {budget.ceiling} reached — {len(ids) - allowed} "
            "id(s) not attempted this run)"
        )
    _set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, synced=False)
    _log(cid, "delete", ok, len(deleted), msg, actor)
    audit.log(
        actor=actor or "system",
        action="connector.airtable.delete_records",
        target=f"connection:{cid}",
        detail=f"base:{base} table:{tbl} deleted:{len(deleted)}",
        customer_id=getattr(conn, "customer_id", None),
    )
    return ok, msg, summary


def jira_translate(
    cid: int,
    issue_keys: list | None = None,
    jql: str | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
) -> tuple[bool, str, dict]:
    """Translate Jira issues (summary/description/text-cfs, optionally existing
    comments) into one or more languages and write each language back as a labelled
    comment (or into a field). The in-Jira translate-button / on-the-fly backend.

    Rich-text fidelity: ADF fields (description) are walked leaf-by-leaf with the
    real AST walker (app.filters.adf) — one segment per inline container (paragraph,
    heading, cell) with placeholder tokens for marks and mentions (#644) — and
    reinserted into the SOURCE tree so marks/links/mentions/tables/code survive.
    A source-vs-target structure-integrity guard (adf.structure_ok) falls back to
    keeping the original (per-pair error, no corrupted write) mirroring DOCX/XLIFF.

    Per-field-type gate: configured field ids are validated (jr.validate_fields) —
    custom fields must be text-typed (`schema.type == 'string'`); non-text cfs are
    skipped with a per-field reason, never written blind.

    Changelog-gated re-translation: a per-(issue, locale) source content hash
    (summary+description+cfs) is stored on the connection (jira_hashes,
    patch_config_merge). A pair whose stored hash matches the current source is
    suppressed — the hash is the authoritative gate. `expand=changelog` (fields
    `updated` semantics) is an OPTIMIZATION only and not required for correctness.
    F1(a) fix (review): fields and comments now suppress INDEPENDENTLY on their
    OWN hashes — the field-level gate no longer has an all-or-nothing escape hatch
    for comment-mode pairs (previously ANY existing-comment translation on a pair
    made the whole pair bypass the field/summary/description gate, so every run
    re-posted the same field-derived comment forever; proven 8->16 posts).

    Comment translation (opt-in, off by default): when translate_comments is set,
    existing issue comments are each translated (ADF-aware) and written back as a
    labelled reply. F1 fixes (review):
      (a) per-comment suppression — each SOURCE comment gets its own content hash
          under key f"{issue}:comment:{comment_id}:{loc}" in jira_hashes (same
          merge-persisted map as the field-level hashes); a comment whose hash is
          unchanged is suppressed independently of the rest of the pair, so an
          EDITED source comment (hash changes) still re-translates but an
          unchanged one is never re-posted.
      (b) self-feedback loop — JiraConnector.fetch_comments now filters out any
          comment whose body starts with our own label prefix (🌐), so our own
          labelled replies are never fed back in as translation sources (previously
          proven exponential: 1 -> 3 -> 7 replies across runs).

    F3 fix (review): a translated `summary` over Jira's ~255-char field limit is
    SKIPPED (not truncated, not written) with a clear per-pair reason; description
    and other fields for that pair still write, and the pair's hash still records
    if everything else landed — so the run converges instead of 400ing forever.

    F4 fix (review): a per-run ceiling (config.MAX_WRITES_PER_RUN) on total writes
    (field PUTs + comment POSTs) stops the write loop cleanly — loud per-run
    message, no exception — once hit; whatever's left is picked up next run.

    only_pairs (retry path): a set of (issue_key, locale) tuples. When given, the
    WRITE-BACK loop skips any (issue_key, locale) pair not in the set — so a retry
    re-posts ONLY the pairs that actually failed before, never a pair that already
    succeeded (which would double-post a comment)."""
    from app.filters import adf as adf_walker
    from app.services import translate
    from app.services.connectors.jira import _adf_doc, _cf_schema_kind

    conn = get(cid)
    if not conn or conn.kind != "jira":
        return False, "No such Jira connection.", {}
    _dis = _refuse_disabled_sync(cid, actor=actor)  # ffadmin Wave 5 (before any egress/bookkeeping)
    if _dis is not None:
        return False, _dis, {}
    _arch = _refuse_archived_sync(cid, actor=actor)  # M6/R1 (jira_translate writes to Jira)
    if _arch is not None:
        return True, _arch, {"created": 0, "updated": 0, "skipped_archived": True}
    cfg = conn.config or {}
    jr = build_fresh(cid, transport)  # proactive OAuth refresh
    configured = [
        f.strip()
        for f in (cfg.get("source_field") or "summary,description").replace(";", ",").split(",")
        if f.strip()
    ]
    locales = effective_locales(conn, target_locales)
    # Mock-egress guard (job-start refusal, better error): if this flow would
    # translate on offline Mock MT and write to a REAL Jira, refuse up front rather
    # than fetch + translate + fire the backstop mid-run. A no-op under the hermetic
    # suite (MockTransport) and when a live provider key is configured.
    try:
        egress_guard.assert_egress_allowed(conn, transport, locales)
    except egress_guard.MockEgressBlocked as e:
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "translate", False, 0, msg, actor)
        return False, msg, {}
    mode = (cfg.get("target_mode") or "comment").strip().lower()
    translate_comments = str(cfg.get("translate_comments", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    max_comments = int(cfg.get("max_comments") or 20)

    # Issue #641: field mode writes ONLY the configured target custom field, so it needs
    # one, and the refusal must come before any fetch or MT spend. The source fields are
    # never written, one target holds one language, and a target that is itself a source
    # field (or summary/description) would overwrite the text being translated.
    field_target = (cfg.get("target_field") or "").strip()
    field_target_kind = "plain"
    if mode == "field":
        field_err = None
        if not field_target:
            field_err = (
                "Field mode needs a target custom field id (e.g. customfield_10050); "
                "set it, or switch the connection to comment mode. Nothing was translated."
            )
        elif not field_target.startswith("customfield_") or field_target in configured:
            field_err = (
                f"Field mode target '{field_target}' must be a custom field that is not one "
                "of the source fields: the source summary and description are never "
                "overwritten. Nothing was translated."
            )
        elif len(locales) > 1:
            field_err = (
                "Field mode writes one language into one target field; this connection has "
                f"{len(locales)} target languages. Use comment mode, or one target locale per "
                "connection. Nothing was translated."
            )
        else:
            target_meta = jr.field_metadata()
            if field_target in target_meta:
                kind, kind_reason = _cf_schema_kind(target_meta[field_target])
            else:
                kind, kind_reason = "plain", ""  # catalogue unavailable: assume plain text
            if not kind:
                field_err = (
                    f"Field mode target {field_target} is not a text field ({kind_reason}). "
                    "Nothing was translated."
                )
            field_target_kind = kind or "plain"
        if field_err:
            _set_status(cid, ConnStatus.ERROR, field_err, synced=False)
            _log(cid, "translate", False, 0, field_err, actor)
            return False, field_err, {"translated": 0, "written": 0}

    # Per-field-type gate: keep only text-typed fields; a non-text cf is skipped
    # (guarded) with a reason rather than corrupting a select/number/date field.
    try:
        fields, skipped_fields = jr.validate_fields(configured)
    except Exception:
        fields, skipped_fields = configured, []
    if not fields:
        msg = "No translatable fields configured (all skipped by type)."
        _log(cid, "translate", False, 0, msg, actor)
        return False, msg, {"skipped_fields": [f for f, _ in skipped_fields]}

    try:
        # F1(b) + B1 (adversarial review, scope escape): pass the connection's OWN
        # configured jql as scope_jql so fetch_issues ANDs it onto BOTH paths -- the
        # issue_keys path (a signed push / an /api caller's issue_keys) AND the
        # free-query path (a caller-supplied `jql` from /api or the CLI). A request can
        # never translate an issue outside the connection's declared scope. We pass the
        # CALLER's `jql` untouched (None on the full-pull path -> fetch_issues uses the
        # scope alone); the connector owns the intersection so the guard lives at the
        # single choke point, not here.
        issues = jr.fetch_issues(
            jql=jql,
            issue_keys=issue_keys,
            fields=tuple(fields),
            cap=int(cfg.get("max_results") or 50),
            scope_jql=cfg.get("jql"),
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "translate", False, 0, msg, actor)
        return False, msg, {}
    if not issues:
        _log(cid, "translate", True, 0, "No issues matched.", actor)
        return True, "No issues matched.", {"translated": 0, "written": 0}

    # --- content-hash suppression (changelog gate) -------------------------------
    # Per (issue, locale) source content hash: unchanged source + already-seen pair
    # => suppress. The hash is authoritative; only_pairs (retry) bypasses it so a
    # failed pair is always re-attempted.
    hashes = dict((conn.config or {}).get("jira_hashes") or {})
    hash_delta: dict[str, str] = {}
    issue_chash: dict[str, str] = {}
    for iss in issues:
        # order-stable field concatenation for a per-issue source fingerprint
        parts = []
        for fld in fields:
            v = iss["texts"].get(fld) or ""
            parts.append(f"{fld}={v}")
        issue_chash[iss["key"]] = content_hash(*parts)

    # Optionally pull existing comments as extra translatable units. jr.fetch_comments
    # already excludes our own labelled replies (F1(b), see jira.JiraConnector) — the
    # self-feedback filter lives at the connector layer since it needs to inspect the
    # RAW comment body, not the plain-text unit built below.
    comment_units_by_issue: dict = {}
    comment_chash: dict[tuple, str] = {}  # (issue_key, comment_id) -> source hash
    if translate_comments:
        for iss in issues:
            try:
                cmts = jr.fetch_comments(iss["key"], cap=max_comments)
            except Exception:
                cmts = []
            usable = []
            for cm in cmts:
                txt = adf_walker.plain_text(cm.get("body")).strip()
                if txt:
                    usable.append({"id": cm["id"], "text": txt})
                    # F1(a): per-comment source hash, so an edited comment (hash
                    # changes) still re-translates but an unchanged one suppresses.
                    comment_chash[(iss["key"], cm["id"])] = content_hash(txt)
            if usable:
                comment_units_by_issue[iss["key"]] = usable

    # --- build translation units (one segment per translatable LEAF) -------------
    # A unit is (issue, field-or-comment, leaf). ADF fields expand to N leaf units;
    # plain fields are a single leaf. `raw` carries the source tree for reinsert.
    units = []
    idx = 0
    adf_source: dict = {}  # (issue, field) -> source ADF tree (for reinsert)
    for iss in issues:
        key = iss["key"]
        for fld in fields:
            if fld in iss.get("adf_fields", []):
                tree = iss["raw"][fld]
                adf_source[(key, fld)] = tree
                for leaf in adf_walker.extract_inline(tree):
                    units.append(
                        {
                            "p_index": idx,
                            "ext_id": key,
                            "field": fld,
                            "kind": "adf_leaf",
                            "leaf": leaf.p_index,
                            "text": leaf.text,
                        }
                    )
                    idx += 1
            elif fld in iss["texts"]:
                units.append(
                    {
                        "p_index": idx,
                        "ext_id": key,
                        "field": fld,
                        "kind": "plain",
                        "text": iss["texts"][fld],
                    }
                )
                idx += 1
        for cm in comment_units_by_issue.get(key, []):
            units.append(
                {
                    "p_index": idx,
                    "ext_id": key,
                    "field": f"comment:{cm['id']}",
                    "kind": "comment",
                    "text": cm["text"],
                }
            )
            idx += 1

    # #576: the #571 caps apply to a Jira run: segments, segments x locales, one huge text.
    # Refused before the Document row and before any engine call; the reason is recorded
    # on the connection and the run log.
    cap_err = seed_caps.seed_error(
        len(units), len(locales), what="This Jira run", texts=(u["text"] for u in units)
    )
    if cap_err:
        _set_status(cid, ConnStatus.ERROR, cap_err, synced=False)
        _log(cid, "translate", False, 0, cap_err, actor)
        return False, cap_err, {"translated": 0, "written": 0}

    if not units:
        # No field/comment text to translate — but attachments are an INDEPENDENT
        # opt-in surface (an issue can have only a document to localize), so still
        # run the attachment step before returning rather than bailing out on it.
        att0 = None
        if str(cfg.get("attachment_translate", "0")).strip().lower() in ("1", "true", "yes", "on"):
            att0 = _jira_translate_attachments(
                cid,
                [iss["key"] for iss in issues],
                locales=locales,
                actor=actor,
                transport=transport,
            )
        msg0 = "No translatable content."
        if att0 and (
            att0["attachments_written"]
            or att0["attachments_skipped"]
            or att0["attachments_suppressed"]
            or att0.get("attachments_untranslated")
        ):
            msg0 = (
                f"No field/comment content; attachments: wrote {att0['attachments_written']}, "
                f"skipped {att0['attachments_skipped']}, suppressed {att0['attachments_suppressed']}."  # noqa: E501
            )
            if att0.get("attachments_untranslated"):
                msg0 += (
                    f" ⚠ {att0['attachments_untranslated']} attachment/locale(s) had ALL "
                    "segments refused/flagged — NOT uploaded (left pending for re-run after review)."  # noqa: E501
                )
        ok0 = bool(att0 and att0["attachments_written"]) or not (att0 and att0["attachment_errors"])
        out0 = {"translated": 0, "written": 0}
        if att0 is not None:
            out0.update(att0)
        _log(cid, "translate", ok0, att0["attachments_written"] if att0 else 0, msg0, actor)
        return ok0, msg0, out0

    # Context harvest (SPEC-context-metadata Phase 3B): map the widened Jira metadata
    # fetch (project/issuetype/labels/summary — fetched, never translated) onto
    # Document.context. jira_translate builds ONE Document spanning ALL matched issues,
    # so _jira_harvest_fields only supplies domain/content_kind when the whole run
    # shares one value, and title/labels only for a single-issue run (see helper).
    # harvest.build gates on harvest_context + returns None when nothing is harvestable
    # -> Document.context stays None -> prompt byte-identical to a pre-harvest run.
    jira_context = harvest.build("jira", _jira_harvest_fields(issues), cfg)
    with Session(engine) as s:
        doc = _new_connector_doc(
            s,
            conn,
            units,
            f"{conn.name} translate {datetime.utcnow():%Y-%m-%d %H:%M}",
            actor,
            locales=locales,
            context=jira_context,
        )
        # one segment per (locale x unit), each at the unit's p_index
        doc_id = doc.id  # read once: a commit expires the instance
        seed_caps.add_in_chunks(
            s,
            (
                Segment(
                    document_id=doc_id,
                    target_locale=loc,
                    order_index=u["p_index"],
                    source_text=u["text"],
                    status=SegStatus.NEW,
                )
                for loc in locales
                for u in units
            ),
        )

    # Phase 2: resolve the provider PER LOCALE (spec=None) so a PER_PAIR project
    # uses each pair's own engine instead of one shared project-default spec.
    translated = 0
    try:
        with Session(engine) as s:
            for loc in locales:
                translated += translate.pretranslate_document(s, doc_id, loc).get("translated", 0)
    except providers.DeadEngineError as e:
        # Dead engine:<id> ref -> fail loud, never post mock output to Jira.
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, synced=False)
        _log(cid, "translate", False, 0, msg, actor)
        return False, msg, {"document_id": doc_id}

    with Session(engine) as s:
        segs = s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
    unit_by_order = {u["p_index"]: u for u in units}
    # by[(issue, locale)] = {"plain": {field: text},
    #                        "adf": {field: {leaf_index: text}},
    #                        "comment": {comment_id: text}}
    by: dict = {}
    # Issue #460: (issue, locale) pairs where a FIELD unit was held back (NEEDS_REVIEW or
    # an empty target). Such a pair is only partly written, so it must never record the
    # suppression hash (the held field would then never reach Jira) and its issue must
    # stay pending (#446).
    held_pairs: set = set()
    for sg in segs:
        u = unit_by_order.get(sg.order_index)
        # Defense-in-depth (QE gate, qe.py): exclude NEEDS_REVIEW segments from
        # write-back — see push_connection's guard for the full rationale.
        if not (u and sg.status != SegStatus.NEEDS_REVIEW and (sg.target_text or "").strip()):
            if u and u["kind"] != "comment":
                held_pairs.add((u["ext_id"], sg.target_locale))
            continue
        bucket = by.setdefault(
            (u["ext_id"], sg.target_locale), {"plain": {}, "adf": {}, "comment": {}}
        )
        if u["kind"] == "adf_leaf":
            bucket["adf"].setdefault(u["field"], {})[u["leaf"]] = sg.target_text
        elif u["kind"] == "comment":
            bucket["comment"][u["field"].split(":", 1)[1]] = sg.target_text
        else:
            bucket["plain"][u["field"]] = sg.target_text

    # F4: re-mint + rebuild the connector before the write phase — the fetch-phase
    # token may have expired across the MT phase; the write loop needs a fresh one.
    jr = build_fresh(cid, transport)
    written, errors = 0, []
    failures: list[dict] = []  # structured, for retry: {issue_key, locale, error}
    suppressed = 0  # fields/summary/description pairs suppressed
    comments_suppressed = 0  # F1(a): individual source comments suppressed
    # Cluster A (adversarial audit): the ceiling runs through the shared WriteBudget —
    # the SINGLE implementation, shared with dynamic_translate + the five sibling
    # connector flows. This is a WRITE-TIME cap: budget.allow() gates each would-be
    # write and budget.record() counts each SUCCESSFUL external write (field PUT /
    # comment POST), so a future change to WriteBudget.allow()/record() genuinely
    # affects this flow too (FINDING 3 — no longer a cosmetic alias). `written` mirrors
    # budget.written for the final message/log/summary.
    budget = WriteBudget()
    # Issues #446/#460: a pair is COMPLETE only when the write-budget never cut it short
    # and nothing was held back. `field_cut` blocks the suppression hash (a half-written
    # pair must be retried); `done_pairs` feeds the "which pending keys were really
    # handled" answer the pending-drain action needs.
    done_pairs: set = set()
    for (issue_key, loc), bucket in by.items():
        if budget.ceiling_hit:
            break  # F4: stop cleanly, remaining pairs picked up next run
        if only_pairs is not None and (issue_key, loc) not in only_pairs:
            continue  # retry: skip a pair that already succeeded (avoid double-post)
        field_cut = False  # the field/summary/description portion was cut by the ceiling
        any_cut = False  # anything (fields or comments) was cut by the ceiling
        # F1(a) fix (review): fields/summary/description and comments now suppress
        # INDEPENDENTLY on their own hashes. The field-level gate used to have a
        # `not bucket["comment"]` escape hatch that made ANY existing-comment
        # translation on a pair bypass field suppression too — every run re-posted
        # the same field-derived comment forever (proven 8->16 posts). Comment
        # suppression is computed per-comment-id further below; the retry-bypass
        # rule (only_pairs) still applies to both.
        chash = issue_chash.get(issue_key)
        hkey = f"{issue_key}:{loc}"
        fields_suppressed = only_pairs is None and chash and hashes.get(hkey) == chash
        if fields_suppressed:
            suppressed += 1

        # Reassemble ADF fields by reinserting translated leaves into the SOURCE
        # tree, then run the structure-integrity guard. Skipped entirely when the
        # field-level gate already suppressed this pair (nothing to reinsert into).
        adf_docs: dict = {}
        pair_ok = True
        if not fields_suppressed:
            for fld, leaf_map in bucket["adf"].items():
                src_tree = adf_source.get((issue_key, fld))
                if src_tree is None:
                    continue
                rebuilt, bad_units = adf_walker.reinsert_inline(src_tree, leaf_map)
                if bad_units or not adf_walker.structure_ok_inline(src_tree, rebuilt):
                    # keep-original fallback (DOCX/XLIFF convention): surface a
                    # per-pair error, do NOT write a structurally-diverged field.
                    errors.append(f"{issue_key}/{fld}: ADF structure guard failed — kept original")
                    failures.append(
                        {
                            "issue_key": issue_key,
                            "locale": loc,
                            "error": f"{fld}: ADF structure guard failed",
                        }
                    )
                    pair_ok = False
                    continue
                adf_docs[fld] = rebuilt

        fields_wrote = False  # tracks ONLY the field/summary/description portion
        if not fields_suppressed and not budget.ceiling_hit:
            if mode == "field":
                # Issue #641: field mode writes ONLY the configured target custom field
                # (never the source summary/description). One combined write per
                # (issue, locale): every translated source field, in configured order.
                if (issue_key, loc) in held_pairs or not pair_ok:
                    # A field is held for review or failed the structure guard: writing
                    # the rest would leave the target half-translated, so wait.
                    if (issue_key, loc) in held_pairs:
                        errors.append(
                            f"{issue_key}/{loc}: a field is held for review; target field "
                            f"{field_target} not written yet (it writes once the held text "
                            "is resolved)"
                        )
                elif not budget.allow():
                    field_cut = True
                else:
                    if field_target_kind == "adf":
                        content: list = []
                        for fld in fields:
                            if fld in bucket["plain"]:
                                content.extend(_adf_doc(bucket["plain"][fld])["content"])
                            elif fld in adf_docs:
                                content.extend(adf_docs[fld].get("content") or [])
                        payload = {"type": "doc", "version": 1, "content": content}
                        ok, m = (
                            jr.set_field_adf(issue_key, field_target, payload)
                            if content
                            else (True, "nothing")
                        )
                    else:
                        chunks = []
                        for fld in fields:
                            if fld in bucket["plain"]:
                                chunks.append(bucket["plain"][fld])
                            elif fld in adf_docs:
                                chunks.append(adf_walker.plain_text(adf_docs[fld]))
                        text_out = "\n\n".join(c for c in chunks if c)
                        ok, m = (
                            jr.set_field(issue_key, field_target, text_out)
                            if text_out
                            else (True, "nothing")
                        )
                    if ok and m != "nothing":
                        budget.record()
                        fields_wrote = True
                    elif not ok:
                        errors.append(f"{issue_key}/{field_target}: {m}")
                        failures.append(
                            {
                                "issue_key": issue_key,
                                "locale": loc,
                                "error": f"{field_target}: {m}",
                            }
                        )
            elif not budget.ceiling_hit:
                # comment mode: assemble one labelled comment from all field texts.
                # Issue #643: ADF fields contribute their rebuilt ADF blocks (paragraphs,
                # lists, tables), so the comment keeps the source layout instead of one
                # flattened paragraph. `parts` stays as the flat-text view (emptiness and
                # held-pair checks). Comments have no Jira field-length cap, so the F3
                # summary-length check does not apply on this path.
                parts = []
                blocks: list = []

                def _para(txt):
                    return _adf_doc(txt)["content"]

                for fld in ("summary", "description"):
                    if fld in bucket["plain"]:
                        parts.append(bucket["plain"][fld])
                        blocks.extend(_para(bucket["plain"][fld]))
                    elif fld in adf_docs:
                        parts.append(adf_walker.plain_text(adf_docs[fld]))
                        blocks.extend(adf_docs[fld].get("content") or [])
                for fld, txt in bucket["plain"].items():
                    if fld not in ("summary", "description"):
                        parts.append(f"{fld}: {txt}")
                        blocks.extend(_para(f"{fld}: {txt}"))
                for fld, doc_tree in adf_docs.items():
                    if fld not in ("summary", "description"):
                        parts.append(f"{fld}: {adf_walker.plain_text(doc_tree)}")
                        blocks.extend(_para(f"{fld}:"))
                        blocks.extend(doc_tree.get("content") or [])
                if parts and (issue_key, loc) in held_pairs:
                    # Gate finding (#460 follow-up): a held pair never records its hash, so
                    # posting a PARTIAL comment here would post a new duplicate comment on
                    # every later run (scheduled, webhook or "Translate pending now") until
                    # the held field is resolved. A comment is append-only: post it only
                    # once the whole pair is ready; until then the pair stays pending.
                    errors.append(
                        f"{issue_key}/{loc}: a field is held for review; translated comment "
                        "not posted yet (it posts once the held text is resolved)"
                    )
                elif parts and not pair_ok:
                    # Gate finding (jira-1010): a field failed the ADF structure guard (with
                    # #644 a real MT that drops a placeholder token trips it). The pair does
                    # not record its hash, so posting the rest now would post another partial
                    # comment on every later run. Post nothing; the guard error is already
                    # in `errors` and the pair is re-attempted next run.
                    errors.append(f"{issue_key}/{loc}: translated comment not posted")
                elif parts:
                    if not budget.allow():
                        field_cut = True  # ceiling reached — remaining pairs picked up next run
                    else:
                        ok, m = jr.post_comment(
                            issue_key, "\n\n".join(parts), label=loc, blocks=blocks
                        )
                        if ok:
                            budget.record()
                        fields_wrote = fields_wrote or ok
                        if not ok:
                            errors.append(f"{issue_key}: {m}")
                            failures.append({"issue_key": issue_key, "locale": loc, "error": m})

        # opt-in: write translated existing comments back as labelled replies.
        # F1(a) fix (review): each SOURCE comment suppresses independently on its
        # OWN hash — an unchanged comment is never re-posted, but an edited one
        # (hash differs) still re-translates, regardless of whether the pair's
        # fields were suppressed above.
        any_cut = field_cut
        for cid_key, ctxt in bucket["comment"].items():
            if budget.ceiling_hit:
                any_cut = True
                break
            comment_hkey = f"{issue_key}:comment:{cid_key}:{loc}"
            comment_source_hash = comment_chash.get((issue_key, cid_key))
            if (
                only_pairs is None
                and comment_source_hash
                and hashes.get(comment_hkey) == comment_source_hash
            ):
                comments_suppressed += 1
                continue
            if not budget.allow():
                any_cut = True
                break
            ok, m = jr.post_comment(issue_key, ctxt, label=f"{loc} (re: comment {cid_key})")
            if ok:
                budget.record()
            if not ok:
                errors.append(f"{issue_key}/comment {cid_key}: {m}")
                failures.append(
                    {"issue_key": issue_key, "locale": loc, "error": f"comment {cid_key}: {m}"}
                )
            elif comment_source_hash:
                # record this comment's own hash so it suppresses on the next run
                # even if the pair's field-level hash never changes.
                hashes[comment_hkey] = comment_source_hash
                hash_delta[comment_hkey] = comment_source_hash

        # record the FIELD-LEVEL source hash when either (a) this pair's fields
        # were already suppressed (chash unchanged — keep recording it, a no-op),
        # or (b) the field/description portion wrote cleanly this run (so a
        # partial/failed pair is re-attempted next run and by a retry). Comment
        # hashes are recorded independently above regardless of the field outcome.
        field_failed = any(
            f["issue_key"] == issue_key
            and f["locale"] == loc
            and not f["error"].startswith("comment ")
            for f in failures
        )
        # Issue #460: a pair the write ceiling cut short, or one with a field held back
        # for review, is only PARTLY written: recording its hash would suppress the
        # remainder forever. Only a fully written pair records.
        # A pair whose fields were SUPPRESSED (unchanged source, hash already recorded)
        # has nothing left to write this run, so a held field cannot pin its issue in
        # the pending queue (an install upgraded from before #460 has such hashes).
        pair_held = (issue_key, loc) in held_pairs and not fields_suppressed
        if chash and (
            fields_suppressed
            or (fields_wrote and pair_ok and not field_failed and not field_cut and not pair_held)
        ):
            hashes[hkey] = chash
            hash_delta[hkey] = chash
        if not any_cut and not pair_held and not field_failed:
            done_pairs.add((issue_key, loc))

    # Persist this run's hash delta as a MERGE (concurrency-safe; no watermark — the
    # per-(issue,locale) content hash IS the change gate). See patch_config_merge.
    patch_config_merge(cid, "jira_hashes", hash_delta)

    written = budget.written  # success counter lives on the shared budget now (field/comment)

    # Outbound attachments (SPEC-attachments Phase A, opt-in). When on, translate
    # each supported FILE attached to the fetched issues and attach the translated
    # file back alongside the original. Folded here (per SPEC "registration") so the
    # web button, the CLI `connector jira-translate`, and POST /api/.../jira/translate
    # all inherit it via the attachment_translate flag with NO new surface. It shares
    # THIS run's WriteBudget, so attachment uploads count against the same
    # MAX_WRITES_PER_RUN ceiling as field/comment writes (SPEC step 6), and reuses the
    # already-re-minted connector `jr`.
    att = None
    if str(cfg.get("attachment_translate", "0")).strip().lower() in ("1", "true", "yes", "on"):
        att = _jira_translate_attachments(
            cid,
            [iss["key"] for iss in issues],
            locales=locales,
            actor=actor,
            transport=transport,
            budget=budget,
            jr=jr,
        )

    # bool(): the `or` chain can return a COUNT (e.g. suppressed == 2), which the SyncLog
    # Boolean column rejects (StatementError) when two or more pairs are suppressed.
    ok = bool(
        written > 0
        or (att and att["attachments_written"])
        or (not errors and (suppressed or comments_suppressed or not by))
    )
    out = "field(s)" if mode == "field" else "comment(s)"
    msg = (
        f"Translated {len(issues)} issue(s) into {len(locales)} language(s); wrote {written} {out}."
    )
    if suppressed:
        msg += f" Suppressed {suppressed} unchanged pair(s)."
    if comments_suppressed:
        msg += f" Suppressed {comments_suppressed} unchanged source comment(s)."
    if att and (
        att["attachments_written"]
        or att["attachments_skipped"]
        or att["attachments_suppressed"]
        or att.get("attachments_untranslated")
    ):
        msg += (
            f" Attachments: wrote {att['attachments_written']}, "
            f"skipped {att['attachments_skipped']}, "
            f"suppressed {att['attachments_suppressed']}."
        )
        if att.get("attachments_untranslated"):
            msg += (
                f" ⚠ {att['attachments_untranslated']} attachment/locale(s) had ALL segments "
                "refused/flagged — NOT uploaded (left pending for re-run after review)."
            )
    if skipped_fields:
        msg += " Skipped fields: " + "; ".join(f"{f} ({r})" for f, r in skipped_fields[:3]) + "."
    if errors:
        msg += " Errors: " + "; ".join(errors[:3])
    if budget.ceiling_hit:
        # F4 (review, unbounded volume): loud, per-run — never a silent stop or an
        # exception. Whatever wasn't reached this run is simply picked up next run
        # (the hash gate + comment gate mean nothing already-written gets re-done).
        msg += (
            f" ⚠ Hit the per-run write ceiling ({budget.ceiling}) — stopped early; "
            "remaining issues/locales/comments will be picked up on the next run."
        )
        log.warning(
            "jira_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early",
            cid,
            budget.ceiling,
        )
    _set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, synced=ok)
    _log(cid, "translate", ok, written, msg, actor)
    # Issue #446: which of the REQUESTED issue keys must stay queued. An issue stays when
    # any of its pairs failed, was cut by the ceiling, never got a turn, or was held back,
    # and a requested key the fetch cap cut off was never fetched at all.
    retain_keys = {f["issue_key"] for f in failures}
    retain_keys.update(pair[0] for pair in by if pair not in done_pairs)
    # A held pair stays queued unless its fields were suppressed (unchanged source with
    # the hash already recorded: nothing left to write, so it must not pin the queue).
    retain_keys.update(
        k
        for (k, loc_) in held_pairs
        if not (
            only_pairs is None
            and issue_chash.get(k)
            and hashes.get(f"{k}:{loc_}") == issue_chash[k]
        )
    )
    if only_pairs is not None:
        retain_keys = {k for k in retain_keys if any(k == p[0] for p in only_pairs)}
    if issue_keys:
        fetched = {iss["key"] for iss in issues}
        if len(issues) >= int(cfg.get("max_results") or 50):
            retain_keys.update(k for k in issue_keys if k not in fetched)
    summary = {
        "retain_keys": sorted(retain_keys),
        "issues": len(issues),
        "locales": locales,
        "written": written,
        "translated": translated,
        "document_id": doc_id,
        "failures": failures,
        "suppressed": suppressed,
        "comments_suppressed": comments_suppressed,
        "skipped_fields": [f for f, _ in skipped_fields],
        **budget.summary(),
    }
    if att is not None:
        summary.update(att)
    return ok, msg, summary


def jira_count(
    cid: int,
    jql: str | None = None,
    issue_keys: list | None = None,
    actor: str = "",
    transport=None,
) -> tuple[bool, str, dict]:
    """Issue-count PREVIEW (G8): how many issues a JQL — SCOPED to the connection —
    would touch, WITHOUT translating any of them. A pure UX aid so an operator sees
    the blast radius before spending a batch run. Delegates to the connector's
    approximate_count, which builds the SAME scoped clause fetch_issues does (via
    _scoped_clause), so the count reflects only in-scope issues and a hostile
    un-wrappable caller jql is refused. Read-only: spends nothing, writes nothing,
    records no pending. Returns (ok, message, {"count": N})."""
    conn = get(cid)
    if not conn or conn.kind != "jira":
        return False, "No such Jira connection.", {}
    cfg = conn.config or {}
    jr = build_fresh(cid, transport)  # proactive OAuth refresh, same as jira_translate
    try:
        count = jr.approximate_count(jql=jql, issue_keys=issue_keys, scope_jql=cfg.get("jql"))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _log(cid, "count", False, 0, msg, actor)
        return False, msg, {}
    msg = f"~{count} issue(s) match this JQL."
    _log(cid, "count", True, count, msg, actor)
    return True, msg, {"count": count}
