"""Customer 360 — customers as first-class entities (P4, item 5).

Shared by the web routes (/customers, /customers/{id}) and the headless CLI
(`customer show/update/set-default/pairs ...`) — the extract-then-CLI parity rule
(ONE source of truth). The web routes and the CLI both call these functions; no HTTP
concerns leak in.

WHAT A CUSTOMER OWNS NOW (beyond a name):
  - settings: a free-form preferences JSON blob (notes, contacts, per-customer knobs)
  - active_pairs: an explicit list of "src->tgt" pairs the customer works in (a soft
    preference, shown ALONGSIDE the pairs derived automatically from its projects)
  - default_provider / default_workflow_id / default_segmentation_profile_id: scoped
    defaults inherited at project creation (segmentation is a forward ref to P6)
  - its projects (with P1 status), its connectors (Connection.customer_id), its
    workflows (Workflow.customer_id), its TM stats — assembled by get_customer_360.

ISOLATION (item 5): every list query here is scoped to ONE customer_id, so a second
customer's projects/connections/workflows never appear in the first's 360 view. TM is
already per-project (= per-customer transitively); connector secrets are already
per-connection (Fernet). The org (multi-org) boundary is the OUTER layer — the web
routes org-gate the customer row first, and get_customer_360 only ever queries within
the one customer, so it can't cross an org either.

SCOPING RULES (the dispatch/picker seam):
  - customer_connections(cid): the connections a customer's contexts may use = the
    customer's own (customer_id == cid) PLUS global/unassigned (customer_id IS NULL).
    A DIFFERENT customer's connection is never included.
  - Workflows reuse the existing Scope.CUSTOMER mechanism (web/queries._applicable_
    workflows already scopes CUSTOMER-scoped workflows to project.customer_id) — so a
    customer-scoped workflow is only offered on that customer's projects. This module
    exposes customer_workflows(cid) for the 360 page (global + that customer's).

Documented in: docs/architecture/tenancy-confinement.md
"""

from __future__ import annotations

import csv
import io
import logging

from sqlmodel import Session, select

from app.models import (
    Connection,
    Customer,
    Document,
    Project,
    ProjectStatus,
    Scope,
    TargetLocale,
    Workflow,
    _utcnow,
)
from app.services import languages_catalog, tm

_log = logging.getLogger("loc_tms.customers")

# SQLite's binder rejects any integer outside a signed 64-bit range with a raw
# OverflowError — see _coerce_id below (mirrors app.cli._common.bounded_id /
# app.services.content._SQLITE_INT64_MAX for this module's id coercion).
_SQLITE_INT64_MAX = 2**63 - 1


class CustomerArchiveError(ValueError):
    """SPEC-customer-archive: an archive/unarchive was a no-op-with-a-reason —
    archiving an already-archived customer, or unarchiving an active one. Idempotent-
    safe: raised as a CLEAN error (400 / EXIT_USAGE) rather than silently double-
    stamping the timestamp, so the surfaces show a precise message."""


class CustomerDeleteBlocked(ValueError):
    """SPEC-rename-delete: a customer delete was REFUSED because something still
    references it (projects, connections, bound users, content types, glossaries,
    workflows, templates, routing rules, audit rows, ...). Customer delete has NO
    cascade — it fails closed with an itemized live count naming what to reassign or
    remove first. Carries the per-kind counts; surfaced as a clean 400/EXIT_USAGE."""

    def __init__(self, customer_id, refs):
        self.customer_id = customer_id
        self.refs = refs
        parts = ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in refs.items() if k != "total" and v
        )
        super().__init__(
            "cannot delete customer %d: still referenced by %s. Reassign or remove "  # noqa: UP031
            "those first (customer delete never cascades)." % (customer_id, parts)
        )


class PairImportError(ValueError):
    """Bulk `customer pairs import` (CLI-parity backlog item 2) rejected the WHOLE
    file -- either a bad header or one or more rows failed BCP-47 catalog validation.
    Raised BEFORE any write (import_pairs_csv validates every row first), so the
    customer's active_pairs are untouched when this is raised -- the atomic,
    all-or-nothing guarantee. The message lists every bad row."""


# --- lookups -----------------------------------------------------------------
def get(session: Session, cid: int) -> Customer | None:
    return session.get(Customer, cid)


# --- duplicate-name labels (#617) ---------------------------------------------
# Creating or renaming to an existing name is refused, but rows that predate that rule can
# still share a name. Every customer picker shows " (#id)" after such a name so two options
# never look identical. The set of ambiguous ids is cached for a couple of seconds (a page
# renders dozens of options) and dropped whenever a customer is created, renamed or deleted.
_DUP_TTL_S = 2.0
_dup_cache: dict = {"at": 0.0, "ids": frozenset()}


def invalidate_duplicate_labels() -> None:
    _dup_cache["at"] = 0.0


def ambiguous_customer_ids() -> frozenset:
    """Ids of customers whose name (trimmed, casefolded) is shared by another customer of the
    same org (or by another org-less customer). Archived customers count."""
    import time

    now = time.monotonic()
    if _dup_cache["at"] and now - _dup_cache["at"] < _DUP_TTL_S:
        return _dup_cache["ids"]
    from app.db import engine

    groups: dict = {}
    with Session(engine) as s:
        for cid, name, org_id in s.exec(select(Customer.id, Customer.name, Customer.org_id)).all():
            groups.setdefault((org_id, (name or "").strip().casefold()), []).append(cid)
    ids = frozenset(i for members in groups.values() if len(members) > 1 for i in members)
    _dup_cache["at"] = now
    _dup_cache["ids"] = ids
    return ids


def customer_label(customer) -> str:
    """The text a customer picker shows: the name, plus " (#id)" when the name is shared.
    Tolerates None / undefined / odd values (templates call it on whatever they hold)."""
    if isinstance(customer, dict):
        name, cid = customer.get("name"), customer.get("id")
    else:
        name, cid = getattr(customer, "name", None), getattr(customer, "id", None)
    if not isinstance(name, str):
        name = "" if name is None else str(name)
    if isinstance(cid, int) and cid in ambiguous_customer_ids():
        return f"{name} (#{cid})"
    return name


# --- rename + fail-closed delete (SPEC-rename-delete) ------------------------
def rename_customer(
    session: Session, cid: int, name: str, *, actor: str = "system", ip: str = "", org_id=None
) -> Customer | None:
    """Rename a customer (display name only; every reference is by id, so nothing
    disconnects). Org-guarded like the customer settings mutations (a cross-org id
    resolves to None). Audits customer.rename old->new. Returns the Customer, or None
    when it doesn't exist / is cross-org; raises ValueError on a blank name and
    DuplicateNameError (a ValueError) when another customer of the org has that name."""
    cust = session.get(Customer, cid)
    if cust is None:
        return None
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return None
    new_name = (name or "").strip()
    if not new_name:
        raise ValueError("customer name is required.")
    # #617: a rename may not take another customer's name in the same org (case and
    # surrounding space ignored). Renaming to a different casing of its own name is fine.
    from app.services import content_project_rename_delete_sp as _cp

    twin = _cp.find_duplicate_customer(
        session, new_name, getattr(cust, "org_id", None), exclude_id=cust.id
    )
    if twin is not None:
        raise _cp.DuplicateNameError("customer", new_name, twin.id)
    old_name = cust.name
    cust.name = new_name
    session.add(cust)
    session.commit()
    session.refresh(cust)
    invalidate_duplicate_labels()
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="customer.rename",
        target=f"customer {cust.id}",
        detail=f"{old_name!r} -> {new_name!r}",
        ip=ip,
        customer_id=cust.id,
    )
    return cust


def reference_count(session: Session, cid: int) -> dict:
    """How many rows reference this customer, by kind + total (read-only). Counts EVERY
    customer.id FK referrer so delete-fails-closed can never under-count and then hit an
    IntegrityError at delete time (FKs are globally enforced). auditlog rows are counted
    too — a customer with any recorded history is deliberately undeletable (fail closed;
    no cascade), so only a truly-unreferenced customer can be removed."""
    from app.models import (
        AuditLog,
        Connection,
        ContentType,
        ContextProfile,
        CustomFieldDef,
        Glossary,
        Project,
        ProjectTemplate,
        PromptTemplate,
        RoutingRule,
        SegmentationProfile,
        User,
        Workflow,
        WorkflowGroup,
    )

    def _n(model) -> int:
        return len(session.exec(select(model.id).where(model.customer_id == cid)).all())

    refs = {
        "projects": _n(Project),
        "connections": _n(Connection),
        "content_types": _n(ContentType),
        "custom_fields": _n(CustomFieldDef),
        "glossaries": _n(Glossary),
        "workflows": _n(Workflow),
        "workflow_groups": _n(WorkflowGroup),
        "templates": _n(ProjectTemplate),
        "prompt_templates": _n(PromptTemplate),
        "segmentation_profiles": _n(SegmentationProfile),
        "context_profiles": _n(ContextProfile),
        "bound_users": _n(User),
        "routing_rules": _n(RoutingRule),
        "audit_rows": _n(AuditLog),
    }
    refs["total"] = sum(refs.values())
    return refs


def delete_customer(
    session: Session, cid: int, *, actor: str = "system", ip: str = "", org_id=None
) -> dict | None:
    """Delete a customer. FAILS CLOSED (CustomerDeleteBlocked with the itemized live
    count) while ANYTHING references it — never a cascade, never orphaning a dangling
    FK. The CALLER owns the admin gate. Org-guarded (a cross-org id resolves to None).
    Returns the (all-zero) reference dict on success, or None when the customer doesn't
    exist / is cross-org."""
    cust = session.get(Customer, cid)
    if cust is None:
        return None
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return None
    refs = reference_count(session, cid)
    if refs["total"] > 0:
        raise CustomerDeleteBlocked(cid, refs)
    name = cust.name
    cust_org = getattr(cust, "org_id", None)  # captured for the DX-207 staged-artifact purge
    session.delete(cust)
    session.commit()
    invalidate_duplicate_labels()
    from app.services import audit

    audit.log(
        actor=actor or "system", action="customer.delete", target=f"customer {cid} ({name})", ip=ip
    )
    # DX-207 (Batch 13): purge live staged full-dump exports clamped to this customer (and every
    # workspace dump of its org, which would contain the customer's now-removed content). Routed
    # through gdpr so app.services.data_export stays imported only there + scheduler. Best-effort.
    try:
        from app.services import gdpr as _gdpr

        _gdpr.purge_export_artifacts_for_customer(cid, org_id=cust_org, actor=actor or "system")
    except Exception:
        _log.warning("delete_customer: staged-export artifact purge failed (customer=%s)", cid)
    return refs


# --- archive / unarchive (SPEC-customer-archive) -----------------------------
def archive_customer(
    session: Session, cid: int, *, actor: str = "system", ip: str = "", org_id=None
) -> Customer | None:
    """Archive (retire) a customer: stamp archived_at=now so it drops out of every
    picker + the default list view. Existing projects/connections/bound users keep
    working untouched — archive is a selection/listing concern, NOT a kill switch, and
    it has NO cascade. Org-guarded (a cross-org id resolves to None, like rename/
    delete). Idempotent-safe: archiving an already-archived customer raises a clean
    CustomerArchiveError rather than re-stamping. Audits customer.archive. Returns the
    Customer, or None when it doesn't exist / is cross-org."""
    cust = session.get(Customer, cid)
    if cust is None:
        return None
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return None
    if cust.archived_at is not None:
        raise CustomerArchiveError(f"customer {cid} ({cust.name}) is already archived.")
    cust.archived_at = _utcnow()
    session.add(cust)
    session.commit()
    session.refresh(cust)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="customer.archive",
        target=f"customer {cust.id} ({cust.name})",
        ip=ip,
        customer_id=cust.id,
    )
    return cust


def unarchive_customer(
    session: Session, cid: int, *, actor: str = "system", ip: str = "", org_id=None
) -> Customer | None:
    """Unarchive (reactivate) a customer: clear archived_at so it is offered in pickers
    again. Org-guarded. Idempotent-safe: unarchiving an active customer raises a clean
    CustomerArchiveError. Audits customer.unarchive. Returns the Customer, or None when
    it doesn't exist / is cross-org."""
    cust = session.get(Customer, cid)
    if cust is None:
        return None
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return None
    if cust.archived_at is None:
        raise CustomerArchiveError(f"customer {cid} ({cust.name}) is not archived.")
    cust.archived_at = None
    session.add(cust)
    session.commit()
    session.refresh(cust)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="customer.unarchive",
        target=f"customer {cust.id} ({cust.name})",
        ip=ip,
        customer_id=cust.id,
    )
    return cust


def active_only(stmt):
    """Append the ACTIVE filter (`archived_at IS NULL`) to a `select(Customer)`
    statement — the SINGLE seam every "available customer" PICKER routes through so no
    picker can drift on the archive rule. A management/name-resolution read (the
    customers-list page, /api name maps) deliberately does NOT call this — it shows
    archived rows (labelled) so existing projects still resolve their customer name."""
    return stmt.where(Customer.archived_at.is_(None))


def is_active(cust: Customer | None) -> bool:
    """True when the customer exists and is not archived."""
    return cust is not None and getattr(cust, "archived_at", None) is None


def selectable(session: Session, cid, *, org_id=None) -> Customer | None:
    """The customer with this id IFF it may be freshly CHOSEN right now: it exists, is
    same-org (when org_id is given — a scoped caller), and is NOT archived. Returns the
    Customer or None. The service-level guard the project/prompt-template create seams
    use so an archived (or foreign) customer can never be attached even via a forged
    POST / stray CLI id, not just omitted from the UI picker."""
    n = _coerce_id(cid)
    if n is None:
        return None
    cust = session.get(Customer, n)
    if cust is None:
        return None
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        return None
    if cust.archived_at is not None:
        return None
    return cust


# --- settings + defaults -----------------------------------------------------
def update_customer_settings(session: Session, cid: int, updates: dict) -> Customer | None:
    """MERGE `updates` into the customer's settings JSON blob (never a wholesale
    replace, so keys other cards own survive). A key whose value is "" / None is
    DROPPED (so a cleared form field removes the preference). Returns the customer,
    or None if the id doesn't exist."""
    cust = session.get(Customer, cid)
    if not cust:
        return None
    merged = dict(cust.settings or {})
    for k, v in (updates or {}).items():
        k = str(k).strip()
        if not k:
            continue
        if v in ("", None):
            merged.pop(k, None)
        else:
            merged[k] = v
    cust.settings = merged
    session.add(cust)
    session.commit()
    session.refresh(cust)
    return cust


def set_customer_defaults(
    session: Session,
    cid: int,
    *,
    provider=None,
    workflow_id=None,
    segmentation_profile_id=None,
    content_type_id=None,
    user=None,
) -> Customer | None:
    """Set the customer's scoped defaults. Only NON-None args change (so a partial
    update leaves the others alone). A blank string CLEARS a default (sets it to
    NULL); a digit-string / int sets the id defaults. Returns the customer, or None
    if missing.

    ISOLATION (item 2/5): default_workflow_id is VALIDATED against customer_workflows
    (global + THIS customer's own). A workflow scoped to a DIFFERENT customer — which
    the picker never offers, but a forged POST / stray CLI id could carry — is
    rejected (raises ValueError) rather than stored, so a customer's default can never
    dereference another customer's private workflow (the owner's "never any mixing"
    bar). A NONEXISTENT id is likewise rejected: the column is a REAL FK
    (workflow.id), so storing a dangling id raises a FOREIGN KEY IntegrityError (an
    HTTP 500) at commit — the guard validates existence up front, exactly as the
    sibling FK default_content_type_id does via content_types.attachable. A blank
    clears it (always allowed)."""
    cust = session.get(Customer, cid)
    if not cust:
        return None
    if provider is not None:
        # A quality-judge engine (jev / jev_local) can never be a translate default;
        # EngineRoleError is a ValueError, which every caller already surfaces cleanly.
        from app.services import engines

        engines.assert_ref_role(provider, "translate")
        # #577: newly choosing Mock as a customer default is admin-only (an already-Mock
        # default keeps saving).
        from app.services import pair_providers

        pair_providers.refuse_mock_unless_admin(user, [provider], already=[cust.default_provider])
        cust.default_provider = (provider or "").strip() or None
    if workflow_id is not None:
        wid = _coerce_id(workflow_id)
        if wid is not None:
            # default_workflow_id is a REAL FK (workflow.id): a stored id that does
            # not resolve raises a FOREIGN KEY IntegrityError at commit (HTTP 500), so
            # the id is validated BEFORE it is written — the same up-front-existence
            # discipline the sibling FK default_content_type_id uses (attachable).
            # Convention (customer_workflows): a default workflow must be an EXISTING
            # global workflow or one scoped to THIS customer.
            #   - nonexistent id                    -> rejected (would 500 on the FK)
            #   - DIFFERENT customer's private wf    -> rejected (isolation breach)
            #   - global / this-customer's           -> stored
            # (Contrast default_segmentation_profile_id, whose column carries NO FK —
            # a dangling id there is genuinely harmless and passes.) A blank -> wid is
            # None -> the default is cleared.
            wf = session.get(Workflow, wid)
            if wf is None:
                raise ValueError(
                    f"workflow {wid} does not exist — a default workflow must be an "
                    "existing global workflow or one scoped to this customer."
                )
            # The SAME predicate the picker reads (gate round 2 MED-3: this write half was
            # pinned by nothing, and a validator that disagrees with the list it validates
            # against is how the original defect survived). A row can be scope=GLOBAL and
            # still belong to another customer, so ownership comes first and scope narrows.
            if not workflow_offered_to_customer(wf, cid):
                raise ValueError(
                    f"workflow {wid} belongs to another customer — a default workflow "
                    "must be global or scoped to this customer."
                )
            # #488 item 3: under multi-org a GLOBAL workflow is org-owned (leak vector 12),
            # and workflow_offered_to_customer is true for any global row. The workflow must
            # live in the CUSTOMER's org; a NULL-org row is quarantined under the gate (the
            # same_org rule). Clearing a default (blank) is always allowed. Gate off: no-op.
            from app.services import access as _access

            if _access.multi_org_active() and (
                getattr(wf, "org_id", None) is None
                or getattr(wf, "org_id", None) != getattr(cust, "org_id", None)
            ):
                raise ValueError(
                    f"workflow {wid} does not exist — a default workflow must be an "
                    "existing global workflow or one scoped to this customer."
                )
        cust.default_workflow_id = wid
    if segmentation_profile_id is not None:
        # P6: same isolation bar as default_workflow_id above — a profile private
        # to a DIFFERENT customer is rejected before it is stored (SegProfileError
        # is a ValueError, so web/CLI error handling is shared with the workflow
        # guard). Blank clears; nonexistent ids pass (dangling → next chain rung).
        from app.services import segmentation_profiles as _segprof

        _segprof.validate_customer_default(session, cid, segmentation_profile_id)
        cust.default_segmentation_profile_id = _coerce_id(segmentation_profile_id)
    if content_type_id is not None:
        # SPEC-content-types: same isolation bar as default_workflow_id above — a
        # content type must be GLOBAL or scoped to THIS customer (never another
        # customer's), validated via content_types.attachable before it is stored.
        # A blank clears it; a nonexistent / foreign id is rejected (ValueError, so
        # web/CLI error handling is shared with the workflow guard).
        cid_val = _coerce_id(content_type_id)
        if cid_val is not None:
            from app.services import content_types as _ct

            if (
                _ct.attachable(
                    session, cid_val, org_id=getattr(cust, "org_id", None), customer_id=cid
                )
                is None
            ):
                raise ValueError(
                    f"content type {cid_val} is not available for this customer — a "
                    "default content type must be global or scoped to this customer."
                )
        cust.default_content_type_id = cid_val
    session.add(cust)
    session.commit()
    session.refresh(cust)
    return cust


def _coerce_id(v):
    """'' / None -> None (clear); an int or digit-string -> int; anything else -> None.

    Bounded to SQLite's signed-64-bit range: an oversized digit-string (a typo, a
    forged POST) resolves to None (treated as "no id" / harmless pass-through by
    the callers above) instead of overflowing the SQLite binder at the eventual
    session.get()."""
    if v is None:
        return None
    if isinstance(v, int):
        return v if 0 < v <= _SQLITE_INT64_MAX else None
    # H2: str.isdigit() is True for Unicode digits int() rejects (e.g. the
    # superscript '2', U+00B2), which used to slip past the gate into int() and
    # raise ValueError -> 500. try/except closes it.
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    # M1 (re-review): keep the positive lower bound isdigit() implicitly enforced --
    # else "-5" -> -5 reaches session.get()/an FK column as a negative id (a POST of
    # default_workflow_id="-5" 500'd on a FOREIGN KEY IntegrityError instead of the
    # clean 303-and-clear). Mirrors the sibling security._coerce_customer_id.
    return n if 0 < n <= _SQLITE_INT64_MAX else None


# --- active language pairs ---------------------------------------------------
def _norm_pair(pair: str) -> str | None:
    """Canonicalize a "src->tgt" (or "src>tgt" / "src,tgt" / "src tgt") pair against
    the BCP-47 catalog. Returns "src->tgt" with both codes canonical, or None when
    either side is missing / not a valid locale."""
    if not pair:
        return None
    raw = str(pair).replace("->", " ").replace(">", " ").replace(",", " ").split()
    if len(raw) != 2:
        return None
    src_valid, src_invalid = languages_catalog.validate_codes([raw[0]])
    tgt_valid, tgt_invalid = languages_catalog.validate_codes([raw[1]])
    if src_invalid or tgt_invalid or not src_valid or not tgt_valid:
        return None
    return f"{src_valid[0]}->{tgt_valid[0]}"


def list_pairs(session: Session, cid: int) -> list[str]:
    """The customer's EXPLICIT active_pairs (the saved preference list). Distinct
    from derived_pairs (computed from the projects)."""
    cust = session.get(Customer, cid)
    return list(cust.active_pairs or []) if cust else []


def add_pair(session: Session, cid: int, pair: str) -> tuple[bool, str]:
    """Add one "src->tgt" pair to the customer's explicit active_pairs. Returns
    (added, canonical_pair). added is False when the pair was already present;
    raises ValueError when the pair is malformed / not in the catalog."""
    canon = _norm_pair(pair)
    if canon is None:
        raise ValueError(
            f"invalid language pair {pair!r} (want 'src->tgt', both in "
            "the language catalog, e.g. en-US->fr-FR)."
        )
    cust = session.get(Customer, cid)
    if not cust:
        raise ValueError(f"no such customer: {cid}")
    pairs = list(cust.active_pairs or [])
    if canon in pairs:
        return False, canon
    pairs.append(canon)
    cust.active_pairs = pairs
    session.add(cust)
    session.commit()
    return True, canon


def remove_pair(session: Session, cid: int, pair: str) -> tuple[bool, str]:
    """Remove one pair from the customer's explicit active_pairs. Accepts either a
    canonical or a loosely-formatted pair; returns (removed, canonical_pair)."""
    canon = _norm_pair(pair) or str(pair).strip()
    cust = session.get(Customer, cid)
    if not cust:
        raise ValueError(f"no such customer: {cid}")
    pairs = list(cust.active_pairs or [])
    if canon not in pairs:
        return False, canon
    pairs = [p for p in pairs if p != canon]
    cust.active_pairs = pairs
    session.add(cust)
    session.commit()
    return True, canon


# --- active-pairs -> allowed target locales (THE SHARED SEAM) ----------------
# The maintainer's P2: "the point of the active-language-pairs section is to reduce the
# Target-languages selection window to only the ones you want." This is the ONE
# function every Target-languages surface (project add-locale, the new-project
# dialog, the CLI) routes through so the confinement can never fork per surface.
def active_pair_targets(customer, *, source_locale=None) -> list[str]:
    """The canonical TARGET locales a customer's EXPLICIT active_pairs confine to.
    Given a Customer (or None) and optionally a source locale, return the target
    side of each active pair — de-duped on the normalized key, order-stable.

    When `source_locale` is given, only pairs whose SOURCE matches (normalized)
    contribute their target (dispatch is source-sensitive; a project confines to the
    targets reachable FROM its own source). When it is omitted, EVERY pair's target
    contributes (the new-project dialog, where the source is still being typed).

    Returns [] when the customer has NO usable pairs (no customer, empty
    active_pairs, or a source filter that matched none). [] is the deliberate
    FALLBACK SIGNAL — a caller reads it as "no confinement; offer the full enabled
    set" so a fresh install with no pairs is never bricked."""
    if customer is None:
        return []
    src_norm = languages_catalog.normalize(source_locale) if source_locale else None
    seen, out = set(), []
    for pair in customer.active_pairs or []:
        parts = str(pair).split("->")
        if len(parts) != 2:
            continue
        psrc, ptgt = parts[0].strip(), parts[1].strip()
        if src_norm and languages_catalog.normalize(psrc) != src_norm:
            continue
        canon = languages_catalog.canonical(ptgt) or ptgt
        key = languages_catalog.normalize(canon)
        if key and key not in seen:
            seen.add(key)
            out.append(canon)
    return out


def confined_target_rows(session: Session, customer, *, source_locale=None):
    """Rows for the langpicker's `enabled` slot, CONFINED to a customer's active
    pairs. Returns (rows, confined):

      * rows     — [{code, name, native}] to pass as the langpicker `enabled`
                   argument (the always-visible subset); the full catalog stays
                   behind the picker's existing "Show all languages" toggle, so the
                   confinement is SOFT (reduce the window, never hard-block).
      * confined — True when active pairs actually narrowed the offering (so a
                   surface can show the "showing this customer's active pairs" note);
                   False when it fell back to the full enabled subset.

    FALLBACK (mandatory — never brick a fresh install): no customer / no pairs / a
    source that matched none -> the full enabled subset, confined=False. `customer`
    may be a Customer or a customer id (ints are resolved through session.get)."""
    cust = customer
    if cust is not None and not isinstance(cust, Customer):
        cust = session.get(Customer, _coerce_id(cust))
    codes = active_pair_targets(cust, source_locale=source_locale)
    if not codes:
        return languages_catalog.enabled_language_rows(), False
    return languages_catalog.rows_for(codes), True


def targets_outside_pairs(customer, targets, *, source_locale=None) -> list[str]:
    """The `targets` NOT covered by the customer's active pairs (normalized compare),
    order-preserving. Used by the CLI/API to WARN (never refuse — matching the web's
    soft-confinement escape hatch) when an operator adds a target off the customer's
    active pairs. Returns [] when the customer has no pairs (fallback: nothing is
    'outside' because there is no confinement)."""
    allowed = active_pair_targets(customer, source_locale=source_locale)
    if not allowed:
        return []
    allowed_norm = {languages_catalog.normalize(a) for a in allowed}
    return [
        t
        for t in (targets or [])
        if str(t).strip() and languages_catalog.normalize(t) not in allowed_norm
    ]


# --- bulk import (CLI-parity backlog item 2) ----------------------------------
# CSV, header row `source_locale,target_locale` -- one pair per row. Chosen to
# match the two-locale-column shape languages_catalog.validate_codes already
# validates per-column (mirrors glossary.import_csv's CSV-header contract), rather
# than a single "src->tgt" cell that would need its own quoting/escaping story.
_PAIR_CSV_COLS = ("source_locale", "target_locale")
# Cap how many bad rows the error message lists, so a huge malformed file doesn't
# dump thousands of lines into a CLI/HTTP error.
_MAX_LISTED_ERRORS = 25
# #592: data-row cap for import_pairs_csv (only a few thousand valid pairs exist).
MAX_PAIR_IMPORT_ROWS = 10000


def import_pairs_csv(session: Session, cid: int, data: bytes) -> dict:
    """Bulk-import a customer's explicit active language pairs from CSV.

    ATOMIC (all-or-nothing): every row is validated against the BCP-47 catalog
    BEFORE any write. A single bad row (unknown locale, missing column, malformed
    header) rejects the WHOLE file -- raises PairImportError listing every bad row
    (capped at _MAX_LISTED_ERRORS) -- and the customer's active_pairs are left
    completely untouched (no partial import).

    IDEMPOTENT: a pair already present in the customer's active_pairs is SKIPPED
    (not duplicated) and counted in the returned `skipped` list; re-importing the
    same file a second time is a no-op (added == [] the second time). A pair
    repeated within the file itself is deduped too (first occurrence wins).

    Returns {"added": [canonical pairs added], "skipped": [canonical pairs already
    present], "total_rows": <valid data rows read>}.

    Reviewer fix (adversarial review of feat/cli-parity-a, finding 1): csv.Error --
    raised by the stdlib csv module for a structurally malformed file (an unbalanced
    quote, a field past csv.field_size_limit(), embedded NUL bytes from a binary
    upload) -- is NOT a ValueError, so a bare try/except (PairImportError, ValueError)
    at each of the three callers (web/CLI/api) let it through as an unhandled
    exception: a 500 on web POST, a 500 on /api, a raw traceback on the CLI. The
    ENTIRE parse (header read via reader.fieldnames + the row-iteration loop, since
    csv.Error can surface from either) is wrapped here at the single service seam,
    so every caller inherits the fix from ONE place -- a clean PairImportError, never
    a 500/traceback, no matter how the file is malformed."""
    cust = session.get(Customer, cid)
    if not cust:
        raise ValueError(f"no such customer: {cid}")

    text = data.decode("utf-8-sig", errors="replace")
    errors: list[str] = []
    canon_rows: list[str] = []
    n_data_rows = 0
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = {(c or "").strip() for c in (reader.fieldnames or [])}
        if not set(_PAIR_CSV_COLS) <= fieldnames:
            raise PairImportError(
                "CSV must have a header row with source_locale,target_locale "
                f"columns (got: {', '.join(reader.fieldnames or []) or '(no header)'})."
            )

        for i, row in enumerate(reader, start=2):  # row 1 is the header
            src_raw = (row.get("source_locale") or "").strip()
            tgt_raw = (row.get("target_locale") or "").strip()
            if not src_raw and not tgt_raw:
                continue  # a wholly blank row is skipped, not an error
            n_data_rows += 1
            if n_data_rows > MAX_PAIR_IMPORT_ROWS:
                raise PairImportError(
                    f"too many rows: a pair import file may have at most "
                    f"{MAX_PAIR_IMPORT_ROWS} data rows; no changes were made."
                )
            src_valid, src_invalid = languages_catalog.validate_codes([src_raw])
            tgt_valid, tgt_invalid = languages_catalog.validate_codes([tgt_raw])
            if src_invalid or tgt_invalid or not src_valid or not tgt_valid:
                errors.append(
                    f"row {i}: invalid pair {src_raw!r} -> {tgt_raw!r} "
                    "(both locales must be in the language catalog)."
                )
                continue
            canon_rows.append(f"{src_valid[0]}->{tgt_valid[0]}")
    except csv.Error as exc:
        raise PairImportError(
            f"could not parse the CSV file: {exc} (no changes were made)."
        ) from exc

    if errors:
        shown = errors[:_MAX_LISTED_ERRORS]
        more = len(errors) - len(shown)
        listing = "; ".join(shown) + (f"; ...and {more} more" if more else "")
        raise PairImportError(
            f"bulk pair import rejected -- {len(errors)} bad row(s), no changes "
            f"were made: {listing}"
        )

    existing = set(cust.active_pairs or [])
    pairs = list(cust.active_pairs or [])
    added: list[str] = []
    skipped: list[str] = []
    seen_in_file: set[str] = set()
    for canon in canon_rows:
        if canon in seen_in_file:
            continue
        seen_in_file.add(canon)
        if canon in existing:
            skipped.append(canon)
        else:
            pairs.append(canon)
            existing.add(canon)
            added.append(canon)

    if added:
        cust.active_pairs = pairs
        session.add(cust)
        session.commit()

    return {"added": added, "skipped": skipped, "total_rows": n_data_rows}


# --- scoping (the isolation seam) --------------------------------------------
def customer_connections(session: Session, cid: int, *, include_global: bool = True):
    """The connections a customer's contexts may use: the customer's OWN
    (customer_id == cid) plus, when include_global, the global/unassigned ones
    (customer_id IS NULL). A DIFFERENT customer's connection is NEVER returned —
    the core data-isolation guarantee (item 5). Newest first.

    ORG BOUNDARY (item 6): under multi-org, connections are ALSO confined to the
    customer's OWN org. Without this, a connection forged (via a hand-crafted POST —
    the picker never offers a cross-org customer) into another org but stamped with
    customer_id == cid would surface in THIS customer's 360 view, leaking a foreign
    org's connection name/kind/status past the sacred org boundary. We scope to the
    customer row's org_id; a strict no-op with the gate off (org_id is NULL for every
    row, so the filter is `IS NULL` and matches everything, byte-identical to today)."""
    from app.services import access

    cust = session.get(Customer, cid)
    cust_org = getattr(cust, "org_id", None) if cust else None
    q = select(Connection).order_by(Connection.created_at.desc())
    if include_global:
        q = q.where((Connection.customer_id == cid) | (Connection.customer_id.is_(None)))
    else:
        q = q.where(Connection.customer_id == cid)
    if access.multi_org_active():
        # Only same-org connections (global connections share the org backfill too).
        q = q.where(Connection.org_id == cust_org)
    # ffadmin Wave 5: EXCLUDE connections of a DISABLED kind so the Customer-360 view
    # never renders a hidden kind's row (kind/name/status/Open link) -- a disabled kind is
    # non-enumerable everywhere it surfaces. Filtered in Python (kind_enabled reads the
    # settings cache). Purely a display filter: the rows are untouched and reappear on
    # re-enable, and any customer-scoped operate path still funnels through the store gates.
    from app.services import connectors as _connreg

    return [
        c
        for c in session.exec(q).all()
        if _connreg.kind_enabled(c.kind) and not (c.config or {}).get("lane_of")
    ]


def workflow_offered_to_customer(w, cid: int) -> bool:
    """ONE predicate for "is this workflow usable in customer `cid`'s contexts", shared
    by the read (customer_workflows) and the write (set_customer_defaults) so the picker
    and the validator cannot disagree about the same row again.

    Ownership FIRST, then scope narrows -- the layering app/web/queries.py:152 uses, and
    the correction to gate round 2 MED-2, which SUBSTITUTED the ownership test for the
    scope test and so widened the picker:

      * `customer_id` is the owner. None means genuinely un-owned; anything else means
        that customer's, whatever `scope` claims (eval_promote used to mint GLOBAL rows
        WITH a customer_id, which is the whole reason this predicate exists).
      * `scope` then narrows. An un-owned row is shared with every customer only when it
        is really GLOBAL; a CUSTOMER- or PROJECT-scoped row with no owner is malformed,
        not shared. An owned row that is PROJECT-scoped belongs to one project, not to
        the customer-wide picker.

    Against the READ (customer_workflows) every combination agrees with the pre-train
    predicate except the one this train is here to change: scope=GLOBAL owned by ANOTHER
    customer, which used to be offered.

    GATE R3 LOW-1: against the WRITE (set_customer_defaults) it is deliberately STRICTER
    than what was there before, in four cells rather than one. The old validator only
    refused `scope == CUSTOMER and customer_id != cid`, so it accepted a PROJECT-scoped
    workflow belonging to anyone, including another customer, as a customer default. That
    is not a behaviour worth preserving: a default must be a row the picker would offer,
    and now it is exactly that. Recorded here because an earlier version of this docstring
    claimed agreement with the pre-train predicate full stop, which was true of the read
    and false of the write.

    GATE R4 LOW-3: this is the SAME layering as app/web/queries.py:152, not the same
    predicate. Eight of nine cells agree; the ninth is (scope=CUSTOMER, owner=None), which
    the run picker in queries.py offers and this one refuses. Both sides are talking about
    an UNOWNED row, so nothing crosses a tenant either way, and the two pickers answer
    different questions: queries.py asks "may this run here", this asks "may this be a
    customer's standing default". A malformed CUSTOMER-scoped row with no owner is a
    reasonable thing to run once and a poor thing to enshrine. Naming the cell rather than
    claiming identity, because an overclaimed docstring is what LOW-1 was about."""
    if w.customer_id is None:
        return w.scope == Scope.GLOBAL
    if w.customer_id != cid:
        return False
    return w.scope in (Scope.GLOBAL, Scope.CUSTOMER)


def customer_workflows(session: Session, cid: int, *, include_archived: bool = False, org_id=None):
    """Workflows usable in a customer's contexts: GLOBAL-scope ones + the customer's
    own CUSTOMER-scoped ones (customer_id == cid). Reuses the existing Scope
    mechanism — a DIFFERENT customer's customer-scoped workflow is excluded.

    ARCHIVED workflows are EXCLUDED by default (residual sweep: archive verbs) — this is
    the run/dispatch PICKER seam, and a retired workflow must not be offered for new work
    (the launch guard is defense in depth). `include_archived=True` is the escape hatch
    for a management view that wants to see retired rows too.

    #73: this is a converged org-read helper, so it speaks the same vocabulary as
    vendors.list_vendors. A bare None org_id under an ACTIVE multi-org gate is
    FAIL-CLOSED (empty list), never see-all; access.UNSCOPED (the key-verified CLI
    --operator door) still sweeps every org. With the gate OFF nothing is blocked and
    the org narrow never applies, so single-tenant behaviour is byte-identical."""
    from app.services import access
    from app.services import workflows as wf_service

    # GATE R1 HIGH-2, org half: this was a BARE unfiltered select, so under multi-org it
    # swept every org's workflows before the customer predicate ever ran. A scoped caller
    # passes its own org and gets confined.
    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    stmt = select(Workflow).order_by(Workflow.name)
    if org_id is not None:
        stmt = stmt.where(Workflow.org_id == org_id)
    rows = session.exec(stmt).all()
    out = []
    for w in rows:
        # Route the archived exclusion through the shipped predicate seam (M4).
        if not include_archived and not wf_service.workflow_is_active(w):
            continue
        # GATE R1 HIGH-2: establish OWNERSHIP on customer_id, never on scope alone.
        #
        # `scope` and `customer_id` are INDEPENDENT columns, and the old predicate
        # (`scope == GLOBAL or (scope == CUSTOMER and customer_id == cid)`) admitted any
        # row marked GLOBAL regardless of who owns it. That is not hypothetical:
        # eval_promote mints its promoted workflow with scope=GLOBAL *and* customer_id
        # set, so every promotion created a row that one customer owned and every other
        # customer could see and select. A customer-bound linguist needed no forgery at
        # all, and `_wf_or_404` disagreed with this list, returning 404 for the very row
        # it had just offered.
        if workflow_offered_to_customer(w, cid):
            out.append(w)
    return out


# --- derived language pairs (from the customer's projects) --------------------
def _derived_pairs(session: Session, projects: list[Project]) -> list[str]:
    """The "src->tgt" pairs IN USE across the customer's projects, from each
    project's source_locale × its TargetLocale rows. Canonical + de-duped, sorted."""
    seen: set[str] = set()
    for p in projects:
        src = p.source_locale or ""
        tgts = session.exec(
            select(TargetLocale.locale).where(TargetLocale.project_id == p.id)
        ).all()
        for t in tgts:
            if src and t:
                seen.add(f"{src}->{t}")
    return sorted(seen)


# --- the 360 assembly --------------------------------------------------------
def get_customer_360(session: Session, cid: int, *, org_id=None, visible_pids=None) -> dict | None:
    """Assemble everything the customer-360 detail page (and `customer show --json`)
    needs, scoped ENTIRELY to this one customer. Returns None if the customer is
    missing.

    Authenticated callers pass access.visible_project_ids as visible_pids. Filter
    before aggregating so document/TM counts and derived pairs cannot disclose hidden
    projects. None retains the trusted local operator view; an empty list shows none.

    The result is intentionally a plain dict of serializable-ish rows so the CLI can
    emit it as --json and the template can render it without re-querying. Every list
    is scoped to cid — no cross-customer aggregate is ever computed here.

    `org_id` is the caller's #73 org scope, forwarded to customer_workflows (the one
    list here whose rows are not already confined by cid, since a GLOBAL workflow has
    no customer). Callers ring the customer row themselves before calling; this only
    decides which org's global workflows are listed."""
    cust = session.get(Customer, cid)
    if not cust:
        return None

    projects = session.exec(
        select(Project).where(Project.customer_id == cid).order_by(Project.created_at.desc())
    ).all()
    visible = visible_pids
    if visible is not None:
        projects = [p for p in projects if p.id in visible]

    # Per-project rollup: status + doc count + TM counts (existing per-project
    # queries; all naturally customer-isolated because they key on project_id).
    project_rows = []
    tm_seg_total = tm_ft_total = 0
    for p in projects:
        n_docs = len(session.exec(select(Document.id).where(Document.project_id == p.id)).all())
        seg_tm = tm.count(session, p.id)
        ft_tm = tm.ft_count(session, p.id)
        tm_seg_total += seg_tm
        tm_ft_total += ft_tm
        project_rows.append(
            {
                "id": p.id,
                "name": p.name,
                "status": p.status,
                "status_label": ProjectStatus.LABELS.get(p.status, p.status),
                "source_locale": p.source_locale,
                "documents": n_docs,
                "tm_segments": seg_tm,
                "tm_fulltext": ft_tm,
            }
        )

    connections = customer_connections(session, cid)
    conn_rows = [
        {
            "id": c.id,
            "kind": c.kind,
            "name": c.name,
            "status": c.status,
            "scope": ("customer" if c.customer_id == cid else "global"),
            "project_id": c.project_id,
        }
        for c in connections
        if visible is None or c.project_id in visible
    ]

    # Forward the caller's org scope so the workflow rows on the 360 obey the same
    # #73 rule as the picker that sits beside them. An un-wired caller passing the
    # default None fails CLOSED under an active gate rather than listing every org.
    workflows = customer_workflows(session, cid, org_id=org_id)
    wf_rows = [
        {
            "id": w.id,
            "name": w.name,
            "scope": w.scope,
            # customer_id, not scope: a promoted workflow is minted GLOBAL with a
            # customer_id set, so a scope-keyed label mislabels it as shared.
            "customer": w.customer_id == cid,
        }
        for w in workflows
        if visible is None or w.project_id is None or w.project_id in visible
    ]

    derived = _derived_pairs(session, list(projects))
    active = list(cust.active_pairs or [])

    return {
        "id": cust.id,
        "name": cust.name,
        "settings": dict(cust.settings or {}),
        "default_provider": cust.default_provider or "",
        "default_workflow_id": cust.default_workflow_id,
        "default_segmentation_profile_id": cust.default_segmentation_profile_id,
        "default_content_type_id": cust.default_content_type_id,
        "active_pairs": active,
        "derived_pairs": derived,
        # Every pair actually in use OR explicitly declared — the union, labelled.
        "all_pairs": _label_pairs(sorted(set(active) | set(derived))),
        "projects": project_rows,
        "connections": conn_rows,
        "workflows": wf_rows,
        "tm_stats": {"segments": tm_seg_total, "fulltext": tm_ft_total},
    }


def _label_pairs(pairs: list[str]) -> list[dict]:
    """Attach human names to each "src->tgt" pair for display."""
    out = []
    for p in pairs:
        parts = p.split("->")
        if len(parts) == 2:
            out.append(
                {
                    "pair": p,
                    "src": parts[0],
                    "tgt": parts[1],
                    "label": f"{languages_catalog.name_for(parts[0])} → "
                    f"{languages_catalog.name_for(parts[1])}",
                }
            )
        else:
            out.append({"pair": p, "src": p, "tgt": "", "label": p})
    return out
