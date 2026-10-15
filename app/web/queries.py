"""Dashboard / report query helpers shared by the router modules.

Documented in: docs/architecture/reporting-and-dashboard.md
"""

from sqlalchemy import case, func, or_
from sqlmodel import select

from app import config
from app.kernel import content, keys, tm, worklist
from app.kernel.providers import provider_is_live
from app.models import (
    Customer,
    Document,
    FullTextTM,
    LQAResult,
    Project,
    ProjectType,
    Scope,
    Segment,
    SegmentTM,
    SegStatus,
    TargetLocale,
    Workflow,
    WorkflowRun,
)

#: SPEC-key-based-projects 2.5 — the one filter every DOCUMENT LISTING applies.
#: A key project's internal per-namespace documents hold key values, not files, and
#: must not appear in a document list, a document count, or a dashboard rollup. The
#: predicate `keys.is_internal_key_document` is the authority for a single row; this
#: is the same test pushed into SQL for the list queries.
_NOT_INTERNAL = Document.fmt != keys.KEY_DOC_FMT


# --- SQL aggregates (#514) ----------------------------------------------------------
# The dashboard, project list and /reports used to load every Segment / TM / run row into
# Python just to call len() or bucket a column. These helpers push the counting into SQL
# (COUNT / GROUP BY) and chunk every IN() at 150 so a large id list cannot exceed SQLite's
# bound-parameter limit ("too many SQL variables"). The numbers are identical to the old
# load-and-count code; verify_dashboard_sql_counts pins that on seeded data.
_IN_CHUNK = 150


def _chunked(ids, size=_IN_CHUNK):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def _count_rows(session, model, *where, in_col=None, ids=None) -> int:
    """SELECT COUNT(*) FROM model [WHERE ...] [AND in_col IN ids (chunked)]. `ids=None`
    means no IN filter; an EMPTY id list counts zero (nothing can match)."""
    if ids is None:
        return int(session.exec(select(func.count()).select_from(model).where(*where)).one())
    total = 0
    for batch in _chunked(ids):
        stmt = select(func.count()).select_from(model).where(in_col.in_(batch), *where)
        total += int(session.exec(stmt).one())
    return total


def _group_counts(session, group_col, *where, in_col=None, ids=None) -> dict:
    """{value of group_col: row count}, optionally restricted to in_col IN ids (chunked,
    merged across chunks). `ids=None` means unrestricted; an empty list yields {}."""
    out: dict = {}
    batches = [None] if ids is None else list(_chunked(ids))
    for batch in batches:
        stmt = select(group_col, func.count()).where(*where)
        if batch is not None:
            stmt = stmt.where(in_col.in_(batch))
        for key, n in session.exec(stmt.group_by(group_col)).all():
            out[key] = out.get(key, 0) + int(n)
    return out


def _without_seeding(session, doc_ids, status: dict) -> dict:
    """#583: drop the rows of a half-cloned (chunked add-a-language) locale from a status
    count, so dashboards do not show a language that is still being added."""
    from app.kernel import locale_seed

    held = locale_seed.seeding_status_counts(session, doc_ids)
    if not held:
        return status
    return {k: n - held.get(k, 0) for k, n in status.items() if n - held.get(k, 0) > 0}


def _score_sum_count(session, doc_ids=None):
    """(sum, count) of the non-null LQAResult.score values, optionally restricted to a
    document id list (chunked). The /reports average is round(sum / count): the same
    arithmetic as averaging the loaded rows, without loading them."""
    total, n = 0.0, 0
    batches = [None] if doc_ids is None else list(_chunked(doc_ids))
    for batch in batches:
        stmt = select(func.sum(LQAResult.score), func.count(LQAResult.score))
        if batch is not None:
            stmt = stmt.where(LQAResult.document_id.in_(batch))
        got_sum, got_n = session.exec(stmt).one()
        total += float(got_sum or 0.0)
        n += int(got_n or 0)
    return total, n


def _leverage_band_counts(session, doc_ids) -> dict[str, int]:
    """SQL twin of tm.leverage_bands(segments): the same four-way bucketing (no target =
    untranslated, 100 = exact, any other cited match = fuzzy, else new) done by a CASE in
    the database, so no Segment row is loaded."""
    bands = tm.leverage_bands([])  # the zeroed dict in the canonical band order
    band = case(
        (or_(Segment.target_text.is_(None), Segment.target_text == ""), "untranslated"),
        (Segment.tm_match_pct == 100, "exact"),
        (Segment.tm_match_pct.is_not(None), "fuzzy"),
        else_="new",
    )
    for key, n in _group_counts(session, band, in_col=Segment.document_id, ids=doc_ids).items():
        bands[key] = bands.get(key, 0) + n
    return bands


def _project_counts(session, doc_ids):
    """The project LIST rollup (documents, segments, status, approved_pct, runs) by SQL
    COUNT / GROUP BY. Same numbers as `_project_stats` without loading a single Segment or
    WorkflowRun row; the project DETAIL page still uses `_project_stats` because it needs
    the rows for 7-band leverage."""
    if not doc_ids:
        return {
            "documents": 0,
            "segments": 0,
            "status": {},
            "approved_pct": 0,
            "runs": 0,
            "source_segments": 0,
            "locale_count": 0,
        }
    status = _without_seeding(
        session,
        doc_ids,
        _group_counts(session, Segment.status, in_col=Segment.document_id, ids=doc_ids),
    )
    total = sum(status.values())
    approved = status.get(SegStatus.APPROVED, 0)
    runs = _count_rows(session, WorkflowRun, in_col=WorkflowRun.document_id, ids=doc_ids)
    # #563: the "S segments x L languages" split, counted in SQL like the rest (same
    # distinct keys as `_project_stats`). Chunks split on document_id, so the per-chunk
    # distinct source counts never overlap and simply add up.
    locales = _group_counts(session, Segment.target_locale, in_col=Segment.document_id, ids=doc_ids)
    # #583 (review): a locale still being added is not counted as a language either.
    from app.kernel import locale_seed

    for _doc_id, _loc in locale_seed.seeding_pairs(session, doc_ids):
        _n = _count_rows(
            session,
            Segment,
            Segment.target_locale == _loc,
            in_col=Segment.document_id,
            ids=[_doc_id],
        )
        locales[_loc] = locales.get(_loc, 0) - _n
    locales = {k: n for k, n in locales.items() if n > 0}
    source_cols = (
        Segment.document_id,
        Segment.order_index,
        Segment.key_id,
        Segment.plural_category,
    )
    source_segments = 0
    for batch in _chunked(doc_ids):
        grouped = (
            select(*source_cols)
            .where(Segment.document_id.in_(batch))
            .group_by(*source_cols)
            .subquery()
        )
        source_segments += int(session.exec(select(func.count()).select_from(grouped)).one())
    return {
        "documents": len(doc_ids),
        "segments": total,
        "status": status,
        "approved_pct": round(100 * approved / total) if total else 0,
        "runs": runs,
        "source_segments": source_segments,
        "locale_count": len(locales),
    }


def _project_stats(session, doc_ids, with_leverage7=False):
    """Dashboard rollup across a project's documents.

    `with_leverage7` (project DETAIL page only — never the multi-project list) adds the
    CV-1 7-band WORD leverage, computed from the SAME already-loaded `segs` (M4: no
    second full segment load per render). The segs are collected across chunks then sorted
    ONCE globally by (order_index, id) — matching vendor_cost.project_leverage's global
    sort — so the leverage repetition split is byte-identical to that loader seam even
    past the chunk boundary and whatever order the caller's doc_ids arrive in (N1 + L1)."""
    if not doc_ids:
        out = {
            "documents": 0,
            "segments": 0,
            "status": {},
            "approved_pct": 0,
            "source_segments": 0,
            "locale_count": 0,
            "runs": 0,
            "tm_leverage": tm.leverage_bands([]),
        }
        if with_leverage7:
            from app.kernel import vendor_cost

            est = vendor_cost.leverage_over_segments([])
            out["leverage7"] = est
            out["leverage7_rows"] = vendor_cost.estimate_band_rows(est)
        return out
    # Chunk the IN() at 150 (SQLite variable limit) like the sibling fetchers — this path
    # now carries the 7-band leverage, so a >999-doc project must not blow the bind (N1).
    segs = []
    _CHUNK = 150
    for _i in range(0, len(doc_ids), _CHUNK):
        _batch = doc_ids[_i : _i + _CHUNK]
        segs.extend(session.exec(select(Segment).where(Segment.document_id.in_(_batch))).all())
    # ONE global sort after collection (identical to project_leverage) so the leverage
    # repetition split matches across surfaces regardless of doc-id / chunk order.
    segs.sort(key=lambda s: (s.order_index, s.id))
    # #583 (review): the detail page agrees with the list and dashboard, so the rows of a
    # locale still being added (chunked clone) are left out of every count and band here.
    from app.kernel import locale_seed

    _held = set(locale_seed.seeding_pairs(session, doc_ids))
    if _held:
        segs = [s for s in segs if (s.document_id, s.target_locale) not in _held]
    status: dict[str, int] = {}
    for s in segs:
        status[s.status] = status.get(s.status, 0) + 1
    runs = session.exec(select(WorkflowRun).where(WorkflowRun.document_id.in_(doc_ids))).all()
    total = len(segs)
    # #563: `segments` counts one ROW per target language, so a 12-segment document in 4
    # languages reads 48. Say what is being counted: the distinct source segments and the
    # number of target languages, so the label can read "12 segments x 4 languages".
    source_keys = {
        (
            s.document_id,
            s.order_index,
            getattr(s, "key_id", None),
            getattr(s, "plural_category", None),
        )
        for s in segs
    }
    locale_count = len({s.target_locale for s in segs})
    approved = status.get(SegStatus.APPROVED, 0)
    # TM-leverage rollup (viz quick-win): reuses the SAME `segs` list already
    # fetched for the status counts above - no new query, no new scope logic.
    out = {
        "documents": len(doc_ids),
        "segments": total,
        "status": status,
        "approved_pct": round(100 * approved / total) if total else 0,
        "source_segments": len(source_keys),
        "locale_count": locale_count,
        "runs": len(runs),
        "tm_leverage": tm.leverage_bands(segs),
    }
    if with_leverage7:
        from app.kernel import vendor_cost

        est = vendor_cost.leverage_over_segments(segs)
        out["leverage7"] = est
        out["leverage7_rows"] = vendor_cost.estimate_band_rows(est)
    return out


def _applicable_workflows(session, project, doc=None, with_excluded=False):
    """Workflows usable on this project: global + its customer's + its own.

    MULTI-ORG (leak vector 12): a GLOBAL-scope workflow is org-owned now, so this
    is scoped to the PROJECT's org — another org's global workflow must never be
    surfaced here. The scope is the project's org_id (the project is the caller's
    own — the route already org-gated it), so no user is threaded through. No-op
    single-tenant: when the gate is off every row shares the Default org, so the
    org filter admits everything, byte-identical to today.

    """
    from app.kernel import access
    from app.kernel import workflows as wf_service

    rows = session.exec(select(Workflow).order_by(Workflow.name)).all()
    scoped = access.multi_org_active()
    proj_org = getattr(project, "org_id", None) if project else None
    out = []
    excluded = []
    for w in rows:
        # Archive (residual sweep): an ARCHIVED workflow is never OFFERED for a new run in
        # this run-form picker (routed through the shipped seam, M4; the launch guard
        # refuses it too, defense in depth).
        if not wf_service.workflow_is_active(w):
            continue
        # In multi-org mode, only surface workflows in the project's own org.
        if scoped and getattr(w, "org_id", None) != proj_org:
            continue
        # Scope gate FIRST (gate round-2 M1): only workflows the caller could otherwise
        # pick count as "applicable", so the excluded list holds real removed choices, not
        # every tm_match workflow in the install. Net offered set is unchanged.
        # GATE R1 HIGH-2: customer_id first, never scope alone. `scope` and `customer_id`
        # are independent columns, so `scope == GLOBAL` does NOT mean "owned by nobody" --
        # a promoted workflow is minted GLOBAL with a customer_id set. Establish ownership
        # on customer_id, then let scope narrow further.
        if w.customer_id is not None and not (project and w.customer_id == project.customer_id):
            continue
        if w.project_id is not None and not (project and w.project_id == project.id):
            continue
        if not (
            w.scope in (Scope.GLOBAL, Scope.CUSTOMER)
            or (w.scope == Scope.PROJECT and project and w.project_id == project.id)
        ):
            continue
        out.append(w)
    if with_excluded:
        return out, excluded
    return out


def preferred_workflow(workflows, source_locale, target_locale):
    """#605: which workflow the document page's run picker should PRESELECT for a locale.

    The list is alphabetical, so the first <option> was whichever name sorted first: an
    "EN to ES419" workflow came up for a de-DE document. A workflow tagged with exactly
    this target language wins, then a language-neutral one (no target tag), and a
    workflow tagged for ANOTHER language is never preselected (returns None, and the page
    asks the person to choose). The source side only breaks ties: a workflow tagged with
    a different source language than the project's is skipped like a wrong target."""
    from app.kernel import languages_catalog as _lc

    def _c(code):
        return (_lc.canonical(code) or str(code or "")).lower() if code else ""

    want_t, want_s = _c(target_locale), _c(source_locale)
    neutral = None
    for w in workflows:
        wt, ws = _c(getattr(w, "target_locale", None)), _c(getattr(w, "source_locale", None))
        if ws and want_s and ws != want_s:
            continue
        if wt and wt == want_t:
            return w
        if not wt and neutral is None:
            neutral = w
    return neutral


def _project_rows(session, projects, cust_by_id, visible_ids=None):
    """Rows for the project tables. visible_ids=None means unrestricted; a list
    restricts the rows to those project ids (see app.services.access)."""
    rows = []
    if visible_ids is not None:
        allowed = set(visible_ids)
        projects = [p for p in projects if p.id in allowed]
    for p in projects:
        docs = session.exec(
            select(Document).where(Document.project_id == p.id, _NOT_INTERNAL)
        ).all()
        # A KEY project's progress comes from its VALUE rows, never from documents
        # (review B12). It has no listable documents by construction, so the rollup
        # read 0 segments and the /projects row rendered a permanent 0% bar however
        # completely translated the catalogue was -- an empty progress bar that can
        # never move is worse than none. The TRASH document is excluded: a deleted
        # key's parked values would hold the bar down forever. `docs` itself stays
        # the real (empty) document list, so every count and listing is unchanged.
        stat_ids = [d.id for d in docs]
        if getattr(p, "project_type", None) == ProjectType.KEY_BASED:
            stat_ids = [d.id for d in keys.translatable_documents(session, p)]
        st = _project_counts(session, stat_ids)  # SQL counts, no row load (#514)
        st["documents"] = len(docs)
        locales = session.exec(select(TargetLocale).where(TargetLocale.project_id == p.id)).all()
        last = max([d.created_at for d in docs] + [p.created_at])
        if not docs:
            state = "new"
        elif st["approved_pct"] >= 100:
            state = "completed"
        else:
            state = "in_progress"
        rows.append(
            {
                "project": p,
                "customer": cust_by_id.get(p.customer_id, "—"),
                "docs": len(docs),
                "approved_pct": st["approved_pct"],
                "segments": st["segments"],
                "source_segments": st["source_segments"],
                "locale_count": st["locale_count"],
                "locales": [l.locale for l in locales],  # noqa: E741
                "owner": p.created_by or "—",
                "updated": last,
                "state": state,
                "runs": st["runs"],
                # Due date (Feature A): the badge status (overdue/due_soon/None)
                # for ACTIVE projects — the dashboard/projects tables render it.
                "due_status": content.due_status(p),
            }
        )
    return rows


def _dashboard_data(session, visible_ids=None, user=None):
    """Dashboard rollup. visible_ids=None means unrestricted; a list scopes the
    projects, documents, segments, runs and TM counts to those project ids.

    `user` scopes the CUSTOMER count/list to the caller's org (access.org_filter):
    projects/documents are already org-confined via visible_ids, but Customer has no
    project link, so its count would otherwise leak a cross-org total on `/`. A no-op
    with the gate off (org_filter returns the statement unchanged).

    R2-3: a CUSTOMER-BOUND user's dashboard customer count/list is confined to its
    OWN customer — otherwise the "customers: N" KPI would leak the total customer
    count to a customer account. A no-op for internal staff (current_customer_id is
    None)."""
    from app.kernel import access

    restricted = visible_ids is not None
    cq = access.org_filter(select(Customer), Customer, user).order_by(Customer.name)
    my_cust = access.current_customer_id(user)
    if my_cust is not None:
        cq = cq.where(Customer.id == my_cust)
    customers = session.exec(cq).all()
    pq = select(Project).order_by(Project.created_at.desc())
    if restricted:
        pq = pq.where(Project.id.in_(visible_ids))
    projects = session.exec(pq).all()
    cust_by_id = {c.id: c.name for c in customers}
    dq = select(Document).where(_NOT_INTERNAL)
    if restricted:
        dq = dq.where(Document.project_id.in_(visible_ids))
    documents = session.exec(dq).all()
    doc_ids = [d.id for d in documents]
    # #514: COUNT / GROUP BY in SQL (chunked IN), never a load of every Segment, run or TM
    # row just to len() it.
    status = _without_seeding(
        session,
        doc_ids,
        _group_counts(session, Segment.status, in_col=Segment.document_id, ids=doc_ids),
    )
    seg_total = sum(status.values())
    run_ids_in = {"in_col": WorkflowRun.document_id, "ids": doc_ids} if restricted else {}
    run_status = _group_counts(session, WorkflowRun.status, **run_ids_in)
    run_total = sum(run_status.values())
    # Newest 8 across the (chunked) scope: take only (started_at, id) per chunk, merge, and
    # hydrate just the winning rows.
    keys_: list = []
    for batch in _chunked(doc_ids) if restricted else [None]:
        rq = (
            select(WorkflowRun.started_at, WorkflowRun.id)
            .order_by(WorkflowRun.started_at.desc())
            .limit(8)
        )
        if batch is not None:
            rq = rq.where(WorkflowRun.document_id.in_(batch))
        keys_.extend(session.exec(rq).all())
    keys_.sort(key=lambda kv: kv[0], reverse=True)
    top_ids = [rid for _started, rid in keys_[:8]]
    by_id = (
        {r.id: r for r in session.exec(select(WorkflowRun).where(WorkflowRun.id.in_(top_ids)))}
        if top_ids
        else {}
    )
    recent = [by_id[rid] for rid in top_ids]
    wf_by_id = {w.id: w.name for w in session.exec(select(Workflow)).all()}
    doc_by_id = {d.id: d.filename for d in documents}
    recent_runs = [
        {
            "id": r.id,
            "workflow": wf_by_id.get(r.workflow_id, "—"),
            "document": doc_by_id.get(r.document_id, "—"),
            "status": r.status,
        }
        for r in recent
    ]
    approved = status.get(SegStatus.APPROVED, 0)
    if restricted:
        tm_total = _count_rows(session, SegmentTM, in_col=SegmentTM.project_id, ids=visible_ids)
        tm_total += _count_rows(session, FullTextTM, in_col=FullTextTM.project_id, ids=visible_ids)
    else:
        tm_total = _count_rows(session, SegmentTM) + _count_rows(session, FullTextTM)
    kpis = {
        "projects": len(projects),
        "customers": len(customers),
        "documents": len(documents),
        "segments": seg_total,
        "approved_pct": round(100 * approved / seg_total) if seg_total else 0,
        "runs": run_total,
        "tm": tm_total,
        "engines_live": sum(1 for pr in config.PROVIDERS if provider_is_live(pr)),
        "engines_total": len(config.PROVIDERS),
    }
    rows = _project_rows(session, projects, cust_by_id)
    order = {"in_progress": 0, "new": 1, "completed": 2}
    rows.sort(key=lambda r: (order.get(r["state"], 3), -r["updated"].timestamp()))
    summary = {
        "projects": len(projects),
        "in_progress": sum(1 for r in rows if r["state"] == "in_progress"),
        "completed": sum(1 for r in rows if r["state"] == "completed"),
        "customers": len(customers),
        "runs": run_total,
        "paused": run_status.get("paused", 0),
        # SPEC-wave3 §8.4 / RV-1: the dashboard's "N queries assigned to you" line, gated
        # on queries_enabled (returns 0 when off, so the line vanishes with the feature)
        # and scoped to the caller's visible projects. Links to /my-work.
        "my_open_queries": worklist.my_open_query_count(session, user),
    }
    return {
        "kpis": kpis,
        "rows": rows,
        "summary": summary,
        "recent_runs": recent_runs,
        "status": status,
        "run_status": run_status,
    }
