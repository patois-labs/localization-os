"""tm_delivery: the three-valued gate decision (§9.1)

Documented in: docs/architecture/translation-pipeline.md
"""

from __future__ import annotations

import logging

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
)

log = logging.getLogger(__name__)

_ACTIVE_RUN_STATUSES = (
    RunStatus.PENDING,
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
)


_TERMINAL_RUN_STATUSES = tuple(
    value
    for name, value in vars(RunStatus).items()
    if name.isupper() and value not in (*_ACTIVE_RUN_STATUSES, RunStatus.SKIPPED)
)


_ACTIVE_ADVANCED_STATUSES = tuple(
    s for s in AdvancedRunStatus.ALL if s not in AdvancedRunStatus.TERMINAL
)


KIND_CLASSIC = "classic"


KIND_ADVANCED = "advanced"


KIND_BATCH = "batch"


_RUN_KIND_LABELS = {
    KIND_CLASSIC: ("run", "Run"),
    KIND_ADVANCED: ("Advanced-Workflow run", "Advanced-Workflow run"),
    KIND_BATCH: ("batch", "Batch"),
}


# In-flight run statuses (issue #105). The SAME tuple `workflows._ACTIVE_RUN_STATUSES` uses for
# "a run is still executing over this thing", re-declared here rather than imported so a
# pure-DOCUMENT install never pulls the whole workflows module in through a delivery surface
# (the P10 keys-inertness / framework-free posture). `verify_tm_empty_delivery` asserts the two
# tuples are EQUAL, so a future status added to one and not the other goes red.
# SKIPPED is StepRun-only, not a terminal WorkflowRun state for delivery purposes.
# Derive run terminality from the enum, excluding active states and that step-only state.
# The Advanced-Workflow tier's in-flight statuses (round-2 HIGH-1). DERIVED from the tier's own
# lifecycle rather than re-listed: `AdvancedRunStatus.TERMINAL` is the authority for "finished",
# so a future non-terminal status (a `queued`, a `waiting`) joins this tuple by construction and
# cannot be forgotten here -- the drift class the classic tuple above needs a ratchet to catch.
# --- the KINDS of run the empty gate calls "in flight" (round-2 HIGH-1, round-3 HIGH-1) ---
# The gate's copy says "a run", and an Advanced-Workflow mission is a run on every surface
# (`/advanced/runs`, `advanced status`, MCP `advanced_run_status`), so the predicate must see
# both tiers or the copy promises more than it enforces. Round-3 HIGH-1 adds the third class:
# a BATCH (a fan-out) is a run class in its own right, because its members are created LAZILY
# by the dispatcher, so a locale whose member has not launched yet has no `WorkflowRun` to
# find while the fan-out is unmistakably still working on it. Each kind carries TWO labels:
# the mid-sentence one the refusal message renders and the sentence-initial one the web panel
# renders, so the surfaces can name the tier without either grammar going wrong.
#: kind -> (mid-sentence label, sentence-initial label)
def run_ref_text(ref) -> str:
    """`"run #12"` / `"Advanced-Workflow run #12"` for a `(kind, id)` in-flight ref, mid-sentence.
    `""` for None, so a caller can interpolate unconditionally."""
    if not ref:
        return ""
    kind, rid = ref
    return f"{_RUN_KIND_LABELS.get(kind, _RUN_KIND_LABELS[KIND_CLASSIC])[0]} #{rid}"


def run_ref_label(ref) -> str:
    """The SENTENCE-INITIAL label for a `(kind, id)` ref (`"Run"` / `"Advanced-Workflow run"`),
    for the web review panel, which starts its sentence with it. `"Run"` for None (the
    template's pre-existing default, so an un-threaded caller renders exactly as before)."""
    if not ref:
        return _RUN_KIND_LABELS[KIND_CLASSIC][1]
    return _RUN_KIND_LABELS.get(ref[0], _RUN_KIND_LABELS[KIND_CLASSIC])[1]


PASS = "pass"  # noqa: S105 -- a gate verdict, not a password


HOLD = "hold"


RELEASE_EMPTIES = "release_empties"


MODE_DOC = "doc"


MODE_KEYS = "keys"


# --- the three-valued gate decision (§9.1) ----------------------------------------------
# not a tm_match-empty situation -> the caller's classic logic governs
# refuse (non-bypassable) -- unapproved empties on a provenanced deliverable
# a covering approval -> deliver WITH empty strings
# The mode discriminator stored in DeliveryEmptyApproval.covered["mode"] (P12).
class LocaleNotDeliverable(ValueError):
    """An approve/revoke was REFUSED (a route answers 400 instead of writing a junk row):
    * A-LOW-3/B-LOW-7 -- the locale is not a real target of the deliverable (a `zz-ZZ`
      typo, a blank, or a locale with no segments); OR
    * the target is NOT held by the delivery empty gate (no unapproved tm_match empties),
      so there is nothing to approve. This confinement is load-bearing: because a covering
      approval now RELEASES a delivery even after its durable marker is erased (the
      B-H1/B-H2 interplay), an approval must only ever be created for a genuinely-gated
      target -- otherwise approving an un-gated partial doc would silently switch it to
      empty-emit."""


class ApprovalRunInFlight(LocaleNotDeliverable):
    """An approve was REFUSED because a run is STILL EXECUTING over this deliverable
    (issue #105, the night-0906 stress flood). A `tm_match -> llm_agent` run stamps the
    durable marker the moment its tm_match step ends, so between that step and the MT
    write-back EVERY segment is a "provenanced empty" and the mint confinement above is
    trivially satisfied -- a mid-run approval minted a covering row over 126 segments the
    human could not have seen, flipped the honest 409 to RELEASE_EMPTIES, and the document
    downloaded as a 61-byte blank (HTTP 200) for the remaining ~21 minutes of the run.

    An approval is a HUMAN statement about empties that are FINAL, so it may only be minted
    once nothing is still working on them. A subclass of LocaleNotDeliverable so all four
    surfaces (web / CLI / /api / MCP-over-/api) keep answering exactly as they do for the
    other refusals -- they all render `str(exc)`."""


def _doc_target_locales(session: Session, doc_id: int) -> set:
    """The distinct target locales a document actually carries segments for -- the same set
    the download route validates against (a locale outside it is not deliverable)."""
    return set(
        session.exec(
            select(Segment.target_locale).where(Segment.document_id == doc_id).distinct()
        ).all()
    )


def _gate_enabled(session: Session) -> bool:
    """`tm_empty_delivery_gate` (default ON). The strict "==1" predicate every kill
    switch uses; a junk/blank value fails toward the DEFAULT (on)."""
    from app.services import settings

    raw = settings.get("tm_empty_delivery_gate")
    if raw is None:
        return True
    return str(raw).strip() == "1"


# ========================================================================================
# DOCUMENT mode
# ========================================================================================
def _doc_segment_targets(session: Session, doc_id: int, locale: str):
    """[(seg_id, order_index, source_text, target_text)] for the (doc, locale), ordered.
    ONE projected query -- the empties leg reuses this rather than loading ORM rows (N8)."""
    rows = session.exec(
        select(Segment.id, Segment.order_index, Segment.source_text, Segment.target_text).where(
            Segment.document_id == doc_id, Segment.target_locale == locale
        )
    ).all()
    return sorted(rows, key=lambda r: r[1] if r[1] is not None else 0)


def _doc_has_empties(session: Session, doc_id: int, locale: str) -> bool:
    """Cheap boolean: any EMPTY target among the (doc, locale) segments. Mirrors
    `held_locale_reason`'s no-segments carve-out (no segments -> not this gate's job)."""
    targets = session.exec(
        select(Segment.target_text).where(
            Segment.document_id == doc_id, Segment.target_locale == locale
        )
    ).all()
    if not targets:
        return False
    return any(not (t or "").strip() for t in targets)


def _doc_empty_ids(session: Session, doc_id: int, locale: str) -> list[int]:
    """The EMPTY-target segment ids -- the identity set for the covering-subset check
    (LAW-TM-15) and the `covered` set an approval records. Built lazily (not on the bundle
    skip path, N8)."""
    return [r[0] for r in _doc_segment_targets(session, doc_id, locale) if not (r[3] or "").strip()]


def doc_empties(session: Session, doc, locale: str) -> list[dict]:
    """The empty units for the delivery-review surfaces: the ACTUAL empty strings the maintainer's
    flag requires (source shown so a reviewer knows WHAT is untranslated), not just a
    count. Lazy -- only a rendering/approval surface calls it."""
    out = []
    for seg_id, order_index, source_text, target_text in _doc_segment_targets(
        session, doc.id, locale
    ):
        if not (target_text or "").strip():
            out.append(
                {
                    "seg_id": seg_id,
                    "order_index": order_index,
                    "source_text": source_text or "",
                }
            )
    return out


def _doc_marker(session: Session, doc_id: int, cache: dict | None = None):
    """(frozenset of target_locales with a used_tm_match run, has_wildcard_empty_locale)
    for a document -- memoized per-doc across a bundle (N8). The `""`-locale leg is
    harmless defence-in-depth (P6).

    Round-1 LOW-4: the shared bundle cache now also carries in-flight run reads, so every
    entry is NAMESPACED by a leading tag -- one dict, two memo families, no key collision."""
    if cache is not None and ("marker", doc_id) in cache:
        return cache[("marker", doc_id)]
    rows = session.exec(
        select(WorkflowRun.target_locale).where(
            WorkflowRun.document_id == doc_id,
            WorkflowRun.used_tm_match == True,  # noqa: E712
        )
    ).all()
    locs = frozenset(r for r in rows if r)
    has_wild = any((r or "") == "" for r in rows)
    result = (locs, has_wild)
    if cache is not None:
        cache[("marker", doc_id)] = result
    return result


def _doc_provenanced(session: Session, doc_id: int, locale: str, cache: dict | None = None) -> bool:
    locs, has_wild = _doc_marker(session, doc_id, cache)
    return (locale in locs) or has_wild


def _doc_approval_row(session: Session, doc_id: int, locale: str):
    """The single live approval row for (doc, locale) -- the ux_dea_doc partial unique
    index guarantees at most one (approved OR revoked)."""
    return session.exec(
        select(DeliveryEmptyApproval).where(
            DeliveryEmptyApproval.document_id == doc_id,
            DeliveryEmptyApproval.locale == locale,
        )
    ).first()


# ========================================================================================
# KEY / project mode  (LAZY keys imports only -- P10 keys-inertness)
# ========================================================================================
def _key_is_empty(segs, is_base: bool, base: dict | None) -> bool:
    """A key is UNTRANSLATED (held) for a locale when, across all three empty shapes (N1):
    (1) it has NO value row; (2) ANY of its (plural) form rows carries an empty target --
    `not all(...)`, NOT `any(...)`, so a plural key with one filled form and empty siblings
    is still held (A-H2); (3) an EMPTY_SKIP-omitted key is the same untranslated shape.
    The base-locale export serves source_text as the value, so a key with any non-empty base
    form counts as present there."""
    if is_base:
        return not any((v or "").strip() for v in (base or {}).values())
    return not segs or not all((s.target_text or "").strip() for s in segs)


def _key_empty_ids(
    session: Session,
    project,
    locale: str,
    *,
    include_archived: bool = False,
    include_hidden: bool = True,
) -> list[int]:
    """StringKey ids UNTRANSLATED for `locale`. `include_archived`/`include_hidden` are
    threaded from the export call (A-M3/B-MED-3): using a WIDER key set than the export
    would ship is FAIL-OPEN (an archived empty key would decision=PASS and ship ""), so the
    gate must see EXACTLY the export's key universe."""
    from app.services import keys as keys_svc

    proj = keys_svc._project(session, project)
    is_base = locale == proj.source_locale
    out: list[int] = []
    # Preserve the public list order while loading key objects in bounded batches.
    order = {}
    for key in keys_svc.iter_keys(
        session, proj.id, include_hidden=include_hidden, include_archived=include_archived
    ):
        base = keys_svc.base_values(session, key) if is_base else None
        segs = None if is_base else keys_svc.values_for(session, key, locale)
        if _key_is_empty(segs, is_base, base):
            order[key.id] = (key.namespace or "", key.name or "", key.context or "", key.id)
            out.append(key.id)
    return sorted(out, key=order.__getitem__)


def key_empties(
    session: Session,
    project,
    locale: str,
    *,
    include_archived: bool = True,
    include_hidden: bool = True,
) -> list[dict]:
    """The empty keys for the key delivery-review surfaces: {key_id, name, source_text}.
    Shows the untranslated key names (the maintainer's flag). Lazy.

    A2-H1: the REVIEW surfaces (web panel, /api GET, CLI `keys empties`) + the export 409
    render default to the WIDEST universe (include_archived=True, include_hidden=True) -- the
    human must SEE every empty they are approving (D15-6), and `approve_keys` covers the SAME
    widest set, so a covering approval is subset-safe for a `--archived` export too (a narrower
    default would leave an archived empty un-shown and un-covered -> a permanent 409)."""
    from app.services import key_files as key_files_svc
    from app.services import keys as keys_svc

    proj = keys_svc._project(session, project)
    is_base = locale == proj.source_locale
    out: list[dict] = []
    # Preserve the public list order while loading key objects in bounded batches.
    order = {}
    for key in keys_svc.iter_keys(
        session, proj.id, include_hidden=include_hidden, include_archived=include_archived
    ):
        base = keys_svc.base_values(session, key) if is_base else None
        segs = None if is_base else keys_svc.values_for(session, key, locale)
        if not _key_is_empty(segs, is_base, base):
            continue
        if is_base:
            source_text = next((v for v in (base or {}).values() if v), "")
        else:
            source_text = next((s.source_text for s in segs if (s.source_text or "")), "")
        order[key.id] = (key.namespace or "", key.name or "", key.context or "", key.id)
        out.append(
            {
                "key_id": key.id,
                "name": key_files_svc.qualified_name(key.namespace or "", key.name)
                if getattr(key, "namespace", "")
                else key.name,
                "source_text": source_text or "",
            }
        )
    return sorted(out, key=lambda row: order[row["key_id"]])


def _key_provenanced(session: Session, project_id: int, locale: str) -> bool:
    """Some WorkflowRun whose document is one of the project's INTERNAL key documents
    (fmt=="keys") carries the durable used_tm_match marker for this locale (or "")."""
    key_doc_ids = _key_doc_ids(session, project_id)
    if not key_doc_ids:
        return False
    return (
        session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.document_id.in_(key_doc_ids),
                WorkflowRun.used_tm_match == True,  # noqa: E712
                or_(WorkflowRun.target_locale == locale, WorkflowRun.target_locale == ""),
            )
        ).first()
        is not None
    )


def _key_approval_row(session: Session, project_id: int, locale: str):
    """The single live KEY-mode approval (document_id IS NULL) for (project, locale) --
    ux_dea_keys partial unique guarantees at most one."""
    return session.exec(
        select(DeliveryEmptyApproval).where(
            DeliveryEmptyApproval.project_id == project_id,
            DeliveryEmptyApproval.document_id.is_(None),
            DeliveryEmptyApproval.locale == locale,
        )
    ).first()


# ========================================================================================
# In-flight runs (issue #105) -- the fact BOTH the approve refusal and the held MESSAGE read
# ========================================================================================
def _key_doc_ids(session: Session, project_id: int) -> list[int]:
    """The project's INTERNAL key documents (fmt=="keys") -- the rows a key project's runs
    hang off. Shared by `_key_provenanced` and the in-flight lookup."""
    return list(
        session.exec(
            select(Document.id).where(Document.project_id == project_id, Document.fmt == "keys")
        ).all()
    )


def _classic_run_id(session: Session, doc_ids: list, locale: str) -> int | None:
    """The newest PENDING / RUNNING / PAUSED `WorkflowRun` over these documents, else None.

    ONE status query (`workflowrun` carries no index on `status` or `document_id`, so it is a
    scan bounded by the table -- honest cost, round-3 LOW-1). A run launched with an EMPTY
    target_locale is a wildcard (it covers every locale of the document), matching the
    durable-marker read, so a legacy/blank-locale run in flight still counts.

    A batch MEMBER is one of this query's rows: `batch_runs._create_member` creates each member
    as an ordinary `WorkflowRun` (status RUNNING, `batch_id` set) on the SAME `document_id`.
    The window this query cannot see -- a locale whose member has not been CREATED yet, because
    the dispatcher creates them one at a time under its concurrency semaphore -- belongs to
    `_batch_id` below, which holds on the fan-out itself (round-3 HIGH-1)."""
    return session.exec(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.document_id.in_(doc_ids),
            WorkflowRun.status.in_(_ACTIVE_RUN_STATUSES),
            or_(WorkflowRun.target_locale == locale, WorkflowRun.target_locale == ""),
        )
        .order_by(WorkflowRun.id.desc())
    ).first()


def _batch_id(session: Session, doc_ids: list, locale: str) -> int | None:
    """Newest unfinished batch covering this locale without a terminal CURRENT member.

    Members are created lazily under the dispatch semaphore, so an unlaunched locale needs
    the batch hold. A completed, failed or cancelled member releases its locale from this
    arm even while other locales have not launched, unless the batch reopened that locale.
    Reopened locales require a member newer than their recorded pre-dispatch boundary.
    Live members match the classic arm first.
    `finished_at IS NULL` is the batch lifetime authority; `_dispatch` finalizes in `finally`.
    Empty or unreadable `requested_locales` is a wildcard, still subject to member terminality.

    RING: the document set is the ring, as in the classic arm; it comes from the deliverable,
    never from a caller's project hint.

    Cost: one unfinished-batch scan (no index), plus a terminal-member query per candidate.
    Recovery advice lives in `run_ref_wait_move`; unknown dispatcher ownership stays held.
    """
    rows = session.exec(
        select(WorkflowBatch.id, WorkflowBatch.requested_locales, WorkflowBatch.reopened_members)
        .where(
            WorkflowBatch.document_id.in_(doc_ids),
            WorkflowBatch.finished_at.is_(None),
        )
        .order_by(WorkflowBatch.id.desc())
    ).all()
    for bid, requested, reopened in rows:
        boundary = reopened.get(locale, 0) if isinstance(reopened, dict) else 0
        if type(boundary) is not int:
            boundary = 0
        terminal = session.exec(
            select(WorkflowRun.id).where(
                WorkflowRun.batch_id == bid,
                WorkflowRun.target_locale == locale,
                WorkflowRun.status.in_(_TERMINAL_RUN_STATUSES),
                WorkflowRun.id > boundary,
            )
        ).first()
        if terminal is not None:
            continue
        if not isinstance(requested, list):
            return bid  # unreadable plan (NULL, or a JSON string) -> wildcard, i.e. HOLD
        planned = [str(x).strip() for x in requested if isinstance(x, str) and str(x).strip()]
        if not planned or locale in planned:
            return bid
    return None


def _advanced_run_id(
    session: Session, *, doc_id: int | None, project_id: int | None, locale: str
) -> int | None:
    """The newest non-terminal `AdvancedRun` over this deliverable, else None (round-2 HIGH-1).

    An Advanced-Workflow mission is a DIFFERENT table but the SAME fact for this gate: it is
    committed RUNNING before its harness loop starts, stays non-terminal for the whole mission,
    and -- exactly like a classic MT run -- leaves every target EMPTY until its terminal
    `deliver` writes them through `quality.commit_mt_output`. Without this arm the classic gate
    saw "no run in flight" for the whole mission, so a covering approval released and the
    document downloaded 200-with-zero-bytes: #105's literal VIOLATION outcome, one tier over.

    RING. The ONLY rows read are `AdvancedRun`s whose `project_id` IS the deliverable's own
    project -- and a project rings up to exactly one org/customer -- so this never reaches
    another ring's rows, and the document match below reads only segments of the deliverable's
    OWN document. Nothing foreign is read, named or returned; the caller gets an int id from
    its own ring or None.

    That guarantee is now kept by CONSTRUCTION, not by trusting the caller (round-3 MED-1). In
    DOCUMENT mode the ring is read off the DOCUMENT (`Document.project_id`); the `project_id`
    argument is a KEY-mode input only and is ignored here when `doc_id` is given. It used to be
    taken as the ring, so a caller passing a mismatched (document, project) pair got a FOREIGN
    project's run id back -- and `active_run_ref`'s memo, which drops `project_id` in document
    mode, then served that foreign answer to the correct caller. Deriving the ring makes both
    the guarantee and that memo key true at once: in document mode the answer is a function of
    (document, locale) alone, so no pair can be mismatched.

    THE MATCH, stated as a decision (the gate offered project-wide as the conservative option
    and roster-precise as the exact one; this takes the exact one):

    * LOCALE -- the mission's roster `target_locale`. BLANK is a wildcard, the same rule the
      classic arm gives a blank `WorkflowRun.target_locale`, so a mission whose roster carries
      no locale (the harness defaults it at drive time) holds every locale rather than none.
      ROUND-3 LOW-3, so the next reader does not file the over-block as a bug: unlike the
      classic arm's blank `target_locale` (a legacy/edge row), a blank roster locale is the
      ORDINARY shape of a SCRIPTED mission -- `cmd_advanced` writes `target_locale` into the
      roster only when `--target-locale` is passed, and a scripted mission never requires one
      (`harness` defaults it at drive time). So an ordinary scripted mission in flight holds
      EVERY locale of its project, deliberately: the wildcard is the common case here, it is
      the conservative side of a gate whose fail-open is a blank deliverable, and any terminal
      status releases it.
    * KEY mode -- ANY matching run on the project. A keys mission's units ARE the project's
      keys; there is no narrower deliverable to match.
    * DOCUMENT mode -- the run's roster must bind a segment that lives in THIS document. The
      binding source is `model_roster["units"]`, the install's own expansion (the same table
      `advanced.tools._document_units_by_key` reads, never the planner's plan JSON), so a
      mission over document B does not hold document A. A roster with NO segment-bound unit
      cannot write into ANY document -- `target.propose` refuses a document unit outside the
      roster, and a scripted row staged with `segment_id` NULL is skipped by `_deliver`'s
      document branch with a named reason -- so it holds nothing rather than holding
      everything. That is a bound on over-blocking, not a fail-open: the write it would have
      to make is refused two layers down.

    Cost (round-3 LOW-1, corrected -- the earlier "one INDEXED status query" was false):
    `advancedrun` carries no index on `project_id` or `status`, so this is a table scan
    filtered to the ring's non-terminal rows, plus one primary-key read of the document's
    project in document mode. On an install with no live Advanced run it returns after that
    scan with no rows and no roster parsing. The document-membership read and the roster JSON
    parse happen only when a candidate run exists AND binds segments."""
    if doc_id is not None:
        # DOCUMENT mode: the ring is the DOCUMENT's project, never the caller's hint (MED-1).
        pid = session.exec(select(Document.project_id).where(Document.id == doc_id)).first()
    else:
        pid = project_id
    if pid is None:
        return None
    rows = session.exec(
        select(AdvancedRun.id, AdvancedMission.model_roster)
        .join(AdvancedMission, AdvancedMission.id == AdvancedRun.mission_id)
        .where(
            AdvancedRun.project_id == pid,
            AdvancedRun.status.in_(_ACTIVE_ADVANCED_STATUSES),
        )
        .order_by(AdvancedRun.id.desc())
    ).all()
    if not rows:
        return None
    doc_segment_ids: set | None = None
    for rid, roster in rows:
        roster = roster if isinstance(roster, dict) else {}
        run_locale = str(roster.get("target_locale") or "").strip()
        if run_locale and run_locale != locale:
            continue
        if doc_id is None:
            return rid  # KEY mode: the mission's units ARE this project's keys
        bound: set = set()
        for unit in roster.get("units") or []:
            if not isinstance(unit, dict):
                continue
            sid = unit.get("segment_id")
            if sid is None:
                continue
            try:
                bound.add(int(sid))
            except (TypeError, ValueError):
                continue
        if not bound:
            continue  # binds no document; cannot write into this one (see the docstring)
        if doc_segment_ids is None:
            doc_segment_ids = set(
                session.exec(select(Segment.id).where(Segment.document_id == doc_id)).all()
            )
        if bound & doc_segment_ids:
            return rid
    return None


# ========================================================================================
# Human approve / revoke (§9.3) -- the human-authoritative decision, audited mode-aware
# ========================================================================================
def _audit_event(
    action: str, *, doc=None, project_id=None, locale: str, actor: str, detail: str = ""
):
    """One audit event with a MODE-AWARE customer_id (P7): document mode derives the
    customer from the document; key/project mode from the project (there is no document).
    Best-effort -- an audit failure never breaks the approval write."""
    try:
        from app.services import audit as _audit

        if doc is not None:
            cust = _audit.customer_for_document(doc.id)
            target = "document %s %s" % (doc.id, locale)  # noqa: UP031
        else:
            cust = _audit.customer_for_project(project_id)
            target = "project %s %s" % (project_id, locale)  # noqa: UP031
        _audit.log(
            actor=actor or "system",
            action=action,
            target=target,
            detail=detail,
            customer_id=cust,
        )
    except Exception:
        # An unaudited human approve/revoke must at least leave a log trace.
        log.warning("audit write failed for %s (%s)", action, locale, exc_info=True)


def revoke_doc(session: Session, doc, locale: str, *, approved_by: str) -> bool:
    """Revoke a DOCUMENT-mode approval (re-hold). Returns True if a live approval was
    revoked, False when there was none (a 200 no-op, N12 -- already held by the default)."""
    locale = (locale or "").strip()
    if not locale or locale not in _doc_target_locales(session, doc.id):
        raise LocaleNotDeliverable(f"locale {locale!r} is not a target locale of document {doc.id}")
    row = _doc_approval_row(session, doc.id, locale)
    if row is None or row.status != "approved":
        return False
    row.status = "revoked"
    session.commit()
    _audit_event(
        "delivery.empty_revoked",
        doc=doc,
        project_id=doc.project_id,
        locale=locale,
        actor=approved_by,
    )
    return True


def revoke_keys(session: Session, project, locale: str, *, approved_by: str) -> bool:
    """Revoke a KEY/project-mode approval (re-hold). 200 no-op when there is none (N12)."""
    from app.services import key_files as key_files_svc
    from app.services import keys as keys_svc

    proj = keys_svc._project(session, project)
    try:
        locale = key_files_svc._resolve_locale(
            session, proj, (locale or "").strip() or proj.source_locale, "revoke"
        )
    except key_files_svc.KeyFileError as exc:
        raise LocaleNotDeliverable(str(exc)) from exc
    row = _key_approval_row(session, proj.id, locale)
    if row is None or row.status != "approved":
        return False
    row.status = "revoked"
    session.commit()
    _audit_event(
        "delivery.empty_revoked",
        project_id=proj.id,
        locale=locale,
        actor=approved_by,
    )
    return True
