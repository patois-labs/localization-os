"""Attach an ALREADY-ESTABLISHED translation memory or glossary to a project.

The maintainer's feedback intake 2026-08-03, item 5: the project panel could only IMPORT a
TMX / CSV / TBX *file*. If the memory or terminology you wanted already lived in this
install — another project of the same customer, an approved module glossary — there
was no way to point at it. This module is the ONE service seam behind both new
attachments, so the project page, the `project tm-sources` / `project glossaries` CLI
verbs and any future /api route are all thin callers (repo law: web + CLI parity by
sharing a service, not by re-implementing).

TWO DIFFERENT MECHANISMS, deliberately:

  * TM = a REFERENCE (ProjectTmSource). There is no first-class "a TM" entity — a
    SegmentTM/FullTextTM/SegmentEmbedding row is scoped by its project_id and nothing
    else. So attaching cannot mean "add this TM"; it means "when THIS project looks
    up, ALSO match that project's rows". Nothing is copied, re-stamped or moved, and
    the write paths (write_back / import_tmx), `tm.count`, the TMX export and the /tm
    browse are all untouched — a project never starts writing into, counting or
    exporting somebody else's memory. Detaching therefore restores the previous
    behaviour byte-identically, which is the property that makes this safe to try.

  * Glossary = a PIN (ProjectGlossary). Glossaries DO have a first-class container, so
    the natural attach is "this project uses that glossary", unioned into the existing
    auto-attach plan (glossary_module.build_glossary_plan).

THE READ-TIME RE-DERIVATION RULE (the security core, and what the two isolation
reviews should attack). A link row is a HINT, never an authority. Both readers —
allowed_tm_source_ids and pinned_glossary_ids — re-resolve every link against the LIVE
Project/Glossary row through access.cross_project_scope_allows /
access.project_can_use_glossary, the exact predicates the write path enforces. So:

  * a link created legitimately and then invalidated (the source project was moved to
    another customer, the glossary was reassigned) STOPS contributing immediately, with
    no cleanup job and no cache to invalidate;
  * a link planted straight into the DB — the hostile case, an attacker with a write
    primitive on the link table but not on `project` — reaches NOTHING, because the ids
    it names are filtered by the same predicate before they ever enter a SQL scope;
  * turning the multi-org gate ON retroactively tightens every existing link.

The alternative (trusting the link and validating only at write time) would make the
link table itself a tenant boundary. It is not one, and it must never become one.

ONE LEVEL, NEVER TRANSITIVE. allowed_tm_source_ids reads only the rows whose
`project_id` IS the reading project; a source's own sources do not chain. Transitivity
would make the reachable set unbounded, un-auditable from the project page, and would
need a per-hop scope proof. Attach each source you want, explicitly.

MATCH, NOT BROWSE (isolation review ISO-3). An attach widens what a project MATCHES.
It does NOT widen any surface that lets a human READ THE SOURCE MEMORY OUT:

  * MATCH paths (100% exact, fuzzy, batch, TM few-shot examples, full-text, semantic)
    honour attached sources. Each answers a bounded question about a segment the caller
    already holds.
  * BROWSE paths do NOT. `tm.concordance` (the editor's concordance box) takes an
    ARBITRARY substring and returns every row containing it, so honouring attachments
    there would let any member of the borrowing project walk the source project's whole
    TM a query at a time, with no visibility into that project required. It passes
    `include_sources=False`. The `/tm` module browse never routed through the lookup
    scope at all (it scopes by access.visible_project_ids) and is unchanged.
  * OWNERSHIP surfaces do not move either: `tm.count`, `tm.ft_count`, TMX export and
    TM write-back stay own-project, and `tm_module.bump_usage` is project-confined so a
    borrower's runs never touch the source owner's usage counters (their curation and
    prune-by-usage inputs).

WHAT YOU BORROW LOSES TIES. When the project's own row and a borrowed row are equally
good, the OWN row wins (tm._own_first / _own_first_key). Otherwise a borrowed row could
shadow a curated one on the auto-applying 100% path, and a reviewer's correction — a
NEW own row, therefore a HIGHER id — could never take effect without detaching.

Documented in: docs/architecture/content-and-projects.md
"""

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import (
    Glossary,
    GlossaryStatus,
    Project,
    ProjectGlossary,
    ProjectTmSource,
    SegmentTM,
)
from app.services import access, ids


class AttachError(ValueError):
    """A refused attach/detach (unknown id, self-link, foreign customer/org, ...).

    One error type for both link kinds so the web routes and the CLI can render the
    same message; the message text is the operator-facing explanation."""


# --- id coercion (one bound, EVERY surface) -----------------------------------
# This module's original "one bound, both surfaces" note (isolation review ISO-5)
# has been promoted to the whole app: the law now lives in app/services/ids.py so
# the web/CLI/api surfaces cannot drift. _coerce_id delegates to keep this module's
# call sites and their AttachError contract unchanged.
def _coerce_id(value):
    """A positive, in-range row id, or None for anything else (blank, non-numeric,
    zero/negative, absurdly large). Never raises. -> app.services.ids.coerce_id."""
    return ids.coerce_id(value)


# --- TM sources: read-side (the seam tm.py calls on EVERY lookup) --------------
def allowed_tm_source_ids(session: Session, project_id) -> tuple:
    """The source project ids `project_id` may CURRENTLY read TM rows from.

    Called from tm._tm_read_scope, i.e. on the exact/fuzzy path, the batch path, the TM
    few-shot example path, the full-text path and the semantic path — one seam, so no
    MATCH path can be forgotten. NOT reached by concordance or any other browse surface
    (they pass include_sources=False; see the module docstring's match/browse
    asymmetry). Returns a sorted tuple; EMPTY (the
    overwhelmingly common case: no links) makes the scope predicate byte-identical to
    what it was before this feature existed.

    Every id is re-validated here, at READ time, against the live Project rows — see
    the module docstring. Only rows whose project_id IS this project are read, so the
    relation is one level deep and never transitive."""
    if session is None or project_id is None:
        return ()
    target = session.get(Project, project_id)
    if target is None:
        return ()
    rows = session.exec(
        select(ProjectTmSource.source_project_id).where(ProjectTmSource.project_id == project_id)
    ).all()
    allowed = []
    # AI opt-out (DPAI-08): an opted-out source project (directly or through its customer)
    # contributes NOTHING to another project's matches -- no exact/fuzzy fill, no TM
    # few-shot example, no full-text or semantic hit -- because the reading project's MT,
    # QE and LQA would carry that content to an AI engine. One seam, every match path.
    ai_off = None
    for sid in rows:
        if sid is None or sid == project_id:
            continue  # self-link: refused at write, inert at read
        src = session.get(Project, sid)  # identity-map cached within a session
        if access.cross_project_scope_allows(target, src):
            if ai_off is None:
                from app.services import ai_optout

                ai_off = ai_optout.opted_out_project_ids(session)
            if sid in ai_off:
                continue
            allowed.append(sid)
    return tuple(sorted(set(allowed)))


# --- TM sources: management ---------------------------------------------------
def _tm_entry_count(session: Session, project_id: int) -> int:
    return int(
        session.exec(
            select(func.count()).select_from(SegmentTM).where(SegmentTM.project_id == project_id)
        ).one()
    )


def _distinct_pairs(session: Session, project_id: int) -> list:
    """The distinct 'src -> tgt' pairs a project's own TM rows cover (label material
    for the attach dropdown), ordered and capped-free — a project has a handful."""
    rows = session.exec(
        select(SegmentTM.source_locale, SegmentTM.target_locale)
        .where(SegmentTM.project_id == project_id)
        .distinct()
    ).all()
    return sorted({f"{s} -> {t}" for s, t in rows if s and t})


def _reachable_count(session: Session, src_project_id: int, target: Project) -> int:
    """How many of `src_project_id`'s TM rows the TARGET project can actually MATCH.

    Behaviour review F4. An attach widens the PROJECT layer of tm._tm_read_scope, but
    the CONTENT-TYPE layer is ANDed on top and is not widened — the reading project's
    own type still governs. So attaching a Legal-typed project to a Marketing-typed one
    is accepted, stores a link, and matches NOTHING: a silent no-op that the card
    reported as "1,240 entries" because it counted the SOURCE's rows rather than the
    ones this project can see. The number a user reads has to be the number they get,
    so both surfaces render THIS, and the attach banner says so when it is short.

    Uses the very predicate the lookup paths use (_content_type_scope over the target's
    resolved type), so the count cannot drift from what matching actually does."""
    from app.services.tm import _content_type_scope, _project_content_type_id

    ct_id = _project_content_type_id(session, target.id)
    return int(
        session.exec(
            select(func.count())
            .select_from(SegmentTM)
            .where(SegmentTM.project_id == src_project_id, _content_type_scope(SegmentTM, ct_id))
        ).one()
    )


def _source_view(session: Session, src: Project, link=None, target: Project = None) -> dict:
    total = _tm_entry_count(session, src.id)
    reachable = _reachable_count(session, src.id, target) if target is not None else total
    return {
        "source_project_id": src.id,
        "name": src.name,
        "customer_id": src.customer_id,
        # entry_count is what THIS project can match (the honest number for a picker
        # and a card); total_count is the source project's own size, kept so a surface
        # can explain a shortfall instead of just showing a smaller number.
        "entry_count": reachable,
        "total_count": total,
        "content_type_limited": reachable < total,
        "pairs": _distinct_pairs(session, src.id),
        "created_by": getattr(link, "created_by", "") or "",
        "created_at": getattr(link, "created_at", None),
    }


def list_tm_sources(session: Session, project_id: int) -> list:
    """The project's CURRENTLY EFFECTIVE TM sources, oldest link first.

    Only sources that pass the read-time predicate are listed, so the project page and
    `project tm-sources list` show exactly what the lookup paths will actually match —
    a link that has gone stale disappears from the UI instead of lying about coverage.

    ACCEPTED POLICY (isolation review ISO-8): this list, like list_glossary_pins, shows
    the source project's NAME and entry count to anyone who may view the borrowing
    project — including a member who has no visibility into the source itself. That is
    deliberate, not an oversight: a borrower must be able to see, audit and detach what
    is feeding its translations, and a name plus a count is the minimum that makes the
    attachment explicable. The row CONTENT stays unreachable (see the match/browse
    asymmetry in the module docstring)."""
    target = session.get(Project, project_id)
    if target is None:
        return []
    out = []
    links = session.exec(
        select(ProjectTmSource)
        .where(ProjectTmSource.project_id == project_id)
        .order_by(ProjectTmSource.created_at, ProjectTmSource.id)
    ).all()
    for link in links:
        src = session.get(Project, link.source_project_id)
        if not access.cross_project_scope_allows(target, src):
            continue
        out.append(_source_view(session, src, link, target=target))
    return out


def tm_source_candidates(session: Session, project_id: int, *, user=None) -> list:
    """Projects this project MAY attach as a TM source and hasn't yet.

    Server-side filtered (never client-side): the same-customer/same-org predicate,
    minus already-attached ones and itself, and — when a user is supplied — further
    confined to projects that user may actually SEE (access.can_view_project), so the
    dropdown can never become an enumeration oracle for projects outside the caller's
    ring.

    A project with ZERO TM entries is NOT offered. The card attaches an ALREADY-
    ESTABLISHED memory, and a project that has none has nothing to establish — offering
    every project in the install as a "source" would bury the two that actually carry
    memory. It also keeps this picker from being a general project directory: as a
    happy consequence the picker only moves when real memory appears, which is what
    lets the key-project inertness invariant (verify_keys_inertness [H1]) keep holding
    — a freshly created project of ANY type does not perturb another project's page.
    Attaching is still possible the moment the source has its first entry.

    NOTE this is a filter on the CANDIDATE list only. An EXISTING link to a project
    whose TM was later emptied keeps working (it simply matches nothing), because
    detaching it is the operator's call, not ours."""
    target = session.get(Project, project_id)
    if target is None:
        return []
    attached = set(
        session.exec(
            select(ProjectTmSource.source_project_id).where(
                ProjectTmSource.project_id == project_id
            )
        ).all()
    )
    # SQL pre-narrowing is a COST optimisation only, never the authority: it drops the
    # obviously-foreign rows so a large install does not load every project just to
    # render one dropdown, and cross_project_scope_allows below still decides. Keeping
    # both means the predicate cannot be quietly bypassed by loosening the query.
    stmt = select(Project).where(
        (Project.customer_id.is_(None))
        if target.customer_id is None
        else (Project.customer_id.is_(None) | (Project.customer_id == target.customer_id))
    )
    out = []
    for src in session.exec(stmt.order_by(Project.name, Project.id)).all():
        if src.id in attached or not access.cross_project_scope_allows(target, src):
            continue
        if user is not None and not access.can_view_project(session, user, src):
            continue
        view = _source_view(session, src, target=target)
        if not view["entry_count"]:
            continue  # no established memory -> nothing to attach
        out.append(view)
    return out


def add_tm_source(
    session: Session, project_id: int, source_project_id, *, created_by: str = "", user=None
) -> dict:
    """Attach `source_project_id` as a TM source of `project_id`. Idempotent.

    Raises AttachError on an unknown project, a self-link, or a source outside the
    project's org/customer ring. When `user` is given the source must ALSO be visible
    to them (a caller cannot attach a project they could not otherwise see). Returns
    the source view; a duplicate attach returns the existing one without a second row
    (the unique index is the backstop, and an IntegrityError from a concurrent
    duplicate is absorbed as the same no-op)."""
    target = session.get(Project, project_id)
    if target is None:
        raise AttachError(f"No such project: {project_id}")
    sid = _coerce_id(source_project_id)
    if sid is None:
        raise AttachError("Choose a project to attach.")
    if sid == project_id:
        raise AttachError("A project already reads its own translation memory.")
    src = session.get(Project, sid)
    if src is None:
        raise AttachError(f"No such project: {sid}")
    if not access.cross_project_scope_allows(target, src):
        raise AttachError(
            f"Project #{sid} cannot be attached: it belongs to a different "
            "customer or organization."
        )
    if user is not None and not access.can_view_project(session, user, src):
        raise AttachError(f"No such project: {sid}")
    existing = session.exec(
        select(ProjectTmSource).where(
            ProjectTmSource.project_id == project_id, ProjectTmSource.source_project_id == sid
        )
    ).first()
    if existing is None:
        session.add(
            ProjectTmSource(
                project_id=project_id, source_project_id=sid, created_by=created_by or ""
            )
        )
        try:
            session.commit()
        except IntegrityError:  # concurrent duplicate: the unique index won
            session.rollback()
    return _source_view(session, src, target=target)


def remove_tm_source(session: Session, project_id: int, source_project_id) -> bool:
    """Detach a TM source. Returns True when a link row was removed.

    Removal is the ONLY cleanup needed: nothing was copied, so the project's lookups
    return to exactly the rows they matched before the attach."""
    sid = _coerce_id(source_project_id)
    if sid is None:
        return False
    row = session.exec(
        select(ProjectTmSource).where(
            ProjectTmSource.project_id == project_id, ProjectTmSource.source_project_id == sid
        )
    ).first()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# --- Glossary pins: read-side (the seam build_glossary_plan calls) -------------
def pinned_glossary_ids(session: Session, project) -> tuple:
    """The module-glossary ids pinned to `project` that may CURRENTLY contribute.

    Filtered, at RUN time, by (a) the isolation predicate against the live Glossary row
    (access.project_can_use_glossary) and (b) APPROVAL: a DRAFT glossary contributes
    nothing until approved, exactly like the workflow-step pin, so pinning is never a
    way to slip unreviewed terminology into a run. Ordered by when it was pinned, so
    precedence between two pins is stable and explainable.

    EMPTY for every project with no pin rows, which is what keeps build_glossary_plan
    byte-identical for existing installs."""
    pid = getattr(project, "id", None) if not isinstance(project, int) else project
    if session is None or pid is None:
        return ()
    proj = project if not isinstance(project, int) else session.get(Project, pid)
    if proj is None:
        return ()
    links = session.exec(
        select(ProjectGlossary)
        .where(ProjectGlossary.project_id == pid)
        .order_by(ProjectGlossary.created_at, ProjectGlossary.id)
    ).all()
    out = []
    for link in links:
        g = session.get(Glossary, link.glossary_id)
        if g is None or g.status != GlossaryStatus.APPROVED:
            continue
        if not access.project_can_use_glossary(proj, g):
            continue
        if g.id not in out:
            out.append(g.id)
    return tuple(out)


# --- Glossary pins: management ------------------------------------------------
def _glossary_view(session: Session, g: Glossary, link=None, project: Project = None) -> dict:
    """One glossary rendered for a picker / card.

    `type_note` (behaviour review F6) is the DISCLOSURE half of the explicit-pin-wins
    precedence. A pin deliberately bypasses the content-type filter — that is what
    makes it explicit — but the consequence is that pinning a Legal glossary to a
    Marketing project silently OUTRANKS the project's own Marketing glossaries on every
    term conflict, and nothing on either surface said so. The precedence is kept (it is
    the point of an explicit pin); what changes is that the picker and the pinned list
    both label the mismatch, so choosing it is a decision rather than an accident."""
    from app.services import glossary_module as gm

    terms, dnt = gm.term_counts(session, g.id)
    type_note = ""
    if (
        project is not None
        and g.content_type_id is not None
        and g.content_type_id != getattr(project, "content_type_id", None)
    ):
        from app.services import content_types

        ct = content_types.get(session, g.content_type_id)
        type_note = "other content type: %s" % (
            getattr(ct, "name", None) or (f"#{g.content_type_id}")
        )
    return {
        "glossary_id": g.id,
        "name": g.name,
        "status": g.status,
        "approved": g.status == GlossaryStatus.APPROVED,
        "customer_id": g.customer_id,
        "content_type_id": g.content_type_id,
        # "" when the glossary's type matches the project's (or either is General), so
        # a surface can render it unconditionally and show nothing in the normal case.
        "type_note": type_note,
        "pairs": [
            f"{p['source_locale']} -> {p['target_locale']}" for p in gm.pairs_for(session, g.id)
        ],
        "term_count": terms,
        "dnt_count": dnt,
        "created_by": getattr(link, "created_by", "") or "",
        "created_at": getattr(link, "created_at", None),
    }


def list_glossary_pins(session: Session, project_id: int) -> list:
    """The project's pinned glossaries, oldest pin first.

    Unlike list_tm_sources this DOES list a pin whose glossary is still a DRAFT (with
    `active` False): the operator deliberately pinned it and needs to see that it is
    waiting on approval rather than wonder where it went. Isolation failures are still
    hidden — a glossary the project may not use is not the operator's to know about."""
    proj = session.get(Project, project_id)
    if proj is None:
        return []
    out = []
    links = session.exec(
        select(ProjectGlossary)
        .where(ProjectGlossary.project_id == project_id)
        .order_by(ProjectGlossary.created_at, ProjectGlossary.id)
    ).all()
    for link in links:
        g = session.get(Glossary, link.glossary_id)
        if g is None or not access.project_can_use_glossary(proj, g):
            continue
        view = _glossary_view(session, g, link, project=proj)
        view["active"] = view["approved"]
        out.append(view)
    return out


def glossary_pin_candidates(
    session: Session, project_id: int, *, org_id=None, bound_customer_id=None
) -> list:
    """Glossaries this project may pin and hasn't yet — APPROVED ones first.

    TWO independent scopes, both server-side, because they answer two different
    questions and only one of them is about the project:

      * PROJECT scope — access.project_can_use_glossary, the same predicate the pin
        write and the run-time read use, so the picker can never name something the
        POST would refuse or the run would ignore;
      * CALLER scope — glossary_module.visible(org_id, bound_customer_id), the module's
        single read-authz seam (isolation review ISO-4). Without it the picker offered
        LEGACY UNASSIGNED (customer-less) glossaries to a customer-BOUND user, which is
        exactly the set /glossaries deliberately hides from them (its "Unassigned
        (needs a customer)" section is can_manage-and-unbound only). A bound linguist
        could see, and pin, a glossary the glossary module refuses to show them —
        a second door into the same rows. Passing both scopes closes it.

    Drafts ARE offered, flagged by their status, so an operator can pin ahead of
    approval; they simply contribute nothing until approved."""
    proj = session.get(Project, project_id)
    if proj is None:
        return []
    pinned = set(
        session.exec(
            select(ProjectGlossary.glossary_id).where(ProjectGlossary.project_id == project_id)
        ).all()
    )
    # Same shape as tm_source_candidates: SQL narrows for cost, the predicate decides.
    stmt = select(Glossary).where(
        (Glossary.customer_id.is_(None))
        if proj.customer_id is None
        else (Glossary.customer_id.is_(None) | (Glossary.customer_id == proj.customer_id))
    )
    from app.services import glossary_module as gm

    out = []
    for g in session.exec(stmt.order_by(Glossary.name, Glossary.id)).all():
        if g.id in pinned or not access.project_can_use_glossary(proj, g):
            continue
        if gm.visible(session, g.id, org_id, bound_customer_id) is None:
            continue  # not this CALLER's to see, let alone pin
        out.append(_glossary_view(session, g, project=proj))
    out.sort(key=lambda v: (0 if v["approved"] else 1, (v["name"] or "").lower(), v["glossary_id"]))
    return out


def add_glossary_pin(
    session: Session,
    project_id: int,
    glossary_id,
    *,
    created_by: str = "",
    org_id=None,
    bound_customer_id=None,
) -> dict:
    """Pin a module glossary to a project. Idempotent.

    Raises AttachError on an unknown glossary or one outside the project's ring. A
    DRAFT glossary may be pinned (see glossary_pin_candidates) — approval is enforced
    at RUN time, not here, so the pin survives an approve/unapprove cycle.

    Enforces BOTH scopes the picker filters by (ISO-4): the project ring AND the
    caller's own module visibility. The picker is a convenience, never the gate — a
    hand-posted glossary_id has to clear the same two checks."""
    proj = session.get(Project, project_id)
    if proj is None:
        raise AttachError(f"No such project: {project_id}")
    gid = _coerce_id(glossary_id)
    if gid is None:
        raise AttachError("Choose a glossary to attach.")
    g = session.get(Glossary, gid)
    if g is None:
        raise AttachError(f"No such glossary: {gid}")
    from app.services import glossary_module as gm

    if gm.visible(session, gid, org_id, bound_customer_id) is None:
        # Same message as the ring refusal below: a caller must not be able to tell a
        # glossary they may not see from one that does not exist.
        raise AttachError(
            f"Glossary #{gid} cannot be attached: it belongs to a different "
            "customer or organization."
        )
    if not access.project_can_use_glossary(proj, g):
        raise AttachError(
            f"Glossary #{gid} cannot be attached: it belongs to a different "
            "customer or organization."
        )
    existing = session.exec(
        select(ProjectGlossary).where(
            ProjectGlossary.project_id == project_id, ProjectGlossary.glossary_id == gid
        )
    ).first()
    if existing is None:
        session.add(
            ProjectGlossary(project_id=project_id, glossary_id=gid, created_by=created_by or "")
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
    view = _glossary_view(session, g, project=proj)
    view["active"] = view["approved"]
    return view


def remove_glossary_pin(session: Session, project_id: int, glossary_id) -> bool:
    """Unpin a glossary. Returns True when a pin row was removed. The glossary itself
    is untouched — this only severs the project's use of it."""
    gid = _coerce_id(glossary_id)
    if gid is None:
        return False
    row = session.exec(
        select(ProjectGlossary).where(
            ProjectGlossary.project_id == project_id, ProjectGlossary.glossary_id == gid
        )
    ).first()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True
