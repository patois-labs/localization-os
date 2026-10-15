"""Segmentation profiles — named SRX rulesets, scoped per customer/project (P6, item 8).

Shared by the web (/settings segmentation section + the per-project picker + the
customer-360 default selector) and the headless CLI (`segprofile ...`) — the
extract-then-CLI parity rule (ONE source of truth). No HTTP concerns leak in.

WHAT A PROFILE IS
  A named SRX 2.0 ruleset with a scope:
    - customer_id NULL  = a GLOBAL profile (offered on every project)
    - customer_id set   = a CUSTOMER profile (offered only on that customer's projects)
  `srx_blob` NULL = the BUILT-IN default (the shipped default.srx, which reproduces
  the legacy splitter byte for byte) — a NULL-blob profile segments identically to
  today. A non-NULL blob is a custom ruleset (1MB-bounded + parse-validated).
  Exactly one profile may carry is_system_default (the 3rd rung of the chain).

THE RESOLUTION CHAIN (resolve_ruleset / resolve, XTM's explicit order — GAP 2)
  At ingest, the ruleset a document is segmented under is resolved:
      project.segmentation_profile_id            (the "untick default" override)
   -> project.customer.default_segmentation_profile_id
   -> the system-default profile (is_system_default)
   -> the built-in default.srx / legacy splitter
  The srx_enabled GLOBAL setting is the master switch: OFF (the default) means the
  legacy splitter always runs and the chain is IGNORED (byte-identical to today).

CLONE-BEFORE-EDIT (memoQ safety convention — GAP 2)
  A profile's srx_blob is IMMUTABLE after creation: to customize, `clone` a profile
  (copying its ruleset) and edit the copy, or upload a fresh ruleset as a new
  profile. This protects a shared default from silent drift and — crucially — keeps
  the seg_engine sha1 stamp stable (a document ingested under a profile always finds
  the exact ruleset bytes it was stamped with, on export). Only non-blob metadata
  (name, description, scope, enabled/system-default flag) is mutable in place.

TM-SAFETY (the seg_engine stamp)
  srx.engine_identity_for_bytes(ruleset) stamps srx:<sha1-of-ruleset> at ingest;
  export reproduces that exact split (srx.segment_spans_for_engine) or falls back to
  legacy with a warning on a hash mismatch. This module registers a resolver so a
  per-project/per-customer profile's blob is findable by hash at export time. Because
  blobs are immutable, a stamp never dangles for a live profile; deleting a profile
  a document was ingested under leaves the stamp resolvable ONLY while another
  profile (or the on-disk custom file / default) shares the same bytes — otherwise
  export degrades to legacy with a warning, never a silent mis-split.

Documented in: docs/architecture/segmentation.md
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.db import engine
from app.models import Customer, Project, SegmentationProfile
from app.services import ids, srx

# Bound a custom SRX ruleset well under the general upload cap — rulesets are tiny
# (a few KB); anything larger is almost certainly not a real .srx. Mirrors
# app/web/admin._MAX_SRX_BYTES.
MAX_SRX_BYTES = 1 * 1024 * 1024

# Sentinel for update_profile's customer_id arg: "not passed" (leave unchanged) vs
# an explicit None/'' (clear the scope → global). A plain None can't distinguish the
# two, so we use a unique object.
_UNSET = object()


class SegProfileError(ValueError):
    """A profile create/clone/assign was rejected (bad name, invalid ruleset, a
    cross-customer assignment, editing an immutable blob, ...). Carries a
    human-readable message for the UI/CLI."""


# --- ruleset bytes -----------------------------------------------------------
def profile_ruleset_bytes(prof: SegmentationProfile) -> bytes:
    """The raw SRX ruleset bytes a profile segments under: its custom blob, or the
    shipped default.srx bytes when the blob is NULL (the built-in rung)."""
    if prof is not None and prof.srx_blob:
        return prof.srx_blob.encode("utf-8")
    return srx.default_ruleset_bytes()


def _blob_resolver(want_hash: str) -> bytes | None:
    """srx.register_profile_blob_resolver callback: find a stored profile's custom
    ruleset bytes whose sha1 prefix matches ``want_hash`` (export hash resolution).
    NULL-blob profiles resolve to the default, which srx already checks, so we only
    scan custom blobs here."""
    with Session(engine) as s:
        rows = s.exec(
            select(SegmentationProfile).where(SegmentationProfile.srx_blob.is_not(None))
        ).all()
    for p in rows:
        data = (p.srx_blob or "").encode("utf-8")
        if srx._ruleset_hash(data) == want_hash:
            return data
    return None


# Register the resolver at import so export's srx._document_for_hash can find a
# profile's blob by hash. Importing this module (which app.services.ingest does)
# wires it up; srx.py stays a leaf (no reverse import).
srx.register_profile_blob_resolver(_blob_resolver)


# --- CRUD --------------------------------------------------------------------
def list_profiles(org_id=None) -> list[SegmentationProfile]:
    """All segmentation profiles, newest first. `org_id`, when given (multi-org
    scoped caller), restricts to that org; None (single-tenant / gate off) lists
    all."""
    with Session(engine) as s:
        q = select(SegmentationProfile).order_by(SegmentationProfile.created_at.desc())
        if org_id is not None:
            q = q.where(SegmentationProfile.org_id == org_id)
        return list(s.exec(q).all())


def get_profile(pid: int, org_id=None) -> SegmentationProfile | None:
    """A profile by id. `org_id`, when given (multi-org scoped caller), treats a
    foreign org's profile as not-found (None); None (single-tenant / gate off /
    unscoped operator) returns any profile — byte-identical to today."""
    with Session(engine) as s:
        prof = s.get(SegmentationProfile, pid)
        if prof is None:
            return None
        if org_id is not None and getattr(prof, "org_id", None) != org_id:
            return None
        return prof


def get_by_name(name: str) -> SegmentationProfile | None:
    with Session(engine) as s:
        return s.exec(select(SegmentationProfile).where(SegmentationProfile.name == name)).first()


def name_taken(name: str, exclude_id: int | None = None) -> bool:
    row = get_by_name(name)
    return bool(row and row.id != exclude_id)


def validate_ruleset(raw: bytes) -> tuple[bool, str]:
    """Bound + parse-validate a custom SRX ruleset (mirrors the /settings upload).
    Returns (ok, message)."""
    if len(raw) > MAX_SRX_BYTES:
        return False, "SRX ruleset too large (max 1MB)."
    return srx.validate(raw)


def create_profile(
    name: str,
    *,
    srx_blob: str | None = None,
    customer_id=None,
    description: str = "",
    is_system_default: bool = False,
    actor: str = "",
    org_id=None,
) -> SegmentationProfile:
    """Create a segmentation profile. `name` must be unique per install. A non-None
    `srx_blob` is PARSE-VALIDATED (invalid → SegProfileError); None = the built-in
    default ruleset. `customer_id` None = a global profile. Setting
    is_system_default clears the flag on any other profile (one system default per
    install)."""
    name = (name or "").strip()
    if not name:
        raise SegProfileError("segmentation profile name is required")
    if name_taken(name):
        raise SegProfileError(f"a segmentation profile named {name!r} already exists")
    cust_id = _coerce_id(customer_id)
    if cust_id is not None:
        with Session(engine) as s:
            if s.get(Customer, cust_id) is None:
                raise SegProfileError(f"no such customer: {cust_id}")
    if srx_blob is not None:
        ok, msg = validate_ruleset(srx_blob.encode("utf-8"))
        if not ok:
            raise SegProfileError(f"invalid SRX ruleset: {msg}")
    with Session(engine) as s:
        if is_system_default:
            # Multi-org: only clear THIS org's existing system default (else org
            # B's admin creating a new default would silently steal org A's
            # slot too). No-op-equivalent (clears every profile) while the gate
            # is off, since org_id is then None for everyone — byte-identical.
            _clear_system_default(s, org_id=org_id)
        prof = SegmentationProfile(
            name=name,
            srx_blob=srx_blob,
            customer_id=cust_id,
            description=(description or "").strip(),
            is_system_default=is_system_default,
            created_by=actor,
            org_id=org_id,
        )
        s.add(prof)
        s.commit()
        s.refresh(prof)
        return prof


def clone_profile(
    pid: int, new_name: str, *, customer_id=None, actor: str = "", org_id=None
) -> SegmentationProfile:
    """Clone a profile's ruleset into a new, editable profile (memoQ clone-before-edit).
    The clone copies the source's srx_blob (or NULL = built-in). `customer_id`
    defaults to the SOURCE's scope; pass a value (or "" to globalize) to re-scope.
    The clone is never the system default. Raises SegProfileError on a bad source /
    duplicate name.

    `org_id`, when given (multi-org scoped caller — web route or CLI --org),
    both GATES the source lookup (a foreign org's profile is not-found, never
    clonable) and STAMPS the new clone's org (you can only ever clone into your
    own org). None (single-tenant / gate off / unscoped operator) does neither —
    byte-identical to today, and the clone inherits the source's org as before."""
    src = get_profile(pid, org_id=org_id)
    if src is None:
        raise SegProfileError(f"no such segmentation profile: {pid}")
    scope = src.customer_id if customer_id is None else _coerce_id(customer_id)
    return create_profile(
        new_name,
        srx_blob=src.srx_blob,
        customer_id=scope,
        description=src.description,
        is_system_default=False,
        actor=actor,
        org_id=org_id if org_id is not None else src.org_id,
    )


def update_profile(
    pid: int,
    *,
    name: str | None = None,
    description: str | None = None,
    customer_id=_UNSET,
    is_system_default: bool | None = None,
    org_id=None,
) -> SegmentationProfile | None:
    """Update a profile's METADATA in place (name/description/scope/system-default).
    The srx_blob is IMMUTABLE (clone-before-edit) — this never touches it. Only
    non-None/args-present change. Setting is_system_default True clears it elsewhere;
    False just clears this one. Returns the profile, or None if missing (also None
    for a foreign org's profile when `org_id` scopes the caller — not-found, never
    enumerable; a no-op filter when org_id is None, byte-identical to today)."""
    with Session(engine) as s:
        prof = s.get(SegmentationProfile, pid)
        if not prof:
            return None
        if org_id is not None and getattr(prof, "org_id", None) != org_id:
            return None
        if name is not None:
            new = name.strip()
            if not new:
                # A blank/whitespace-only name used to be a silent no-op: every
                # rename surface (web /settings, the rename API, the CLI) would
                # then report "success" and write an old-name -> old-name audit
                # row. create_profile already refuses a blank name; update_profile
                # must match it instead of quietly doing nothing (FIX 3).
                raise SegProfileError("segmentation profile name is required")
            if new != prof.name:
                if name_taken(new, exclude_id=pid):
                    raise SegProfileError(f"a segmentation profile named {new!r} already exists")
                prof.name = new
        if description is not None:
            prof.description = description.strip()
        if customer_id is not _UNSET:
            cid = _coerce_id(customer_id)
            if cid is not None and s.get(Customer, cid) is None:
                raise SegProfileError(f"no such customer: {cid}")
            prof.customer_id = cid
        if is_system_default is not None:
            if is_system_default:
                # Multi-org: only clear THIS profile's own org's default (an
                # org-B admin flipping their own profile must not touch org A's
                # default slot). org_id=None (single-tenant/gate off) clears
                # every profile — byte-identical to today.
                _clear_system_default(s, keep=pid, org_id=prof.org_id)
            prof.is_system_default = is_system_default
        s.add(prof)
        s.commit()
        s.refresh(prof)
        return prof


def delete_profile(pid: int, org_id=None) -> bool:
    """Delete a profile. FAILS CLOSED itself now (the guard was previously duplicated
    at each call site with a "callers should guard" comment — a direct service call
    could bypass it and orphan a live reference): refused (SegProfileError with the
    live in-use count) while any project or customer default still references it.
    The routes/CLI keep their own friendly pre-check messages, but can no longer be
    routed around — this is the single fail-closed seam, mirroring
    content_types.delete / engines.delete_profile.

    `org_id`, when given (multi-org scoped caller), treats a foreign org's
    profile as not-found (returns False, row survives) rather than deleting it —
    a no-op filter when org_id is None, byte-identical to today. Existing documents
    keep their stamped seg_engine either way (TM safety).

    The in-use count runs in THIS SAME session/transaction as the delete (not a
    separate in_use_count() call opening its own session) — closing the TOCTOU
    window where a referrer could be written between an independent count and
    the delete."""
    with Session(engine) as s:
        prof = s.get(SegmentationProfile, pid)
        if not prof:
            return False
        if org_id is not None and getattr(prof, "org_id", None) != org_id:
            return False
        # T5-01 fail-closed: _in_use_count only ever counted Project/Customer
        # REFERRERS, never the profile's OWN is_system_default flag, so deleting
        # the profile that IS the install's (or, under multi-org, this org's)
        # system default used to succeed silently and leave ZERO profiles
        # flagged default — resolve_profile's rung 3 then falls straight through
        # to the built-in ruleset with no warning to anyone. Refuse instead and
        # tell the admin to promote another profile first, mirroring
        # engines.delete_profile's fail-closed check of the global
        # default_fallback_provider Setting (an entity that IS somebody else's
        # default must not be deletable out from under them).
        if prof.is_system_default:
            raise SegProfileError(
                "cannot delete segmentation profile %d (%r): it is the system "  # noqa: UP031
                "default — set another profile as the default first." % (pid, prof.name)
            )
        use = _in_use_count(s, pid)
        if use["total"]:
            raise SegProfileError(
                "cannot delete segmentation profile %d (%r): still referenced by "  # noqa: UP031
                "%d project(s) and %d customer default(s) — reassign them first."
                % (pid, prof.name, use["projects"], use["customers"])
            )
        s.delete(prof)
        s.commit()
        return True


def _in_use_count(s: Session, pid: int) -> dict:
    """The counting logic behind in_use_count / delete_profile, run against a
    CALLER-SUPPLIED session (so delete_profile can share one transaction with its
    own count — see delete_profile's docstring)."""
    n_proj = len(s.exec(select(Project.id).where(Project.segmentation_profile_id == pid)).all())
    n_cust = len(
        s.exec(select(Customer.id).where(Customer.default_segmentation_profile_id == pid)).all()
    )
    return {"projects": n_proj, "customers": n_cust, "total": n_proj + n_cust}


def in_use_count(pid: int) -> dict:
    """How many projects + customers currently reference this profile (delete guard).
    Returns {"projects": n, "customers": m, "total": n+m}."""
    with Session(engine) as s:
        return _in_use_count(s, pid)


def _clear_system_default(s: Session, keep: int | None = None, org_id=None) -> None:
    """Clear is_system_default on every profile (single-tenant / gate off — the
    ONE-per-install invariant), or, when the multi-org gate is active, only on
    profiles in `org_id` (the ONE-per-ORG invariant — clearing org A's default
    must never touch org B's). `org_id` is ignored (no-op) while the gate is
    off, so single-tenant behavior is byte-identical to before this fix."""
    from app.services import access

    q = select(SegmentationProfile).where(SegmentationProfile.is_system_default == True)  # noqa: E712
    if access.multi_org_active():
        q = q.where(SegmentationProfile.org_id == org_id)
    for p in s.exec(q).all():
        if keep is not None and p.id == keep:
            continue
        p.is_system_default = False
        s.add(p)


def system_default(org_id=None) -> SegmentationProfile | None:
    """The single system-default profile — the whole install's (single-tenant /
    gate off), or `org_id`'s own (multi-org active). `org_id` is ignored (no-op)
    while the gate is off, matching access.org_filter elsewhere."""
    from app.services import access

    with Session(engine) as s:
        q = select(SegmentationProfile).where(SegmentationProfile.is_system_default == True)  # noqa: E712
        if access.multi_org_active():
            q = q.where(SegmentationProfile.org_id == org_id)
        return s.exec(q).first()


def _coerce_id(v):
    """'' / None / malformed -> None; an int or digit-string in range -> int. Routes
    through the app-wide id law so a request-supplied Unicode digit / >2**63 value is
    None, never a 500 at int()/the SQLite bind."""
    return ids.coerce_id(v)


# --- assignment (with cross-customer validation) -----------------------------
def _assert_assignable(s: Session, prof: SegmentationProfile, customer_id) -> None:
    """A profile may be attached to a project/customer only if it is GLOBAL
    (customer_id NULL) or scoped to THAT customer (the P4 isolation pattern). A
    profile private to another customer is rejected."""
    if prof.customer_id is not None and prof.customer_id != customer_id:
        raise SegProfileError(
            f"segmentation profile {prof.id} belongs to another customer — a "
            "project/customer default must use a global or its own profile."
        )


def validate_for_create(session: Session, profile_id, customer_id, org_id=None) -> int | None:
    """Validate a --seg-profile / segmentation_profile_id pick BEFORE the project
    it will attach to exists (fix #5 — the create-time success-lie: project_create
    commits first, so validating only AFTER creation meant a bad/foreign profile
    id failed the CLI/web call while the project was already persisted). Returns
    the coerced profile id (or None for a blank pick) on success; raises
    SegProfileError on a nonexistent or cross-customer profile — callers must
    check this BEFORE calling content.project_create so nothing is persisted on
    failure. `customer_id` is the RAW form/CLI value (matches _coerce_id).
    `org_id`, when given (multi-org scoped CLI caller), also rejects a foreign
    org's profile as not-found — a no-op filter when org_id is None."""
    pid = _coerce_id(profile_id)
    if pid is None:
        return None
    prof = session.get(SegmentationProfile, pid)
    if prof is None:
        raise SegProfileError(f"no such segmentation profile: {pid}")
    if org_id is not None and getattr(prof, "org_id", None) != org_id:
        raise SegProfileError(f"no such segmentation profile: {pid}")
    _assert_assignable(session, prof, _coerce_id(customer_id))
    return pid


def assign_to_project(session: Session, project_id: int, profile_id, org_id=None) -> Project | None:
    """Set (or clear, when profile_id is ''/None) a project's segmentation override
    (the "untick default" pick). Validates the profile is global or this project's
    customer's. Returns the project, or None if missing. Raises SegProfileError on a
    bad/foreign profile.

    `org_id`, when given (multi-org scoped CLI caller), treats a foreign org's
    PROJECT as not-found (None) and a foreign org's PROFILE as not-assignable
    (SegProfileError) — a no-op filter when org_id is None, byte-identical to
    today (the web route relies on _visibility_block for the project instead)."""
    proj = session.get(Project, project_id)
    if not proj:
        return None
    if org_id is not None and getattr(proj, "org_id", None) != org_id:
        return None
    pid = _coerce_id(profile_id)
    if pid is not None:
        prof = session.get(SegmentationProfile, pid)
        if prof is None:
            raise SegProfileError(f"no such segmentation profile: {pid}")
        if org_id is not None and getattr(prof, "org_id", None) != org_id:
            raise SegProfileError(f"no such segmentation profile: {pid}")
        _assert_assignable(session, prof, proj.customer_id)
    proj.segmentation_profile_id = pid
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


def validate_customer_default(session: Session, customer_id: int, profile_id) -> None:
    """Validate a customer's default_segmentation_profile_id BEFORE it is stored
    (called from customers.set_customer_defaults). A blank/None clears it (always
    OK). A profile that EXISTS must be global or this customer's — a profile private
    to a DIFFERENT customer is rejected (SegProfileError), mirroring the P4
    default_workflow_id cross-customer guard. A nonexistent id is harmless (the
    default is display/resolution-only and dereferences to None → next chain rung),
    so it passes — keeping the 'set any id' ergonomics."""
    pid = _coerce_id(profile_id)
    if pid is None:
        return
    prof = session.get(SegmentationProfile, pid)
    if prof is not None:
        _assert_assignable(session, prof, customer_id)


# --- the resolution chain ----------------------------------------------------
def resolve_profile(session: Session, project: Project) -> SegmentationProfile | None:
    """Resolve which SegmentationProfile governs a project's segmentation, per the
    fallback chain (GAP 2, XTM order):
        project.segmentation_profile_id
     -> project.customer.default_segmentation_profile_id
     -> the system-default profile (is_system_default)
     -> None  (= the built-in default.srx / legacy — the last rung is implicit)
    A dangling id (profile deleted) at any rung is skipped to the next rung. Returns
    the resolved profile, or None to mean the built-in default."""
    if project is None:
        return None
    # 1) project override
    if project.segmentation_profile_id:
        prof = session.get(SegmentationProfile, project.segmentation_profile_id)
        if prof is not None:
            return prof
    # 2) customer default
    if project.customer_id:
        cust = session.get(Customer, project.customer_id)
        if cust and cust.default_segmentation_profile_id:
            prof = session.get(SegmentationProfile, cust.default_segmentation_profile_id)
            if prof is not None:
                return prof
    # 3) system default — the PROJECT's own org's, under the multi-org gate (a
    # no-op filter while the gate is off, so single-tenant stays byte-identical:
    # org B's system default must never silently govern org A's ingest).
    from app.services import access

    q = select(SegmentationProfile).where(SegmentationProfile.is_system_default == True)  # noqa: E712
    if access.multi_org_active():
        q = q.where(SegmentationProfile.org_id == getattr(project, "org_id", None))
    prof = session.exec(q).first()
    if prof is not None:
        return prof
    # 4) built-in (None)
    return None


def resolve_ruleset(session: Session, project: Project) -> bytes:
    """The raw SRX ruleset bytes a project segments under (the resolved profile's
    blob, or — when the chain lands on None — the GLOBALLY-ACTIVE ruleset). This is
    what both the segmentation call AND the seg_engine stamp derive from at ingest,
    so they can never diverge.

    The last rung is srx.active_ruleset_bytes(), NOT default_ruleset_bytes():
    pre-P6, srx_enabled=1 segmented with the active ruleset (the legacy single
    custom .srx upload when configured, else the shipped default). An install with
    a legacy custom upload and no profiles assigned must keep segmenting with that
    custom ruleset — byte-identical — until an admin opts into profiles."""
    prof = resolve_profile(session, project)
    if prof is None:
        return srx.active_ruleset_bytes()
    return profile_ruleset_bytes(prof)


def profile_row(prof: SegmentationProfile) -> dict:
    """A safe, echo-able summary of a profile for CLI --json / the web (the raw SRX
    blob is NOT included — only whether it is custom)."""
    return {
        "id": prof.id,
        "name": prof.name,
        "customer_id": prof.customer_id,
        "scope": "customer" if prof.customer_id else "global",
        "custom": bool(prof.srx_blob),
        "builtin_default": not bool(prof.srx_blob),
        "is_system_default": prof.is_system_default,
        "description": prof.description,
    }


# --- legacy custom-.srx migration --------------------------------------------
def migrate_legacy_custom() -> SegmentationProfile | None:
    """Idempotently seed a "Custom (migrated)" SegmentationProfile from the legacy
    single custom .srx (the srx_file Setting pointing at DATA_DIR/segmentation.srx).

    Runs at most once: if ANY custom-blob profile named "Custom (migrated)" already
    exists, no-op. Only fires when a custom .srx is actually configured AND parses;
    a fresh install (no custom file) seeds nothing (the built-in default rung covers
    it, so a NULL-blob "system default" is NOT auto-created — the chain's implicit
    last rung already IS the default, keeping behavior byte-identical). The migrated
    profile is GLOBAL (customer_id NULL) and NOT the system default (an admin opts a
    profile in explicitly); it exists so the pre-P6 custom ruleset becomes a
    manageable, assignable row."""
    from app.services import settings

    existing = get_by_name("Custom (migrated)")
    if existing is not None:
        return existing
    name = (settings.get("srx_file") or "").strip()
    if not name:
        return None
    path = srx.custom_srx_path()
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    ok, _ = srx.validate(raw)
    if not ok:
        return None
    try:
        return create_profile(
            "Custom (migrated)",
            srx_blob=raw.decode("utf-8", errors="replace"),
            customer_id=None,
            description="Migrated from the legacy custom SRX upload.",
            is_system_default=False,
            actor="system",
        )
    except SegProfileError:
        return get_by_name("Custom (migrated)")
