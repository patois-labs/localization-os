"""glossary_module: locale canonicalization (mirrors pair_providers._canon/_norm)

Documented in: docs/architecture/tm-and-glossary.md
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple

from sqlmodel import Session, select

from app.models import (
    Customer,
    Glossary,
    GlossaryConcept,
    GlossaryEntry,
    GlossaryPair,
    GlossarySense,
    GlossaryStatus,
    GlossaryTerm,
)
from app.services import glossary as gloss_engine
from app.services import ids, languages_catalog

log = logging.getLogger("loc_tms.glossary")


_NO_CT_FILTER = object()


# Sentinel for options_for_step's content_type_id: DISTINCT from None. None is a real
# input (a project-scoped dropdown for an UNTYPED project -> offer only General/NULL
# glossaries); the sentinel means "no content-type filtering at all" (a reusable
# global/customer-scoped workflow whose dropdown shows every approved glossary, the
# current behaviour of every caller — enforcement then happens at RUN time).
def _utcnow() -> datetime:
    return datetime.utcnow()


class GlossaryModuleError(ValueError):
    """A module operation was invalid (blank name, unknown glossary, a pair with an
    unknown/blank locale). Surfaced by the web route / CLI / API as a clean error."""


def _reject_archived_customer(session: Session, customer_id) -> None:
    """SPEC-customer-archive: a glossary can never be created for / assigned to an
    ARCHIVED customer (archive retires a customer from every "choose a customer"
    surface). Checked at the SERVICE seam — mirroring content.project_create's
    ArchivedCustomerError guard — so a forged /api POST or a stray CLI id is refused,
    not just omitted from the web picker. The web _valid_customer_id refuses it early
    too, but the CLI (_get_customer) and /api (_customer_in_org) helpers do NOT check
    archived_at, so without this the two non-web surfaces silently stamped an archived
    customer onto a brand-new glossary (a web-vs-CLI/API parity gap). No-op when
    customer_id is blank/None; a nonexistent id is left to the caller's own scope
    check / the FK path (unchanged) — only an EXISTING-but-archived row is rejected."""
    cid = ids.coerce_id(customer_id)
    if cid is None:
        return
    cust = session.get(Customer, cid)
    if cust is not None and getattr(cust, "archived_at", None) is not None:
        raise GlossaryModuleError(
            f"customer {cid} is archived and cannot own a glossary "
            "(unarchive it first, or pick an active customer)."
        )


# --- locale canonicalization (mirrors pair_providers._canon/_norm) -----------
def _norm(loc: str | None) -> str:
    return languages_catalog.normalize(loc) or ""


def _canon(loc: str | None) -> str:
    """Catalog-canonical display form (es_ES -> es-ES) for STORING a locale code.

    T2-03: a blank code is a legitimate "any/unset" value and returns "" unchanged
    (callers decide whether blank is acceptable). A NON-blank code must be in the
    language catalog (``languages_catalog.validate_codes``, mirroring
    ``customers.add_pair``'s gate) — an unknown/garbage code (e.g. 'not-A-LOCALE')
    RAISES GlossaryModuleError rather than being coerced into a plausible-looking
    fake canonical form (the pre-fix fallback that let garbage through)."""
    s = (loc or "").strip()
    if not s:
        return ""
    valid, _ = languages_catalog.validate_codes([s])
    if valid:
        return valid[0]
    raise GlossaryModuleError(f"{s!r} is not a known locale code (see the language catalog).")


# --- CRUD --------------------------------------------------------------------
def create(
    session: Session,
    name: str,
    org_id=None,
    customer_id=None,
    created_by: str = "",
    content_type_id=None,
    require_customer: bool = False,
) -> Glossary:
    """Create a DRAFT glossary. Raises on a blank name.

    `require_customer` (2026-07-14, customer-first glossaries): when True (every
    web/CLI/API create seam passes it), a glossary MUST belong to a customer — a
    blank/None customer_id raises GlossaryModuleError. The default stays False so the
    hermetic suites and the legacy-assignment path can still mint an unassigned row.

    `content_type_id` (SPEC-content-types phase 2): an optional ContentType to scope
    the glossary to. Validated via content_types.attachable against the glossary's OWN
    org + customer (a customer-bound glossary can only ever attach a GLOBAL or its own
    customer's type — the R2-3 confinement, mirroring project_create); a bad/foreign id
    raises GlossaryModuleError. Blank/None = the shared "General" bucket (byte-identical
    to before this parameter existed)."""
    name = (name or "").strip()
    if not name:
        raise GlossaryModuleError("glossary name is required.")
    cust_id = ids.coerce_id(customer_id)
    if require_customer and cust_id is None:
        raise GlossaryModuleError("a glossary must belong to a customer.")
    _reject_archived_customer(session, cust_id)
    # #73: attachable now fails closed on a bare None org_id under an active gate, so the
    # CLI --operator create door threads access.UNSCOPED (op_org_scope) here -- the
    # resolution below keeps the raw scope (UNSCOPED sweeps every org), while the row is
    # STAMPED with org_scope_value(org_id), which collapses the UNSCOPED sentinel back to
    # None (identity on a real id / -1 / None) so the persisted org is byte-identical.
    from app.services import access

    ct_id = _resolve_content_type(session, content_type_id, org_id, cust_id)
    g = Glossary(
        name=name,
        status=GlossaryStatus.DRAFT,
        org_id=access.org_scope_value(org_id),
        customer_id=cust_id,
        created_by=created_by or "",
        content_type_id=ct_id,
    )
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def _resolve_content_type(session, content_type_id, org_id, customer_id):
    """Validate a submitted content_type_id against an (org, customer) scope, or None.

    Blank/None -> None (the General bucket). A non-blank id must be ATTACHABLE to a
    glossary of this org+customer (GLOBAL or the glossary's own customer) — else a
    GlossaryModuleError (never silently dropped, never a foreign type stamped). The
    single validation seam shared by create() + set_content_type()."""
    if content_type_id is None or str(content_type_id).strip() == "":
        return None
    from app.services import content_types as _ct

    ct = _ct.attachable(session, content_type_id, org_id=org_id, customer_id=customer_id)
    if ct is None:
        raise GlossaryModuleError(
            f"content type {content_type_id} is not available for this glossary "
            "(unknown, or scoped to a different customer/org)."
        )
    return ct.id


def set_content_type(session: Session, glossary_id: int, content_type_id) -> Glossary:
    """Set or clear a glossary's content type (SPEC-content-types phase 2 — editable).

    Validates the new id via content_types.attachable against the GLOSSARY's own org +
    customer BEFORE writing (raises GlossaryModuleError on a bad/foreign one), so a
    forged request can never plant another customer's type on the glossary. Blank/None
    CLEARS it (back to the shared "General" bucket).

    Does NOT revert APPROVED->DRAFT: content type is SCOPING METADATA, not glossary
    content (same rationale as rename() above — a changed scope does not invalidate the
    linguistic sign-off). This is the deliberate, documented difference from the term/
    pair edits, which DO touch_draft()."""
    g = _require(session, glossary_id)
    g.content_type_id = _resolve_content_type(session, content_type_id, g.org_id, g.customer_id)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def assign_customer(session: Session, glossary_id: int, customer_id: int) -> Glossary:
    """One-time assignment of a LEGACY unassigned glossary to a customer (the maintainer
    2026-07-14). ONLY valid FROM NULL — never a reassignment between customers, because
    a glossary's terms/TM provenance are bound to whoever owned it. Raises
    GlossaryModuleError if the glossary already has a customer (the from-NULL-only
    invariant) or if customer_id is blank/invalid. The caller (web/CLI/API) is
    responsible for validating the customer belongs to the caller's org/scope and for
    the can_manage gate + audit; this seam enforces the from-NULL invariant so no
    surface can bypass it.

    Does NOT revert approval (assigning an owner is scoping metadata, not a content
    edit — same rationale as set_content_type/rename)."""
    g = _require(session, glossary_id)
    if g.customer_id is not None:
        raise GlossaryModuleError(
            f"glossary {glossary_id} already belongs to a customer; "
            "reassignment between customers is not allowed (term/TM provenance)."
        )
    cid = ids.coerce_id(customer_id)
    if cid is None:
        raise GlossaryModuleError("a customer id is required to assign this glossary.")
    _reject_archived_customer(session, cid)
    g.customer_id = cid
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def rename(session: Session, glossary_id: int, name: str) -> Glossary:
    """Rename a glossary (metadata; does NOT revert approval)."""
    g = _require(session, glossary_id)
    name = (name or "").strip()
    if not name:
        raise GlossaryModuleError("glossary name is required.")
    g.name = name
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def delete(session: Session, glossary_id: int) -> None:
    """Delete a glossary and everything it owns (entries + their senses + pairs).

    Any WORKFLOW STEP pinned to it is unpinned (glossary_id -> NULL) so a run falls
    back to project-scoped matching rather than a dangling FK -- and, since feedback
    item 5, so is any PROJECT pin (ProjectGlossary). That second sweep is not
    cosmetic: ProjectGlossary.glossary_id is a real FK, so without it a pinned
    glossary became PERMANENTLY UNDELETABLE (every delete attempt raised
    IntegrityError at the final DELETE), with nothing on the glossary page naming the
    project responsible. Both unpins happen in THIS transaction, so a delete either
    fully happens or fully does not."""
    from app.services import glossary_concepts

    g = _require(session, glossary_id)
    entries = session.exec(
        select(GlossaryEntry).where(GlossaryEntry.glossary_id == glossary_id)
    ).all()
    # Delete ALL senses first and FLUSH before touching their parent entries:
    # GlossarySense.glossary_entry_id -> glossaryentry.id, but no SQLModel
    # Relationship declares that edge, so the unit-of-work can't order a single
    # flush's DELETEs by it. Interleaving delete(sense)/delete(entry) let a
    # later select's autoflush emit DELETE glossaryentry before its senses and
    # trip the FK (which Postgres, always FK-enforcing, would have rejected too).
    for e in entries:
        for sense in session.exec(
            select(GlossarySense).where(GlossarySense.glossary_entry_id == e.id)
        ).all():
            session.delete(sense)
    session.flush()
    for e in entries:
        session.delete(e)
    session.flush()
    # Whole-GLOSSARY concept delete UNIT (LAW-TERM-2b): drop this glossary's concepts/
    # terms in THIS transaction — AFTER the entries/senses go, so glossaryentry.term_id /
    # glossarysense.term_id no longer reference a term being deleted (else the FK Postgres
    # and SQLite-under-PRAGMA both enforce raises). Clears inbound deprecated_replacement
    # refs from ANY glossary first (M11); child-before-parent, identical on both engines.
    glossary_concepts.delete_glossary_concepts(session, glossary_id)
    for pair in session.exec(
        select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
    ).all():
        session.delete(pair)
    from app.models import ProjectGlossary, TermCandidate, WorkflowStep

    for step in session.exec(
        select(WorkflowStep).where(WorkflowStep.glossary_id == glossary_id)
    ).all():
        step.glossary_id = None
        session.add(step)
    # PROJECT pins are DELETED rather than nulled: unlike a step, a pin row IS the
    # relationship, so there is nothing left to keep once the glossary is gone.
    for pin in session.exec(
        select(ProjectGlossary).where(ProjectGlossary.glossary_id == glossary_id)
    ).all():
        session.delete(pin)
    # EXTRACTED TERM CANDIDATES (re-review NEW-5) -- pre-dating the attach feature.
    # TermCandidate.glossary_id is a NOT NULL FK, so a glossary that had ever been
    # through term extraction was PERMANENTLY UNDELETABLE: the final DELETE raised
    # IntegrityError and no surface named the candidates as the reason. They are owned
    # by the glossary outright (a candidate is a proposal INTO it, and the accepted
    # ones already exist as real GlossaryEntry rows), so they cascade with it -- the
    # dropped-candidate noise-control history is per-glossary and dies with it too.
    for cand in session.exec(
        select(TermCandidate).where(TermCandidate.glossary_id == glossary_id)
    ).all():
        session.delete(cand)
    session.flush()
    session.delete(g)
    session.commit()


def get(session: Session, glossary_id: int) -> Glossary | None:
    return session.get(Glossary, glossary_id)


def _require(session: Session, glossary_id: int) -> Glossary:
    g = session.get(Glossary, glossary_id)
    if g is None:
        raise GlossaryModuleError(f"glossary {glossary_id} not found.")
    return g


def list_glossaries(session: Session, org_id=None, customer_id=None) -> list[Glossary]:
    """The module list, org-scoped (+ R2-3 customer-confined for a bound caller).

    org_id None = single-tenant / gate off (no org filter). customer_id non-None =
    a customer-bound caller sees ONLY that customer's glossaries; a staff caller
    (customer_id None) sees the org's glossaries (all customers + unassigned).

    #73 (fail-closed BY CONSTRUCTION): under an ACTIVE multi-org gate a bare None
    org_id lists NOTHING, not every org's glossaries -- access.org_scope_blocked is
    the single authority for that reading and it matches same_org()/org_filter().
    access.UNSCOPED (the key-verified CLI --operator door) still sweeps every org.
    Gate off: unchanged, byte-identical (None means unrestricted)."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(Glossary)
    if org_id is not None:
        stmt = stmt.where(Glossary.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where(Glossary.customer_id == customer_id)
    return list(session.exec(stmt.order_by(Glossary.created_at.desc())).all())


# --- customer-first list scoping (2026-07-14: glossaries belong to customers) --
def list_for_customer(session: Session, org_id, customer_id) -> list[Glossary]:
    """Glossaries owned by ONE customer (the customer-first list page's main section).
    customer_id is required (a real customer id) — this never returns unassigned rows.

    #73: same fail-closed reading as list_glossaries -- a bare None org_id under an
    ACTIVE gate returns [] (access.UNSCOPED still sweeps); gate off byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(Glossary).where(Glossary.customer_id == customer_id)
    if org_id is not None:
        stmt = stmt.where(Glossary.org_id == org_id)
    return list(session.exec(stmt.order_by(Glossary.created_at.desc())).all())


def list_unassigned(session: Session, org_id=None) -> list[Glossary]:
    """LEGACY glossaries with NO customer (customer_id IS NULL) — the "Unassigned
    (needs a customer)" section, shown to can_manage only. Every new glossary now
    requires a customer; these are the pre-policy rows awaiting one-time assignment.

    #73: a bare None org_id under an ACTIVE gate returns [] (fail closed), not every
    org's unassigned rows; access.UNSCOPED still sweeps; gate off byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(Glossary).where(Glossary.customer_id.is_(None))
    if org_id is not None:
        stmt = stmt.where(Glossary.org_id == org_id)
    return list(session.exec(stmt.order_by(Glossary.created_at.desc())).all())


def visible(session: Session, glossary_id: int, org_id=None, customer_id=None) -> Glossary | None:
    """Fetch a glossary only if it is visible to the caller's org + customer scope
    (else None -> the route 404s). The single read-authz seam for the module.

    #73: a bare None org_id under an ACTIVE gate resolves NOTHING (returns None),
    not any org's glossary by id; access.UNSCOPED (the CLI --operator door) keeps the
    cross-org read; gate off is byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    g = session.get(Glossary, glossary_id)
    if g is None:
        return None
    if org_id is not None and getattr(g, "org_id", None) != org_id:
        return None
    if customer_id is not None and getattr(g, "customer_id", None) != customer_id:
        return None
    return g


# --- approval lifecycle ------------------------------------------------------
def approve(session: Session, glossary_id: int, approver_email: str = "") -> Glossary:
    """Mark a glossary APPROVED (records approver + timestamp). Idempotent."""
    g = _require(session, glossary_id)
    g.status = GlossaryStatus.APPROVED
    g.approved_by = approver_email or ""
    g.approved_at = _utcnow()
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def unapprove(session: Session, glossary_id: int) -> Glossary:
    """Revert a glossary to DRAFT (clears approver + timestamp)."""
    g = _require(session, glossary_id)
    g.status = GlossaryStatus.DRAFT
    g.approved_by = ""
    g.approved_at = None
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def touch_draft(session: Session, glossary_id: int) -> None:
    """Editing an approved glossary's CONTENT reverts it to draft (re-approval
    required before it re-appears in the workflow dropdown). No-op if already draft."""
    g = session.get(Glossary, glossary_id)
    if g is not None and g.status == GlossaryStatus.APPROVED:
        g.status = GlossaryStatus.DRAFT
        g.approved_by = ""
        g.approved_at = None
        session.add(g)
        session.commit()


def _parse_pairs(pairs) -> list[tuple[str, str]]:
    """Accepts a (source, target) tuple/list, a "SRC:TGT"/"SRC>TGT" string, OR a
    {"source_locale":..., "target_locale":...} dict -- the EXACT shape
    `pairs_for()`/`to_dict()` return (BUG-09 QA finding, 2026-07-29). Before this,
    a dict-shaped pair fell through to the string branch below, where
    `str(p or "")` on a dict produces its Python repr (e.g.
    "{'source_locale': 'en-US', 'target_locale': 'es-ES'}") -- which then
    happens to CONTAIN a colon, so it silently mis-split into garbage locale
    codes instead of raising the intended "expected 'SRC:TGT'" message, and the
    raw repr leaked into the resulting "not a known locale code" error shown to
    the caller. This matters because a client that reads a glossary (whose
    "pairs" are exactly this dict shape) and re-submits them unchanged -- an
    edit form resubmitting untouched rows, or a naive round-trip of a GET body
    into set_pairs -- would hit it on every request."""
    parsed: list[tuple[str, str]] = []
    seen = set()
    for p in pairs or []:
        if isinstance(p, dict):
            src, tgt = p.get("source_locale", ""), p.get("target_locale", "")
        elif isinstance(p, (tuple, list)):
            src, tgt = (p[0] if len(p) > 0 else ""), (p[1] if len(p) > 1 else "")
        else:
            s = str(p or "").strip().replace("->", ">")
            if ":" in s:
                src, tgt = s.split(":", 1)
            elif ">" in s:
                src, tgt = s.split(">", 1)
            else:
                raise GlossaryModuleError(f"bad pair {p!r} (expected 'SRC:TGT').")
        csrc, ctgt = _canon(src), _canon(tgt)
        if not csrc or not ctgt:
            raise GlossaryModuleError(
                f"a language pair needs both a source and a target (got {src!r}->{tgt!r})."
            )
        key = (_norm(csrc), _norm(ctgt))
        if key in seen:
            continue
        seen.add(key)
        parsed.append((csrc, ctgt))
    return parsed


def pairs_for(session: Session, glossary_id: int) -> list[dict]:
    """A glossary's pairs as {source_locale, target_locale} dicts (sorted)."""
    rows = session.exec(
        select(GlossaryPair)
        .where(GlossaryPair.glossary_id == glossary_id)
        .order_by(GlossaryPair.target_locale)
    ).all()
    return [{"source_locale": r.source_locale, "target_locale": r.target_locale} for r in rows]


def covers_pair(session: Session, glossary_id: int, source_locale, target_locale) -> bool:
    """True when a glossary has a pair matching (source, target) (normalized). A
    blank source on a stored pair is treated as a wildcard matching any source."""
    tgt, src = _norm(target_locale), _norm(source_locale)
    for r in session.exec(
        select(GlossaryPair).where(GlossaryPair.glossary_id == glossary_id)
    ).all():
        if _norm(r.target_locale) != tgt:
            continue
        rsrc = _norm(r.source_locale)
        if not rsrc or not src or rsrc == src:
            return True
    return False


def option_label(session: Session, g) -> str:
    """A workflow-step glossary dropdown label: the glossary's name followed by the
    language pairs it covers, e.g. "Acme Marketing (en-US > de-DE, en-US > fr-FR)". A
    glossary with no pairs set shows "(no languages set)". Uses the same pair rows the
    glossary module page lists (``pairs_for``). Plain ASCII ">" separators (no arrow
    character, no em dash) so the label reads in a terminal and a <select> alike."""
    pairs = pairs_for(session, g.id)
    if pairs:
        body = ", ".join(f"{p['source_locale']} > {p['target_locale']}" for p in pairs)
    else:
        body = "no languages set"
    return f"{g.name} ({body})"


# --- the workflow-step dropdown filter (the payoff) -------------------------
def content_type_allows(glossary_content_type_id, project_content_type_id) -> bool:
    """True when a glossary of content-type `glossary_content_type_id` applies to a
    project (or dropdown) of type `project_content_type_id` (SPEC-content-types §3).

    A GENERAL (NULL-type) glossary applies to EVERY project (the shared bucket — this
    is what makes the invariant hold: with all glossaries NULL-type, this is always
    True). A TYPED glossary applies only to a project of the SAME type. An untyped
    project (project_content_type_id None) therefore accepts ONLY General glossaries."""
    return glossary_content_type_id is None or glossary_content_type_id == project_content_type_id


def options_for_step(
    session: Session,
    source_locale,
    target_locale,
    org_id=None,
    customer_id=None,
    content_type_id=_NO_CT_FILTER,
) -> list[Glossary]:
    """The glossaries offered in a workflow step's dropdown for a given language pair.

    Filters on EXACTLY (the maintainer's ask):
      * status == APPROVED (draft glossaries never appear), AND
      * a GlossaryPair covering the step's (source, target) pair — normalized, a
        blank stored source is a wildcard, AND
      * customer confinement (customer_id, 2026-07-14 customer-first): when a
        customer is in scope, offer that customer's glossaries PLUS the LEGACY
        unassigned ones (customer_id NULL) — a workflow for Acme sees Acme's + the
        pre-policy shared glossaries, never another customer's. (This differs from the
        module LIST, which segregates unassigned into a can_manage-only section; a
        pinned unassigned glossary is a benign legacy fallback.) A staff/global scope
        (customer_id None) still sees every approved glossary; run-time enforcement is
        the load-bearing guard for a reusable workflow, AND
      * org visibility (org_id), AND
      * CONTENT-TYPE scope (SPEC-content-types §3.2): when `content_type_id` is passed
        (a PROJECT-scoped dropdown), offer only glossaries matching that content type
        OR General (NULL). When it is left as the _NO_CT_FILTER sentinel (a reusable
        GLOBAL/customer-scoped workflow — every current caller), no content-type filter
        is applied and all approved glossaries show; run-time enforcement
        (orchestrator._resolved_glossary_id) is the load-bearing guard there, since one
        workflow is shared across projects of different content types.

    When source_locale/target_locale are blank (a universal/ungrouped workflow with
    no fixed pair), every APPROVED visible glossary is offered (the author picks; the
    run-time pair-mismatch simply matches no terms, never an error).

    #73 (fail-closed BY CONSTRUCTION, mirrors list_glossaries/visible): a bare None
    org_id under an ACTIVE multi-org gate offers NOTHING (returns []), not every org's
    approved glossaries; access.UNSCOPED (the key-verified CLI --operator door) still
    sweeps every org; gate off is byte-identical (None = unrestricted). Every current
    caller feeds a poison-protected principal org (_my_org_id/_ident_org_id -> -1) or a
    concrete row-org, so this is construction-level closure, not a today-live leak."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    filter_ct = content_type_id is not _NO_CT_FILTER
    # Customer scope: confine to the customer's glossaries + LEGACY unassigned (NULL),
    # not an exact-match-only (which would hide the shared legacy glossaries a step may
    # still legitimately pin). Staff/global (customer_id None) -> no customer filter.
    stmt = select(Glossary)
    if org_id is not None:
        stmt = stmt.where(Glossary.org_id == org_id)
    if customer_id is not None:
        stmt = stmt.where((Glossary.customer_id == customer_id) | (Glossary.customer_id.is_(None)))
    result = []
    for g in session.exec(stmt.order_by(Glossary.created_at.desc())).all():
        if g.status != GlossaryStatus.APPROVED:
            continue
        if filter_ct and not content_type_allows(g.content_type_id, content_type_id):
            continue
        if _norm(source_locale) or _norm(target_locale):  # noqa: SIM102
            if not covers_pair(session, g.id, source_locale, target_locale):
                continue
        result.append(g)
    return result


# --- auto-attach + merge (the shared seam BOTH the orchestrator and the plain    -
# --- pretranslate path call — one helper so the two paths never drift) -----------
class GlossaryPlan(NamedTuple):
    """Which glossaries feed a run's per-segment matching, in precedence order.

    glossary_ids   ordered module-glossary ids, HIGHEST precedence first
                   (content-type-specific, then General/NULL, each id-ascending).
    include_legacy also match the project's LEGACY per-project entries (lowest
                   precedence) — always True except on an explicit step pin (which is
                   an exclusive override, byte-identical to the pre-phase-2 gid=X path).
    """

    glossary_ids: tuple
    include_legacy: bool


def _concept_shaped_import(data: bytes, filename: str | None, concepts: bool) -> bool:
    """Will this import take the concept-preserving arm? Only a ``.glossary.json`` import does:
    it returns written concept/term COUNTS with no entry-path ``changed`` counter, so
    :func:`import_bytes` measures its change on the compat store instead (M4). A CSV or ANY TBX
    import takes the entry path and carries its own ``changed`` (a TBX ``concepts=True`` import
    is RESERVED and refused before any write -- concept-preserving TBX import is deferred to the
    terminology authority train), so neither needs the snapshot. ``concepts`` is accepted for
    signature parity with :func:`import_bytes` and is no longer a routing input."""
    fmt = gloss_engine.detect_format(data, filename)
    return fmt == gloss_engine.JSON_FORMAT


def _compat_fingerprint(session: Session, glossary_id: int) -> tuple:
    """A content fingerprint of the glossary's ENFORCEMENT surface, with row ids and the
    created/updated timestamps EXCLUDED (an idempotent bulk re-import bumps updated_at, so it
    cannot be part of a no-change test). Used by :func:`import_bytes` to tell an idempotent
    concept re-import (fingerprint unchanged) from one that moved a stored value, the same
    distinction the entry path draws with its ``changed`` counter (M4).

    Two halves:
      * the COMPAT STORE (source/target terms, DNT, case-sensitivity, meta, each entry's
        senses) -- what a flat entry import touches; and
      * the CONCEPT/TERM STORE that reaches the T3 prompt block (round-2 M2). A
        concept-preserving re-import is append/update-only against the compat store
        (``project`` derives the compat rows from the concept store, LAW-TERM-18), so a
        change that only rewrites a concept's definition, or a term's status/audience/POS,
        is INVISIBLE to the compat half yet ``glossary_concepts.render_for_llm`` /
        ``_resolve_concept_meta`` render it live into the model prompt. Without this half a
        genuinely changed termbase kept an APPROVED glossary approved while sending the model
        different terminology (gate probe). The fields mirror the T3 "shown set":
        per concept definition/domain/subdomain/notes; per term
        locale/surface/status/term_type/part_of_speech/usage_note/audience/dnt plus the
        deprecated-replacement surface (drives the ``do not use ... use X`` line)."""
    entries = session.exec(
        select(
            GlossaryEntry.id,
            GlossaryEntry.source_term,
            GlossaryEntry.source_locale,
            GlossaryEntry.target_term,
            GlossaryEntry.target_locale,
            GlossaryEntry.dnt,
            GlossaryEntry.case_sensitive,
            GlossaryEntry.meta,
        ).where(GlossaryEntry.glossary_id == glossary_id)
    ).all()
    ent_by_id = {e[0]: e[1:] for e in entries}
    senses: dict[int, list] = {}
    if ent_by_id:
        for s in session.exec(
            select(
                GlossarySense.glossary_entry_id,
                GlossarySense.sense_label,
                GlossarySense.context_cue,
                GlossarySense.target_term,
            ).where(GlossarySense.glossary_entry_id.in_(list(ent_by_id)))
        ).all():
            senses.setdefault(s[0], []).append(tuple(str(x) for x in s[1:]))
    fp = sorted(
        (tuple(str(x) for x in ent_by_id[eid]), tuple(sorted(senses.get(eid, []))))
        for eid in ent_by_id
    )
    return (tuple(fp), _concept_fingerprint(session, glossary_id))


def _concept_fingerprint(session: Session, glossary_id: int) -> tuple:
    """The concept/term-store half of :func:`_compat_fingerprint` (round-2 M2): the T3
    shown-set fields (never a row id or timestamp) so an idempotent re-import fingerprints
    identically and a real concept-level change does not.

    Round-3 M1: the fingerprint is keyed on the rendered CONTENT of each concept, NOT on its
    ``external_id``. A concept-preserving re-import whose conceptEntry ids are REGENERATED or
    ABSENT mints all-new concept rows with fresh ids but IDENTICAL content; keying on
    ``external_id`` grew the fingerprint (new id rows) and reverted an APPROVED glossary even
    though the model sees the same terminology. Content-keying (a SET of content rows, so an
    added duplicate-content concept collapses onto the existing row) keeps such a re-import
    idempotent, while a real change to any rendered field still moves the fingerprint. The
    ``external_id`` is never rendered by T3, so dropping it from the key loses no signal."""
    concepts = session.exec(
        select(
            GlossaryConcept.id,
            GlossaryConcept.external_id,
            GlossaryConcept.definition,
            GlossaryConcept.domain,
            GlossaryConcept.subdomain,
            GlossaryConcept.notes,
        ).where(GlossaryConcept.glossary_id == glossary_id)
    ).all()
    if not concepts:
        return ()
    by_id = {c[0]: c for c in concepts}
    repl_by_id: dict[int, int | None] = {}
    terms_by_concept: dict[int, list] = {}
    for t in session.exec(
        select(
            GlossaryTerm.id,
            GlossaryTerm.concept_id,
            GlossaryTerm.locale,
            GlossaryTerm.term,
            GlossaryTerm.status,
            GlossaryTerm.term_type,
            GlossaryTerm.part_of_speech,
            GlossaryTerm.usage_note,
            GlossaryTerm.audience,
            GlossaryTerm.dnt,
            GlossaryTerm.deprecated_replacement_term_id,
        ).where(GlossaryTerm.concept_id.in_(list(by_id)))
    ).all():
        repl_by_id[t[0]] = t[10]
        terms_by_concept.setdefault(t[1], []).append(t)
    # resolve each deprecated-replacement id to the SURFACE it points at (what T3 renders),
    # so a re-import that repoints or renames the replacement is a change even though the id
    # differs run to run.
    surface_by_id: dict[int, str] = {}
    if repl_by_id:
        want = {v for v in repl_by_id.values() if v is not None}
        if want:
            for tid, surface in session.exec(
                select(GlossaryTerm.id, GlossaryTerm.term).where(GlossaryTerm.id.in_(list(want)))
            ).all():
                surface_by_id[tid] = surface or ""
    rows: set[tuple] = set()
    for cid, c in by_id.items():
        cterms = sorted(
            (
                t[2] or "",  # locale
                t[3] or "",  # term surface
                str(t[4]),  # status
                t[5] or "",  # term_type
                t[6] or "",  # part_of_speech
                t[7] or "",  # usage_note
                t[8] or "",  # audience
                bool(t[9]),  # dnt
                surface_by_id.get(t[10], "") if t[10] is not None else "",
            )
            for t in terms_by_concept.get(cid, [])
        )
        # round-3 M1: key on rendered CONTENT, not external_id -- a SET so a duplicate-content
        # concept (a re-import with regenerated/absent ids) collapses and does not move the
        # fingerprint, while any real change to a rendered field does.
        rows.add(
            (
                c[2] or "",  # definition
                c[3] or "",  # domain
                c[4] or "",  # subdomain
                c[5] or "",  # notes
                tuple(cterms),
            )
        )
    return tuple(sorted(rows))


def _import_changed_content(res: dict) -> bool:
    """Did an import actually CHANGE the glossary's content? The single predicate
    behind both import seams' approval revert.

    True when the import created a term (`entries`), created a sense (`senses`),
    or rewrote a stored value on a term/sense that already existed (`changed` --
    glossary._apply_rows's tm.import_tmx-style value-changed row count, and, for the
    concept-preserving arm, :func:`import_bytes`'s compat-store fingerprint diff). False for
    a genuinely IDEMPOTENT re-import, whose rows all land in `unchanged`.

    Both halves matter. Without the `changed` term, re-importing a CSV/TBX that
    rewrites every target of an APPROVED glossary left it APPROVED with different
    content underneath it (the bug this predicate fixes, reproduced 2026-07-29) --
    while editing ONE term in the UI correctly reverted. With a naive `updated`
    (a ROW count that includes byte-identical rewrites) instead, the opposite
    breakage: a no-op re-import would revert a published glossary to draft, and a
    workflow step pinned to it requires APPROVED, so an idempotent re-import
    would silently break a running workflow. A concept import reporting only a
    written-`terms` COUNT is NOT a change signal for the same reason `updated` is not
    (round-1 M4) -- its change is measured on the compat store instead."""
    return bool(res.get("entries") or res.get("senses") or res.get("changed"))


def list_entries(session: Session, glossary_id: int) -> list[dict]:
    """The glossary's entries + senses (the module term editor grid)."""
    return gloss_engine.list_entries_for_glossary(session, glossary_id)


def page_entries(session: Session, glossary_id: int, *, cursor=None, limit=100) -> dict:
    """One bounded module entry page with senses; scope is the named glossary."""
    return gloss_engine.page_entries(session, glossary_id=glossary_id, cursor=cursor, limit=limit)


def entry_count(session: Session, glossary_id: int) -> int:
    """Exact module entry total in one aggregate query, with no row loads."""
    from sqlalchemy import func

    return int(
        session.exec(
            select(func.count())
            .select_from(GlossaryEntry)
            .where(GlossaryEntry.glossary_id == glossary_id)
        ).one()
    )


def term_counts(session: Session, glossary_id: int) -> tuple[int, int]:
    """(total_terms, dnt_terms) for a glossary via two cheap COUNT(*) queries — never
    loads a row or a sense subquery. Flat cost regardless of glossary size (the
    Workflow Studio rail path; list_entries_for_glossary materializes all terms
    and must not be used just to count)."""
    from sqlalchemy import func

    total = session.exec(
        select(func.count())
        .select_from(GlossaryEntry)
        .where(GlossaryEntry.glossary_id == glossary_id)
    ).one()
    dnt = session.exec(
        select(func.count())
        .select_from(GlossaryEntry)
        .where(GlossaryEntry.glossary_id == glossary_id, GlossaryEntry.dnt == True)  # noqa: E712
    ).one()
    return int(total), int(dnt)


def preview_terms(session: Session, glossary_id: int, limit: int = 50) -> list[dict]:
    """Up to `limit` of a glossary's terms as lean dicts (source/target/dnt) — a
    bounded, sense-free LIMIT fetch for the rail's click-to-insert list. Skips the
    batched GlossarySense fetches list_entries_for_glossary runs, so the cost is
    O(limit), not O(glossary size)."""
    rows = session.exec(
        select(GlossaryEntry)
        .where(GlossaryEntry.glossary_id == glossary_id)
        .order_by(GlossaryEntry.id)
        .limit(max(0, int(limit)))
    ).all()
    return [
        {"source": r.source_term or "", "target": r.target_term or "", "dnt": bool(r.dnt)}
        for r in rows
    ]


def to_dict(session: Session, g: Glossary) -> dict:
    """A JSON-serializable summary (CLI --json, /api) of a glossary + its pairs +
    entry count."""
    total, _dnt = term_counts(session, g.id)
    return {
        "id": g.id,
        "name": g.name,
        "status": g.status,
        "org_id": g.org_id,
        "customer_id": g.customer_id,
        "content_type_id": g.content_type_id,
        "approved_by": g.approved_by,
        "approved_at": g.approved_at.isoformat() if g.approved_at else None,
        "created_by": g.created_by,
        "pairs": pairs_for(session, g.id),
        "entry_count": total,
    }
