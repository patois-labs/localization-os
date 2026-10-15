"""keys: second pass part 2 (layered pure move)

Documented in: docs/architecture/string-keys.md
"""

from __future__ import annotations

import time

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import (
    KeyChangeLog,
    KeyEntity,
    KeyOp,
    Project,
    RunStatus,
    StringKey,
)
from app.services.keys_common import (
    _audit,
    _key_order,
    _key_statement,
    _key_target,
    _mock_fallback_workflow,
    _next_order_index,
    _utcnow,
    active_locales,
)
from app.services.keys_hub import (
    CLDR_CATEGORIES,
    DEFAULT_DELTA_LIMIT,
    MAX_DELTA_LIMIT,
    BadTimestampError,
    KeyBudgetError,
    KeyServiceError,
    TranslationEngineError,
    _babel_locale,
    _gettext_cache,
    _log,
    _project,
    changelog_floor,
    key_views,
    parse_modified_after,
    plural_categories_for,
    translatable_documents,
    trash_document,
    values_for,
)
from app.services.keys_part2 import _enrich_value_entries


def gettext_categories_for(locale: str, plural_expr: str = "", num_plurals: int = 0) -> list[str]:
    """The CLDR category each gettext `msgstr[N]` index means, INDEX-ORDERED.

    gettext addresses plural forms by a NUMERIC INDEX chosen by the C expression in a
    .po file's `Plural-Forms:` header; CLDR addresses them by NAME. We store names
    (spec 4: "store CLDR names, never gettext indices"), so a .po round trip needs
    this map -- and it must come from the ACTUAL expression, because the two systems
    genuinely disagree for some locales (modern CLDR gives Spanish a `many` category
    that gettext's `nplurals=2; plural=(n != 1)` has no slot for).

    Method: evaluate BOTH rules over a probe series and collect, for each index, the
    SET of CLDR categories that land on it. `gettext.c2py` (stdlib) is the same
    evaluator GNUTranslations uses on real .mo files, and Babel supplies the CLDR side,
    so neither half is hand-rolled. `plural_expr`/`num_plurals` override the locale's
    defaults with the values the FILE declared (a real .po is authority over our table
    for its own contents).

    NAMING A SLOT THAT COLLECTS MORE THAN ONE CATEGORY. Only the expression's
    FALL-THROUGH slot -- the `else` branch, identified as the slot the probe series
    lands on most often -- may be called "other", the CLDR catch-all (review B19).
    That is what a REDUCED header needs: a hand-written 2-form Russian catalog
    (`nplurals=2; plural=(n != 1)`) has slot 1 swallowing few + many + one at once, and
    calling it "many" would silently file a translation of "5 files" under a category a
    3-form export then renders in the wrong place.

    Any OTHER multi-category slot is NOT a catch-all, and naming it "other" was a
    regression that cost Maltese a translation (re-review R1). mt's gettext expression
    sends `n == 0 || n%100 in 1..10` to slot 1, which collects {few, two, other} -- but
    slot 3 is the real fall-through, so both were called "other", the two collided, and
    the collision resolved first-wins and SILENTLY: an import stored slot 1's text as
    `other` and lost slot 3's, and the export then wrote slot 1's text into the >=20
    form. A non-fall-through merged slot therefore takes the category it MOSTLY means
    (probe-count order, CLDR order breaking ties) and never one an earlier slot already
    claimed -- mt slot 1 is `few` (18 of its 20 probe values), Breton slot 4 is `many`
    (its `n%1000000==0` branch), and neither can collide with the fall-through.

    DUPLICATES CAN STILL BE STRUCTURAL, and are not hidden. 14 catalog locales (id, ms,
    jv, su, my, ig, yo, mi, la, tg, tt, sm...) have exactly ONE CLDR category and TWO
    gettext slots, so there is no injective map and no rule can invent one; asserting
    uniqueness here would only turn 14 legitimate catalogs into crashes. The loss is
    made LOUD where it is real instead: `key_files.parse_po_keys` reports every msgstr
    it drops onto an already-filled category into the import report's `notes`, and
    `render_po_keys` fills a duplicated slot from its own category rather than leaving
    a hole. Slots that really do mean one category (ru's genuine 3-form `many`) are
    unaffected either way, because only one category ever lands on them.
    """
    import gettext as _gettext

    from babel.messages.plurals import get_plural

    expr, num = (plural_expr or "").strip(), int(num_plurals or 0)
    if not expr or num <= 0:
        try:
            fallback = get_plural(_babel_locale(locale) or "en")
            expr, num = expr or fallback.plural_expr, num or fallback.num_plurals
        except Exception:
            expr, num = expr or "(n != 1)", num or 2
    cache_key = ((locale or "").strip().lower(), expr, num)
    hit = _gettext_cache.get(cache_key)
    if hit is not None:
        return list(hit)
    cats = plural_categories_for(locale)
    try:
        index_of = _gettext.c2py(expr)
    except Exception:
        return (cats + ["other"] * num)[:num]

    from babel.plural import to_python

    loc = _babel_locale(locale)
    rule = to_python(getattr(loc, "plural_form", None)) if loc is not None else None
    seen: list[list[str]] = [[] for _ in range(num)]
    counts: list[dict[str, int]] = [{} for _ in range(num)]
    hits = [0] * num
    # 0..200 covers every ordinary rule (ar's %100 bands, ru/pl's %10 bands, cy's
    # literals); the two large probes exist for locales whose only `many` trigger is
    # a millions rule (es, it, br). The WHOLE series is walked (no early exit) because
    # the catch-all test needs every category a slot collects -- and HOW OFTEN -- not
    # just the first one to land.
    for n in list(range(0, 201)) + [1000000, 2000000]:  # noqa: RUF005
        try:
            idx = int(index_of(n))
        except Exception:  # noqa: S112 -- plural rule undefined for this probe n; skip the n
            continue
        if 0 <= idx < num:
            cat = rule(n) if rule is not None else "other"
            if cat not in seen[idx]:
                seen[idx].append(cat)
            counts[idx][cat] = counts[idx].get(cat, 0) + 1
            hits[idx] += 1
    out: list[str | None] = [None] * num
    claimed: set[str] = set()
    # 1. Slots that mean exactly one category are not up for interpretation.
    for i, got in enumerate(seen):
        if len(got) == 1:
            out[i] = got[0]
            claimed.add(got[0])
    # 2. The FALL-THROUGH slot -- the expression's `else`, i.e. the one the probe
    #    series lands on most often -- is the only slot allowed to be named "other".
    #    (cy's is index 0, ru's reduced 2-form is index 1, mt's is index 3: it is not
    #    a position, which is exactly why it has to be measured.)
    home = max(range(num), key=lambda i: hits[i]) if num else 0
    if num and len(seen[home]) > 1:
        out[home] = "other"
        claimed.add("other")
    # 3. Every other merged slot takes what it MOSTLY means, never a claimed category.
    for i, got in enumerate(seen):
        if out[i] is not None or not got:
            continue
        ranked = sorted(got, key=lambda c: (-counts[i][c], CLDR_CATEGORIES.index(c)))
        pick = next((c for c in ranked if c not in claimed), ranked[0])
        out[i] = pick
        claimed.add(pick)
    # Any index the probe never reached (a rule keyed on values outside the series)
    # falls back to CLDR order, then to "other" -- never to a crash or a silent drop.
    spare = [c for c in cats if c not in out]
    resolved = [o if o is not None else (spare.pop(0) if spare else "other") for o in out]
    _gettext_cache[cache_key] = tuple(resolved)
    return resolved


def has_live_hidden_keys(session: Session, project_id: int) -> bool:
    """True when the project holds at least one live (not deleted) key hidden from translators."""
    return (
        session.exec(
            select(StringKey.id)
            .where(
                StringKey.project_id == project_id,
                StringKey.deleted_at.is_(None),
                StringKey.is_hidden.is_(True),
            )
            .limit(1)
        ).first()
        is not None
    )


def launch_translation(
    session: Session,
    project,
    workflow_id,
    locale,
    *,
    actor: str,
    wait: bool = False,
    ip: str | None = None,
    include_hidden: bool = True,
) -> dict:
    """Launch a workflow run over a KEY project's internal namespace documents, for one
    target locale or every active target locale (`locale == "all"`).

    Shared by web, API, and local/remote CLI. Visibility checks stay at the surface;
    archive gates, preflight, durable queue creation, and audit live here once.
    The scheduler restores the persisted actor when starting each member.

    Every planned run is persisted before execution. `launched`
    names run ids admitted immediately; `queued` names durable waiting rows with run_id,
    locale, document_id and document_name. The same split is returned on every surface,
    alongside busy/paused/skipped and skipped_incompatible. queue_group identifies the
    dispatch and queue_incomplete names rows that could not be written.

    The in-process CLI uses wait=True to drain admitted work and wait for queued rows
    under one total admission deadline. Queued rows still waiting are drain_incomplete;
    failed executions are also named there. Paused runs stop the wait.
    `include_hidden=False` is for a caller below manager (#574): a run translates every key
    of a namespace document and its run view shows the source text, so a project that holds
    a key hidden from translators refuses the launch (fail closed) instead of translating
    and exposing that key. Managers and admins launch as before.
    Raises KeyServiceError for an unknown target locale,
    ProjectArchivedError / WorkflowArchivedError for an archived project/workflow, and
    TranslationEngineError (mock-409) if any resolved step would silently echo. NOTHING
    launches until BOTH preflights pass (spec: refuse before enqueuing)."""
    from app.models import WorkflowRun
    from app.services import content as content_svc
    from app.services import orchestrator
    from app.services import workflows as wf_service

    if not include_hidden and has_live_hidden_keys(session, project.id):
        raise KeyServiceError(
            "This project has keys hidden from translators, so only a manager can launch "
            "a translation run on it."
        )
    active = active_locales(session, project.id)
    # `all` is spelled out, never inferred from a blank field: a fan-out across every
    # language is a SPEND decision, and a typo in a locale code must never trigger it.
    if locale != "all" and locale not in active:
        raise KeyServiceError(f"{locale} is not a target locale on this project.")
    targets = active if locale == "all" else [locale]
    # Archive gate (before any resolution): refuse launching runs on an archived
    # project/workflow, the same block start_run itself enforces.
    content_svc.refuse_if_project_archived(session, project.id)
    wf_service.refuse_if_workflow_archived(session, workflow_id)
    wf_service.refuse_if_workflow_customer_mismatch(session, workflow_id, project=project)
    # AI opt-out (DPAI-08): an AI workflow never launches on an opted-out key project.
    from app.services import ai_optout

    ai_optout.refuse_workflow_for_project(session, workflow_id, project)
    # CV-2 (M1): per-project USD budget, checked ONCE here BEFORE the launch loop so a
    # fan-out over many docs/locales can never half-launch then 500 on start_run's raw
    # ProjectBudgetExhausted mid-loop. start_run still enforces per-run (defence in depth
    # for any other launcher). A passing fan-out also evaluates the 80/100 alert.
    from app.services import project_budgets

    _pb_ok, _pb_rem, _pb_msg = project_budgets.project_check(project.id)
    if not _pb_ok:
        project_budgets.evaluate_thresholds(
            project.id, actor=actor
        )  # H1: the block IS the crossing
        raise KeyBudgetError(_pb_msg)
    project_budgets.evaluate_thresholds(project.id, actor=actor)
    docs = translatable_documents(session, project)
    # Resolve every locale's P5 variant FIRST so the mock-409 can inspect the exact
    # workflows that would run before anything launches (mirrors document translate).
    per_locale = [
        (
            loc,
            wf_service.resolve_variant(
                session,
                workflow_id,
                loc,
                project.source_locale,
                doc_customer_id=getattr(project, "customer_id", None),
            ),
        )
        for loc in targets
    ]
    if _mock_fallback_workflow(session, [wid for _loc, wid in per_locale]):
        raise TranslationEngineError(
            "No translation engine configured - set an API key before submitting for translation."
        )
    started, restarted, busy, paused, skipped = [], [], [], [], []
    # Retained response key for compatibility; FULL_TEXT tm_match is now supported.
    skipped_incompatible: list = []
    # PLAN FIRST, THEN TAKE WHAT IS FREE, THEN QUEUE THE REST AS ROWS (gate A round 3,
    # HIGH-1/2/3). Round 1 exempted this fan-out from the admission gate entirely, which made
    # the sizing invariant false on any key project (one "translate all" over 12 locales x 6
    # namespace documents is 72 runs, each pinning a pooled connection, on an install that
    # admits 8). Round 2 replaced that with an all-or-nothing reservation, which was worse:
    # `needed > cap` is unsatisfiable, so the same fan-out was refused forever on an IDLE
    # install. Round 2's fix — an in-memory queue object — ran the work but made it
    # invisible: `/api` answered `{"ok": true, "launched": [1, 2]}` for a 5-pair fan-out, the
    # audit row read `launched=2 skipped=0`, a queued launch that RAISED left one run row of
    # three and a log line, and a restart dropped the lot with no record.
    #
    # So the queue is DURABLE: every pair this install has no free slot for right now gets a
    # WorkflowRun row in RunStatus.QUEUED, holding no slot and no pooled connection, and the
    # one process-wide admission scheduler promotes them as slots free. Queued work is then
    # visible everywhere a run is visible, cancellable through the ordinary cancel, adopted
    # again after a restart, and a launch that fails is recorded ON THE ROW.
    #
    # NOTHING IS REFUSED FOR CAPACITY ANY MORE on this path: `acquire_many` no longer waits
    # (that 2s wait blocked a request thread holding a pooled connection — round 3, MED-3)
    # and an empty result queues the whole plan instead of raising.
    launch_plan: list = []
    for loc, wf_id in per_locale:
        live = [d for d in docs if not orchestrator.document_has_no_content(session, d, loc)]
        if not live:
            skipped.append(loc)
            continue
        for doc in live:
            launch_plan.append((loc, wf_id, doc))

    outcome = orchestrator.queue_runs(
        session,
        [(wf_id, doc.id, loc, doc.filename) for loc, wf_id, doc in launch_plan],
        created_by=actor,
        start_scheduler=False,
    )
    queue_group = outcome["group"]
    queued = list(outcome["queued"])
    superseded = [row for row in queued if row.get("superseded")]
    queue_incomplete = list(outcome["failed"])
    # Pairs REFUSED because a run of a DIFFERENT workflow is already in flight on that
    # (document, locale) (GitHub #213 part 2): reported per pair with the running run's
    # id and the standard refusal sentence; the rest of the fan-out runs.
    conflicts = list(outcome.get("conflicts", []))
    # Close the request checkout before the shared, non-waiting admission pass.
    session.commit()
    orchestrator.ADMISSION.drain_once(slot_wait=0, limit=len(queued), queue_group=queue_group)
    still_queued = []
    for row in queued:
        run = session.get(WorkflowRun, row["run_id"], populate_existing=True)
        if run is not None and run.started_at is not None and run.status != RunStatus.QUEUED:
            started.append(row["run_id"])
        else:
            still_queued.append(row)
    queued = still_queued
    session.commit()
    for row in outcome["attached"]:
        if row["status"] == RunStatus.QUEUED:
            queued.append(row)
        elif row["status"] == RunStatus.PAUSED:
            paused.append(row["run_id"])
        else:
            busy.append(row["run_id"])
    if queued:
        orchestrator.ADMISSION.ensure_started()
        orchestrator.ADMISSION.notify()
    admitted_immediately = list(started + restarted)
    queued_at_admission = list(queued)
    from app.services import audit

    audit.log(
        actor=actor,
        action="keys.translate",
        ip=ip or "",
        target=f"project {project.id} -> {locale}",
        detail=f"launched={len(started)} queued={len(queued)} "
        f"busy={len(busy)} paused={len(paused)} "
        f"skipped={len(skipped)} queue_group={queue_group} "
        f"queued_runs={[r['run_id'] for r in queued]} "
        f"queue_failed={len(queue_incomplete)} conflicts={len(conflicts)}",
        customer_id=audit.customer_for_project(project.id),
    )
    drain_incomplete: list = []
    if wait:
        from app import db as _db

        # A DRAIN IS NOT AN ORDINARY REQUEST (gate A round 2, MED-2). The per-request
        # pool-wait budget bounds a page load to one checkout timeout; this scope lives for
        # as long as every launched run takes and issues a Session per poll inside
        # observe_run, so one momentary saturation would latch it out of the database for
        # the rest of the drain and end the fan-out in a 503 whose body says "nothing was
        # left half-written" — false, the runs are executing. A long-lived scope gets the
        # per-CHECKOUT timeout instead, which is what it was designed for.
        _db.exempt_pool_budget()
        # THE QUEUE IS PART OF "WHAT THIS CALL LAUNCHED" (round 2 HIGH-1; round 3 HIGH-2). A
        # one-shot CLI process kills its daemon threads on exit, so a `wait=True` caller must
        # see the queued launches through as well as the inline ones. It now waits on the
        # DURABLE ROWS rather than on an in-memory object: each queued row is watched until
        # the scheduler admits it (then drained like any other run) or something terminal
        # happens to it — a cancel, or an admission that FAILED with its reason stamped on the
        # row. Nothing can end this loop with a pair unaccounted for.
        #
        # BOUNDED (round 3, LOW-2). Round 2 called `FanoutQueue.join()` with NO timeout while
        # each pending launch could wait an hour, so `keys translate --wait` on a busy install
        # could block for (pairs x 1h). `orchestrator.QUEUE_DRAIN_WAIT_S` bounds the whole
        # queue. Rows still waiting when it expires are reported as `drain_incomplete`
        # with status `queued` — still running on the server, still visible, just not waited
        # on any longer.
        if queue_group:
            _qdeadline = time.monotonic() + orchestrator.QUEUE_DRAIN_WAIT_S
            while True:
                session.expire_all()
                still = orchestrator.queued_runs(session, queue_group)
                session.commit()
                if not still:
                    break
                if time.monotonic() > _qdeadline:
                    drain_incomplete.extend(
                        {"run_id": r.id, "status": RunStatus.QUEUED} for r in still
                    )
                    break
                time.sleep(0.2)
            # Every row this fan-out queued that DID get admitted is drained below, exactly
            # like an inline launch; a row that never started is already FAILED with a
            # reason on it (orchestrator._mark_queue_launch_failed) and is reported here.
            _waited = {row["run_id"] for row in queued if row.get("run_id")}
            _incomplete_ids = {row["run_id"] for row in drain_incomplete}
            for _rid in sorted(_waited - _incomplete_ids):
                started.append(_rid)
            queued = [row for row in queued if row.get("run_id") in _incomplete_ids]

        # In-process CLI drain (P-A2). Block until every run WE launched this call
        # (started + restarted -- the fresh daemon threads alive in THIS process)
        # reaches a terminal or human-review-paused state. `observe_run` is the
        # read-only observer that REPLAYS then TAILS a run's rows and RETURNS the
        # moment the run is terminal or PAUSED -- draining it joins the run without
        # forking the execution logic and without spinning on a human pause. `busy`
        # and `paused` runs were re-attached to pre-existing state, not launched
        # here, so they are not drained (a durable PAUSED row must not be waited on).
        for rid in started + restarted:
            last_status = None
            for _run, evt in orchestrator.observe_run(rid):
                if evt.get("type") == "closed":
                    last_status = evt.get("status")
            # observe_run's terminal `closed` event carries the run's RunStatus. A run
            # that FAILED, or that the observer left RUNNING at its 900s ceiling, is NOT a
            # success the CLI may report as "Started" -- capture it so the surface can warn
            # and exit non-zero (F3: the narrow residual of the very false-success class
            # P-A2 fixed -- a false success by omission of the verdict, not by the daemon
            # dying). A None status is an absent run row (only reachable through a stubbed
            # start_run in tests), left UNclassified so it can never false-warn.
            if last_status is not None and last_status not in (
                RunStatus.COMPLETED,
                RunStatus.PAUSED,
            ):
                drain_incomplete.append({"run_id": rid, "status": str(last_status)})
    out = {
        "superseded": superseded,
        "started": started,
        "restarted": restarted,
        "busy": busy,
        "paused": paused,
        "skipped": skipped,
        "skipped_incompatible": skipped_incompatible,
        "launched": admitted_immediately,
        "drain_incomplete": drain_incomplete,
        # Per-launch status `queued` (round 2 HIGH-1; round 3 HIGH-1): the (locale, document)
        # pairs this install had no free slot for, each with the id of the DURABLE run row
        # that is waiting for one. Every surface carries this — the /api body, the audit
        # detail, the web message, `keys translate`'s human output and MCP — because a
        # response that reports two of five launches as an unqualified success is the same
        # lie whichever surface tells it. This admission snapshot also survives wait=True;
        # waiting separately names rows the drain gave up waiting on.
        "queued": queued_at_admission,
        # The fan-out token every queued row carries, so a client (or a later CLI call) can
        # name exactly this action's rows.
        "queue_group": queue_group,
    }
    if wait:
        # Keep the initial admission split stable on the synchronous CLI too.
        # Human output uses waiting to avoid describing drained rows as still queued.
        out["waiting"] = queued
    if queue_incomplete:
        # Pairs whose ROW COULD NOT BE WRITTEN (write contention under launch pressure).
        # Reported, never silently absent -- the inline half of the same loop surfaces the
        # identical error to the caller (gate A round 3, HIGH-2).
        out["queue_incomplete"] = queue_incomplete
    if conflicts:
        # GitHub #213 part 2: pairs REFUSED because a run of a DIFFERENT workflow is
        # already in flight on that (document, locale) -- never queued, never run. Each
        # row names the running run (code `run_conflict`) with the standard refusal
        # sentence; every surface carries the list the same way it carries `queued`.
        out["conflicts"] = conflicts
    return out


def _move_value_rows(session: Session, key, target_doc) -> int:
    """Re-park `key`'s value rows under `target_doc`, appending each one.

    Only `document_id`/`order_index` change, and the change feed watches
    target_text/source_text/status ONLY, so a move appends no `value` entries --
    which is exactly right: a re-park is not a content change, and `delete_key` /
    `restore_key` own the entries that describe what really happened."""
    moved = 0
    # Append slot per locale, read from the database ONCE and then counted up locally
    # (#512): the per-row `_next_order_index` SELECT plus a flush per row made a trash
    # move cost grow with the size of the trash document. Every row lands in the same
    # target document, so a running counter per locale is exactly what a re-read gives.
    next_idx: dict[str, int] = {}
    for seg in values_for(session, key):
        if seg.document_id == target_doc.id:
            continue
        # The append slot is read BEFORE `document_id` is touched. Assigning it first
        # makes `seg` dirty, and the SELECT behind `_next_order_index` AUTOFLUSHES the
        # half-updated row (new document, OLD order_index) straight into
        # `ux_segment_doc_locale_order` and raises IntegrityError. Reproduced by
        # verify_keys_files the moment a cleanup import deleted a second key.
        loc = seg.target_locale
        if loc not in next_idx:
            next_idx[loc] = _next_order_index(session, target_doc.id, loc)
        idx = next_idx[loc]
        next_idx[loc] = idx + 1
        seg.document_id, seg.order_index = target_doc.id, idx
        session.add(seg)
        moved += 1
    if moved:
        session.flush()
    return moved


def namespaces(
    session: Session, project_id: int, *, org_id=None, customer_id=None, include_hidden: bool = True
) -> list[str]:
    """Every namespace that has at least one live key, "" (the default) included.
    `include_hidden=False` leaves out keys hidden from translators (and so a namespace
    that only holds hidden keys)."""
    _guard(session, project_id, org_id, customer_id)
    stmt = select(StringKey.namespace).where(
        StringKey.project_id == project_id, StringKey.deleted_at.is_(None)
    )
    if not include_hidden:
        stmt = stmt.where(StringKey.is_hidden.is_(False))
    rows = session.exec(stmt).all()
    return sorted({r for r in rows})  # noqa: C416


def _guard(session: Session, project_id, org_id, customer_id) -> None:
    """Apply the isolation guards on a seam that only takes a project ID. A no-op when
    both are None (the caller established scope already), so the read paths below cost
    nothing extra on the CLI/admin shape."""
    if org_id is None and customer_id is None:
        return
    _project(session, project_id, org_id=org_id, customer_id=customer_id)


def base_values(session: Session, key) -> dict[str, str]:
    """The key's BASE (source-locale) value(s), read back from the `source_text` its
    value rows carry. `{"": text}` for a simple key; `{category: text}` for a plural
    one. See the module docstring's "known edges": a source-locale plural form that no
    target locale has a slot for cannot be recovered here."""
    out: dict[str, str] = {}
    for seg in values_for(session, key):
        cat = seg.plural_category or ""
        if cat not in out or not out[cat]:
            out[cat] = seg.source_text or ""
    return out


def _categories_for_key(key, locale: str) -> list[str | None]:
    return plural_categories_for(locale) if key.is_plural else [None]


# --- key CRUD ----------------------------------------------------------------
def get_key(
    session: Session,
    project_id: int,
    name: str,
    namespace: str = "",
    context: str = "",
    include_deleted: bool = False,
    *,
    org_id=None,
    customer_id=None,
) -> StringKey | None:
    """Resolve one key by its IDENTITY TUPLE. branch_id is always NULL in v1."""
    _guard(session, project_id, org_id, customer_id)
    stmt = select(StringKey).where(
        StringKey.project_id == project_id,
        StringKey.namespace == (namespace or ""),
        StringKey.name == name,
        StringKey.context == (context or ""),
    )
    if not include_deleted:
        stmt = stmt.where(StringKey.deleted_at.is_(None))
    rows = sorted(session.exec(stmt).all(), key=lambda k: k.id)
    return rows[-1] if rows else None


def list_keys(
    session: Session,
    project_id: int,
    *,
    namespace=None,
    tag=None,
    q=None,
    include_deleted: bool = False,
    include_archived: bool = True,
    include_hidden: bool = True,
    limit=None,
    offset: int = 0,
    org_id=None,
    customer_id=None,
) -> list[StringKey]:
    """Keys in stable order (namespace, name, context, id) -- stable ordering is a
    product requirement, not polish: an export that reorders keys produces a diff
    nobody will merge (research 01 section 9.6)."""
    _guard(session, project_id, org_id, customer_id)
    stmt = _key_statement(
        session,
        project_id,
        namespace=namespace,
        tag=tag,
        q=q,
        include_deleted=include_deleted,
        include_archived=include_archived,
        include_hidden=include_hidden,
    ).order_by(*_key_order(session))
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.exec(stmt).all())


def count_keys(
    session: Session, project_id: int, *, org_id=None, customer_id=None, **filters
) -> int:
    """Grid-only filtered count, capped at the 10,001 sentinel; no cursor or page limit."""
    _guard(session, project_id, org_id=org_id, customer_id=customer_id)
    bounded = _key_statement(session, project_id, **filters).limit(10001).subquery()
    return session.exec(select(func.count()).select_from(bounded)).one()


def key_view(session: Session, key, locales=None) -> dict:
    """Single-key compatibility wrapper over the shared batch payload."""
    return key_views(session, [key], locales, project_id=key.project_id)[0]


def search_keys(
    session: Session,
    project_id: int,
    *,
    namespace=None,
    tag=None,
    q=None,
    status=None,
    untranslated_in=None,
    include_deleted: bool = False,
    include_archived: bool = True,
    include_hidden: bool = True,
    org_id=None,
    customer_id=None,
) -> list[StringKey]:
    """`list_keys` plus the two VALUE-derived filters the grid/API/CLI all offer.

    `status` keeps a key with at least one value row in that segment status;
    `untranslated_in=<locale>` keeps a key with at least one EMPTY value row in that
    locale. Both compose with the metadata predicates in SQL. No limit /
    offset here on purpose -- paging happens after filtering (`page_keys`), because
    slicing before a value filter would hand back short pages."""
    _guard(session, project_id, org_id, customer_id)
    stmt = _key_statement(
        session,
        project_id,
        namespace=namespace,
        tag=tag,
        q=q,
        status=status,
        untranslated_in=untranslated_in,
        include_deleted=include_deleted,
        include_archived=include_archived,
        include_hidden=include_hidden,
    ).order_by(*_key_order(session))
    return list(session.exec(stmt).all())


def page_keys(
    session: Session,
    project_id: int,
    *,
    cursor: int = 0,
    limit: int = 100,
    org_id=None,
    customer_id=None,
    **filters,
) -> dict:
    """One CURSOR page of `search_keys`, ordered by key id.

    The cursor is the last key id of the previous page, so it is a STABLE position
    even while keys are being created, renamed or moved between namespaces -- unlike
    the grid's (namespace, name, context) display order, under which one rename can
    shuffle a key across a page boundary and hide it from a paging client forever.
    Returns {"keys": [...], "next_cursor": int, "has_more": bool}."""
    try:
        cursor = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        cursor = 0
    try:
        limit = int(limit or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    _guard(session, project_id, org_id, customer_id)
    stmt = (
        _key_statement(session, project_id, **filters)
        .where(
            StringKey.id > cursor,
        )
        .order_by(StringKey.id)
        .limit(limit + 1)
    )
    rows = list(session.exec(stmt).all())
    page, has_more = rows[:limit], len(rows) > limit
    return {"keys": page, "next_cursor": page[-1].id if page else cursor, "has_more": has_more}


def delete_key(
    session: Session, key, *, actor: str = "", ip: str = "", audit_event: bool = True
) -> StringKey:
    """SOFT delete + tombstone. The value rows are deliberately LEFT IN PLACE: read
    paths exclude a deleted key, and `restore_key` must be able to bring the content
    back (a cleanup-mode import that guessed wrong is otherwise unrecoverable).

    `audit_event=False` is for the ONE caller that describes itself: `import_keys`'s
    cleanup sweep writes a single `keys.import` row carrying its deleted COUNT, so a
    per-key row for each of 200 swept keys would bury it rather than add to it."""
    if key.deleted_at is not None:
        return key
    # Resolved BEFORE the key is touched: `trash_document` commits when it has to
    # create the row, and a commit between the tombstone and the move would leave a
    # deleted key whose rows are still in a translatable document if the move then
    # failed -- i.e. the exact bug this parking exists to prevent, but silent.
    trash = trash_document(session, session.get(Project, key.project_id))
    key.deleted_at = key.updated_at = _utcnow()
    session.add(key)
    _log(session, key, entity=KeyEntity.KEY, op=KeyOp.DELETED, actor=actor or None)
    session.flush()
    # ...and PARK the value rows out of every document-scoped reader's way (Wave B):
    # a run targets a DOCUMENT, so rows left in the namespace document would still be
    # translated after a cleanup import deleted their keys. The flush above puts
    # `deleted_at` in the session BEFORE the move, so the change-feed listener sees a
    # tombstoned key and stays silent (review A9: a tombstoned key emits no `value`
    # entries) -- belt and braces, since a move touches none of the fields it watches.
    _move_value_rows(session, key, trash)
    # The ADVANCED tier's own content for this key's units (review gate round 1, L2). A keys unit
    # stages with key_id set + segment_id NULL, so a run over this key holds its translated value
    # in the tier's own staged targets / transcript / roster and its run-parented findings -- none
    # of which the key delete reaches (the same SOFT-reference silent-residue class as the document
    # path). Sever it here, in this same transaction, keyed on key_id, keeping the run + mission
    # rows. NOTE (surfaced for the gate/errata): delete_key is a SOFT, RESTORABLE delete; the
    # Advanced run CONTENT it severs does not come back on restore_key (restore brings back the
    # key's own value rows, never a run's staged snapshot -- which it never touched either way).
    # Lazy import (the HARD RULE + the retention idiom); a keys project with no Advanced data is a
    # cheap no-op.
    from app import extensions as _extensions

    _extensions.run_reapers(
        "document_content", session, key_ids=[key.id], project_id=key.project_id
    )
    session.commit()
    session.refresh(key)
    if audit_event:
        _audit(
            "keys.delete",
            key.project_id,
            _key_target(key),
            actor=actor,
            ip=ip or "",
            detail=(
                "soft delete (tombstoned in the change feed; restorable; "
                "Advanced run content severed)"
            ),
        )
    return key


def parse_modified_after_cursor(raw):
    """(naive-UTC datetime, after_id|None) for a `modified_after` value that MAY be the
    opaque `ISO~id` token (gate NEW-3) or a plain ISO timestamp (a human-authored first
    call). A malformed id in the token is a BadTimestampError. Used by the service AND
    the CLI's --remote pre-validation, so both split the token identically."""
    if isinstance(raw, str) and "~" in raw:
        iso_part, _, id_part = raw.rpartition("~")
        try:
            token_id = int(id_part)
        except (TypeError, ValueError) as e:
            raise BadTimestampError(raw) from e
        # F-3: an out-of-range id is a malformed token, not a silent 'match everything'
        # (a negative id makes `StringKey.id > after_id` admit every row). Refuse loudly,
        # like the other malformed-token forms. Real cursor ids are positive 64-bit.
        if token_id < 0 or token_id > 0x7FFFFFFFFFFFFFFF:
            raise BadTimestampError(raw)
        return parse_modified_after(iso_part), token_id
    return parse_modified_after(raw), None


def changes_since(
    session: Session,
    project_id: int,
    cursor: int = 0,
    limit: int = DEFAULT_DELTA_LIMIT,
    *,
    org_id=None,
    customer_id=None,
    include_hidden: bool = True,
) -> dict:
    """THE delta read (spec 5), CHANGELOG mode. Ordered by the changelog PK -- a global
    autoincrement filtered per project -- so it has no timestamp ties, no drift when a
    row is updated between pages, and DELETIONS ARRIVE AS ENTRIES (`op: "deleted"`)
    instead of as an absence the consumer can never notice. This mode reports EVERY op
    (create/update/delete, key + value) with a resync floor; it is the ONLY mode that
    reports deletions and metadata-only key edits. Contrast `changes_modified_after`,
    which reports CURRENT-STATE value rows changed since a timestamp and never reports
    deletions.

    `cursor=0` is a full bootstrap from the oldest retained row and NEVER reports
    resync_required (a bootstrapping client is already re-reading everything). A
    non-zero cursor below the pruned floor answers `resync_required: True` with an
    honest message and no entries (Graph's 410 pattern). The cursor is a WHOLE NUMBER:
    the web route (`since: int`) and the CLI (`--since`, argparse `type=int`) both
    reject a non-integer at their own validation layer (a 422 / an argparse error), so
    a non-int never reaches here; a NEGATIVE cursor clamps to 0.

    VALUE entries are ENRICHED with `text` (the current target string) and
    `content_hash` (see `content_hash`) so a consumer stops N+1-ing back per key; the
    fields are ADDITIVE and absent on key entries and on deleted tombstones.

    `org_id`/`customer_id` are the isolation guards (see `_project`): a cross-scope
    project raises NotAKeyProjectError BEFORE any row is read, so the delta feed can
    never become the enumeration oracle for another tenant's key names."""
    _guard(session, project_id, org_id, customer_id)
    try:
        cursor = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        cursor = 0
    try:
        limit = int(limit or DEFAULT_DELTA_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_DELTA_LIMIT
    limit = max(1, min(limit, MAX_DELTA_LIMIT))

    floor = changelog_floor(session, project_id)
    if cursor and cursor < floor:
        return {
            "entries": [],
            "next_cursor": cursor,
            "has_more": False,
            "resync_required": True,
            "message": "cursor %d is older than the retained history (floor %d); "  # noqa: UP031
            "re-bootstrap with since=0." % (cursor, floor),
        }
    rows = list(
        session.exec(
            select(KeyChangeLog)
            .where(KeyChangeLog.project_id == project_id, KeyChangeLog.id > cursor)
            .order_by(KeyChangeLog.id)
            .limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not include_hidden and rows:
        # #540: keys hidden from translators stay out of the feed for a caller below
        # manager. The cursor still advances past them (next_cursor reads `rows`), so
        # the consumer never stalls and never learns that an entry was withheld.
        hidden_ids = set(
            session.exec(
                select(StringKey.id).where(
                    StringKey.project_id == project_id,
                    StringKey.is_hidden.is_(True),
                    StringKey.id.in_({r.key_id for r in rows if r.key_id is not None}),
                )
            ).all()
        )
        visible = [r for r in rows if r.key_id not in hidden_ids]
    else:
        visible = rows
    entries = [
        {
            "cursor": r.id,
            "entity": r.entity,
            "op": r.op,
            "key_id": r.key_id,
            "namespace": r.namespace,
            "name": r.name,
            "target_locale": r.target_locale,
            "plural_category": r.plural_category,
            "changed_at": r.changed_at.isoformat() if r.changed_at else "",
            "actor": r.actor,
        }
        for r in visible
    ]
    _enrich_value_entries(session, entries)
    return {
        "mode": "since",
        "entries": entries,
        "next_cursor": rows[-1].id if rows else cursor,
        "has_more": has_more,
        "resync_required": False,
    }
