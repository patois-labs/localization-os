"""Content types (SPEC-content-types, phase 1) — the ONE service the /content-types
web surface, the `content-type` CLI subgroup, and the /api content-type endpoints all
call.

A ContentType is a small, first-class, org/customer-scoped taxonomy object (e.g.
"Marketing", "Product UI", "Legal"). A project binds to exactly one at creation
(Project.content_type_id); that assignment drives the domain scoping of the project's
language assets (glossaries in phase 2, TM in phase 3 — NOT built here).

Ownership / scoping mirrors Glossary/PromptTemplate: ``org_id`` stamped at create and
filtered in list/visible routes; ``customer_id`` for R2-3 confinement (NULL = a GLOBAL
type offered to every customer). No route-local business logic (repo law) — every
web/CLI/api surface delegates here, and every scope decision goes through this module
(never a hand-rolled filter).

Two distinct scope helpers, do not confuse them:

  * ``visible()`` — the MANAGEMENT read-authz seam (mirrors glossary_module.visible):
    may THIS caller (org + optional customer binding) see/edit THIS content type?
    Used by the CRUD routes so a foreign org's/customer's type 404s.
  * ``options_for_project()`` / ``attachable()`` — the SELECTION seam: which content
    types may a project of a given customer be assigned? = GLOBAL (customer NULL) OR
    that customer's own, within the org. A customer-bound caller (whose project is
    force-stamped to their own customer) therefore can only ever attach a global or
    own-customer type — the R2-3 confinement, for free.

DELETE FAILS CLOSED: a type is refused (with a live reference count) while any Project
or Customer.default references it. Phases 2-3 extend ``reference_count`` to also count
Glossary / TM / Connection references — the count is the single extension point.

Documented in: docs/architecture/content-and-projects.md
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    Connection,
    ContentType,
    ContextProfile,
    Customer,
    FullTextTM,
    Glossary,
    Project,
    SegmentEmbedding,
    SegmentTM,
)
from app.services import ids


def _utcnow() -> datetime:
    return datetime.utcnow()


class ContentTypeError(ValueError):
    """A content-type operation was invalid (blank name, duplicate name in the same
    scope, unknown id, or a delete refused while still referenced). Surfaced by the
    web route / CLI / API as a clean error, never a 500."""


# --- CRUD --------------------------------------------------------------------
def _coerce_customer_id(customer_id):
    """A submitted customer_id -> an int, or None (blank/None/non-digit/malformed =
    GLOBAL). Routes through the app-wide id law so a Unicode digit / >2**63 value is
    None (the existing GLOBAL path for a non-digit), never a 500 at int()/the bind."""
    return ids.coerce_id(customer_id)


def _duplicate(session: Session, name: str, org_id, customer_id, exclude_id=None) -> bool:
    """True when a content type with this (org_id, customer_id, name) already exists.

    NULL-aware (SQL treats NULLs as DISTINCT, so a plain UNIQUE constraint alone does
    NOT catch two GLOBAL types sharing a name — this application check does), and
    CASE-FOLDED so "Marketing", "marketing " and "MARKETING" all collide in one scope.

    THE FOLD MUST USE THE SAME ENGINE AS THE DB INDEX: both sides go through the DB's
    ``lower()`` (``func.lower`` on the column AND on the bound literal), NOT Python's
    ``str.lower()``. SQLite's ``lower()`` folds ASCII only; Python's folds full Unicode.
    Mixing them made this pre-check DISAGREE with the ``lower(name)`` in
    ``ux_contenttype_scope_name`` for any non-ASCII uppercase letter (e.g. "CAFÉ",
    Cyrillic/Greek/Turkish names) — a real duplicate slipped the friendly check and hit
    the index as an uncaught IntegrityError. Folding both sides with ``func.lower`` keeps
    the pre-check byte-for-byte consistent with the index (create()/rename() also catch
    IntegrityError as a belt-and-suspenders backstop for any residual mismatch/race)."""
    stmt = select(ContentType).where(
        func.lower(ContentType.name) == func.lower((name or "").strip())
    )
    stmt = (
        stmt.where(ContentType.org_id.is_(None))
        if org_id is None
        else stmt.where(ContentType.org_id == org_id)
    )
    stmt = (
        stmt.where(ContentType.customer_id.is_(None))
        if customer_id is None
        else stmt.where(ContentType.customer_id == customer_id)
    )
    for row in session.exec(stmt).all():
        if exclude_id is not None and row.id == exclude_id:
            continue
        return True
    return False


def create(
    session: Session,
    name: str,
    description: str = "",
    org_id=None,
    customer_id=None,
    created_by: str = "",
) -> ContentType:
    """Create a content type. Raises on a blank name or a duplicate in the same
    (org, customer) scope."""
    name = ids.clean_name(name, field="content type name")
    if not name:
        raise ContentTypeError("content type name is required.")
    cust_id = _coerce_customer_id(customer_id)
    if _duplicate(session, name, org_id, cust_id):
        scope = ("customer %d" % cust_id) if cust_id is not None else "global"  # noqa: UP031
        raise ContentTypeError(f"a content type named {name!r} already exists ({scope}).")
    ct = ContentType(
        name=name,
        description=(description or "").strip(),
        org_id=org_id,
        customer_id=cust_id,
        created_by=created_by or "",
    )
    session.add(ct)
    # Belt-and-suspenders: even after the pre-check, the functional unique index is the
    # authority — a concurrent create (TOCTOU) or any residual fold mismatch surfaces
    # here as an IntegrityError, which we convert to the friendly ContentTypeError so a
    # route NEVER 500s (it only catches ContentTypeError). Rollback first so the session
    # is reusable by the caller's error path.
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        scope = ("customer %d" % cust_id) if cust_id is not None else "global"  # noqa: UP031
        raise ContentTypeError(f"a content type named {name!r} already exists ({scope}).") from e
    session.refresh(ct)
    return ct


def rename(session: Session, ct_id: int, name: str) -> ContentType:
    """Rename a content type. Raises on a blank name or a same-scope duplicate."""
    ct = _require(session, ct_id)
    name = (name or "").strip()
    if not name:
        raise ContentTypeError("content type name is required.")
    if _duplicate(session, name, ct.org_id, ct.customer_id, exclude_id=ct.id):
        raise ContentTypeError(f"a content type named {name!r} already exists in this scope.")
    ct.name = name
    session.add(ct)
    # Same belt-and-suspenders backstop as create(): the functional unique index catches
    # any pre-check/index fold mismatch or concurrent rename, surfaced as a friendly
    # ContentTypeError (never a 500).
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        raise ContentTypeError(
            f"a content type named {name!r} already exists in this scope."
        ) from e
    session.refresh(ct)
    return ct


def set_description(session: Session, ct_id: int, description: str) -> ContentType:
    """Set a content type's description (metadata; never affects scoping)."""
    ct = _require(session, ct_id)
    ct.description = (description or "").strip()
    session.add(ct)
    session.commit()
    session.refresh(ct)
    return ct


def get(session: Session, ct_id: int) -> ContentType | None:
    return session.get(ContentType, ct_id)


def _require(session: Session, ct_id: int) -> ContentType:
    ct = session.get(ContentType, ct_id)
    if ct is None:
        raise ContentTypeError(f"content type {ct_id} not found.")
    return ct


# --- delete fails closed -----------------------------------------------------
def reference_count(session: Session, ct_id: int, org_id=None, customer_id=None) -> dict:
    """How many rows reference this content type, by kind + total.

    PHASE 1 counted Project.content_type_id + Customer.default_content_type_id.
    PHASE 2 adds Glossary.content_type_id (a content type in use by a glossary cannot
    be deleted). PHASE 3 adds TM rows (SegmentTM / FullTextTM / SegmentEmbedding) AND
    Connection.content_type_id — so once ANY translation memory has been WRITTEN under
    a content type, that type is effectively undeletable (deleting it would orphan the
    provenance of those rows). That is CORRECT and deliberate: the fail-closed message
    breaks the count out by category so the operator sees exactly what still holds it.
    This is the single extension point, so delete-fails-closed tightens automatically.
    Never mutates.

    org_id / customer_id (defense-in-depth, Finding 4): OPTIONAL assert-filters. delete()
    calls this UNSCOPED (both None) so fail-closed counts EVERY reference anywhere — the
    safest posture. But a future write path that resolved a content type inside a known
    org/customer scope can pass them to confine the count to that scope, so a mis-scoped
    aggregate can never under-count. None = no filter = today's behaviour.

    TM rows carry NO org_id/customer_id of their own (scoped via project_id -> Project);
    when an org/customer filter is given the TM counts join through Project so the scope
    holds. A NULL-project TM row (org-less global) is only counted when UNSCOPED, which
    matches the tm_module visibility rule."""
    pstmt = select(Project.id).where(Project.content_type_id == ct_id)
    cstmt = select(Customer.id).where(Customer.default_content_type_id == ct_id)
    gstmt = select(Glossary.id).where(Glossary.content_type_id == ct_id)
    connstmt = select(Connection.id).where(Connection.content_type_id == ct_id)
    cpstmt = select(ContextProfile.id).where(ContextProfile.content_type_id == ct_id)
    # TM stores counted below: SegmentTM and FullTextTM row-level rows, plus the
    # denormalized SegmentEmbedding. (Named without a trailing "(" so the
    # refusal-choke-point row-construction tripwire doesn't read this prose as a ctor.)
    segstmt = select(SegmentTM.id).where(SegmentTM.content_type_id == ct_id)
    ftstmt = select(FullTextTM.id).where(FullTextTM.content_type_id == ct_id)
    embstmt = select(SegmentEmbedding.id).where(SegmentEmbedding.content_type_id == ct_id)
    if org_id is not None:
        pstmt = pstmt.where(Project.org_id == org_id)
        cstmt = cstmt.where(Customer.org_id == org_id)
        gstmt = gstmt.where(Glossary.org_id == org_id)
        connstmt = connstmt.where(Connection.org_id == org_id)
        cpstmt = cpstmt.where(ContextProfile.org_id == org_id)
        # TM rows have no org_id column — confine through the owning Project.
        _org_pids = select(Project.id).where(Project.org_id == org_id)
        segstmt = segstmt.where(SegmentTM.project_id.in_(_org_pids))
        ftstmt = ftstmt.where(FullTextTM.project_id.in_(_org_pids))
        embstmt = embstmt.where(SegmentEmbedding.project_id.in_(_org_pids))
    if customer_id is not None:
        pstmt = pstmt.where(Project.customer_id == customer_id)
        cstmt = cstmt.where(Customer.id == customer_id)
        gstmt = gstmt.where(Glossary.customer_id == customer_id)
        connstmt = connstmt.where(Connection.customer_id == customer_id)
        cpstmt = cpstmt.where(ContextProfile.customer_id == customer_id)
        _cust_pids = select(Project.id).where(Project.customer_id == customer_id)
        segstmt = segstmt.where(SegmentTM.project_id.in_(_cust_pids))
        ftstmt = ftstmt.where(FullTextTM.project_id.in_(_cust_pids))
        embstmt = embstmt.where(SegmentEmbedding.project_id.in_(_cust_pids))
    projects = len(session.exec(pstmt).all())
    customer_defaults = len(session.exec(cstmt).all())
    glossaries = len(session.exec(gstmt).all())
    connections = len(session.exec(connstmt).all())
    context_profiles = len(session.exec(cpstmt).all())
    tm_rows = (
        len(session.exec(segstmt).all())
        + len(session.exec(ftstmt).all())
        + len(session.exec(embstmt).all())
    )
    return {
        "projects": projects,
        "customer_defaults": customer_defaults,
        "glossaries": glossaries,
        "connections": connections,
        "context_profiles": context_profiles,
        "tm_rows": tm_rows,
        "total": (
            projects + customer_defaults + glossaries + connections + context_profiles + tm_rows
        ),
    }


def delete(session: Session, ct_id: int) -> None:
    """Delete a content type. FAILS CLOSED: refused (ContentTypeError with the live
    reference count) while any Project, Customer.default or Glossary still references
    it — never a cascade, never orphaning a dangling FK."""
    ct = _require(session, ct_id)
    refs = reference_count(session, ct_id)
    if refs["total"] > 0:
        raise ContentTypeError(
            "cannot delete content type %d (%r): still referenced by %d project(s), "  # noqa: UP031
            "%d customer default(s), %d glossary(ies), %d connection(s), %d context "
            "card(s) and %d translation-memory row(s). Reassign the projects/"
            "glossaries/connections and remove the context cards first; note that TM "
            "rows written under this type keep it (their provenance), so a type that "
            "has stored TM cannot be deleted."
            % (
                ct_id,
                ct.name,
                refs["projects"],
                refs["customer_defaults"],
                refs["glossaries"],
                refs["connections"],
                refs["context_profiles"],
                refs["tm_rows"],
            )
        )
    session.delete(ct)
    session.commit()


# --- management read-authz (org + R2-3 customer confinement) -----------------
def list_types(session: Session, org_id=None, customer_id=None) -> list[ContentType]:
    """The management list, org-scoped (+ R2-3 customer-confined for a bound caller).

    org_id None = single-tenant / gate off (no org filter). customer_id non-None = a
    customer-bound caller sees ONLY that customer's types; a staff caller (customer_id
    None) sees the org's types (all customers + global). Mirrors
    glossary_module.list_glossaries EXACTLY.

    #73 (fail-closed BY CONSTRUCTION): a bare None org_id under an ACTIVE multi-org
    gate lists NOTHING, not every org's types; access.UNSCOPED (the CLI --operator
    door) still sweeps; gate off is byte-identical (None = unrestricted)."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(ContentType)
    if org_id is not None:
        stmt = stmt.where(ContentType.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where(ContentType.customer_id == customer_id)
    return list(session.exec(stmt.order_by(ContentType.name)).all())


def visible(session: Session, ct_id: int, org_id=None, customer_id=None) -> ContentType | None:
    """Fetch a content type only if visible to the caller's org + customer scope
    (else None -> the route 404s). The single management read-authz seam. Mirrors
    glossary_module.visible.

    #73: a bare None org_id under an ACTIVE gate resolves NOTHING (returns None);
    access.UNSCOPED keeps the cross-org read; gate off byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    ct = session.get(ContentType, ct_id)
    if ct is None:
        return None
    if org_id is not None and getattr(ct, "org_id", None) != org_id:
        return None
    if customer_id is not None and getattr(ct, "customer_id", None) != customer_id:
        return None
    return ct


# --- selection seam (what a PROJECT of a given customer may be assigned) ------
def options_for_project(session: Session, org_id=None, customer_id=None) -> list[ContentType]:
    """The content types a project of `customer_id` may be assigned: GLOBAL (customer
    NULL) + that customer's own, within the org. When `customer_id` is None (an
    un-customer project) only GLOBAL types are offered. This is the dropdown source
    for the new-project dialog + project settings — filtered to exactly the set the
    project could legally attach, so the UI can't offer a foreign type.

    #73: a bare None org_id under an ACTIVE gate offers NOTHING (fail closed), not
    every org's global types; access.UNSCOPED still sweeps; gate off byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(ContentType)
    if org_id is not None:
        stmt = stmt.where(ContentType.org_id == org_id)
    if customer_id is None:
        stmt = stmt.where(ContentType.customer_id.is_(None))
    else:
        stmt = stmt.where(
            (ContentType.customer_id.is_(None)) | (ContentType.customer_id == customer_id)
        )
    return list(session.exec(stmt.order_by(ContentType.name)).all())


def attachable(session: Session, ct_id, org_id=None, customer_id=None) -> ContentType | None:
    """Return the ContentType iff a project of `customer_id` (in `org_id`) may attach
    it — i.e. it exists, is in the org, and is GLOBAL or scoped to THAT customer. Else
    None. This is the validation seam content.project_create / the settings route use:
    a customer-bound caller (whose project is force-stamped to their own customer) can
    therefore never attach another customer's type (the R2-3 confinement, mirroring
    ForeignCustomerError). `ct_id` blank/None returns None (caller treats as "unset").

    #73 (fail-closed BY CONSTRUCTION, mirrors list_types/visible/options_for_project):
    a bare None org_id under an ACTIVE multi-org gate attaches NOTHING (returns None),
    not any org's type; access.UNSCOPED is the DELIBERATE cross-org scope the CLI
    --operator create/validation door passes (never a bare None), so it keeps the sweep;
    gate off is byte-identical. Every current caller feeds a poison-protected principal
    org or a concrete row-org, so this is construction-level closure, not a live leak."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    # coerce_id absorbs both an int and a digit-string, and never raises on a malformed
    # request-supplied ct_id (Unicode digit / >2**63) -- a bad id reads as "unset" (None),
    # never a 500 at int()/session.get.
    cid = ids.coerce_id(ct_id)
    if cid is None:
        return None
    ct = session.get(ContentType, cid)
    if ct is None:
        return None
    if org_id is not None and getattr(ct, "org_id", None) != org_id:
        return None
    ct_cust = getattr(ct, "customer_id", None)
    if ct_cust is not None and ct_cust != customer_id:
        return None
    return ct


def to_dict(session: Session, ct: ContentType) -> dict:
    """A JSON-serializable summary (CLI --json, /api) of a content type + its live
    reference count."""
    return {
        "id": ct.id,
        "name": ct.name,
        "description": ct.description,
        "org_id": ct.org_id,
        "customer_id": ct.customer_id,
        "created_by": ct.created_by,
        "created_at": ct.created_at.isoformat() if ct.created_at else None,
        "references": reference_count(session, ct.id),
    }
