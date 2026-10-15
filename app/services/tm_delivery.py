"""The TM-only delivery empty gate (Batch 15 T2, SPEC-tm-match-step-config §9, D15-6).

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _ACTIVE_RUN_STATUSES, _TERMINAL_RUN_STATUSES,
_ACTIVE_ADVANCED_STATUSES, KIND_CLASSIC, KIND_ADVANCED, KIND_BATCH, _RUN_KIND_LABELS, run_ref_text,
run_ref_label, PASS, HOLD, RELEASE_EMPTIES, MODE_DOC, MODE_KEYS, LocaleNotDeliverable,
ApprovalRunInFlight, _doc_target_locales, _gate_enabled, _doc_segment_targets, _doc_has_empties,
_doc_empty_ids, doc_empties, _doc_marker, _doc_provenanced, _doc_approval_row, _key_is_empty,
_key_empty_ids, key_empties, _key_provenanced, _key_approval_row, _key_doc_ids, _classic_run_id,
_batch_id, _advanced_run_id, _audit_event, revoke_doc, revoke_keys from
app.services.tm_delivery_the_three_valued_gate_de.

The maintainer's decision D15-6: in a TM-only workflow, unmatched segments reach the final step but
must NOT deliver silently -- delivery of a deliverable that still carries EMPTY
(untranslated) targets is gated behind an explicit, audited human approval that SHOWS the
empty strings; on approval, delivery proceeds WITH the empty segments (empty strings, NOT
source-fallback).

THE GATE IS A LIVE COMPANION PREDICATE (LAW-TM-13/14), never an armed flag. The held state
is DERIVED at delivery time from three live facts:
  (1) the gate setting `tm_empty_delivery_gate` is on,
  (2) live EMPTIES remain on the deliverable, and
  (3) the deliverable is tm_match-PROVENANCED -- some `WorkflowRun` for it carries the
      DURABLE `used_tm_match=True` marker (T1, §5.1b) -- a monotonic per-run fact, NOT a
      mutable WorkflowRun->Workflow->WorkflowStep.step_type join (which a builder's
      delete_step/update_step could flip, silently un-holding or retro-holding
      deliverables -- the defect class N2 closes),
plus the absence of a covering approval. Because it is derived from the CURRENT deliverable
+ an immutable marker, there is NO fail-open path among marker-bearing (Batch-15-onward)
runs: a FAILED / CANCELLED / paused / jobs / editor-clear / import route that leaves live
empties on a marker-bearing tm_match (doc, locale) is still HELD.

It is a COMPANION consulted ALONGSIDE `export.held_locale_reason` at each delivery site
(its str|None contract UNCHANGED, N4) -- NOT a fold into it -- placed so `allow_held`
cannot bypass a HOLD (LAW-TM-14) and a covering approval OVERRIDES the classic all-empty
hold (LAW-TM-17). Because it is a companion, the Jira push guard (which calls
`held_locale_reason`) is left UNCHANGED in v1 (N6).

KEYS-INERTNESS (P10, LAW): this module is imported from the DOCUMENT delivery surfaces that
run on PURE-DOCUMENT installs, so it MUST NOT import `keys` / `key_files` at module top
level (that would arm key machinery on a doc-only install -- the exact retention.py prior
bug class). The KEY-mode branch imports them LAZILY inside itself (the retention.py
`fmt=="keys"` inline-predicate pattern). `verify_keys_inertness.py` enforces this.

FRAMEWORK-FREE (kernel-surface law): no fastapi / starlette / jinja2 import here -- this is
a pure service module re-exported through `app.kernel`.

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models import (
    AdvancedMission,
    AdvancedRun,
    AdvancedRunStatus,
    DeliveryEmptyApproval,
    Document,
    RunStatus,
    Segment,
    WorkflowBatch,
    WorkflowRun,
    _utcnow,
)
from app.services.tm_delivery_the_three_valued_gate_de import (
    _ACTIVE_ADVANCED_STATUSES,
    _ACTIVE_RUN_STATUSES,
    _RUN_KIND_LABELS,
    _TERMINAL_RUN_STATUSES,
    HOLD,
    KIND_ADVANCED,
    KIND_BATCH,
    KIND_CLASSIC,
    MODE_DOC,
    MODE_KEYS,
    PASS,
    RELEASE_EMPTIES,
    ApprovalRunInFlight,
    LocaleNotDeliverable,
    _advanced_run_id,
    _audit_event,
    _batch_id,
    _classic_run_id,
    _doc_approval_row,
    _doc_empty_ids,
    _doc_has_empties,
    _doc_marker,
    _doc_provenanced,
    _doc_segment_targets,
    _doc_target_locales,
    _gate_enabled,
    _key_approval_row,
    _key_doc_ids,
    _key_empty_ids,
    _key_is_empty,
    _key_provenanced,
    doc_empties,
    key_empties,
    revoke_doc,
    revoke_keys,
    run_ref_label,
    run_ref_text,
)

__all__ = [
    "HOLD",
    "KIND_ADVANCED",
    "KIND_BATCH",
    "KIND_CLASSIC",
    "MODE_DOC",
    "MODE_KEYS",
    "PASS",
    "RELEASE_EMPTIES",
    "_ACTIVE_ADVANCED_STATUSES",
    "_ACTIVE_RUN_STATUSES",
    "_RUN_KIND_LABELS",
    "_TERMINAL_RUN_STATUSES",
    "AdvancedMission",
    "AdvancedRun",
    "AdvancedRunStatus",
    "ApprovalRunInFlight",
    "Document",
    "LocaleNotDeliverable",
    "RunStatus",
    "Segment",
    "WorkflowBatch",
    "_advanced_run_id",
    "_audit_event",
    "_batch_id",
    "_classic_run_id",
    "_doc_approval_row",
    "_doc_empty_ids",
    "_doc_has_empties",
    "_doc_marker",
    "_doc_provenanced",
    "_doc_segment_targets",
    "_doc_target_locales",
    "_gate_enabled",
    "_key_approval_row",
    "_key_doc_ids",
    "_key_empty_ids",
    "_key_is_empty",
    "_key_provenanced",
    "doc_empties",
    "key_empties",
    "or_",
    "revoke_doc",
    "revoke_keys",
    "run_ref_label",
    "run_ref_text",
]


def run_ref_wait_move(ref) -> str:
    """One authority for waiting or recovery advice on every refusal surface.

    Manual recovery requires a confirmed-dead dispatcher process; unknown ownership stays held.
    startup recovery can finalize interrupted batches once no old worker is live."""
    if ref and ref[0] == KIND_BATCH:
        return (
            "Wait for the batch to finish; `loc-tms db recover-jobs` finalizes batches with a "
            "confirmed-dead dispatcher on this host (install-wide). Live or unknown owners "
            "are left untouched; for unknown owners, only if the server died, "
            "after confirming no worker "
            "is live, restart the server to recover interrupted batches"
        )
    return "Wait for the run to finish (or cancel it)"


def _doc_decision(session: Session, doc, locale: str, cache: dict | None) -> str:
    # ---- THE APPROVAL-RECORD DOCTRINE (both gates, round 2) --------------------------------
    # The DeliveryEmptyApproval row is ITSELF the durable provenance. Minting is confined to
    # genuinely-gated targets -- `approve_doc` refuses on a live decision of PASS, the sole
    # gate (LAW-TM-18 byte-inertness depends on it: a forged row on a NEVER-tm_match doc would
    # flip source-fill -> empty-emit; `git grep 'DeliveryEmptyApproval('` shows only the two
    # approve_* fns write rows, so forgery is out of the app-writer threat model, but a future
    # restore/import path MUST preserve this invariant). THEREFORE any row -- approved OR
    # revoked -- is proof the target WAS gated, and is checked BEFORE the (erasable) marker so
    # deleting the workflow's runs cannot silently un-hold or revoke a human's decision.
    if not _doc_has_empties(session, doc.id, locale):
        return PASS
    row = _doc_approval_row(session, doc.id, locale)
    if row is not None:
        if row.status == "approved":
            cov = row.covered or {}
            if cov.get("mode") == MODE_DOC:
                current = set(_doc_empty_ids(session, doc.id, locale))
                # ISSUE #105, ROUND-1 HIGH-1 -- THE APPROVAL IS SUSPENDED WHILE A RUN IS IN
                # FLIGHT. Closing the MINT path alone left the harm reachable with NO mid-run
                # human action: approve legitimately (no run, final empties), then run an MT
                # workflow over the same (doc, locale) -- for the whole run the download
                # answered 200 with a blank file, because the READ path still honoured a
                # covering approval over targets a live run had not reached. An approval is a
                # human statement about FINAL empties, so it is SUSPENDED (never revoked, the
                # row is untouched) for the duration of any live run and RESUMES on its own the
                # moment nothing is in flight -- covering, then, only the targets that are
                # still empty (the subset check above re-runs against the CURRENT empty set).
                # Counter-case, accepted as honest: a document under continuous/scheduled runs
                # stays unexportable while runs keep starting. That is the correct answer --
                # empties cannot ship while a run may still fill them -- and `held_message`
                # says so to the operator. Short-circuit order matters: a non-covering set never
                # pays for the in-flight query.
                if (
                    current <= set(cov.get("ids") or [])
                    and active_run_ref(
                        session,
                        doc_id=doc.id,
                        project_id=doc.project_id,
                        locale=locale,
                        cache=cache,
                    )
                    is None
                ):
                    return RELEASE_EMPTIES  # covering approval outlives the gate setting (N9)
            # Grown set / mode mismatch / suspended by a live run -> re-held (LAW-TM-15). Still
            # gated by the record.
        # A REVOKED row (B2-H1) re-HOLDS -- and restores re-approvability (the mint-confinement
        # PASS-check now sees HOLD): a revoke after the marker was erased must NOT fall through
        # to the missing marker and ship source-fill.
        return HOLD if _gate_enabled(session) else PASS
    if not _doc_provenanced(session, doc.id, locale, cache):
        return PASS  # non-tm_match deliverable, no record -> byte-identical to today (LAW-TM-3/18)
    # Provenanced empties, no covering approval.
    return HOLD if _gate_enabled(session) else PASS


def _key_decision(
    session: Session,
    project,
    locale: str,
    *,
    include_archived: bool = False,
    include_hidden: bool = True,
    cache: dict | None = None,
) -> str:
    from app.services import keys as keys_svc

    proj = keys_svc._project(session, project)
    loc = (locale or "").strip() or proj.source_locale
    empty_ids = _key_empty_ids(
        session, proj, loc, include_archived=include_archived, include_hidden=include_hidden
    )
    if not empty_ids:
        return PASS
    # The approval-record doctrine (see _doc_decision): the record IS the durable provenance,
    # checked BEFORE the marker; ANY row -- approved OR revoked -- proves the target was gated.
    row = _key_approval_row(session, proj.id, loc)
    if row is not None:
        if row.status == "approved":
            cov = row.covered or {}
            # ISSUE #105, round-1 HIGH-1 (the doc-mode twin carries the full rationale): a
            # covering approval is SUSPENDED, never revoked, while a run is still executing
            # over the project's key documents, and resumes by itself once nothing is live.
            if (
                cov.get("mode") == MODE_KEYS
                and set(empty_ids) <= set(cov.get("ids") or [])
                # ROUND-2 LOW-3: thread the bundle memo here too. The doc-mode twin has always
                # done it, so "one in-flight read for the decision + the held message" held in
                # document mode only and key mode paid for two.
                and active_run_ref(session, project_id=proj.id, locale=loc, cache=cache) is None
            ):
                return RELEASE_EMPTIES
            # Grown set / mode mismatch / suspended by a live run -> re-held (LAW-TM-15). Still
            # gated by the record.
        # A REVOKED row (B2-H1) re-HOLDS + restores re-approvability -- never falls through to
        # the (erasable) marker and ships source/empty-as un-gated.
        return HOLD if _gate_enabled(session) else PASS
    if not _key_provenanced(session, proj.id, loc):
        return PASS
    return HOLD if _gate_enabled(session) else PASS


def active_run_ref(
    session: Session,
    *,
    doc_id: int | None = None,
    project_id: int | None = None,
    locale: str = "",
    cache: dict | None = None,
) -> tuple | None:
    """`(kind, run_id)` for the run still executing over this deliverable, else None.

    THE ONE FACT the whole #105 gate rests on, now covering EVERY run class that can leave a
    target empty until its own terminal write (round-2 HIGH-1, round-3 HIGH-1):

      * a classic `WorkflowRun` -- including a fan-out's member runs, which ARE `WorkflowRun`s;
      * a still-running `WorkflowBatch` -- the fan-out ITSELF, because its members are created
        lazily, one per launch-loop turn, so a locale whose member has not been created yet
        has no `WorkflowRun` to find while the batch is plainly still working towards it;
      * an Advanced-Workflow `AdvancedRun`.

    `kind` is `KIND_CLASSIC`, `KIND_BATCH` or `KIND_ADVANCED` so every surface can NAME what is
    working -- the narrowing that made the old int-only answer invisible at the call sites,
    while `held_message` told the operator the universal rule ("empties cannot ship while a run
    may still fill them") the predicate did not keep.

    ORDER: classic, then batch, then Advanced -- most specific first. While a locale's own
    member run is live the operator is told about THAT run (`run #12`), which is the fact they
    can wait on or cancel; the batch answer (`batch #3`) is what they get for a locale the
    fan-out has not reached yet, where there is no run id to name.

    DOCUMENT mode reads the document's own runs and batches; KEY mode reads those of the
    project's internal key documents (the same universe `_key_provenanced` uses). `project_id`
    is a KEY-mode input: the classic and batch arms take the document ids, and since round-3
    MED-1 the Advanced arm derives its ring from the DOCUMENT in document mode rather than
    trusting this hint, so a caller cannot widen the ring by passing a mismatched pair.

    The classic arm is asked FIRST and short-circuits, so an install with no batches and no
    Advanced tier in use pays one extra bounded status query over `workflowbatch`.

    THREE callers read this same fact so they can never disagree: `approve_*` REFUSES on it
    (the mint path), the gate DECISION SUSPENDS a covering approval on it (the read path,
    round-1 HIGH-1), and the held-message builder NAMES the run.

    `cache` (round-1 LOW-4) memoizes the read per (doc|project, locale) across a bundle export,
    which evaluates the decision AND builds a per-document held reason: the pair now costs one
    lookup instead of two, and only on the exception path (a HELD document -- the same path
    that already loads the empties list). Keys are namespaced, so the bundle's marker cache and
    this one share a single dict."""
    locale = (locale or "").strip()
    # The memo key drops `project_id` in DOCUMENT mode, and that is now TRUE rather than
    # assumed (round-3 MED-1): every arm derives its ring from the document there, so the
    # answer is a function of (doc_id, locale) alone and a caller that threads `project_id`
    # and one that does not cannot disagree. In KEY mode `project_id` IS the identity.
    ckey = ("run", doc_id, None if doc_id is not None else project_id, locale)
    if cache is not None and ckey in cache:
        return cache[ckey]
    doc_ids = [doc_id] if doc_id is not None else _key_doc_ids(session, project_id or 0)
    ref = None
    if doc_ids:
        rid = _classic_run_id(session, doc_ids, locale)
        if rid is not None:
            ref = (KIND_CLASSIC, rid)
        if ref is None:
            bid = _batch_id(session, doc_ids, locale)
            if bid is not None:
                ref = (KIND_BATCH, bid)
    if ref is None:
        arid = _advanced_run_id(session, doc_id=doc_id, project_id=project_id, locale=locale)
        if arid is not None:
            ref = (KIND_ADVANCED, arid)
    if cache is not None:
        cache[ckey] = ref
    return ref


def active_run_id(
    session: Session,
    *,
    doc_id: int | None = None,
    project_id: int | None = None,
    locale: str = "",
    cache: dict | None = None,
) -> int | None:
    """The id of the run still executing over this deliverable, else None -- `active_run_ref`
    without the tier. Kept because the two web review panels only need the id (they render the
    label separately) and because the id is what every existing refusal string interpolates."""
    ref = active_run_ref(session, doc_id=doc_id, project_id=project_id, locale=locale, cache=cache)
    return ref[1] if ref else None


def held_message(
    session: Session,
    *,
    doc=None,
    project_id: int | None = None,
    locale: str,
    count: int,
    unit: str,
    way_out: str,
    cache: dict | None = None,
) -> str:
    """The ONE held-empties refusal sentence every delivery surface renders (issue #105).

    The seven refusal sites each hard-coded "from a TM-only workflow", which the gate's own
    contract never established: the hold keys on the durable `used_tm_match` MARKER, so a
    `tm_match -> llm_agent` workflow is held mid-run exactly like a TM-only one -- and the
    stress flood's operator was told a run that was still translating was a finished TM-only
    job, then handed the deliver-approve instruction that blanked the document. So the
    sentence now states what is TRUE: which run is still working, when one is -- and, since
    round-2 HIGH-1, which CLASS that work belongs to (`run #12` for a classic workflow run,
    `Advanced-Workflow run #12` for an Advanced mission, `batch #3` for a fan-out that has not
    launched this locale's member yet -- round-3 HIGH-1), because all three leave targets empty
    until their own terminal write.

    `way_out` is the SURFACE's own instruction (a URL, a CLI command, its own bypass note),
    appended verbatim -- the shared part is the diagnosis, not the remedy. ROUND-1 LOW-1: it is
    NOT appended on the in-flight branch. The harm chain in #105 began with a 409 printing the
    deliver-approve instruction to an operator whose run was still going; that branch now ends
    with the only moves that help, read from `run_ref_wait_move`. Live batches must finish;
    recovery is only for server death after confirming no worker is live.

    `cache` is the bundle's shared memo (round-1 LOW-4) so the decision and this message pay
    for one in-flight read between them, not two."""
    ref = active_run_ref(
        session,
        doc_id=(doc.id if doc is not None else None),
        project_id=(getattr(doc, "project_id", None) if doc is not None else project_id),
        locale=locale,
        cache=cache,
    )
    if ref is not None:
        rid = run_ref_text(ref)  # "run #12" / "Advanced-Workflow run #12" / "batch #3"
        is_batch = ref[0] == KIND_BATCH
        what = "batch" if is_batch else "run"
        move = run_ref_wait_move(ref) + ". Then export again."
        # ROUND-1 HIGH-1: the READ path now suspends a covering approval too, so this branch is
        # reached BOTH un-approved and approved. State the suspension explicitly -- an operator
        # who approved yesterday must not read this as "my approval was thrown away" -- and name
        # the continuous-run consequence rather than leaving it to be discovered.
        return (
            f"locale {locale} has {count} untranslated (empty) {unit}(s) that {rid} has "
            f"not translated yet - that {what} is STILL IN PROGRESS, so these are not final. "
            f"{move} Any standing empty-delivery "
            "approval is SUSPENDED, not revoked, while a run is in flight: it resumes on its "
            "own once nothing is running, covering the targets that are still empty then. A new "
            "approval is refused for the same reason. A deliverable under back-to-back runs "
            "stays held for as long as runs keep starting - empties cannot ship while a run "
            "may still fill them."
        )
    return (
        f"locale {locale} has {count} untranslated (empty) {unit}(s) that a tm_match workflow "
        f"left untranslated. Delivering empty targets needs an explicit human approval. "
        f"{way_out}"
    )


# ========================================================================================
# Public predicate + surface helpers
# ========================================================================================
def empty_gate_decision(
    session: Session,
    *,
    doc=None,
    project=None,
    locale: str,
    include_archived: bool = False,
    include_hidden: bool = True,
    _marker_cache: dict | None = None,
) -> str:
    """PASS / HOLD / RELEASE_EMPTIES for the (doc, locale) OR (project, locale) delivery.

    Consulted ALONGSIDE `export.held_locale_reason` at every delivery site (§9.2). A HOLD is
    NON-bypassable by `allow_held`; a RELEASE_EMPTIES makes the site skip the classic held
    check and the seam emit empty strings; a PASS leaves the caller's existing logic
    untouched. `include_archived`/`include_hidden` (KEY mode) are threaded from the export
    call so the gate's key universe MATCHES the export's -- a wider default would be
    fail-open (A-M3). `_marker_cache` (doc mode) memoizes the durable-marker read per
    document across a bundle (N8) and, since round-1 LOW-4, the in-flight read too.

    ISSUE #105 / round-1 HIGH-1: a covering approval RELEASES only while nothing is executing
    over the deliverable -- in EITHER tier (round-2 HIGH-1): a non-terminal classic
    `WorkflowRun` (a fan-out's member runs included, they are `WorkflowRun`s) or a non-terminal
    Advanced-Workflow `AdvancedRun` bound to this (project, document, locale). While one covers
    this (doc|project, locale) the approval is SUSPENDED and the decision is HOLD -- the same
    honest refusal an un-approved deliverable gets, with `held_message` naming the run and its
    TIER. The row is never touched:
    the release resumes by itself when no run is in flight, and re-evaluates the covering
    subset against the empties that are still there."""
    locale = (locale or "").strip()
    if doc is not None:
        return _doc_decision(session, doc, locale, _marker_cache)
    if project is not None:
        return _key_decision(
            session,
            project,
            locale,
            include_archived=include_archived,
            include_hidden=include_hidden,
            cache=_marker_cache,
        )
    return PASS


def covering_approval_releases(
    session: Session, doc, locale: str, _marker_cache: dict | None = None
) -> bool:
    """True when a covering approval releases the (doc, locale) empties -- consumed by
    `export.paragraph_texts_for` to resolve the deliver-empties emit mode from the
    covering-approval state itself (§9.4, no site flag). `_marker_cache` (A-LOW-1) lets the
    bundle reuse its per-document marker read instead of a fresh query per doc-locale.

    Round-1 HIGH-1: this rides the decision, so it goes False while a run is in flight -- the
    emit seam never zeroes a target a live run may still fill. Every delivery site refuses
    (HOLD) before reaching the build, so this is the second, inner fail-closed answer."""
    return (
        empty_gate_decision(session, doc=doc, locale=locale, _marker_cache=_marker_cache)
        == RELEASE_EMPTIES
    )


def approve_doc(session: Session, doc, locale: str, *, approved_by: str) -> DeliveryEmptyApproval:
    """Record (upsert) a DOCUMENT-mode approval covering the CURRENT empty set for
    (doc, locale). The ux_dea_doc partial unique index means at most one row exists, so a
    re-approval UPDATES it (growing `covered` to the new set)."""
    locale = (locale or "").strip()
    if not locale or locale not in _doc_target_locales(session, doc.id):
        raise LocaleNotDeliverable(f"locale {locale!r} is not a target locale of document {doc.id}")
    # A2-L2: compute the empty id set ONCE and reuse it for the mint-confinement check + the
    # `covered` write (no recompute / TOCTOU).
    ids = _doc_empty_ids(session, doc.id, locale)
    row = _doc_approval_row(session, doc.id, locale)
    # A2-H2 -- THE MINT CONFINEMENT (the sole gate; LAW-TM-18 byte-inertness depends on it): a
    # row may be minted ONLY for a genuinely-gated target -- live empties present AND (an
    # existing record OR the durable tm_match marker). A forged row on a never-tm_match doc
    # would flip source-fill -> empty-emit; only these two approve_* fns write rows.
    if not ids or not (row is not None or _doc_provenanced(session, doc.id, locale)):
        raise LocaleNotDeliverable(
            f"(document {doc.id}, {locale}) is not held by the delivery empty gate "
            "(no unapproved tm_match empties); there is nothing to approve."
        )
    # ISSUE #105 -- THE IN-FLIGHT REFUSAL. Mid-run, the mint confinement above is trivially
    # satisfied (tm_match stamped the marker; the MT step has not written back), so an approval
    # would cover segments that are empty ONLY because the run has not reached them yet, and the
    # honest 409 would become a silent blank export for the rest of the run. An approval is a
    # human statement about FINAL empties: refuse while anything is still executing over this
    # (document, locale). The escape hatch is whatever ENDS the thing that is running, and the
    # message names the right one per class (`run_ref_wait_move`): a cancel for a run, the
    # guarded `db recover-jobs` reconciler for a confirmed-dead dispatcher; unknown owners
    # require startup recovery after all workers stop. A batch has no cancel.
    _ref = active_run_ref(session, doc_id=doc.id, project_id=doc.project_id, locale=locale)
    _live = run_ref_text(_ref)
    if _live:
        raise ApprovalRunInFlight(
            f"(document {doc.id}, {locale}) has {_live} still in progress; its targets are "
            "empty because the run has not translated them yet, not because they are final. "
            "Approving now would cover segments no human has seen and deliver a BLANK file. "
            f"{run_ref_wait_move(_ref)}, then approve what is still empty."
        )
    run_id = session.exec(
        select(WorkflowRun.id)
        .where(WorkflowRun.document_id == doc.id)
        .order_by(WorkflowRun.id.desc())
    ).first()
    if row is None:
        row = DeliveryEmptyApproval(project_id=doc.project_id, document_id=doc.id, locale=locale)
        session.add(row)
    row.status = "approved"
    row.covered = {"mode": MODE_DOC, "ids": ids}
    row.covered_count = len(ids)
    row.approved_by = approved_by or ""
    row.approved_at = _utcnow()
    row.run_id = run_id
    session.commit()
    session.refresh(row)
    _audit_event(
        "delivery.empty_approved",
        doc=doc,
        project_id=doc.project_id,
        locale=locale,
        actor=approved_by,
        detail="covered=%d empty segment(s)" % len(ids),  # noqa: UP031
    )
    return row


def approve_keys(
    session: Session, project, locale: str, *, approved_by: str
) -> DeliveryEmptyApproval:
    """Record (upsert) a KEY/project-mode approval (document_id IS NULL) covering the
    CURRENT empty key set for (project, locale)."""
    from app.services import key_files as key_files_svc
    from app.services import keys as keys_svc

    proj = keys_svc._project(session, project)
    # B-LOW-7: resolve to the CANONICAL locale (an alias binds to its canonical form) and
    # validate against the project's active targets -- the same seam `export_locale` uses, so
    # the approval covers the locale the export will actually refuse.
    try:
        locale = key_files_svc._resolve_locale(
            session, proj, (locale or "").strip() or proj.source_locale, "approve"
        )
    except key_files_svc.KeyFileError as exc:
        raise LocaleNotDeliverable(str(exc)) from exc
    # A2-H1: build the covered set + evaluate the mint-confinement over the WIDEST key universe
    # (include_archived=True, include_hidden=True) -- a superset is subset-safe for BOTH a plain
    # and a `--archived` export, so a covering approval never leaves an archived empty un-covered
    # (which was a permanent 409). A2-L2: compute `ids` ONCE and reuse for the check + `covered`.
    ids = _key_empty_ids(session, proj, locale, include_archived=True, include_hidden=True)
    row = _key_approval_row(session, proj.id, locale)
    # A2-H2 -- THE MINT CONFINEMENT (the sole gate; LAW-TM-18 byte-inertness depends on it): a
    # row may be minted ONLY for a genuinely-gated target -- live empties present AND (an
    # existing record OR the durable tm_match marker). A forged row on a never-tm_match doc
    # would flip source-fill -> empty-emit; only these two approve_* fns write rows.
    if not ids or not (row is not None or _key_provenanced(session, proj.id, locale)):
        raise LocaleNotDeliverable(
            f"(project {proj.id}, {locale}) is not held by the delivery empty gate "
            "(no unapproved tm_match empties); there is nothing to approve."
        )
    # ISSUE #105, key mode (the doc-mode twin above carries the full rationale): a run still
    # executing over the project's key documents leaves values empty because it has not reached
    # them, so an approval minted now would ship blanks the human never saw.
    _ref = active_run_ref(session, project_id=proj.id, locale=locale)
    _live = run_ref_text(_ref)
    if _live:
        raise ApprovalRunInFlight(
            f"(project {proj.id}, {locale}) has {_live} still in progress; its values are "
            "empty because the run has not translated them yet, not because they are final. "
            "Approving now would cover keys no human has seen and deliver BLANK values. "
            f"{run_ref_wait_move(_ref)}, then approve what is still empty."
        )
    key_doc_ids = _key_doc_ids(session, proj.id)
    run_id = None
    if key_doc_ids:
        run_id = session.exec(
            select(WorkflowRun.id)
            .where(WorkflowRun.document_id.in_(key_doc_ids))
            .order_by(WorkflowRun.id.desc())
        ).first()
    if row is None:
        row = DeliveryEmptyApproval(project_id=proj.id, document_id=None, locale=locale)
        session.add(row)
    row.status = "approved"
    row.covered = {"mode": MODE_KEYS, "ids": ids}
    row.covered_count = len(ids)
    row.approved_by = approved_by or ""
    row.approved_at = _utcnow()
    row.run_id = run_id
    session.commit()
    session.refresh(row)
    _audit_event(
        "delivery.empty_approved",
        project_id=proj.id,
        locale=locale,
        actor=approved_by,
        detail="covered=%d empty key(s)" % len(ids),  # noqa: UP031
    )
    return row
