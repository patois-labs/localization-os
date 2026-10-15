"""Vendors — first-class external translation/review vendors (PROPOSAL-external-
processing-step §9 decision 6; PROPOSAL-vendor-human-review-handoff; combined build
Wave A).

Shared by the web routes (/vendors), the headless CLI (`vendor add/list/show/update/
link-customer/rate-card`) and the /api routes — the extract-then-parity rule (ONE source
of truth, no HTTP concerns leak in), the same shape as customers.py.

WHAT A VENDOR OWNS (decision 6, near-verbatim): a name, the connection (endpoint
URL + a Fernet-encrypted signing secret / auth token, stored EXACTLY like connector
credentials — never echoed), the MSA rate card (app/services/vendor_cost.py), and its
customer links (a vendor is connectable to any number of customers; a workflow's external
step may only select a vendor LINKED to the document's customer — the fail-closed
scoping seam `vendor_for_customer`).

ISOLATION: every list/lookup is org-scoped (the OUTER multi-org boundary); the customer
link is the per-customer scoping layer under it. `archived_at` retires a vendor from every
picker (the Customer.archived_at precedent) without breaking dispatch history.

Documented in: docs/architecture/vendor-outsourcing.md
"""

from __future__ import annotations

import json

from sqlalchemy import func
from sqlmodel import Session, select

from app import config
from app.models import (
    Customer,
    Vendor,
    VendorCustomer,
    VendorDispatch,
    VendorStatus,
    WorkflowStep,
    _utcnow,
)
from app.services import vendor_cost

_SQLITE_INT64_MAX = 2**63 - 1


def _validate_endpoint(url: str) -> str:
    """M2: enforce the https scheme allowlist at SAVE time (§5). A blank endpoint is
    allowed (filled in later); a non-blank one must be https (never cleartext http, since
    dispatch carries a signed body + bearer token). Raises VendorError. Returns the
    stripped url.

    HIGH-2 (defence in depth, mirrors s3_blobstore._reject_userinfo_endpoint): also REJECT
    an endpoint that embeds credentials in a `user:pass@host` userinfo component. Such a
    value is almost always an operator mistake, and it would otherwise be stored verbatim
    and surface in a connection-error traceback AND in a GDPR subject-access export. Reject
    on the raw '@' FIRST (a schemeless `key:sekrit@host` parses as scheme='key' with no
    username, dodging the urlsplit check) — an '@' is never legitimate in a vendor intake
    URL. Credentials belong in the Fernet secret blob, never the endpoint.

    R2 LOW-D (documented, DELIBERATE — not a false positive): the raw-'@' reject also fires
    on an '@' anywhere in the PATH (e.g. `https://host/hooks/u@ser`). That is intended: an
    '@' in a vendor intake path is far more likely an accidental pasted credential than a
    legitimate path, and rejecting the whole class keeps the rule one-line and un-bypassable.
    R1 verified this bricks no existing vendor (no seeded/pinned endpoint carries a path '@')."""
    u = (url or "").strip()
    if not u:
        return ""
    if "@" in u:
        raise VendorError(
            "vendor endpoint must not contain '@' (an embedded-credential "
            "'user:pass@host' userinfo component); put the shared secret / auth token in "
            "the vendor's secret, and set the endpoint to a plain https://host[:port]/path "
            "URL. (A userinfo endpoint leaks verbatim into error tracebacks and exports.)"
        )
    from urllib.parse import urlsplit

    parts = urlsplit(u)
    scheme = (parts.scheme or "").lower()
    if scheme != "https":
        raise VendorError(
            f"vendor endpoint must use https (got {scheme or '(none)'!r}); a dispatch "
            "carries a signed payload and must never leave over cleartext http."
        )
    if parts.username or parts.password:
        raise VendorError(
            "vendor endpoint must not embed credentials in a 'user:pass@host' userinfo "
            "component; put the secret in the vendor's secret and use a plain "
            "https://host[:port]/path URL."
        )
    from app.services.credential_origin import origin_unreadable

    if origin_unreadable(u):
        # Gate round 2 MEDIUM-1: the credential pin must be able to read the origin
        # (httpx accepts `https://h:+443`; urlsplit does not).
        raise VendorError(
            "vendor endpoint must look like https://host[:port]/path with a plain numeric "
            "port; this one could not be read."
        )
    return u


def _validate_config_urls(config_json) -> None:
    """Gate round 2 MEDIUM-1: a `poll_url` in the vendor config receives the vendor's
    bearer token, so its origin must be readable by the credential pin (checked again,
    fail closed, at poll time)."""
    from app.services.credential_origin import origin_unreadable

    poll = (config_json or {}).get("poll_url") if isinstance(config_json, dict) else None
    if poll and origin_unreadable(str(poll)):
        raise VendorError(
            "vendor poll_url must look like https://host[:port]/path with a plain numeric "
            "port; this one could not be read."
        )


def _validated_config(config_json):
    _validate_config_urls(config_json)
    return config_json or None


def _validate_protocol(protocol) -> str:
    """Validate a vendor TRANSPORT PROTOCOL at WRITE time (interop R1) — never at dispatch
    time. Blank/None normalizes to `native` (today's behavior). An UNKNOWN protocol is
    REFUSED here (VendorError) so a bad value can never reach the dispatch-by-protocol seam.
    Returns the normalized (lowercased) protocol name. Kept a local import to avoid the
    vendors <-> vendor_dispatch import cycle."""
    from app.services import vendor_dispatch

    p = str(protocol).strip().lower() if protocol else vendor_dispatch.NATIVE_PROTOCOL
    if not vendor_dispatch.is_known_protocol(p):
        raise VendorError(
            f"unknown vendor protocol {p!r}; valid protocols: "
            f"{', '.join(vendor_dispatch.protocol_names())}."
        )
    return p


class VendorError(ValueError):
    """A vendor operation failed with a clean, surface-shared reason (blank name,
    unknown id, malformed rate). Raised as 400 / EXIT_USAGE by the callers."""


class VendorDeleteBlocked(VendorError):
    """A vendor delete was REFUSED because a workflow step still selects it, or a
    dispatch leg still references it — fail closed with an itemized count, never a
    cascade that would orphan a step's `vendor_id` or lose dispatch history (the
    customer-delete precedent). Customer LINKS are not counted here: they exist only to
    scope the vendor and are removed as part of the delete."""

    def __init__(self, vendor_id, refs):
        self.vendor_id = vendor_id
        self.refs = refs
        parts = ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in refs.items() if k != "total" and v
        )
        super().__init__(
            "cannot delete vendor %d: still referenced by %s. Reassign or remove those "  # noqa: UP031
            "first (vendor delete never cascades)." % (vendor_id, parts)
        )


def _refuse_when_vendors_off() -> None:
    """ffadmin Wave 3 SERVICE SEAM: refuse a vendor link/unlink MUTATION when the Vendors
    feature is switched off, naming the switch. This is the DEEPEST backstop -- the
    web/api/CLI routes carry their own first-line flag gate, but this catches EVERY caller
    of the guarded mutation FOREVER (a new ungated caller cannot silently write a
    VendorCustomer row while off -- the HIGH-1 lesson: /customers/{cid}/link-vendor
    mutated with vendors off, reached from projects.py, not the vendor module). Raises
    VendorError, the module's existing surface-shared error type."""
    from app import features

    if not features.is_enabled("vendors"):
        raise VendorError("vendors are disabled (setting vendors_enabled=0).")


# --- secret at rest (Fernet, exactly like connector credentials) -------------
def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(config.ENCRYPTION_KEY.encode())


def _enc_secret(d: dict) -> str:
    """Encrypt the credentials JSON blob to a Fernet ciphertext (never plaintext)."""
    return _fernet().encrypt(json.dumps(d or {}).encode()).decode()


def _dec_secret(token: str) -> dict:
    """Decrypt a vendor's credentials blob to a dict ({} when unset/undecryptable — a
    rotated/lost key is treated as "no credentials", never a crash)."""
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except Exception:
        return {}


def secret_dict(vendor: Vendor) -> dict:
    """The decrypted credentials dict for a vendor (server-side only — NEVER hand this
    to a template/JSON surface). Used at dispatch time to sign the outbound request."""
    return _dec_secret(vendor.secret_enc if vendor else "")


def has_secret(vendor: Vendor) -> bool:
    """True when the vendor has any stored credential — the write-only presence flag a
    surface may show ("signing secret: set"), never the value itself."""
    return bool(secret_dict(vendor))


def _coerce_id(v):
    """'' / None -> None; an int or digit-string in the signed-64-bit range -> int;
    anything else / out of range / non-positive -> None (mirrors customers._coerce_id)."""
    if v is None:
        return None
    if isinstance(v, int):
        return v if 0 < v <= _SQLITE_INT64_MAX else None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= _SQLITE_INT64_MAX else None


# --- lookups + listing -------------------------------------------------------
def get(session: Session, vid) -> Vendor | None:
    n = _coerce_id(vid)
    return session.get(Vendor, n) if n is not None else None


def visible(session: Session, vid, *, org_id=None) -> Vendor | None:
    """The vendor with this id IFF it is same-org (when org_id is given — a scoped
    caller). Returns the Vendor or None. Includes archived rows (name resolution /
    detail page) — the picker uses `selectable` for the not-archived gate.

    #73 (fail-closed BY CONSTRUCTION): a bare None org_id under an ACTIVE multi-org
    gate resolves NOTHING (returns None), not any org's vendor by id -- the same
    reading same_org()/org_filter() take. access.UNSCOPED is the DELIBERATE cross-org
    scope: the org-agnostic internal callers (cross-org vendor linking + dispatch,
    where the customer LINK is the authority, and the CLI --operator door) pass
    access.UNSCOPED, not a bare None, so they keep the sweep. Gate off is
    byte-identical (None = not-a-scoped-caller = any org)."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    v = get(session, vid)
    if v is None:
        return None
    if org_id is not None and getattr(v, "org_id", None) != org_id:
        return None
    return v


def selectable(session: Session, vid, *, org_id=None) -> Vendor | None:
    """The vendor with this id IFF it may be freshly CHOSEN right now: exists, same-org,
    and NOT archived. The service-level guard the step-config seam uses so an archived
    (or foreign) vendor can never be attached even via a forged POST / stray CLI id."""
    v = visible(session, vid, org_id=org_id)
    if v is None or v.archived_at is not None:
        return None
    return v


def _active(stmt):
    return stmt.where(Vendor.archived_at.is_(None))


def list_vendors(
    session: Session, *, org_id=None, include_archived: bool = False, customer_id=None
) -> list[Vendor]:
    """List vendors, newest first. Org-scoped when org_id is given. `customer_id`, when
    given, restricts to vendors LINKED to that customer (the picker offered inside a
    customer's contexts). `include_archived` shows retired vendors (management view).

    #73: a bare None org_id under an ACTIVE multi-org gate is FAIL-CLOSED (empty list),
    not see-all -- access.org_scope_blocked is the single authority and it matches
    same_org()/org_filter(). access.UNSCOPED (the key-verified CLI --operator door)
    still sweeps every org. Gate off: unchanged, byte-identical."""
    from app.services import access

    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    q = select(Vendor).order_by(Vendor.created_at.desc(), Vendor.id.desc())
    if org_id is not None:
        q = q.where(Vendor.org_id == org_id)
    if not include_archived:
        q = _active(q)
    cust = _coerce_id(customer_id)
    if cust is not None:
        linked = set(
            session.exec(
                select(VendorCustomer.vendor_id).where(VendorCustomer.customer_id == cust)
            ).all()
        )
        rows = [v for v in session.exec(q).all() if v.id in linked]
        return rows
    return list(session.exec(q).all())


def list_vendors_for_picker(session: Session, *, org_id=None, customer_id=None) -> list[Vendor]:
    """The ACTIVE vendors offerable on a workflow's external-step picker. The UNION of:
      * this org's active vendors (so an admin can pick one of their own vendors, and,
        for a global workflow, pick a vendor the per-run link will be checked against), and
      * any active vendor LINKED to the workflow's customer — regardless of org (cross-org
        linking, 2026-08-08): a customer's connected vendor may live in another org, and it
        must still appear for that customer's workflows.
    De-duped, own-org rows first (newest-first within each group). With the multi-org gate
    OFF (org_id=None) the first list is already every active vendor, so the union is a
    byte-identical no-op — this only widens the picker when a scoped caller has cross-org
    links to surface. The picker is a convenience; `vendor_for_customer` is the run-time
    fail-closed gate.

    #73: a bare None org_id under an ACTIVE gate returns [] (fail closed), not every
    org's vendors; access.UNSCOPED (the CLI --operator door) still sweeps. The
    cross-org linked-vendor UNION below is UNAFFECTED -- its list_vendors(customer_id=)
    call passes access.UNSCOPED (the customer LINK is the authority, not a principal
    scope), because list_vendors now fails a bare None closed. Gate off is
    byte-identical.

    round-1 LOW-1: the guard ALSO refuses the poison access.BLOCKED_ORG_ID (-1) -- the
    value a NULL-org principal resolves to. org_scope_blocked() is True only for a bare
    None, but the own-org leg collapses -1 to an empty set (Vendor.org_id == -1) while
    the org-agnostic linked leg would still sweep, so -1 is failed closed HERE before
    either leg runs. (Both call sites resolve customer_id from a workflow a -1 principal
    can never reach, so this is defense in depth, not a live leak.)"""
    from app.services import access

    if access.org_scope_blocked(org_id) or org_id == access.BLOCKED_ORG_ID:
        return []
    # Own-org leg: pass the caller's scope THROUGH so list_vendors applies its own
    # guard/collapse (a real id narrows to it; access.UNSCOPED / gate-off None = all).
    own = list_vendors(session, org_id=org_id)
    seen = {v.id for v in own}
    n_cid = _coerce_id(customer_id)
    if n_cid is None:
        return own
    # Cross-org linked leg: the customer LINK is the authority, so this listing is
    # deliberately org-agnostic -- access.UNSCOPED, NOT a bare None (which list_vendors
    # now fails closed under an active gate).
    linked_extra = [
        v
        for v in list_vendors(session, customer_id=n_cid, org_id=access.UNSCOPED)
        if v.id not in seen
    ]
    return own + linked_extra


# --- CRUD --------------------------------------------------------------------
def create(
    session: Session,
    name: str,
    *,
    description: str = "",
    endpoint_url: str = "",
    config_json: dict | None = None,
    secret: dict | None = None,
    rate_card: dict | None = None,
    protocol=None,
    org_id=None,
    created_by: str = "system",
    actor: str = "system",
    ip: str = "",
) -> Vendor:
    """Create a vendor. Only `name` is required; the connection/rate-card can be filled
    in later on the detail page. Raises VendorError on a blank name or an unknown protocol.
    Audits vendor.create."""
    nm = (name or "").strip()
    if not nm:
        raise VendorError("vendor name is required.")
    if rate_card:
        # H3: refuse a non-finite / negative / non-numeric / unknown-band rate card
        # BEFORE any store (nothing is persisted on a bad card).
        try:
            vendor_cost.validate_rate_card(rate_card)
        except vendor_cost.RateCardError as exc:
            raise VendorError(str(exc)) from exc
    v = Vendor(
        name=nm,
        description=(description or "").strip(),
        endpoint_url=_validate_endpoint(endpoint_url),
        config=_validated_config(config_json),
        secret_enc=_enc_secret(secret) if secret else "",
        rate_card=vendor_cost.normalize_rate_card(rate_card) if rate_card else None,
        protocol=_validate_protocol(protocol),  # interop R1 seam (default 'native')
        org_id=org_id,
        created_by=created_by or actor or "system",
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    from app.services import audit

    audit.log(
        actor=actor or "system", action="vendor.create", target=f"vendor {v.id} ({v.name})", ip=ip
    )
    return v


def update(
    session: Session,
    vid,
    *,
    name=None,
    description=None,
    endpoint_url=None,
    config_json=None,
    protocol=None,
    org_id=None,
    actor: str = "system",
    ip: str = "",
) -> Vendor | None:
    """Partial update — only NON-None args change (a blank string clears a text field).
    Org-guarded. Returns the Vendor, None if missing/cross-org; raises VendorError on a
    blank name or an unknown protocol."""
    v = visible(session, vid, org_id=org_id)
    if v is None:
        return None
    if name is not None:
        nm = (name or "").strip()
        if not nm:
            raise VendorError("vendor name is required.")
        v.name = nm
    if description is not None:
        v.description = (description or "").strip()
    moved = None
    if endpoint_url is not None:
        new_url = _validate_endpoint(endpoint_url)
        from app.services.credential_origin import origin_label, same_origin

        old_origin, new_origin = origin_label(v.endpoint_url), origin_label(new_url)
        # Gate round 2 MEDIUM-1: fail closed; an unreadable origin is never "the same".
        moved_origin = not same_origin(v.endpoint_url, new_url)
        v.endpoint_url = new_url
        if moved_origin:
            # Shared credential-destination rule (#165/#218/#265): the signing secret
            # and auth token were saved for the old origin, so they do not follow the
            # endpoint to a new one. update() takes no secret; set_secret() re-enters it.
            cleared = bool(_dec_secret(v.secret_enc))
            if cleared:
                v.secret_enc = ""
            moved = (old_origin, new_origin, cleared)
    if config_json is not None:
        v.config = _validated_config(config_json)
    if protocol is not None:
        # interop R1: an unknown protocol is refused here (VendorError), never at dispatch.
        v.protocol = _validate_protocol(protocol)
    session.add(v)
    session.commit()
    session.refresh(v)
    from app.services import audit

    audit.log(
        actor=actor or "system", action="vendor.update", target=f"vendor {v.id} ({v.name})", ip=ip
    )
    if moved is not None:
        from app.services.credential_origin import audit_repoint

        audit_repoint(
            actor=actor,
            action="vendor.destination_changed",
            target=f"vendor {v.id} ({v.name})",
            old_origin=moved[0],
            new_origin=moved[1],
            cleared=moved[2],
        )
    return v


def set_secret(
    session: Session, vid, secret: dict, *, org_id=None, actor: str = "system", ip: str = ""
) -> Vendor | None:
    """MERGE credential fields into the vendor's encrypted secret blob (a blank/omitted
    field keeps the existing value — the connector "empty field means keep" convention;
    an explicit empty-string value CLEARS that field). Never echoes anything. Returns
    the Vendor or None."""
    v = visible(session, vid, org_id=org_id)
    if v is None:
        return None
    merged = dict(_dec_secret(v.secret_enc))
    for k, val in (secret or {}).items():
        k = str(k).strip()
        if not k:
            continue
        if val is None:
            continue  # omitted -> keep existing
        if str(val).strip() == "":
            merged.pop(k, None)  # explicit clear
        else:
            merged[k] = str(val)
    v.secret_enc = _enc_secret(merged) if merged else ""
    session.add(v)
    session.commit()
    session.refresh(v)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.set_secret",
        target=f"vendor {v.id} ({v.name})",
        ip=ip,
    )
    return v


def set_rate_card(
    session: Session, vid, rate_card: dict, *, org_id=None, actor: str = "system", ip: str = ""
) -> Vendor | None:
    """Set the vendor's MSA rate card (per-band cost per word). Normalized into the
    canonical band shape (unknown bands dropped, missing bands -> 0.0). Returns the
    Vendor or None."""
    v = visible(session, vid, org_id=org_id)
    if v is None:
        return None
    # H3: strict validation BEFORE the store (raises VendorError naming the bad band).
    try:
        vendor_cost.validate_rate_card(rate_card)
    except vendor_cost.RateCardError as exc:
        raise VendorError(str(exc)) from exc
    v.rate_card = vendor_cost.normalize_rate_card(rate_card)
    session.add(v)
    session.commit()
    session.refresh(v)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.set_rate_card",
        target=f"vendor {v.id} ({v.name})",
        ip=ip,
    )
    return v


def rate_card_for(vendor: Vendor) -> dict:
    """The vendor's normalized rate card, or the zero-rate default when none is set."""
    if vendor is None or not vendor.rate_card:
        return vendor_cost.default_rate_card()
    return vendor_cost.normalize_rate_card(vendor.rate_card)


def omit_locked_targets(vendor: Vendor) -> bool:
    """VP-0 per-vendor dispatch option (vendor.config JSON, default absent/False): when
    True, a dispatched LOCKED (covered-band) segment ships its source + locked marker but
    NO target — the vendor never receives (nor is expected to edit) the leveraged text. The
    id still ships and still returns, so the return-side id accounting is unchanged. Read
    off the free-form config blob so it needs no schema/migration and rides every config
    surface (web panel / CLI `--config-json` / PATCH-style /api config) additively."""
    if vendor is None:
        return False
    cfg = getattr(vendor, "config", None) or {}
    return bool(cfg.get("omit_locked_targets"))


def requires_quote_approval(vendor: Vendor) -> bool:
    """Vendor Wave E R3 per-vendor dispatch option (vendor.config JSON, default absent/
    False): when True, an external dispatch to this vendor is recorded QUOTED (not
    DISPATCHED) -- it parks the run PAUSED, ships the package carrying `quote_requested`, and
    awaits a vendor/operator QUOTE and then an operator APPROVAL before work proceeds (and
    before any return is admitted). Default OFF keeps the ENTIRE dispatch/return lifecycle
    BYTE-IDENTICAL for every existing vendor -- a leg is never QUOTED, the contract never
    carries `quote_requested`, and no approval gate exists. Read off the free-form config
    blob (like `omit_locked_targets` / `include_xliff`) so it needs no schema/migration and
    rides every config surface additively."""
    if vendor is None:
        return False
    cfg = getattr(vendor, "config", None) or {}
    return bool(cfg.get("requires_quote_approval"))


def include_xliff(vendor: Vendor) -> bool:
    """Vendor Wave D per-vendor dispatch option (vendor.config JSON, default absent/False):
    when True, the signed dispatch contract carries an `assets.xliff` string -- an XLIFF
    1.2 rendition of the same segments (vendor_xliff.render_contract_xliff) -- so a vendor
    system can hand the package straight to a CAT tool without parsing our JSON. Default
    OFF keeps the wire BYTE-IDENTICAL for every existing vendor (the JSON contract stays
    canonical); it is a pure opt-in add. Read off the free-form config blob (like
    `omit_locked_targets`) so it needs no schema/migration and rides every config surface
    additively. The operator "Download XLIFF" surfaces + Vendor Studio's own XLIFF export
    do NOT depend on this flag -- they render on demand -- so an XLIFF is always reachable
    manually regardless."""
    if vendor is None:
        return False
    cfg = getattr(vendor, "config", None) or {}
    return bool(cfg.get("include_xliff"))


# --- archive / unarchive -----------------------------------------------------
def archive(
    session: Session, vid, *, org_id=None, actor: str = "system", ip: str = ""
) -> Vendor | None:
    v = visible(session, vid, org_id=org_id)
    if v is None:
        return None
    if v.archived_at is not None:
        raise VendorError(f"vendor {v.id} ({v.name}) is already archived.")
    v.archived_at = _utcnow()
    session.add(v)
    session.commit()
    session.refresh(v)
    from app.services import audit

    audit.log(
        actor=actor or "system", action="vendor.archive", target=f"vendor {v.id} ({v.name})", ip=ip
    )
    return v


def unarchive(
    session: Session, vid, *, org_id=None, actor: str = "system", ip: str = ""
) -> Vendor | None:
    v = visible(session, vid, org_id=org_id)
    if v is None:
        return None
    if v.archived_at is None:
        raise VendorError(f"vendor {v.id} ({v.name}) is not archived.")
    v.archived_at = None
    session.add(v)
    session.commit()
    session.refresh(v)
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.unarchive",
        target=f"vendor {v.id} ({v.name})",
        ip=ip,
    )
    return v


# --- fail-closed delete ------------------------------------------------------
def reference_count(session: Session, vid: int) -> dict:
    """How many meaningful rows reference this vendor (read-only): workflow steps that
    select it + dispatch legs that recorded it. Customer LINKS are NOT counted (they are
    removed as part of the delete).

    WAVE-2 TRANSITIVE DEPENDENCY (L8): SegmentPostEdit rows carry vendor_id + dispatch_id
    FKs, so they too depend on this vendor — but they are NOT counted separately here and
    need no new arm: a PE vendor row can only exist for a dispatch leg (its dispatch_id FK
    requires one), so `dispatches > 0` already fails the delete closed for exactly the
    vendors that have post-edit rows. If leg rows are ever hard-deleted independently of
    their PE children, revisit this."""
    steps = len(session.exec(select(WorkflowStep.id).where(WorkflowStep.vendor_id == vid)).all())
    dispatches = len(
        session.exec(select(VendorDispatch.id).where(VendorDispatch.vendor_id == vid)).all()
    )
    refs = {"workflow_steps": steps, "dispatches": dispatches}
    refs["total"] = steps + dispatches
    return refs


def delete(
    session: Session, vid, *, org_id=None, actor: str = "system", ip: str = ""
) -> dict | None:
    """Delete a vendor. FAILS CLOSED (VendorDeleteBlocked) while a workflow step selects
    it or a dispatch leg references it — never a cascade. Customer links are removed as
    part of the delete (a link only scopes the vendor). Returns the (all-zero) reference
    dict on success, None when missing/cross-org."""
    v = visible(session, vid, org_id=org_id)
    if v is None:
        return None
    refs = reference_count(session, v.id)
    if refs["total"] > 0:
        raise VendorDeleteBlocked(v.id, refs)
    # Remove the scoping links (no independent meaning) then the vendor.
    for link in session.exec(select(VendorCustomer).where(VendorCustomer.vendor_id == v.id)).all():
        session.delete(link)
    name = v.name
    session.delete(v)
    session.commit()
    from app.services import audit

    audit.log(
        actor=actor or "system", action="vendor.delete", target=f"vendor {vid} ({name})", ip=ip
    )
    return refs


# --- customer linking (the fail-closed scoping seam) -------------------------
def link_customer(
    session: Session, vid, cid, *, org_id=None, actor: str = "system", ip: str = ""
) -> tuple[bool, Vendor]:
    """Link a vendor to a customer. Idempotent — returns (linked, vendor) where linked is
    False when the link already existed. Raises VendorError when either id is unknown.
    Both ids validated up front so a dangling FK can never be written.

    AUTHORITY LIVES ON THE CUSTOMER SIDE (2026-08-08 — "link ANY outside vendor
    with ANY of MY internal customers"): the LINKING authority is the CUSTOMER's org
    (buyer consent), NOT the vendor's. So:
      * the vendor is unrestricted as a link TARGET — ANY active, non-archived vendor in
        the install is linkable (resolved with `org_id=None`, never ringed by the caller);
      * a SCOPED caller may only link a customer in THEIR OWN org (buyer consent). A
        foreign customer is refused with the IDENTICAL message to the unknown-id case, so
        a foreign customer id is indistinguishable from a missing one (no existence
        oracle). With the multi-org gate OFF (org_id=None) this ring is inert — single-org
        installs are fully unrestricted, full many-to-many.
    Pair-uniqueness (vendor_id, customer_id) is still enforced (no duplicate links); an
    archived vendor is not linkable; admin gating still lives in the callers (web/CLI/api)."""
    _refuse_when_vendors_off()  # ffadmin Wave 3: no vendor mutation while off (HIGH-1)
    # #73: the vendor is an unrestricted link TARGET (cross-org linking, 2026-08-08) --
    # resolve it org-agnostically. access.UNSCOPED (NOT a bare None) is the DELIBERATE
    # "every org" scope now that visible() fails closed on a bare None under the gate;
    # the customer-side ring below is the actual authorization.
    from app.services import access

    v = selectable(session, vid, org_id=access.UNSCOPED)
    if v is None:
        raise VendorError(f"vendor {vid} does not exist.")
    n_cid = _coerce_id(cid)
    cust = session.get(Customer, n_cid) if n_cid is not None else None
    if cust is None:
        raise VendorError(f"customer {cid} does not exist.")
    # CUSTOMER-AUTHORITY RING: a scoped caller may only link customers in their own org.
    # Non-enumerable — the same message as the unknown-id case (kills the existence oracle).
    if org_id is not None and getattr(cust, "org_id", None) != org_id:
        raise VendorError(f"customer {cid} does not exist.")
    existing = session.exec(
        select(VendorCustomer).where(
            VendorCustomer.vendor_id == v.id, VendorCustomer.customer_id == cust.id
        )
    ).first()
    if existing is not None:
        return False, v
    session.add(VendorCustomer(vendor_id=v.id, customer_id=cust.id))
    session.commit()
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.link_customer",
        target=f"vendor {v.id} ({v.name})",
        detail=f"customer {cust.id}",
        ip=ip,
        customer_id=cust.id,
    )
    return True, v


def unlink_customer(
    session: Session, vid, cid, *, org_id=None, actor: str = "system", ip: str = ""
) -> tuple[bool, Vendor | None]:
    """Remove a vendor<->customer link. Returns (removed, vendor); removed is False when
    the link did not exist.

    EITHER-PARTY AUTHORITY (2026-08-08, mirrors the customer-side link authority): a
    scoped caller may sever the link if they administer EITHER party's org — the
    CUSTOMER's org (buyer, mirrors `link_customer`) OR the VENDOR's org (a vendor admin
    severing their own vendor's link). A caller who administers neither is refused
    non-enumerably (returned as `(False, None)`, identical to an unknown vendor). Inert
    with the multi-org gate OFF (org_id=None)."""
    _refuse_when_vendors_off()  # ffadmin Wave 3: no vendor mutation while off (HIGH-1)
    v = get(session, vid)  # resolve org-agnostically; authorize below
    if v is None:
        return False, None
    n_cid = _coerce_id(cid)
    cust = session.get(Customer, n_cid) if n_cid is not None else None
    if org_id is not None:
        vendor_ok = getattr(v, "org_id", None) == org_id
        customer_ok = cust is not None and getattr(cust, "org_id", None) == org_id
        if not (vendor_ok or customer_ok):
            return False, None  # administers neither party -> non-enumerable
    link = session.exec(
        select(VendorCustomer).where(
            VendorCustomer.vendor_id == v.id, VendorCustomer.customer_id == n_cid
        )
    ).first()
    if link is None:
        return False, v
    session.delete(link)
    session.commit()
    from app.services import audit

    audit.log(
        actor=actor or "system",
        action="vendor.unlink_customer",
        target=f"vendor {v.id} ({v.name})",
        detail=f"customer {n_cid}",
        ip=ip,
        customer_id=n_cid,
    )
    return True, v


def linked_customer_ids(session: Session, vid: int) -> list[int]:
    """The customer ids a vendor is linked to (order-stable)."""
    return list(
        session.exec(
            select(VendorCustomer.customer_id)
            .where(VendorCustomer.vendor_id == vid)
            .order_by(VendorCustomer.customer_id)
        ).all()
    )


def is_linked(session: Session, vid: int, cid: int) -> bool:
    """True when vendor `vid` is linked to customer `cid`."""
    return (
        session.exec(
            select(VendorCustomer.id).where(
                VendorCustomer.vendor_id == vid, VendorCustomer.customer_id == cid
            )
        ).first()
        is not None
    )


def vendor_for_customer(session: Session, vid, cid, *, org_id=None) -> Vendor | None:
    """THE FAIL-CLOSED SCOPING SEAM: the vendor with id `vid` IFF it exists, is NOT
    archived, AND is linked to customer `cid`. Returns None otherwise — a step can never
    dispatch to a vendor the customer is not connected to, even via a forged config. When
    `cid` is None (a document with no customer), NO vendor resolves (fail closed): an
    external step requires a customer-scoped vendor.

    CROSS-ORG (2026-08-08): the customer LINK is the sole authorization for DISPATCH — a
    vendor is dispatchable to ANY customer it is linked to, regardless of org (cross-org
    linking is unrestricted, so the buyer's org and the vendor's org may differ). (The
    RETURN path authorizes separately: claim token + HMAC signature + buyer org — see
    vendor_return.) The `org_id` param
    is accepted for call-site stability but is deliberately NOT used as a vendor-org gate:
    at dispatch the run passes the CUSTOMER's org (the buyer side), and re-asserting it
    against the vendor's own org would refuse every cross-org-linked vendor. The link is
    the tenant boundary here (the customer_id is already org-scoped upstream)."""
    n_cid = _coerce_id(cid)
    if n_cid is None:
        return None
    # exists + not-archived (org-agnostic — the link below is the authorization).
    # #73: access.UNSCOPED is the DELIBERATE cross-org scope (a bare None now fails
    # closed in visible() under the gate) so cross-org-linked dispatch still resolves.
    from app.services import access

    v = selectable(session, vid, org_id=access.UNSCOPED)
    if v is None:
        return None
    if not is_linked(session, v.id, n_cid):
        return None
    return v


# --- serialization (secret-masked) -------------------------------------------
def to_dict(session: Session, vendor: Vendor) -> dict:
    """A JSON/CLI-serialisable view of a vendor. NEVER carries the secret value — only a
    `has_secret` presence flag. Includes the normalized rate card + linked customer ids."""
    return {
        "id": vendor.id,
        "name": vendor.name,
        "description": vendor.description or "",
        "endpoint_url": vendor.endpoint_url or "",
        "config": dict(vendor.config or {}),
        "has_secret": has_secret(vendor),
        "rate_card": rate_card_for(vendor),
        "protocol": (vendor.protocol or "native"),  # interop R1 transport seam
        "status": vendor.status or VendorStatus.NEW,
        "last_message": vendor.last_message or "",
        "archived": vendor.archived_at is not None,
        "customer_ids": linked_customer_ids(session, vendor.id),
        "created_at": vendor.created_at.isoformat() if vendor.created_at else None,
    }


def customer_ids_in_org(session: Session, vid: int, org_id) -> list[int]:
    """The vendor's linked customer ids RESTRICTED to customers in `org_id` (order-stable).
    org_id=None -> all (single-org / unscoped). Used to keep a cross-org link/unlink
    response from leaking another org's customer ids that a shared vendor is also linked to."""
    ids = linked_customer_ids(session, vid)
    if org_id is None:
        return ids
    out = []
    for cid in ids:
        c = session.get(Customer, cid)
        if c is not None and getattr(c, "org_id", None) == org_id:
            out.append(cid)
    return out


def scoped_link_view(session: Session, vendor: Vendor, *, org_id=None) -> dict:
    """The vendor view the LINK / UNLINK surfaces (/api + the MCP tools that ride those
    routes) return. When the caller's ring COVERS the vendor — org_id is None (single-org /
    unscoped) OR vendor.org_id == org_id (own-org vendor) — this is the FULL, byte-identical
    `to_dict` shape callers have always received. For an OUT-OF-RING vendor (a cross-org link
    TARGET the caller administers via the customer side, never the vendor side) it is a
    MINIMAL, ring-safe view: identity + status only — NO connection (endpoint_url / config /
    has_secret), NO rate card — and `customer_ids` FILTERED to the caller's OWN org's
    customers, so the response never discloses another org's customer that this shared vendor
    is also linked to. This closes the link/unlink result-body side channel around the
    vendor-admin 404 wall (a foreign vendor's detail/update/secret stay 404 for this caller)."""
    if org_id is None or getattr(vendor, "org_id", None) == org_id:
        return to_dict(session, vendor)
    return {
        "id": vendor.id,
        "name": vendor.name,
        "status": vendor.status or VendorStatus.NEW,
        "customer_ids": customer_ids_in_org(session, vendor.id, org_id),
        "out_of_ring": True,
    }


def open_leg_count(session: Session, vid, *, customer_id=None) -> int:
    """The number of still-OPEN dispatch legs for vendor `vid`, optionally restricted to
    one buyer customer. OPEN = the canonical `vendor_dispatch._OPEN_LEG_STATUSES`
    (DISPATCHED, RETRY_WAIT, and — Wave E R3 — QUOTED, a leg parked awaiting a quote +
    operator approval). Sourced from that single set so this count tracks the leg lifecycle
    authoritatively and can never drift as open states are added (a terminal RETURNED /
    REJECTED / CANCELLED / DEAD_LETTER leg is excluded). Feeds the Customer-360
    "Connected vendors" panel's per-vendor "open legs" fact — computed for IN-RING vendors
    ONLY (a foreign vendor's operational state stays behind the ring)."""
    from app.services.vendor_dispatch import _OPEN_LEG_STATUSES

    # FAIL CLOSED: a caller that ASKED to scope by customer but handed an unparseable id
    # ("abc" / 0 / negative -> _coerce_id None) must NOT silently drop the filter and count
    # the vendor's legs across every customer (a cross-tenant count leak). Refuse to 0.
    if customer_id is not None:
        n_cid = _coerce_id(customer_id)
        if n_cid is None:
            return 0
    else:
        n_cid = None
    q = (
        select(func.count())
        .select_from(VendorDispatch)
        .where(
            VendorDispatch.vendor_id == vid, VendorDispatch.status.in_(tuple(_OPEN_LEG_STATUSES))
        )
    )
    if n_cid is not None:
        q = q.where(VendorDispatch.customer_id == n_cid)
    return int(session.exec(q).one())


def linked_vendor_views(session: Session, cid, *, org_id=None) -> list[dict]:
    """Ring-scoped views of the vendors LINKED to customer `cid` — the read model the
    Customer-360 "Connected vendors" panel renders (the customer side of the cross-org
    link, 2026-08-08).

    Each row is exactly `scoped_link_view`'s shape, so the ring rule lives in ONE place
    (never re-derived in the route/template): an IN-RING vendor carries the full
    `to_dict` (the panel renders its identity + a /vendors/{id} link, which is safe
    because that page resolves for this caller); an OUT-OF-RING vendor — a cross-org link
    TARGET the caller administers from the customer side but NOT the vendor side — carries
    ONLY {id, name, status, customer_ids (filtered to this org), out_of_ring: True}, with
    NO endpoint/config/has_secret/rate_card. The template keys off `out_of_ring` to
    withhold the vendor link (it would 404 for a foreign vendor). Archived vendors are
    included (a stale link must still be visible so it can be severed). Resolved
    org-agnostically (access.UNSCOPED on the listing) because a linked vendor may live
    in another org; `org_id` is applied only as the DISCLOSURE ring by scoped_link_view.

    #73: the listing passes access.UNSCOPED, NOT a bare None -- list_vendors now fails a
    bare None closed under an active gate, so the deliberate org-agnostic intent must be
    stated explicitly (the per-row ring in scoped_link_view is the real disclosure gate)."""
    from app.services import access

    rows = []
    for v in list_vendors(session, customer_id=cid, include_archived=True, org_id=access.UNSCOPED):
        row = scoped_link_view(session, v, org_id=org_id)
        # Display-honesty overlay (review 2026-08-08, F5): the out-of-ring minimal dict
        # carries no `archived` key, so an archived foreign link would otherwise render as
        # a healthy live row. A deliberate ONE-BIT ring-widening, HERE ONLY — never in
        # scoped_link_view (that is the /api response shape, which must not gain a field).
        row.setdefault("archived", v.archived_at is not None)
        # Per-vendor PANEL FACTS (2026-08-08) — IN-RING vendors ONLY. `open_legs` is the
        # vendor's still-open dispatch count FOR THIS CUSTOMER (Wave E-aware via
        # open_leg_count) and `has_rate_card` is MSA/cost-model PRESENCE (a boolean — never
        # the bands, which are admin-gated vendor cost data). Both are DELIBERATELY WITHHELD
        # for an out-of-ring (foreign) vendor: its operational state + MSA presence sit
        # behind the vendor-admin ring exactly like its endpoint/rate/secret, so the panel
        # never becomes a side channel around the 404 wall. The template shows a muted dash
        # when a key is absent. These keys live HERE ONLY (never in scoped_link_view — the
        # /api response shape must not gain fields).
        if not row.get("out_of_ring"):
            row["open_legs"] = open_leg_count(session, v.id, customer_id=cid)
            row["has_rate_card"] = v.rate_card is not None
            # RING overlay (review F1): an in-ring row is the full `to_dict`, whose
            # `customer_ids` is the vendor's UNFILTERED link list — for a vendor shared
            # across orgs that can carry another org's customer ids. The out-of-ring branch
            # already filters via customer_ids_in_org; apply the SAME filter to in-ring rows
            # HERE (never in scoped_link_view — that is the frozen /api shape) so the panel's
            # read model never discloses a foreign org's customer id.
            row["customer_ids"] = customer_ids_in_org(session, v.id, org_id)
        rows.append(row)
    return rows
