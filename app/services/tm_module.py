"""Translation Memory MANAGEMENT module — the /tm sidebar tab + `tm` CLI group.

The maintainer's ask (verbatim sense): "go through the saved TMs and see when they're used
and how often, and delete old ones." This is a MANAGEMENT surface over the EXISTING
segment TM (app.services.tm / models.SegmentTM) — NOT a new container model and NOT
an approval lifecycle (unlike the glossary MODULE, which introduced a named Glossary
container). The unit here is the SegmentTM ENTRY that already exists; this module
adds browse/filter/paginate, the NEW usage stats, a stats summary, and lifecycle
delete (single + bulk-by-filter with a dry-run count).

SCOPING (repo law, the Reports v1 seam): SegmentTM carries no org_id/customer_id of
its own — it is scoped through project_id -> Project (org_id/customer_id). So every
query here filters on a VISIBLE-PROJECT-ID set resolved ONCE by
access.visible_project_ids (org + membership + R2-3 customer confinement). A value of
None means unrestricted (single-tenant / see-all staff); a concrete list confines the
query to exactly those projects. A NULL-project ("global") TM row is visible only to
an unrestricted caller — a scoped caller (multi-org / customer-bound) never sees the
org-less global rows (matches tm._tm_scope's multi-org isolation).

USAGE TRACKING (the genuine new machinery): bump_usage() is the single write seam.
It is called at every APPLY site (translate segmented 100%, workflow step-0 100%,
apply-tm-to-document 100%) with the ids of the entries whose target was ACTUALLY
written to a segment. Fuzzy/semantic SUGGESTIONS are never passed here (only 100%
exact hits auto-apply). It is a set-based bulk UPDATE (one statement) so a run that
applies the same entry N times counts N applications without N python round-trips,
and it never breaks when an id repeats within one run.

Documented in: docs/architecture/tm-and-glossary.md
"""

from datetime import datetime, timezone

from sqlalchemy import func, update
from sqlmodel import Session, select

from app.models import Customer, Project, SegmentEmbedding, SegmentTM
from app.services import ids
from app.services.tm import _like_escape


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- customer-first browse (2026-07-14: "there is NO global TM") --------
# TM is browsed BY CUSTOMER: a customer picker drives /tm (and /api/tm, and the
# `tm list`/`tm search` CLI verbs). SegmentTM carries no customer_id of its own — it
# is scoped through project_id -> Project.customer_id — so a customer selection is
# realised by narrowing the caller's visible-project set to the chosen customer's
# projects (customer_project_ids ∩ base scope). This is BROWSE/ORGANIZATION only:
# TM MATCHING/lookup during translation (tm.lookup / lookup_batch / semantic_lookup)
# is UNTOUCHED and still keyed by the document's own project.
def browsable_customers(session: Session, *, org_id=None, bound_customer_id=None) -> list[Customer]:
    """Active (non-archived) customers to offer in the TM customer picker, sorted by
    name. Org-scoped (org_id None = single-tenant no-op). A customer-BOUND caller
    (bound_customer_id given, R2-3) collapses to ONLY their own customer, so the
    picker is never rendered for them and their customer is forced.

    #73: under an ACTIVE multi-org gate a bare None org_id is FAIL-CLOSED (empty
    picker), not see-all — access.org_scope_blocked is the single authority for that
    reading and it matches same_org()/org_filter(). `access.UNSCOPED` (the key-verified
    CLI operator door) still means "every org". Gate off: unchanged, byte-identical."""
    from app.services import access
    from app.services import customers as customer_service

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = customer_service.active_only(select(Customer))
    if org_id is not None:
        stmt = stmt.where(Customer.org_id == org_id)
    if bound_customer_id is not None:
        stmt = stmt.where(Customer.id == bound_customer_id)
    return list(session.exec(stmt.order_by(Customer.name)).all())


def customer_project_ids(session: Session, customer_id: int) -> list[int]:
    """The project ids owned by a customer — the confinement set a customer selection
    narrows the TM browse to."""
    return list(session.exec(select(Project.id).where(Project.customer_id == customer_id)).all())


def resolve_browse_customer(customers, requested_id):
    """Pick the EFFECTIVE customer for a customer-first browse, or None (show the
    picker). `customers` is the visible/browsable set (already org + bound scoped).

      * a `requested_id` (from ?customer=) that is in the visible set -> that customer;
      * exactly ONE visible customer -> auto-selected (the maintainer: "If I only have 1
        customer it's always selected");
      * otherwise None -> the caller shows the picker + an empty state, NEVER an
        all-customers TM dump.

    A requested id NOT in the visible set resolves to None (falls back to the picker /
    auto-select rule) — a foreign/archived id can never scope the browse."""
    by_id = {c.id: c for c in customers}
    if requested_id is not None:
        n = _coerce_customer_id(requested_id)
        if n is not None and n in by_id:
            return by_id[n]
    if len(customers) == 1:
        return customers[0]
    return None


def scoped_pids_for_customer(session: Session, base_pids, customer_id) -> list[int]:
    """Narrow a base visible-project set to ONE customer's projects (the realisation
    of a customer selection over TM's project-scoped queries). base_pids None means
    the caller is otherwise unrestricted (single-tenant / see-all), so the result is
    simply the customer's own projects; a concrete base set is intersected so a scoped
    caller can never widen past its ring by choosing a customer."""
    cust_pids = set(customer_project_ids(session, customer_id))
    if base_pids is None:
        return sorted(cust_pids)
    return sorted(cust_pids & set(base_pids))


def _coerce_customer_id(v):
    """'' / None / non-digit / malformed -> None; an int or digit-string in range -> int.
    Routes through the app-wide id law so a request-supplied ?customer= that is a Unicode
    digit / >2**63 value resolves to None (falls back to the picker), never a 500."""
    return ids.coerce_id(v)


# --- usage tracking (the write seam wired into the translate path) ------------
def bump_usage(session: Session, entry_ids, project_id=None) -> int:
    """Record that the given SegmentTM entries were APPLIED (target written to a
    segment) — one bulk UPDATE incrementing use_count and stamping last_used_at.

    `entry_ids` is any iterable of ids (a Counter of applications is fine — it is
    de-duplicated to a set; the COUNT of applications is bumped by the CALLER passing
    an id once per application, i.e. increment by the number of times each id appears
    — see the loop callers which accumulate a list). To keep the hot path cheap the
    accumulation strategy is: callers collect the applied ids into a plain list
    (duplicates allowed) and hand it here; this function groups by id and issues one
    UPDATE per distinct increment-count bucket (usually just "+1"), so a whole
    document's TM hits cost at most a handful of statements regardless of segment
    count. Returns the number of distinct entries bumped. A None/empty list is a
    no-op (no query). NEVER raises into the translate path — a usage-tracking failure
    must not fail a translation, so any error rolls back only the counter write and
    is swallowed.

    `project_id` confines use_count to the applying project's own rows.
    Attached-source applications refresh only activity_at for retention;
    they do not change the source owner's curation counters."""
    ids = [i for i in (entry_ids or []) if i is not None]
    if not ids:
        return 0
    # Group ids by how many times each was applied in this batch (a segment TM entry
    # can legitimately apply to many identical segments in one run). Almost always
    # every count is 1; grouping keeps it correct + still one UPDATE per bucket.
    counts: dict[int, int] = {}
    for i in ids:
        counts[i] = counts.get(i, 0) + 1
    by_delta: dict[int, list[int]] = {}
    for entry_id, delta in counts.items():
        by_delta.setdefault(delta, []).append(entry_id)

    now = _utcnow()
    try:
        bumped = 0
        for delta, id_group in by_delta.items():
            where = [SegmentTM.id.in_(id_group)]
            if project_id is not None:
                # The confinement. Applied in the UPDATE itself rather than by
                # pre-filtering the ids in Python, so there is exactly ONE place a
                # foreign row could be reached and it is closed in SQL.
                where.append(SegmentTM.project_id == project_id)
            res = session.exec(
                update(SegmentTM)
                .where(*where)
                .values(use_count=SegmentTM.use_count + delta, last_used_at=now, activity_at=now)
            )
            bumped += res.rowcount if getattr(res, "rowcount", -1) >= 0 else len(id_group)
        if project_id is not None:
            from app.services.project_sources import allowed_tm_source_ids

            sources = allowed_tm_source_ids(session, project_id)
            for start in range(0, len(ids), 500):
                session.exec(
                    update(SegmentTM)
                    .where(
                        SegmentTM.id.in_(ids[start : start + 500]),
                        (SegmentTM.project_id.in_(sources) | SegmentTM.project_id.is_(None)),
                    )
                    .values(activity_at=now)
                )
        session.commit()
        # Report what was ACTUALLY bumped: with a confinement in play the count of
        # DISTINCT ids handed in overstates it whenever borrowed rows were applied.
        return bumped if project_id is not None else len(counts)
    except Exception:
        try:  # noqa: SIM105
            session.rollback()
        except Exception:  # noqa: S110 -- use-count bump is advisory; 0 bumped is reported
            pass
        return 0


# --- scoping ------------------------------------------------------------------
def _scope_clause(visible_pids):
    """The project-scope predicate for a TM query, or None (unrestricted).

    visible_pids None = unrestricted (single-tenant / see-all staff): no clause, so
    every row — including NULL-project global rows — is visible. A concrete list =
    confine to exactly those projects (a scoped caller never sees NULL-project global
    rows, matching tm._tm_scope's multi-org isolation)."""
    if visible_pids is None:
        return None
    return SegmentTM.project_id.in_(list(visible_pids))


def _apply_filters(
    stmt,
    *,
    visible_pids=None,
    source_locale=None,
    target_locale=None,
    project_id=None,
    content_type_id=None,
    q=None,
    created_before=None,
    created_after=None,
    min_use=None,
    max_use=None,
    unused=None,
):
    """Attach the shared filter set to a SegmentTM select/count statement.

    Every filter is optional; a None value is a no-op. `q` is a case-insensitive
    substring matched against EITHER source_text OR target_text (LIKE wildcards
    escaped so they match literally). `unused` True = never-used entries only
    (use_count == 0); `min_use`/`max_use` bound use_count. `created_before/after`
    bound created_at (naive-UTC datetimes, the _utcnow convention).

    `content_type_id` (SPEC-content-types phase 3) is a BROWSE filter, distinct from
    the read-scope predicate the translate path uses: it matches EXACTLY the given
    content type (so a manager can inspect one domain's TM), not own-type-plus-General.
    The sentinel string "__none__" filters to the General (NULL-content-type) bucket;
    an int/digit filters to that type id; None = no filter (every content type)."""
    scope = _scope_clause(visible_pids)
    if scope is not None:
        stmt = stmt.where(scope)
    if source_locale:
        stmt = stmt.where(SegmentTM.source_locale == source_locale)
    if target_locale:
        stmt = stmt.where(SegmentTM.target_locale == target_locale)
    if project_id is not None:
        stmt = stmt.where(SegmentTM.project_id == project_id)
    if content_type_id is not None:
        if str(content_type_id) == "__none__":
            stmt = stmt.where(SegmentTM.content_type_id.is_(None))
        else:
            # Single app-wide id law: content_type_id arrives as a request-supplied
            # string from the web/CLI/api browse filters. A malformed value (a Unicode
            # digit int() rejects, or an out-of-range value SQLite rejects at the bind
            # layer) matches no content type -> empty browse result, never a 500.
            ct_id = ids.coerce_id(content_type_id)
            stmt = stmt.where(SegmentTM.content_type_id == (ct_id if ct_id is not None else -1))
    if q:
        s = q.strip().lower()
        if s:
            esc = _like_escape(s)
            pat = "%" + esc + "%"
            stmt = stmt.where(
                func.lower(SegmentTM.source_text).like(pat, escape="\\")
                | func.lower(SegmentTM.target_text).like(pat, escape="\\")
            )
    if created_before is not None:
        stmt = stmt.where(SegmentTM.created_at < created_before)
    if created_after is not None:
        stmt = stmt.where(SegmentTM.created_at >= created_after)
    if unused:
        stmt = stmt.where(SegmentTM.use_count == 0)
    if min_use is not None:
        stmt = stmt.where(SegmentTM.use_count >= min_use)
    if max_use is not None:
        stmt = stmt.where(SegmentTM.use_count <= max_use)
    return stmt


def entry_to_dict(e: SegmentTM) -> dict:
    """JSON/CLI-serializable view of one TM entry (source/target, locales, origin,
    created, usage stats). `origin` is DERIVED, not stored: the segment TM has no
    provenance column, so we report the neutral "segment" origin every SegmentTM row
    shares (import_tmx + approve write-back both land here). A richer per-origin split
    is a documented DEFER (would need an origin column)."""
    return {
        "id": e.id,
        "project_id": e.project_id,
        "content_type_id": e.content_type_id,
        "source_locale": e.source_locale,
        "target_locale": e.target_locale,
        "source_text": e.source_text,
        "target_text": e.target_text,
        "origin": "segment",
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "use_count": e.use_count or 0,
        "last_used_at": e.last_used_at.isoformat() if e.last_used_at else None,
    }


# --- browse (paginated) -------------------------------------------------------
def count_entries(session: Session, **filters) -> int:
    """Total matching entries for the given filters (cheap COUNT — never loads
    rows). Same filter kwargs as list_entries."""
    stmt = _apply_filters(select(func.count()).select_from(SegmentTM), **filters)
    return session.exec(stmt).one()


def list_entries(
    session: Session, *, page: int = 1, per_page: int = 50, order: str = "recent", **filters
) -> dict:
    """One page of matching TM entries + the total count (so a page never loads the
    whole table — TMs can be huge). Returns
    {entries: [entry_dict], total, page, per_page, pages}.

    `order` one of: recent (created desc, the default), oldest (created asc),
    most_used (use_count desc), least_used (use_count asc), recently_used
    (last_used_at desc, NULLs last). Filters: source_locale, target_locale,
    project_id, q, created_before, created_after, min_use, max_use, unused,
    visible_pids (the scope set)."""
    page = max(1, int(page or 1))
    per_page = max(1, min(500, int(per_page or 50)))
    total = count_entries(session, **filters)

    stmt = _apply_filters(select(SegmentTM), **filters)
    if order == "oldest":
        stmt = stmt.order_by(SegmentTM.created_at.asc(), SegmentTM.id.asc())
    elif order == "most_used":
        stmt = stmt.order_by(SegmentTM.use_count.desc(), SegmentTM.id.desc())
    elif order == "least_used":
        stmt = stmt.order_by(SegmentTM.use_count.asc(), SegmentTM.id.asc())
    elif order == "recently_used":
        # NULLs last: an entry that was never used sorts after every used one.
        stmt = stmt.order_by(
            SegmentTM.last_used_at.is_(None), SegmentTM.last_used_at.desc(), SegmentTM.id.desc()
        )
    else:  # recent (default)
        stmt = stmt.order_by(SegmentTM.created_at.desc(), SegmentTM.id.desc())

    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    rows = session.exec(stmt).all()
    pages = (total + per_page - 1) // per_page if total else 0
    return {
        "entries": [entry_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


def get_entry(session: Session, entry_id: int, visible_pids=None) -> SegmentTM | None:
    """Fetch one TM entry only if it is in the caller's scope (else None -> 404).
    The single read-authz seam for a single-entry route/command."""
    e = session.get(SegmentTM, entry_id)
    if e is None:
        return None
    if visible_pids is not None and e.project_id not in set(visible_pids):
        return None
    return e


class TMEditRefused(ValueError):
    """Raised when a TM target EDIT is rejected because the new text trips the same
    refusal/hedge gate every TM WRITE goes through (quality.is_refusal_check, the
    commit_mt_output / import_tmx choke point). An edit must not become a bypass seam
    for text the write path would refuse — a stored hedge is a 100%-match that would
    poison every later document. The route/CLI/api surface it as a clean 400 /
    EXIT_USAGE, never a 500."""


class TMEditIntegrityRefused(TMEditRefused):
    """Issue #567: the edited target breaks placeholders or inline tags the way the
    approve-time gate (#562) refuses. `code` is the shared skip code (`placeholder_integrity`,
    `tag_integrity`, `tagged_source`); only `placeholder_integrity` can be overridden with
    `acknowledge_placeholder_loss=True`. A TMEditRefused subclass, so every surface that
    already maps TMEditRefused to a clean 400 / usage error does so for this too."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def edit_entry(
    session: Session,
    entry_id: int,
    new_target: str,
    *,
    visible_pids=None,
    actor: str = "system",
    ip: str = "",
    acknowledge_placeholder_loss: bool = False,
) -> SegmentTM | None:
    """Edit the TARGET text of one TM entry IN PLACE (scoped), audited old->new.

    SEMANTICS (documented):
      * Only the TARGET is editable. Editing the SOURCE is OUT — the source (+ its
        source_hash) is the entry's match key/identity; changing it would be a
        different entry, not an edit. Callers pass target only.
      * The new target runs through quality.is_refusal_check — the SAME gate
        commit_mt_output / import_tmx enforce on every TM write. A refusal/hedge is
        REJECTED (TMEditRefused), never stored: an edit is a write site and must join
        the refusal perimeter, not bypass it.
      * INTEGRITY (Issue #567): the pair (entry source, new target) runs through the SAME
        gate segment approve applies (tm_common.integrity_skip: quality.placeholder_violation
        and quality.tag_damage with default args, tagged sources). A placeholder drop/add is
        REFUSED (TMEditIntegrityRefused, code placeholder_integrity) unless
        `acknowledge_placeholder_loss=True`; tag breakage is refused unconditionally. An
        acknowledged loss is recorded in the audit detail.
      * A blank target is rejected (ValueError) — an empty TM target is useless and
        would silently blank a 100%-match's output.
      * EMBEDDING: SegmentEmbedding is keyed by segment_tm_id and embeds the entry's
        NORMALIZED SOURCE (models.SegmentEmbedding / tm.upsert_embedding). A
        target-only edit leaves the source (and thus source_hash + the embedding
        vector) unchanged, so NO embedding invalidation/re-embed is needed — the
        vector stays valid. (Verified: only a source change would require a re-embed;
        that path is not offered.)
      * activity_at records the edit for retention; usage stats stay unchanged.
        The audit log is the edit's provenance record.

    Returns the updated entry, None when it is missing / out of scope (route 404s)."""
    from app.services import audit, quality

    e = get_entry(session, entry_id, visible_pids=visible_pids)
    if e is None:
        return None
    text = (new_target or "").strip()
    if not text:
        raise ValueError("the new TM target text is required (cannot be blank).")
    if quality.is_refusal_check(text):
        raise TMEditRefused(
            "the edited target reads as a refusal/hedge and was rejected - TM edits "
            "run through the same guard as TM writes, so a hedge cannot be stored."
        )
    old = e.target_text
    if text == old:
        return e  # no-op: identical target, nothing to write or audit
    from app.services.tm_common import TM_SKIP_PLACEHOLDER, integrity_skip

    skip = integrity_skip(
        e.source_text, text, source_locale=e.source_locale, target_locale=e.target_locale
    )
    acknowledged = False
    if skip is not None:
        if skip[0] == TM_SKIP_PLACEHOLDER and acknowledge_placeholder_loss:
            acknowledged = True
        else:
            hint = (
                "; acknowledge the placeholder loss to save it anyway"
                if skip[0] == TM_SKIP_PLACEHOLDER
                else ""
            )
            raise TMEditIntegrityRefused(
                f"the edited target was rejected ({skip[0]}): {skip[1]}{hint}.", code=skip[0]
            )
    e.target_text = text
    e.activity_at = _utcnow()
    session.add(e)
    session.commit()
    session.refresh(e)
    audit.log(
        actor=actor or "system",
        action="tm.edit",
        target=f"tm entry {entry_id}",
        detail=f"target {old!r} -> {text!r}"
        + (";placeholder_loss_acknowledged" if acknowledged else ""),
        ip=ip,
    )
    return e


# --- stats summary ------------------------------------------------------------
def stats(session: Session, visible_pids=None, top_n: int = 5) -> dict:
    """Cheap aggregate summary for the page header: total entries, per-pair counts,
    used-vs-never-used split, top-used entries, oldest/newest created. All bounded
    aggregate queries (no full-table load into python)."""
    scope = _scope_clause(visible_pids)

    def _scoped(stmt):
        return stmt.where(scope) if scope is not None else stmt

    total = session.exec(_scoped(select(func.count()).select_from(SegmentTM))).one()
    used = session.exec(
        _scoped(select(func.count()).select_from(SegmentTM).where(SegmentTM.use_count > 0))
    ).one()
    total_applications = session.exec(
        _scoped(select(func.coalesce(func.sum(SegmentTM.use_count), 0)).select_from(SegmentTM))
    ).one()

    # Per-language-pair counts (a small GROUP BY, one row per distinct pair).
    pair_rows = session.exec(
        _scoped(
            select(SegmentTM.source_locale, SegmentTM.target_locale, func.count())
            .select_from(SegmentTM)
            .group_by(SegmentTM.source_locale, SegmentTM.target_locale)
            .order_by(func.count().desc())
        )
    ).all()
    pairs = [{"source_locale": r[0], "target_locale": r[1], "count": r[2]} for r in pair_rows]

    oldest = session.exec(
        _scoped(select(func.min(SegmentTM.created_at)).select_from(SegmentTM))
    ).one()
    newest = session.exec(
        _scoped(select(func.max(SegmentTM.created_at)).select_from(SegmentTM))
    ).one()

    top_stmt = (
        _scoped(select(SegmentTM).where(SegmentTM.use_count > 0))
        .order_by(SegmentTM.use_count.desc(), SegmentTM.id.desc())
        .limit(max(0, int(top_n)))
    )
    top = [entry_to_dict(e) for e in session.exec(top_stmt).all()]

    return {
        "total": total,
        "used": used,
        "never_used": total - used,
        "total_applications": total_applications,
        "pairs": pairs,
        "oldest_created_at": oldest.isoformat() if oldest else None,
        "newest_created_at": newest.isoformat() if newest else None,
        "top_used": top,
    }


# --- lifecycle: delete (single + bulk-by-filter, with embedding invalidation) -
def _invalidate_embeddings(session: Session, entry_ids) -> None:
    """Remove the semantic-TM vectors for the given SegmentTM ids so a deleted entry
    can never be resurrected by semantic search. SegmentEmbedding is keyed by
    segment_tm_id (models.SegmentEmbedding); we delete every matching vector BEFORE
    (or with) the entry row itself. Done in id chunks so a huge bulk delete never
    builds an unbounded IN-list."""
    ids = [i for i in (entry_ids or []) if i is not None]
    CHUNK = 500
    for i in range(0, len(ids), CHUNK):
        group = ids[i : i + CHUNK]
        for emb in session.exec(
            select(SegmentEmbedding).where(SegmentEmbedding.segment_tm_id.in_(group))
        ).all():
            session.delete(emb)


def delete_entry(session: Session, entry_id: int, visible_pids=None) -> bool:
    """Delete ONE TM entry (scoped) + its semantic embedding. Returns True on delete,
    False if the entry is missing or out of scope (the route/command 404s)."""
    e = get_entry(session, entry_id, visible_pids=visible_pids)
    if e is None:
        return False
    _invalidate_embeddings(session, [e.id])
    session.delete(e)
    session.commit()
    return True


class BulkDeleteRefused(ValueError):
    """A bulk delete (or its dry-run preview) was refused BEFORE touching any row:
    a malformed filter, or a request that is not scoped to one customer. Callers map
    it to a 400 / usage error. Fail closed (#490)."""


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def parse_flag(value, name: str) -> bool:
    """A strict boolean request flag: None/"" -> False, a bool, 0/1, or a recognised
    true/false word. Anything else is refused (a typo in dry_run must never turn a
    preview into a real delete)."""
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE or v == "":
            return False
    raise BulkDeleteRefused(f"{name} must be true or false.")


def strict_bulk_filters(raw: dict) -> dict:
    """Validate the raw bulk-delete filter inputs (JSON body values or form strings)
    into the tm_module filter kwargs, REFUSING anything malformed instead of dropping
    it. Dropping a filter widens the delete (a bad `created_before` would otherwise
    delete every entry, not just the old ones). #490 part 2."""

    def _text(name):
        v = raw.get(name)
        if v is None:
            return None
        if not isinstance(v, str):
            raise BulkDeleteRefused(f"{name} must be text.")
        return v.strip() or None

    def _count(name):
        v = raw.get(name)
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            raise BulkDeleteRefused(f"{name} must be a non-negative whole number.")
        if isinstance(v, int):
            n = v
        elif isinstance(v, str) and v.strip().isascii() and v.strip().isdigit():
            n = int(v.strip())
        else:
            raise BulkDeleteRefused(f"{name} must be a non-negative whole number.")
        if n < 0 or n >= 2**63:
            raise BulkDeleteRefused(f"{name} must be a non-negative whole number.")
        return n

    def _date(name):
        v = raw.get(name)
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            raise BulkDeleteRefused(f"{name} must be a YYYY-MM-DD date.")
        try:
            d = datetime.strptime(v.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise BulkDeleteRefused(f"{name} must be a YYYY-MM-DD date.") from exc
        return d.replace(tzinfo=timezone.utc).replace(tzinfo=None)

    def _ctype():
        v = raw.get("content_type_id")
        if v is None or v == "":
            return None
        if isinstance(v, bool):
            raise BulkDeleteRefused("content_type_id must be an id or __none__.")
        if isinstance(v, int):
            return str(v) if v >= 0 else _bad_ctype()
        if isinstance(v, str):
            s = v.strip()
            if s == "":
                return None
            if s == "__none__":
                return "__none__"
            if s.isascii() and s.isdigit():
                return s
        return _bad_ctype()

    def _bad_ctype():
        raise BulkDeleteRefused("content_type_id must be an id or __none__.")

    return {
        "source_locale": _text("source_locale"),
        "target_locale": _text("target_locale"),
        "project_id": _count("project_id"),
        "content_type_id": _ctype(),
        "q": _text("q"),
        "created_before": _date("created_before"),
        "created_after": _date("created_after"),
        "min_use": _count("min_use"),
        "max_use": _count("max_use"),
        "unused": parse_flag(raw.get("unused"), "unused"),
    }


def scope_label(customer, project_id=None) -> str:
    """A short audit label for the scope a bulk TM mutation ran under (#490)."""
    cid = _coerce_customer_id(customer) if customer not in (None, "") else None
    if cid is not None:
        return f"customer {cid}"
    if project_id is not None:
        return f"project {project_id}"
    return "single customer"


def resolve_bulk_delete_scope(
    session: Session, base_pids, *, customers, customer, project_id, what="bulk delete"
):
    """The project-id scope a bulk delete may touch, or BulkDeleteRefused.

    TM is organised by customer (there is NO global TM), so a bulk delete must be
    scoped server side to ONE customer, never trusted to the client's page:
      * `customer` given -> must be one of `customers` (the caller's browsable set;
        None skips the membership check, for the trusted in-process CLI, where the
        intersection with `base_pids` still leaves a foreign id with no projects);
        scope = that customer's projects intersected with the caller's own ring;
      * no customer but a `project_id` filter -> one project belongs to one customer,
        so the filter already is a single-customer scope;
      * neither -> allowed only when the caller's whole visible set spans at most one
        customer, otherwise refused (an unscoped request deletes nothing).
    """
    if customer not in (None, ""):
        cid = _coerce_customer_id(customer)
        if cid is None or (customers is not None and cid not in {c.id for c in customers}):
            raise BulkDeleteRefused("customer is not one of your customers; nothing was deleted.")
        return scoped_pids_for_customer(session, base_pids, cid)
    if project_id is not None:
        return base_pids
    stmt = select(Project.customer_id).distinct()
    if base_pids is not None:
        stmt = stmt.where(Project.id.in_(list(base_pids)))
    if len(set(session.exec(stmt).all())) > 1:
        raise BulkDeleteRefused(
            f"{what} must be scoped to one customer (pass customer, or a project "
            "filter); nothing was changed."
        )
    return base_pids


def bulk_delete(session: Session, *, dry_run: bool = False, **filters) -> int:
    """Delete every entry matching the filters (the same filter kwargs as
    list_entries — e.g. unused=True, pair es-ES, project N, created_before X), and
    invalidate their semantic embeddings. When dry_run is True NOTHING is deleted and
    the COUNT that WOULD be deleted is returned (so the web can show "this will delete
    N entries" and the CLI --dry-run prints the count) — this is the count-first
    confirm step. Returns the number deleted (or that would be deleted on a dry run).

    Safety: a bulk_delete with NO filters at all (would wipe the whole visible TM) is
    still allowed but the caller (web confirm / CLI --yes) gates it; the count-first
    step makes an accidental all-rows sweep visible before it runs."""
    count = count_entries(session, **filters)
    if dry_run or count == 0:
        return count
    # Fetch the ids in the matching set, then delete embeddings + rows. We select ids
    # (not whole rows) and page through so a giant match never loads every row's text.
    id_stmt = _apply_filters(select(SegmentTM.id), **filters)
    ids = [row for row in session.exec(id_stmt).all()]  # noqa: C416
    _invalidate_embeddings(session, ids)
    CHUNK = 500
    for i in range(0, len(ids), CHUNK):
        group = ids[i : i + CHUNK]
        for e in session.exec(select(SegmentTM).where(SegmentTM.id.in_(group))).all():
            session.delete(e)
    session.commit()
    return len(ids)


# --- historical hedge scrub (one-off remediation, Deliverable 4) --------------
def _ft_scope_clause(visible_pids):
    """FullTextTM project-scope predicate (mirrors _scope_clause for SegmentTM).
    None = unrestricted (sees NULL-project globals); a concrete list confines to
    exactly those projects (a scoped caller never sees org-less global rows)."""
    from app.models import FullTextTM

    if visible_pids is None:
        return None
    return FullTextTM.project_id.in_(list(visible_pids))


def scrub_hedges(
    session: Session,
    *,
    apply: bool = False,
    actor: str = "system",
    visible_pids=None,
    project_id=None,
    scope_label: str | None = None,
) -> dict:
    """Scan BOTH stored-TM targets — segment TM (SegmentTM) and full-text TM
    (FullTextTM) — for refusal / meta-assistant HEDGES and, on `apply`, quarantine
    them (delete the poisoned row/entry). This is the one-off remediation for TM that
    was poisoned BEFORE the import gate + choke point existed: a stored hedge is a
    100%%-match that bypasses the live guard on every later document forever.

    DRY-RUN by default (`apply=False`): NOTHING is deleted; returns the matches
    (id, kind, pair, project_id, first 80 chars of the target) so a reviewer sees
    exactly what `--apply` would remove. `apply=True` deletes the matching SegmentTM
    rows (+ invalidates their semantic embeddings) and FullTextTM entries, and writes
    ONE audit row summarizing the run.

    NEVER touches `Segment.target_text` history (only the TM stores). PROVENANCE
    CAVEAT (documented, per the spec): SegmentTM/FullTextTM carry NO provenance column
    — there is no way to tell an MT-imported row from one a human later edited. So the
    scrub relies on `refusal.is_refusal`'s CONSERVATIVE, position-anchored contract as
    the safety criterion: it flags only a leading assistant hedge / a tiny set of
    high-confidence full phrases, and by design essentially never fires on a genuine
    human translation. That conservatism IS the "delete only exact refusal-marker
    matches" guarantee — a legitimate translation that merely mentions refusal
    vocabulary mid-string is not matched, so it is not deleted.

    Scoped through project_id like every other tm_module query: `visible_pids` (org /
    membership / customer confinement) and an optional single-`project_id` filter."""
    from app.models import FullTextTM
    from app.services import audit, refusal

    seg_hits: list[dict] = []
    seg_stmt = _apply_filters(select(SegmentTM), visible_pids=visible_pids, project_id=project_id)
    for e in session.exec(seg_stmt).all():
        if refusal.is_refusal(e.target_text):
            seg_hits.append(
                {
                    "id": e.id,
                    "kind": "segment",
                    "project_id": e.project_id,
                    "pair": f"{e.source_locale}->{e.target_locale}",
                    "sample": (e.target_text or "")[:80],
                }
            )

    ft_hits: list[dict] = []
    ft_stmt = select(FullTextTM)
    ft_scope = _ft_scope_clause(visible_pids)
    if ft_scope is not None:
        ft_stmt = ft_stmt.where(ft_scope)
    if project_id is not None:
        ft_stmt = ft_stmt.where(FullTextTM.project_id == project_id)
    for e in session.exec(ft_stmt).all():
        if refusal.is_refusal(e.target_text):
            ft_hits.append(
                {
                    "id": e.id,
                    "kind": "full_text",
                    "project_id": e.project_id,
                    "pair": f"{e.source_locale}->{e.target_locale}",
                    "sample": (e.target_text or "")[:80],
                }
            )

    matched = len(seg_hits) + len(ft_hits)
    deleted = 0
    if apply and matched:
        seg_ids = [h["id"] for h in seg_hits]
        _invalidate_embeddings(session, seg_ids)
        CHUNK = 500
        for i in range(0, len(seg_ids), CHUNK):
            group = seg_ids[i : i + CHUNK]
            for e in session.exec(select(SegmentTM).where(SegmentTM.id.in_(group))).all():
                session.delete(e)
                deleted += 1
        ft_ids = [h["id"] for h in ft_hits]
        for i in range(0, len(ft_ids), CHUNK):
            group = ft_ids[i : i + CHUNK]
            for e in session.exec(select(FullTextTM).where(FullTextTM.id.in_(group))).all():
                session.delete(e)
                deleted += 1
        session.commit()
        scope_note = scope_label or (
            f"project {project_id}"
            if project_id is not None
            else ("scoped" if visible_pids is not None else "all")
        )
        audit.log(
            actor=actor,
            action="tm.scrub_hedges",
            target=f"tm ({scope_note})",
            detail=f"deleted {deleted} hedged TM entr{'y' if deleted == 1 else 'ies'} "
            f"(segment={len(seg_ids)}, full_text={len(ft_ids)})",
        )

    return {
        "matched": matched,
        "segment": seg_hits,
        "full_text": ft_hits,
        "applied": bool(apply),
        "deleted": deleted,
    }


# --- historical scaffold-echo remediation (one-off, core-QA Part 1 cleanup) ---
def scrub_scaffold(
    session: Session, *, apply: bool = False, actor: str = "system", project_id=None
) -> dict:
    """Scan Segment TARGETS and BOTH stored-TM targets (SegmentTM + FullTextTM) for
    PROMPT-SCAFFOLD echoes -- our OWN injected fences / LQA-findings preamble persisted as
    a translation -- and, on `apply`, QUARANTINE them. The one-off remediation for content
    poisoned BEFORE the Q10 scaffold-echo gate shipped (core-QA campaign Part 1).

    WHY. The Q10 gate (`app.services.scaffold` + `quality.commit_mt_output`) stops FUTURE
    seeding, but rows written before it carry the leak forever. Known prod case: doc 83
    (doc09.docx, es-ES) -- seg 11 stored `... MQM LQA encuentra para resolver:` as CLEAN
    `mt`, segs 0/12 stored the fenced style guide, and any SegmentTM/FullTextTM seeded from
    those carries it too.

    DETECTION REUSES `app.services.scaffold.scaffold_echo` -- the SAME single-sourced marker
    set the live gate uses, with the same ABSTAIN-WHEN-THE-SOURCE-CARRIES-IT rule (a source
    that legitimately says "MQM LQA" or carries a fence tag is not a leak, so it is not
    matched). Markers are NEVER re-hardcoded here.

    DRY-RUN by default (`apply=False`): nothing is written; returns the matches (id, kind,
    pair/locale, first 80 chars) so a reviewer sees exactly what `--apply` would touch.
    `apply=True`:
      * SEGMENTS -> routed to NEEDS_REVIEW with the scaffold qa_flag, using the SAME flag
        shape / type / reason the live gate applies to a fresh echo (`quality.scaffold_echo_flag`
        via `refusal.merge_nontranslation_flag`) -- differing only in the honest `source`
        provenance ("remediation" here vs "mt"/"workflow" at the live seam). The target TEXT is
        NEVER rewritten or deleted -- a reviewer must still SEE the leaked structure to fix it.
        IDEMPOTENT for the common case: a segment ALREADY at NEEDS_REVIEW carrying the scaffold
        flag is reported but not re-written, so a second `--apply` changes nothing (a scrubbed
        segment still MATCHES on re-scan because its text is unchanged -- that is expected,
        hence `would_quarantine` counts only the rows that still NEED the change, never the
        whole matched set). NOTE: a segment a human has APPROVED that STILL carries poisoned
        text is deliberately re-downgraded to NEEDS_REVIEW on each run (poison must not stay
        approved) -- so re-running against still-approved poison is the one non-idempotent path
        and will write a fresh audit row each time.
      * SEGMENT_TM / FULL_TEXT -> DELETED (+ semantic embeddings invalidated), exactly like
        `scrub_hedges`: a poisoned TM row is a 100%-match that re-seeds the leak on every
        later document, and unlike a segment it has NO review surface, so removal is the only
        correct quarantine.
    Writes ONE audit row summarizing an applied run (mirrors `scrub_hedges`).

    `project_id` optionally confines the scan to one project (segments via their document);
    default None scans EVERYTHING -- this is a global data-integrity sweep, incl. throwaway
    customer data. NOT org/visible_pids-scoped: it is a local operator remediation
    (`db scrub-scaffold`), not a customer-facing TM lifecycle act.

    SCALE: like `scrub_hedges`, the scan materializes the matching rows in memory. Fine for
    the known cleanup targets (the prod DB is small); on a very large corpus prefer running it
    project-by-project via `project_id`, and take a `db backup` first -- the TM deletes are
    irreversible."""
    from app.models import Document, FullTextTM, Segment, SegStatus
    from app.services import audit, quality, refusal, scaffold

    def _has_scaffold_flag(flags) -> bool:
        """True when qa_flags already carries THIS remediation's verdict (a scaffold-reason
        entry in the nontranslation family) -- the idempotency test."""
        return any(
            isinstance(f, dict)
            and f.get("type") == refusal.NONTRANSLATION_FLAG_TYPE
            and str(f.get("reason") or "").startswith(scaffold.SCAFFOLD_ECHO_REASON)
            for f in (flags or [])
        )

    # --- Segments: QUARANTINE (flag + needs_review), never delete/rewrite --------
    seg_stmt = select(Segment)
    if project_id is not None:
        doc_ids = [  # noqa: C416
            d
            for d in session.exec(
                select(Document.id).where(Document.project_id == project_id)
            ).all()
        ]
        # empty doc set -> a predicate matching nothing (never a bare/unscoped scan).
        seg_stmt = seg_stmt.where(Segment.document_id.in_(doc_ids or [-1]))
    seg_hits: list[dict] = []
    seg_to_change: list = []
    for seg in session.exec(seg_stmt).all():
        reason = scaffold.scaffold_echo(seg.target_text, seg.source_text)
        if not reason:
            continue
        already = seg.status == SegStatus.NEEDS_REVIEW and _has_scaffold_flag(seg.qa_flags)
        seg_hits.append(
            {
                "id": seg.id,
                "kind": "segment",
                "document_id": seg.document_id,
                "target_locale": seg.target_locale,
                "status": seg.status,
                "already_quarantined": already,
                "sample": (seg.target_text or "")[:80],
            }
        )
        if not already:
            seg_to_change.append((seg, reason))

    # --- SegmentTM + FullTextTM: DELETE (like scrub_hedges) ----------------------
    def _tm_scan(model, kind: str) -> list[dict]:
        stmt = select(model)
        if project_id is not None:
            stmt = stmt.where(model.project_id == project_id)
        hits = []
        for e in session.exec(stmt).all():
            if scaffold.scaffold_echo(e.target_text, e.source_text):
                hits.append(
                    {
                        "id": e.id,
                        "kind": kind,
                        "project_id": e.project_id,
                        "pair": f"{e.source_locale}->{e.target_locale}",
                        "sample": (e.target_text or "")[:80],
                    }
                )
        return hits

    seg_tm_hits = _tm_scan(SegmentTM, "segment_tm")
    ft_hits = _tm_scan(FullTextTM, "full_text")

    matched = len(seg_hits) + len(seg_tm_hits) + len(ft_hits)
    quarantined = deleted = 0
    if apply and matched:
        for seg, reason in seg_to_change:
            seg.qa_flags = refusal.merge_nontranslation_flag(
                seg.qa_flags, quality.scaffold_echo_flag(reason, source="remediation")
            )
            seg.status = SegStatus.NEEDS_REVIEW
            session.add(seg)
            quarantined += 1
        seg_tm_ids = [h["id"] for h in seg_tm_hits]
        _invalidate_embeddings(session, seg_tm_ids)
        CHUNK = 500
        for i in range(0, len(seg_tm_ids), CHUNK):
            group = seg_tm_ids[i : i + CHUNK]
            for e in session.exec(select(SegmentTM).where(SegmentTM.id.in_(group))).all():
                session.delete(e)
                deleted += 1
        ft_ids = [h["id"] for h in ft_hits]
        for i in range(0, len(ft_ids), CHUNK):
            group = ft_ids[i : i + CHUNK]
            for e in session.exec(select(FullTextTM).where(FullTextTM.id.in_(group))).all():
                session.delete(e)
                deleted += 1
        session.commit()
        # Audit ONLY a run that actually CHANGED something. Unlike scrub_hedges (where any
        # match is deleted, so matched>0 == changed>0), a scaffold match can be a segment
        # ALREADY quarantined -- its text is unchanged so it re-matches forever. A second
        # --apply that changes nothing must not write a no-op "quarantined 0, deleted 0" row.
        if quarantined or deleted:
            scope_note = f"project {project_id}" if project_id is not None else "all"
            audit.log(
                actor=actor,
                action="data.scrub_scaffold",
                target=f"data ({scope_note})",
                detail=(
                    f"quarantined {quarantined} segment(s) -> needs_review + scaffold "
                    f"flag; deleted {deleted} scaffold TM entr"
                    f"{'y' if deleted == 1 else 'ies'} "
                    f"(segment_tm={len(seg_tm_ids)}, full_text={len(ft_ids)})"
                ),
            )

    return {
        "matched": matched,
        "segments": seg_hits,
        "segment_tm": seg_tm_hits,
        "full_text": ft_hits,
        "applied": bool(apply),
        "quarantined": quarantined,
        "deleted": deleted,
        "would_quarantine": len(seg_to_change),
        "would_delete": len(seg_tm_hits) + len(ft_hits),
    }


# --- distinct language pairs (for the filter dropdowns) -----------------------
def distinct_pairs(session: Session, visible_pids=None) -> list[dict]:
    """The distinct (source, target) locale pairs present in the visible TM — used
    for the "By language pair" summary rollup (one row per real pair). Cheap
    DISTINCT/GROUP BY.

    NOT the source of the source/target filter DROPDOWNS: a locale that appears in
    N pairs shows up in N rows here, so feeding this straight into a single-locale
    <select> repeats that locale N times (the /tm dropdown-dup bug). The per-axis
    filter dropdowns build from distinct_source_locales / distinct_target_locales
    below, which each collapse to one option per locale."""
    scope = _scope_clause(visible_pids)
    stmt = select(SegmentTM.source_locale, SegmentTM.target_locale).distinct()
    if scope is not None:
        stmt = stmt.where(scope)
    stmt = stmt.order_by(SegmentTM.source_locale, SegmentTM.target_locale)
    return [{"source_locale": r[0], "target_locale": r[1]} for r in session.exec(stmt).all()]


def distinct_source_locales(session: Session, visible_pids=None) -> list[str]:
    """The distinct SOURCE locales in the visible TM — one option per locale, sorted.

    The source-locale filter dropdown builds from THIS (a de-duplicated single-column
    DISTINCT), never from distinct_pairs: a source locale paired with several targets
    must appear ONCE in the filter, not once per target (the dropdown-dup bug)."""
    scope = _scope_clause(visible_pids)
    stmt = select(SegmentTM.source_locale).distinct()
    if scope is not None:
        stmt = stmt.where(scope)
    stmt = stmt.order_by(SegmentTM.source_locale)
    return [r for r in session.exec(stmt).all() if r]


def distinct_target_locales(session: Session, visible_pids=None) -> list[str]:
    """The distinct TARGET locales in the visible TM — one option per locale, sorted.
    The target-locale filter dropdown's de-duplicated source (see
    distinct_source_locales for the rationale)."""
    scope = _scope_clause(visible_pids)
    stmt = select(SegmentTM.target_locale).distinct()
    if scope is not None:
        stmt = stmt.where(scope)
    stmt = stmt.order_by(SegmentTM.target_locale)
    return [r for r in session.exec(stmt).all() if r]
