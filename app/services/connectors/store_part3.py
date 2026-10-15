"""store: part 3 (layered pure move)

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
    TransMode,
)
from app.services import audit, providers, seed_caps
from app.services.connectors import egress_guard, harvest
from app.services.connectors.base import ConnectorError, PulledItem, content_hash, fetch_cap
from app.services.connectors.store_common import (
    WriteBudget,
    _airtable_scope_target,
    _latest_doc,
    _log,
    _set_status,
    effective_locales,
    get,
    locale_is_active,
)
from app.services.connectors.store_part2 import (
    _new_connector_doc,
    _refuse_archived_sync,
    _refuse_disabled_sync,
    _with_oauth_retry,
    build_fresh,
    patch_config,
    patch_config_merge,
    primary_locale,
)

log = logging.getLogger("loc_tms.connectors")


def containers(cid: int, transport=None) -> list[dict]:
    # build_fresh (not build) so the picker works on an expired OAuth token: a
    # proactive refresh mints a valid access token before the browse call.
    conn = get(cid)
    return build_fresh(cid, transport).containers() if conn else []


def schema_fields(cid: int, base: str, table: str, transport=None) -> list[str]:
    """Field names of an Airtable table, for the picker's source-field dropdown.

    Airtable-only (only that connector exposes a schema): any other kind, or a
    missing connection, returns []. A ConnectorError (bad base/table, missing
    scope) is left to propagate so the browse route turns it into a 400 the UI
    can show — matching AirtableConnector.schema_fields, which raises rather than
    swallowing (unlike the best-effort containers())."""
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return []
    # Security review 2026-10 (connectors-02): the base/table must be the
    # connection's configured scope, never a caller-chosen base the stored
    # credential happens to reach. Refused before any network call.
    base, table, scope_err = _airtable_scope_target(conn.config or {}, base, table)
    if scope_err:
        raise ConnectorError(scope_err)
    # build_fresh (not build): proactive OAuth refresh so the field picker works on
    # an expired token.
    return build_fresh(cid, transport).schema_fields(base, table)


def pull_connection(cid: int, actor: str = "", transport=None) -> tuple[bool, str, int]:
    """Fetch source content and store it as a new Document + Segments. One pulled
    item == one segment, so push() maps back 1:1 to its external record/field.
    Segments are seeded in the connection's primary locale (explicit
    target_locale, else the first effective locale — see primary_locale)."""
    conn = get(cid)
    if not conn:
        return False, "No such connection.", 0
    _dis = _refuse_disabled_sync(cid, actor=actor, op="pull")  # ffadmin Wave 5
    if _dis is not None:
        return False, _dis, 0
    _arch = _refuse_archived_sync(cid, actor=actor, op="pull")  # M6/R1
    if _arch is not None:
        return True, _arch, 0

    skipped_pages: list = []  # #587: source pages the connector could not read this pull

    def _do_pull():
        # (ok, payload): payload is the item list on success, else the error msg.
        try:
            conn_obj = build_fresh(cid, transport)
            pulled = conn_obj.pull()
            # #579 gate: a record the fetch dropped for size would leave this ONE pulled
            # Document silently partial. Refuse the whole pull with the reason instead.
            notes = getattr(conn_obj, "fetch_notes", None) or []
            if notes:
                return False, notes[0]["error"]
            skipped_pages[:] = list(getattr(conn_obj, "skipped_pages", None) or [])
            return True, pulled
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # Generic pull gets the reactive one-shot 401 retry (see _with_oauth_retry).
    ok_pull, payload = _with_oauth_retry(cid, _do_pull, transport=transport)
    if not ok_pull:
        msg = payload
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "pull", False, 0, msg, actor)
        return False, msg, 0
    items = payload
    if not items:
        _log(cid, "pull", True, 0, "No translatable records found.", actor)
        return True, "No translatable records found.", 0

    # #576: the #571 caps apply to a pull too. Refused here, before the Document row, so
    # nothing half-written is stored; the reason lands on the connection and the run log.
    cap_err = seed_caps.seed_error(len(items), 1, what="This pull", texts=(it.text for it in items))
    if cap_err:
        _set_status(cid, ConnStatus.ERROR, cap_err, tested=True)
        _log(cid, "pull", False, 0, cap_err, actor)
        return False, cap_err, 0

    target = primary_locale(conn)
    units = [
        {
            "p_index": i,
            "text": it.text,
            "ext_id": it.external_id,
            "field": it.field,
            "title": it.title,
        }
        for i, it in enumerate(items)
    ]
    with Session(engine) as s:
        doc = _new_connector_doc(
            s,
            conn,
            units,
            f"{conn.name} sync {datetime.utcnow():%Y-%m-%d %H:%M}",
            actor,
            extra_skeleton={"mode": TransMode.SEGMENTED},
        )
        doc_id = doc.id  # read once: a commit expires the instance
        seed_caps.add_in_chunks(
            s,
            (
                Segment(
                    document_id=doc_id,
                    target_locale=target,
                    order_index=i,
                    source_text=it.text,
                    status=SegStatus.NEW,
                )
                for i, it in enumerate(items)
            ),
        )
    msg = f"Pulled {len(items)} item(s) into document #{doc_id}."
    if skipped_pages:
        # #587: a page the source would not let us read is left out of the document; say so,
        # with the count and which pages, on the run log and the connection status.
        shown = ", ".join(
            f"{p['id']} ({p['title']})" if p.get("title") else p["id"] for p in skipped_pages[:10]
        )
        more = f" and {len(skipped_pages) - 10} more" if len(skipped_pages) > 10 else ""
        msg += (
            f" Skipped {len(skipped_pages)} page(s) that could not be read: {shown}{more}."
            " They are not in this document."
        )
    _set_status(cid, ConnStatus.CONNECTED, msg, synced=True)
    _log(cid, "pull", True, len(items), msg, actor)
    return True, msg, doc_id


def push_connection(cid: int, actor: str = "", transport=None) -> tuple[bool, str, int]:
    """Collect translated segments (in the connection's primary locale — see
    primary_locale) from the connection's most recent synced document and write
    them back to the external system."""
    conn = get(cid)
    if not conn:
        return False, "No such connection.", 0
    _dis = _refuse_disabled_sync(cid, actor=actor, op="push")  # ffadmin Wave 5
    if _dis is not None:
        return False, _dis, 0
    _arch = _refuse_archived_sync(cid, actor=actor, op="push")  # M6/R1
    if _arch is not None:
        return True, _arch, 0
    target = primary_locale(conn)
    with Session(engine) as s:
        doc = _latest_doc(s, conn)
        if not doc:
            return False, "Nothing pulled yet -- pull content first.", 0
        units = (doc.skeleton or {}).get("units", [])
        segs = {
            sg.order_index: sg
            for sg in s.exec(
                select(Segment).where(
                    Segment.document_id == doc.id, Segment.target_locale == target
                )
            ).all()
        }
        items: list[PulledItem] = []
        for u in units:
            sg = segs.get(u["p_index"])
            # Exclude anything still NEEDS_REVIEW from write-back. This is LIVE and
            # LOAD-BEARING, not defense-in-depth for a future path: the refusal
            # guard (app/services/refusal.py, is_refusal()) flags a hedged/refused
            # fresh-MT output by setting SegStatus.NEEDS_REVIEW, and this exclusion
            # is what actually stops that hedge from reaching a live external
            # system via push. It also still covers the QE gate (qe.py), which
            # sets the same status. Do not delete this thinking it's inert.
            if sg and sg.status != SegStatus.NEEDS_REVIEW and (sg.target_text or "").strip():
                items.append(
                    PulledItem(
                        external_id=u["ext_id"],
                        field=u["field"],
                        text=sg.target_text,
                        title=u.get("title", ""),
                    )
                )
    if not items:
        return False, "No translated segments to push yet.", 0
    # Mock-egress guard (egress-time backstop): refuse to push if these segments were
    # produced by non-live (offline Mock) MT AND this is a real external write. A
    # no-op under the hermetic suite (MockTransport) and when a live key is set.
    try:
        egress_guard.assert_egress_allowed(conn, transport)
    except egress_guard.MockEgressBlocked as e:
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, synced=False)
        _log(cid, "push", False, 0, msg, actor)
        return False, msg, 0
    try:
        count, msg = build_fresh(cid, transport).push(items)
        ok = count > 0
    except Exception as e:
        msg, ok, count = f"{type(e).__name__}: {e}", False, 0
    _set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, synced=ok)
    _log(cid, "push", ok, count, msg, actor)
    return ok, msg, count


def dynamic_translate(
    cid: int,
    base_id: str | None = None,
    table: str | None = None,
    record_ids: list | None = None,
    source_field: str | None = None,
    jobs: list | None = None,
    actor: str = "",
    transport=None,
) -> tuple[bool, str, dict]:
    """Convention-driven translation triggered from Airtable (the webhook path).

    records come from the request; base/table may be sent too but must equal the
    connection's configured base/table (_airtable_scope_target), so a connection only
    ever touches the base it was set up for. If `jobs` (explicit target field+locale) are
    not supplied, target-language columns are auto-detected by the connection's
    naming pattern — so a user just adds a "Notes (de-DE)" column and triggers a row.

    F1 fix (review, MONEY finding): a per-run write ceiling (config.MAX_WRITES_
    PER_RUN, same knob jira_translate enforces) caps the total number of
    translated UNITS (record x target-locale) built in one run — a 500-row bulk
    paste x 3 locale columns would otherwise fire ~1500 uncapped MT calls in one
    wave. Once hit, no MORE units are built (already-built ones still translate
    and write normally); the run stops cleanly with a loud per-run message
    (summary["ceiling_hit"], summary["capped_record_ids"]) instead of an
    exception or a silent partial run — whatever was capped out is left pending
    (see consume_airtable_payloads / F2) so a later "Translate pending now" or
    the next ping picks it up.
    """
    from app.services import translate
    from app.services.connectors.airtable import detect_targets

    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "No such Airtable connection.", {}
    _dis = _refuse_disabled_sync(cid, actor=actor)  # ffadmin Wave 5 (before any egress/bookkeeping)
    if _dis is not None:
        return False, _dis, {}
    _arch = _refuse_archived_sync(cid, actor=actor)  # M6/R1
    if _arch is not None:
        return True, _arch, {"created": 0, "updated": 0, "skipped_archived": True}
    cfg = conn.config or {}
    # Security review 2026-10 (connectors-01): a request-supplied base/table must
    # match the connection's configured scope (the #268 guard the create/delete
    # siblings already use). Without it the stored credential read and wrote ANY
    # base it could reach, for any can_tune_ai caller. Refused before any egress.
    base, tbl, scope_err = _airtable_scope_target(cfg, base_id, table)
    if scope_err:
        _log(cid, "webhook", False, 0, scope_err, actor)
        return False, scope_err, {}
    src_field = (source_field or cfg.get("source_field") or "").strip()
    pattern = (cfg.get("naming_pattern") or "{src} ({loc})").strip()
    if not base or not tbl or not src_field:
        return False, "base_id, table, and source_field are required.", {}
    # Mock-egress guard (early fast-fail): refuse before any fetch/MT if EVERY
    # configured target locale is non-live and this is real egress. This is the
    # cheap up-front check for the common fully-keyless case; it is NOT authoritative
    # for PER_PAIR projects, because dynamic_translate does not write the CONFIGURED
    # locales — it writes the locales AUTO-DETECTED from the Airtable column names
    # (detect_targets), an independent set. The authoritative guard runs below once
    # those detected write-locales are known. No-op under the hermetic suite
    # (MockTransport) / when a live key is set.
    try:
        egress_guard.assert_egress_allowed(conn, transport, effective_locales(conn))
    except egress_guard.MockEgressBlocked as e:
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "webhook", False, 0, msg, actor)
        return False, msg, {}

    from app.services.connectors.airtable import (
        encode_cell,
        field_kind,
        is_select,
        is_single_select_writable,
        is_text_writable,
        select_option_names,
    )

    at = build_fresh(cid, transport)  # AirtableConnector (proactive OAuth refresh)
    # allow_select_typecast (round-2 #4): default OFF. When OFF a select target is
    # skipped-and-reported (#1's path); when ON a select value is written via typecast
    # and any NEW option is reported loudly. A plain non-empty string like "0" is OFF.
    allow_select = str(cfg.get("allow_select_typecast") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # Field-id rename-proofing (round-2 #5): the stored name->id map from prior runs,
    # merged UNDER the freshly-resolved ids (fresh wins if a name is reused for a NEW
    # field). Additive + reversible — a live connection with only names keeps working
    # by name and GAINS ids on the next sync; clearing the map falls back to names.
    stored_field_ids: dict = dict((conn.config or {}).get("airtable_field_ids") or {})

    # Full field schema (BEST-EFFORT): {name: {id,type,options}} gives the write-target
    # TYPE for validation (#1/#4), the field ID for rename-proofing (#5), and select
    # option choices for new-option detection (#4). The meta API needs schema.bases:read
    # and a webhook payload carries no types, so a failure degrades to name-based,
    # type-unknown handling (is_text_writable(None) -> True: attempt the write, a real
    # 400 still surfaces) — never aborting the run when jobs are already known. When
    # jobs must be AUTO-DETECTED, the schema is required (we can't detect columns
    # without it), so a failure there is a hard error (the outer except).
    field_types: dict = {}
    field_ids: dict = dict(stored_field_ids)
    skipped_fields: list = []
    try:
        detailed: dict = {}
        if not jobs:
            detailed = at.schema_fields_detailed(base, tbl)  # REQUIRED to detect columns
        else:
            try:
                detailed = at.schema_fields_detailed(base, tbl)
            except Exception:
                detailed = {}
        field_types = {n: d.get("type") for n, d in detailed.items()}
        fresh_field_ids = {n: d.get("id") for n, d in detailed.items() if d.get("id")}
        field_ids = {**stored_field_ids, **fresh_field_ids}

        raw_jobs = (
            [
                {"target_field": f, "locale": loc}
                for f, loc in detect_targets(src_field, list(detailed.keys()), pattern)
            ]
            if not jobs
            else list(jobs)
        )
        # Connected projects: a project with a content source decides its own languages. A
        # column whose language is not on the project's active list (stopped, or never added)
        # is not translated or written; it is reported like any other skipped column.
        _kept_jobs = []
        for _j in raw_jobs:
            if locale_is_active(conn, _j.get("locale")):
                _kept_jobs.append(_j)
            else:
                skipped_fields.append(
                    {
                        "field": _j.get("target_field"),
                        "type": field_types.get(_j.get("target_field")),
                        "locale": _j.get("locale"),
                        "reason": (
                            f"language {_j.get('locale')!r} is not an active language of the "
                            "project (stopped or not added), so its column was not written."
                        ),
                    }
                )
        raw_jobs = _kept_jobs

        # #1/#4: classify every target by field TYPE before any write is attempted.
        # Text -> written normally; select + opt-in -> typecast write (option choices
        # captured for new-option reporting); select w/o opt-in OR any other non-text
        # type -> skipped and REPORTED with a distinct, field-specific reason.
        classified: list = []
        for j in raw_jobs:
            f = j.get("target_field")
            loc = j.get("locale")
            ftype = field_types.get(f)
            if is_select(ftype):
                if allow_select and is_single_select_writable(ftype):
                    classified.append(
                        {
                            "target_field": f,
                            "locale": loc,
                            "typecast": True,
                            "existing_options": select_option_names(detailed.get(f)),
                        }
                    )
                elif not is_single_select_writable(ftype):
                    # LOW-3: multipleSelects is not writable in v1 (typecast expects an
                    # array, not our single translated string) — skip-report regardless
                    # of the opt-in, so a wrong-shape write never reaches a real base.
                    skipped_fields.append(
                        {
                            "field": f,
                            "type": ftype,
                            "locale": loc,
                            "reason": (
                                f"field {f!r} is type {ftype}; multi-select typecast "
                                "writes are not supported in this version and were "
                                "skipped (single-select is supported)."
                            ),
                        }
                    )
                else:
                    skipped_fields.append(
                        {
                            "field": f,
                            "type": ftype,
                            "locale": loc,
                            "reason": (
                                f"field {f!r} is type {ftype}, a select column not "
                                "written by default; set allow_select_typecast=1 to "
                                "write it (creates a new option when the value is new)."
                            ),
                        }
                    )
            elif is_text_writable(ftype):
                classified.append({"target_field": f, "locale": loc, "typecast": False})
            else:
                skipped_fields.append(
                    {
                        "field": f,
                        "type": ftype,
                        "locale": loc,
                        "reason": (
                            f"field {f!r} is type {ftype or 'unknown'}, not writable as "
                            "translated text; rename it to a text column or drop it "
                            "from the naming pattern."
                        ),
                    }
                )
        jobs = classified

        if not jobs:
            parts = [f"No writable target-language column for {src_field!r} (pattern {pattern!r})."]
            if skipped_fields:
                parts.append("Skipped: " + "; ".join(s["reason"] for s in skipped_fields[:5]))
            msg = " ".join(parts)
            _log(cid, "webhook", True, 0, msg, actor)
            return (
                True,
                msg,
                {"translated": 0, "pushed": 0, "targets": 0, "skipped_fields": skipped_fields},
            )
        src_field_id = field_ids.get(src_field)
        use_ids = bool(field_ids)
        records = at.fetch(
            base,
            tbl,
            record_ids=record_ids,
            cap=fetch_cap(cfg.get("max_records"), 500),
            by_field_id=use_ids,
            fields=[
                field_ids.get(name, name)
                for name in [src_field, *(j["target_field"] for j in jobs), cfg.get("title_field")]
                if name
            ],
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "webhook", False, 0, msg, actor)
        return False, msg, {}

    # Persist the merged field-id map (additive; skipped when nothing resolved or the
    # map is unchanged, so a name-only connection stays byte-identical).
    if field_ids and field_ids != stored_field_ids:
        patch_config(cid, {"airtable_field_ids": field_ids})

    src_kind = field_kind(field_types.get(src_field))

    # Write-back loop-guard (item 4): a per-(record, target_field) content-hash of
    # what WE last wrote, merge-persisted under `airtable_written_hashes`. If an
    # incoming source text — once translated — would reproduce a target cell we
    # already wrote, that (record, field) is an ECHO of our own write and is
    # suppressed. The official-webhook path (Phase 2) already excludes source
    # `publicApi`, so this is the CHEAP defense-in-depth backstop for ANY trigger
    # path (the convention endpoint, a manual re-fire) that lacks source tagging —
    # mirrors the zendesk/document360 _content_hash poll-suppression pattern.
    #
    # HONEST SCOPE (not a manual-edit lock): this guard is keyed on the SOURCE hash,
    # not the target cell's content, so it cannot detect or protect a human's
    # hand-edit of the TARGET cell — a re-fire over the SAME unchanged source will
    # not re-write that cell (the guard sees an echo and skips it), so a manual
    # target fix "survives" only incidentally, for as long as: (a) the source cell
    # stays byte-identical, AND (b) `airtable_written_hashes` for that
    # (record, target_field) key is never cleared, AND (c) the target field is not
    # renamed/recreated (a new field name is a new guard key with no stored hash,
    # so the next run treats it as unwritten and overwrites it). Any edit to the
    # SOURCE cell — even a single character — produces a new hash, is treated as
    # "genuinely new content," and OVERWRITES the human's target edit on the next
    # run. There is no separate manual-edit lock in Phase 1; if that's needed, the
    # target write would have to be gated on the target cell's own last-known hash
    # too (out of scope here — flagged for a future phase).
    written_hashes: dict = dict((conn.config or {}).get("airtable_written_hashes") or {})

    # F1 fix (review, MONEY finding): dynamic_translate had NO write ceiling —
    # jira_translate caps total writes at config.MAX_WRITES_PER_RUN but the
    # airtable paid path didn't reference it at all, so a 500-row bulk paste
    # x 3 locale columns could fire ~1500 uncapped MT calls in one wave. Cap the
    # total number of TRANSLATED UNITS (record x locale) built this run at the
    # same per-run ceiling, mirroring jira_translate's ceiling+loud-message shape.
    # Whatever doesn't fit is simply never built as a unit this run — its record
    # stays (or is added back to, see F2 below) `airtable_pending_records` so a
    # later "Translate pending now" / next ping picks it up. This is a BUILD-TIME
    # cap (not a write-time cap like jira's) because the expensive, billable step
    # here is the MT call during pretranslate, not the eventual Airtable PATCH —
    # capping units built directly caps MT spend.
    #
    # Cluster A (adversarial audit): the ceiling now runs through the shared
    # WriteBudget so this flow, jira_translate, and the five sibling connector flows
    # share ONE implementation (allow()/record() here counts each unit built as one
    # future external write). `write_ceiling`/`ceiling_hit` below are read off the
    # budget so the structured summary keeps the exact keys tests assert on.
    budget = WriteBudget()
    write_ceiling = budget.ceiling
    capped_record_ids: set = set()

    # Build a transient document: one segment per (record × target locale). The
    # source text is ENCODED through the airtable codec (mention + rich-text markup
    # shielding) so the MT engine never sees a mention token or Markdown marker; the
    # per-unit ctx is kept to decode the MT output back before write-back.
    units, seeds = [], []
    # #4: select-option sets are kept OUT of the (JSON-serialized) unit dicts — a set
    # is not JSON-safe and the units land in the document skeleton. Side-mapped by the
    # unit's p_index, consumed in-memory during write-back below.
    select_existing: dict = {}
    idx = 0
    for rec in records:
        recf = rec.get("fields", {}) or {}
        # #5: prefer the stable field ID (returnFieldsByFieldId response), fall back
        # to the field NAME (a name-keyed / degraded response, or no id resolved).
        raw = recf.get(src_field_id) if (use_ids and src_field_id) else None
        if raw is None:
            raw = recf.get(src_field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        enc_text, ctx = encode_cell(raw, src_kind)
        for job in jobs:
            # Loop-guard: if we already wrote this record/field and the SOURCE that
            # produced our stored target is byte-identical to the incoming source,
            # skip — an echo of our own write. Keyed on the source hash so a genuine
            # new source edit (different hash) is never suppressed.
            guard_key = f"{rec['id']}:{job['target_field']}"
            if written_hashes.get(guard_key) == content_hash(raw):
                continue
            if not budget.allow():
                # F1: stop building NEW units once the ceiling is hit — this
                # record's remaining (or not-yet-seen) target locales are left
                # untranslated this run. Record it so the caller can keep it
                # pending rather than silently dropping the remainder.
                capped_record_ids.add(rec["id"])
                continue
            budget.record()
            # #5: write by field ID when resolved (rename-proof across the MT window),
            # else by name. Guard/hash keys stay on the NAME (stable config identity).
            tgt_id = field_ids.get(job["target_field"])
            write_field = tgt_id if (use_ids and tgt_id) else job["target_field"]
            if job.get("typecast"):
                select_existing[idx] = job.get("existing_options") or set()
            units.append(
                {
                    "p_index": idx,
                    "text": enc_text,
                    "ext_id": rec["id"],
                    "field": job["target_field"],
                    "write_field": write_field,
                    "locale": job["locale"],
                    "title": raw[:60],
                    "ctx": ctx,
                    "src_raw": raw,
                    "typecast": job.get("typecast", False),
                }
            )
            seeds.append((idx, rec["id"], job["target_field"], job["locale"], enc_text))
            idx += 1
    ceiling_hit = budget.ceiling_hit
    # #576: a webhook run is also capped by the #571 limits (MAX_WRITES_PER_RUN is the
    # per-run spend ceiling, an operator can raise it past them). Checked before the
    # Document row and before any engine call.
    cap_err = seed_caps.seed_error(len(units), 1, what="This run", texts=(u["text"] for u in units))
    if cap_err:
        _set_status(cid, ConnStatus.ERROR, cap_err, tested=True)
        _log(cid, "webhook", False, 0, cap_err, actor)
        return False, cap_err, {}
    if not units:
        msg = f"No records had (new) text in {src_field!r}."
        if ceiling_hit:
            msg += (
                f" ⚠ Hit the per-run write ceiling ({write_ceiling}) before "
                "building any unit — remaining records stay pending."
            )
            log.warning(
                "dynamic_translate: connection %s hit MAX_WRITES_PER_RUN=%s "
                "before any unit was built",
                cid,
                write_ceiling,
            )
        _log(cid, "webhook", True, 0, msg, actor)
        return (
            True,
            msg,
            {
                "translated": 0,
                "pushed": 0,
                "targets": len(jobs),
                "ceiling_hit": ceiling_hit,
                "write_ceiling": write_ceiling,
                "capped_record_ids": sorted(capped_record_ids),
            },
        )

    # Context harvest (Phase 3A): the Airtable TABLE name is a real in-hand name
    # (from the request/config), not an opaque id -> source.section. None when
    # harvest is off -> Document.context stays None (byte-identical prompt).
    at_context = harvest.build("airtable", {"section": tbl}, cfg)
    with Session(engine) as s:
        doc = _new_connector_doc(
            s,
            conn,
            units,
            f"{conn.name} webhook {datetime.utcnow():%Y-%m-%d %H:%M}",
            actor,
            locales=[job["locale"] for job in jobs],
            extra_skeleton={"dynamic": True, "mode": TransMode.SEGMENTED},
            context=at_context,
        )
        # one segment per unit, in the unit's OWN locale, at its p_index
        doc_id = doc.id  # read once: a commit expires the instance
        seed_caps.add_in_chunks(
            s,
            (
                Segment(
                    document_id=doc_id,
                    target_locale=locale,
                    order_index=i,
                    source_text=text,
                    status=SegStatus.NEW,
                )
                for i, ext_id, field, locale, text in seeds
            ),
        )

    # Mock-egress guard (AUTHORITATIVE, PER_PAIR leak fix): the locales below are the
    # ACTUAL write locales — auto-detected from the Airtable column NAMES
    # (detect_targets), an independent set from the connection's configured
    # target_locales the EARLY guard above checked. In PER_PAIR mode these diverge: a
    # configured `fr-FR` (live) would pass the early check while a detected `de-DE`
    # column (keyless -> MockProvider) still got written with mock `[xx]` text. This
    # runs AFTER _new_connector_doc (which _ensure_project'd the connection, so the
    # per-pair spec resolves against the real project) and BEFORE any MT/write, so the
    # run is blocked as a whole if ANY column it would write resolves to a non-live
    # provider. (No union with the configured set: this IS the exact write set.)
    locales = sorted({u["locale"] for u in units})
    try:
        # Re-fetch: _new_connector_doc just _ensure_project'd this connection, so the
        # fresh row carries the project_id the per-pair resolution needs.
        egress_guard.assert_egress_allowed(get(cid), transport, locales)
    except egress_guard.MockEgressBlocked as e:
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, tested=True)
        _log(cid, "webhook", False, 0, msg, actor)
        return False, msg, {}

    # Translate each target locale (TM-first, glossary-aware, project engine).
    # Phase 2: resolve the provider PER LOCALE (spec=None) so a PER_PAIR project
    # uses each pair's own engine — a single shared spec would wrongly pin every
    # locale to one provider.
    translated = 0
    try:
        with Session(engine) as s:
            for loc in locales:
                stats = translate.pretranslate_document(s, doc_id, loc)
                translated += stats.get("translated", 0)
    except providers.DeadEngineError as e:
        # Dead engine:<id> ref -> fail loud, never write mock output back.
        msg = str(e)
        _set_status(cid, ConnStatus.ERROR, msg, synced=False)
        _log(cid, "webhook", False, 0, msg, actor)
        return False, msg, {"document_id": doc_id}

    # Gather translations and write back, merged per record.
    with Session(engine) as s:
        segs = {
            sg.order_index: sg
            for sg in s.exec(select(Segment).where(Segment.document_id == doc_id)).all()
        }
    # LOW-1 (review): typecast is a REQUEST-level Airtable flag, so a single PATCH
    # with typecast:true would coerce EVERY field in it — a marginal risk for
    # email/url/phoneNumber targets riding alongside a select. Partition the writes:
    # plain text/rich fields go in a typecast:FALSE PATCH; opted-in select fields go
    # in their OWN typecast:TRUE PATCH. A record with both simply gets two PATCHes
    # (Airtable merges field writes), so a non-select target never rides typecast.
    updates_plain: dict[str, dict] = {}
    updates_select: dict[str, dict] = {}
    # Track (source, is_select) per written cell so the loop-guard hash is persisted
    # only when THAT field's pass actually landed (field-level, not record-level).
    written_src: dict[str, tuple] = {}
    # #4: every opted-in select value that isn't an EXISTING option is reported LOUDLY
    # (a permanent, hard-to-undo mutation of the live base — the operator must see it,
    # and a bad MT can never silently mint one because the rail is opt-in per conn).
    new_select_options: list = []
    for u in units:
        sg = segs.get(u["p_index"])
        # Defense-in-depth (QE gate, qe.py): exclude NEEDS_REVIEW segments from
        # write-back — see push_connection's guard for the full rationale.
        if sg and sg.status != SegStatus.NEEDS_REVIEW and (sg.target_text or "").strip():
            # DECODE the MT output back through the codec: restore rich-text markup
            # and mention tokens the encode step shielded. A faithful engine gives a
            # byte-identical restore; a mangling engine that dropped a sentinel
            # leaves it visible rather than crashing (decode_cell is total).
            from app.services.connectors.airtable import decode_cell

            decoded = decode_cell(sg.target_text, u.get("ctx") or {})
            is_sel = bool(u.get("typecast"))
            target_map = updates_select if is_sel else updates_plain
            target_map.setdefault(u["ext_id"], {})[u["write_field"]] = decoded
            written_src[f"{u['ext_id']}:{u['field']}"] = (u.get("src_raw") or "", is_sel)
            if is_sel and decoded not in (select_existing.get(u["p_index"]) or set()):
                new_select_options.append(
                    {
                        "record_id": u["ext_id"],
                        "field": u["field"],
                        "locale": u["locale"],
                        "value": decoded,
                    }
                )
                log.warning(
                    "airtable dynamic_translate: connection %s creating a NEW "
                    "select option %r in field %r (record %s, locale %s) via "
                    "typecast",
                    cid,
                    decoded,
                    u["field"],
                    u["ext_id"],
                    u["locale"],
                )
    # F4: the OAuth access token minted for the fetch phase can expire during the
    # (potentially minutes-long) MT phase above. Re-mint and REBUILD the connector
    # for the write so the write-back uses a fresh token, not the stale one the
    # fetch instance still holds. (Token connections: build_fresh is a no-op refresh.)
    at = build_fresh(cid, transport)
    failed_plain: set = set()
    failed_select: set = set()
    push_msgs: list = []
    try:
        if updates_plain:
            _n, _f, _m = at.patch_records(base, tbl, updates_plain, typecast=False)
            failed_plain = set(_f)
            push_msgs.append(_m)
        if updates_select:
            _n, _f, _m = at.patch_records(base, tbl, updates_select, typecast=True)
            failed_select = set(_f)
            push_msgs.append(_m)
    except Exception as e:
        push_msgs.append(f"{type(e).__name__}: {e}")
        failed_plain = set(updates_plain)  # whole write failed -> every record unwritten
        failed_select = set(updates_select)
    attempted_rids = set(updates_plain) | set(updates_select)
    # A record is FULLY written when it failed in neither pass it was part of.
    failed_ids = sorted((failed_plain | failed_select) & attempted_rids)
    written_rids = attempted_rids - failed_plain - failed_select
    n = len(written_rids)
    ok = n > 0
    push_msg = "; ".join(m for m in push_msgs if m) or "Nothing to write."

    # Loop-guard persist (item 4): record a per-(record, field) hash of the SOURCE
    # that produced each SUCCESSFULLY-written cell, merge-persisted so a concurrent
    # run never clobbers another's entries (patch_config_merge discipline). A cell
    # whose PASS failed for its record is NOT recorded (its write didn't land, so a
    # re-fire SHOULD retry it) — field-level, so a record that succeeded in the plain
    # pass but failed the select pass still records only the plain cell's hash.
    hash_delta = {}
    for key, (src, is_sel) in written_src.items():
        rid = key.split(":", 1)[0]
        if (is_sel and rid in failed_select) or ((not is_sel) and rid in failed_plain):
            continue
        hash_delta[key] = content_hash(src)
    if hash_delta:
        patch_config_merge(cid, "airtable_written_hashes", hash_delta)

    # Structured failures for retry: any record whose write-back failed, plus any
    # record that produced no translated cell at all (attributed generically).
    failures = [{"record_id": rid, "error": "write-back failed"} for rid in failed_ids]
    attempted = {u["ext_id"] for u in units}
    for rid in sorted(attempted - attempted_rids):
        failures.append({"record_id": rid, "error": "no translation produced"})

    summary = {
        "document_id": doc_id,
        "records": len(attempted_rids),
        "targets": len(jobs),
        "translated": translated,
        "pushed": n,
        "locales": locales,
        "failures": failures,
        "ceiling_hit": ceiling_hit,
        "write_ceiling": write_ceiling,
        "capped_record_ids": sorted(capped_record_ids),
        "skipped_fields": skipped_fields,
        "new_select_options": new_select_options,
    }
    _set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, push_msg, synced=ok)
    _log(
        cid,
        "webhook",
        ok,
        n,
        f"{len(attempted_rids)} record(s) × {len(jobs)} locale(s); {push_msg}",
        actor,
    )
    full = (
        (f"Translated {translated} cell(s) into {len(locales)} language(s) across {n} record(s).")
        if ok
        else f"Translated, but write failed: {push_msg}"
    )
    if new_select_options:
        # #4: LOUD — a new permanent option was minted in the live base per typecast.
        full += (
            f" WARNING: created {len(new_select_options)} new select option(s): "
            + "; ".join(f"{o['value']!r} in {o['field']}" for o in new_select_options[:5])
            + "."
        )
    if skipped_fields:
        # #1: non-text / opt-out select targets reported, never an opaque failure.
        full += f" Skipped {len(skipped_fields)} non-writable target(s): " + "; ".join(
            s["reason"] for s in skipped_fields[:5]
        )
    if ceiling_hit:
        # F1: loud, per-run — never a silent stop. Whatever wasn't built this run
        # (capped_record_ids) is picked up next run (F2 keeps it pending).
        full += (
            f" ⚠ Hit the per-run write ceiling ({write_ceiling} unit(s)) — stopped "
            "early; remaining record(s)/locale(s) will be picked up on the next run."
        )
        log.warning(
            "dynamic_translate: connection %s hit MAX_WRITES_PER_RUN=%s, stopped early "
            "(%d record(s) left pending)",
            cid,
            write_ceiling,
            len(capped_record_ids),
        )
    return ok, full, summary


def airtable_create_records(
    cid: int,
    rows: list,
    base_id: str | None = None,
    table: str | None = None,
    typecast: bool = False,
    actor: str = "",
    transport=None,
) -> tuple[bool, str, dict]:
    """Create new Airtable record(s). rows = [{field: value}, ...]. Returns
    (ok, message, summary) where summary carries created ids / failures and the
    write-ceiling telemetry. Never raises for an HTTP failure (surfaced in msg)."""
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "No such Airtable connection.", {}
    cfg = conn.config or {}
    base, tbl, scope_err = _airtable_scope_target(cfg, base_id, table)
    if scope_err:
        return False, scope_err, {}
    if not base or not tbl:
        return False, "base_id and table are required to create records.", {}
    clean = [r for r in (rows or []) if isinstance(r, dict) and r]
    if not clean:
        return False, "No record fields provided to create.", {}
    # WriteBudget: cap the number of rows CREATED this run (one create = one write).
    budget = WriteBudget()
    allowed = budget.trim(len(clean))
    to_create = clean[:allowed]
    if not to_create:
        return (
            False,
            (f"Write ceiling ({budget.ceiling}) is 0 — nothing created."),
            {"created": [], "ceiling_hit": True, "write_ceiling": budget.ceiling},
        )
    at = build_fresh(cid, transport)
    try:
        created, failed, msg = at.create_records(base, tbl, to_create, typecast=typecast)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    ok = bool(created)
    created_ids = [rec.get("id") for rec in created if rec.get("id")]
    summary = {
        "created_ids": created_ids,
        "created": len(created),
        "failed": len(failed),
        "failures": failed,
        "ceiling_hit": budget.ceiling_hit,
        "write_ceiling": budget.ceiling,
    }
    if budget.ceiling_hit:
        msg += (
            f" (write ceiling {budget.ceiling} reached — {len(clean) - allowed} "
            "row(s) not attempted this run)"
        )
    _set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, synced=False)
    _log(cid, "create", ok, len(created), msg, actor)
    audit.log(
        actor=actor or "system",
        action="connector.airtable.create_records",
        target=f"connection:{cid}",
        detail=f"base:{base} table:{tbl} created:{len(created)}",
        customer_id=getattr(conn, "customer_id", None),
    )
    return ok, msg, summary
