"""SCIM 2.0 provisioning service (Train I) — the service seam behind the /scim/v2
router (app/web/scim.py).

WHAT THIS IS. A minimal, enterprise-interoperable SCIM 2.0 server for the two IdPs
that matter (Okta, Entra): the /Users lifecycle (create / read / list+filter / replace
/ patch / soft-deactivate) and /Groups as DIRECTORY METADATA (full CRUD + membership,
with ZERO authorization effect in v1 — a group member gains no visibility, no role, no
project scope; group->authz mapping is post-demo policy work).

INVARIANTS this seam upholds (the same ones every other write path inherits):
  * ALL account creation funnels through security.create_user (email validation, dup
    check, customer-binding invariant, org stamping). SCIM never constructs a User().
  * Soft lifecycle only: DELETE / active=false deactivates, never deletes a row.
    GDPR erase stays a human-only act. The last-active-admin guard is serialized
    with other account mutations and SURFACED as a 409, never bypassed.
  * Org confinement: a SCIM key resolves to its owner's HOME org (access.current_org_id).
    Every list/get/create/update is confined to that org; a cross-org id is a 404 (no
    oracle), never a 403.
  * Role is NOT settable via SCIM in v1 (enterprise role mapping is a policy decision):
    the role is set once at create from the scim_default_role setting and never changed
    by PUT/PATCH.

ERRORS follow RFC 7644 §3.12 (urn:ietf:params:scim:api:messages:2.0:Error) — raised as
ScimError and rendered by the router. Auth failures are 401 (WWW-Authenticate: Bearer);
insufficient scope / non-admin owner is 403; a cross-org or missing id is 404.

Documented in: docs/architecture/sso-provisioning.md
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import engine
from app.models import Role, User, UserGroup, UserGroupMember
from app.services import access, apikeys, audit, security, settings
from app.services.admin_invariant import lock_user_mutations

# --- SCIM schema URNs --------------------------------------------------------- #
SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_PATCHOP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCHEMA_SPCONFIG = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"

_MAX_COUNT = 200  # cap a page so a hostile `count` can't force a huge serialization


class ScimError(Exception):
    """An RFC 7644 §3.12 SCIM error. `status` is the HTTP status; `scim_type` is the
    optional scimType keyword (e.g. 'uniqueness', 'invalidFilter', 'invalidValue')."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        self.scim_type = scim_type

    def to_dict(self) -> dict:
        body: dict = {"schemas": [SCHEMA_ERROR], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body


# --------------------------------------------------------------------------- #
# Feature flag + auth
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """Strict on-value whitelist (matches the four delegated feature services)."""
    return str(settings.get("scim_enabled", "0")) == "1"


def resolve_identity(token: str) -> dict:
    """Resolve a SCIM caller from an ALREADY-EXTRACTED token (Bearer or X-Loc-Token),
    or raise ScimError. Requirements (in order — auth FIRST, before any body parse):
      * a token is present (else 401);
      * it resolves through the EXISTING ApiKey path (apikeys.verify_key — key active +
        owner active), else 401;
      * the key carries the 'scim' scope, else 403;
      * the owner's org is active (multi-org suspend), else 401;
      * the owner is an ADMIN, else 403.
    Returns {actor, admin_email, user, org_id}. No new credential mechanism — the
    /scim mount is the ONLY place a Bearer token is accepted; every other surface keeps
    rejecting Bearer."""
    sent = (token or "").strip()
    if not sent:
        raise ScimError(401, "Authentication required.")
    key = apikeys.verify_key(sent)
    if key is None:
        raise ScimError(401, "Invalid or inactive token.")
    if "scim" not in apikeys.scope_set(key.scopes or ""):
        raise ScimError(403, "The presented token lacks the 'scim' scope.")
    with Session(engine) as s:
        user = s.get(User, key.user_id)
    if user is None or not user.active:
        raise ScimError(401, "Invalid or inactive token.")
    if not access.user_org_active(user):
        raise ScimError(401, "Invalid or inactive token.")
    if user.role != Role.ADMIN:
        raise ScimError(403, "The token owner is not an administrator.")
    return {
        "actor": f"scim:{key.prefix}:{user.email}",
        "admin_email": user.email,
        "user": user,
        "org_id": access.current_org_id(user),
    }


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if isinstance(dt, datetime) else None


def _int_id(raw: str) -> int:
    """Parse a resource id path segment to an int, or raise 404 (a non-numeric id can
    never name a row, and must be indistinguishable from a missing one)."""
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        raise ScimError(404, f"Resource {raw!r} not found.") from None


def _default_role() -> str:
    role = settings.get("scim_default_role") or Role.TRANSLATOR
    return role if role in Role.ALL else Role.TRANSLATOR


def _display_name(payload: dict) -> str:
    """SCIM name mapping: prefer name.formatted, then displayName, then a given+family
    join, else "" (never None — User.name is a non-null string)."""
    name = payload.get("name")
    if isinstance(name, dict):
        formatted = (name.get("formatted") or "").strip()
        if formatted:
            return formatted
        parts = [(name.get("givenName") or "").strip(), (name.get("familyName") or "").strip()]
        joined = " ".join(p for p in parts if p)
        if joined:
            return joined
    dn = payload.get("displayName")
    return (dn or "").strip() if isinstance(dn, str) else ""


def _username(payload: dict) -> str:
    un = (payload.get("userName") or "").strip()
    if not un:
        raise ScimError(400, "userName is required.", "invalidValue")
    return un


_FILTER_RE = re.compile(r'^\s*(\w+)\s+eq\s+"(.*)"\s*$', re.IGNORECASE)


def parse_filter(raw: str, allowed: tuple[str, ...]) -> tuple[str, str] | None:
    """Parse the SUBSET of SCIM filters Okta/Entra actually send: `<attr> eq "<value>"`.
    Returns (canonical_attr, value) or None (no filter). Anything else — a different
    operator, a compound filter, an unsupported attribute — is a 400 invalidFilter (the
    honest 'we support a limited filter' answer, advertised in ServiceProviderConfig)."""
    if not raw or not raw.strip():
        return None
    m = _FILTER_RE.match(raw)
    if not m:
        raise ScimError(400, f"Unsupported filter: {raw!r}", "invalidFilter")
    attr, value = m.group(1), m.group(2)
    canon = {a.lower(): a for a in allowed}
    if attr.lower() not in canon:
        raise ScimError(400, f"Filtering on {attr!r} is not supported.", "invalidFilter")
    return canon[attr.lower()], value


def _page_params(start_index, count) -> tuple[int, int | None]:
    """1-based startIndex per RFC 7644 §3.4.2, and a capped count. A missing/blank count
    means 'all remaining'; a hostile huge count is capped at _MAX_COUNT."""
    try:
        si = int(start_index) if start_index not in (None, "") else 1
    except (TypeError, ValueError):
        raise ScimError(400, "startIndex must be an integer.", "invalidValue") from None
    if si < 1:
        si = 1
    if count in (None, ""):
        return si, None
    try:
        c = int(count)
    except (TypeError, ValueError):
        raise ScimError(400, "count must be an integer.", "invalidValue") from None
    if c < 0:
        c = 0
    return si, min(c, _MAX_COUNT)


def _list_response(resources: list, total: int, start_index: int) -> dict:
    return {
        "schemas": [SCHEMA_LIST],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


# --------------------------------------------------------------------------- #
# Resource serialization
# --------------------------------------------------------------------------- #
def user_resource(u: User, base: str = "") -> dict:
    body: dict = {
        "schemas": [SCHEMA_USER],
        "id": str(u.id),
        "userName": u.email,
        "name": {"formatted": u.name or ""},
        "displayName": u.name or "",
        "active": bool(u.active),
        "emails": [{"value": u.email, "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": _iso(u.created_at),
            "lastModified": _iso(u.last_login or u.created_at),
            "location": f"{base}/Users/{u.id}" if base else f"/scim/v2/Users/{u.id}",
        },
    }
    if u.external_id:
        body["externalId"] = u.external_id
    return body


def group_resource(g: UserGroup, members: list[User] | None = None, base: str = "") -> dict:
    body: dict = {
        "schemas": [SCHEMA_GROUP],
        "id": str(g.id),
        "displayName": g.name,
        "members": [
            {"value": str(m.id), "display": m.email, "$ref": f"/scim/v2/Users/{m.id}"}
            for m in (members or [])
        ],
        "meta": {
            "resourceType": "Group",
            "created": _iso(g.created_at),
            "lastModified": _iso(g.created_at),
            "location": f"{base}/Groups/{g.id}" if base else f"/scim/v2/Groups/{g.id}",
        },
    }
    if g.external_id:
        body["externalId"] = g.external_id
    return body


# --------------------------------------------------------------------------- #
# Org-scoped fetch helpers
# --------------------------------------------------------------------------- #
def _user_in_org(s: Session, uid: int, org_id) -> User | None:
    """A User visible to a SCIM key bound to org_id, or None. Under an active gate a
    row in another org (or a NULL-org row) is invisible — the same_org rule."""
    u = s.get(User, uid)
    if u is None:
        return None
    if org_id is not None and u.org_id != org_id:
        return None
    return u


def _group_in_org(s: Session, gid: int, org_id) -> UserGroup | None:
    g = s.get(UserGroup, gid)
    if g is None:
        return None
    if org_id is not None and g.org_id != org_id:
        return None
    return g


def _group_members(s: Session, gid: int) -> list[User]:
    rows = s.exec(
        select(User)
        .where(UserGroupMember.group_id == gid, UserGroupMember.user_id == User.id)
        .order_by(User.id)
    ).all()
    return list(rows)


# --------------------------------------------------------------------------- #
# /Users
# --------------------------------------------------------------------------- #
def list_users(org_id, filter_raw, start_index, count, base: str = "") -> dict:
    filt = parse_filter(filter_raw, ("userName", "externalId"))
    si, c = _page_params(start_index, count)
    with Session(engine) as s:
        stmt = select(User)
        if org_id is not None:
            stmt = stmt.where(User.org_id == org_id)
        if filt:
            attr, val = filt
            if attr == "userName":
                stmt = stmt.where(User.email == security.normalize_email(val))
            else:  # externalId
                stmt = stmt.where(User.external_id == val)
        rows = list(s.exec(stmt.order_by(User.id)).all())
        total = len(rows)
        page = rows[si - 1 : (si - 1 + c) if c is not None else None]
        resources = [user_resource(u, base) for u in page]
    return _list_response(resources, total, si)


def get_user(org_id, raw_id, base: str = "") -> dict:
    uid = _int_id(raw_id)
    with Session(engine) as s:
        u = _user_in_org(s, uid, org_id)
        if u is None:
            raise ScimError(404, f"User {raw_id} not found.")
        return user_resource(u, base)


def create_user(org_id, actor: str, payload: dict, base: str = "") -> tuple[dict, bool]:
    """POST /Users. Returns (resource, created) — created=True => 201 (fresh account),
    False => 200 (ADOPTED an existing account). Adoption: an existing email is stamped
    with the SCIM externalId and its name/active updated per the payload; its
    auth_source and login method are UNCHANGED (SCIM push wins on lifecycle, not on how
    the person signs in). A 409 uniqueness fires only when the payload's externalId
    already belongs to a DIFFERENT user's email identity."""
    email = security.normalize_email(_username(payload))
    ext = (payload.get("externalId") or "").strip() or None
    name = _display_name(payload)
    active = _payload_active(payload)

    with Session(engine) as s:
        # externalId uniqueness (org-scoped): a different email holding this externalId
        # is a hard conflict (a SCIM 409 uniqueness).
        if ext:
            stmt = select(User).where(User.external_id == ext)
            if org_id is not None:
                stmt = stmt.where(User.org_id == org_id)
            other = s.exec(stmt).first()
            if other is not None and other.email != email:
                raise ScimError(
                    409, f"externalId {ext!r} already belongs to another user.", "uniqueness"
                )
        existing = security.get_by_email(email)

    if existing is not None:
        # Global email uniqueness predates multi-org, so an email could live in ANOTHER
        # org. Adopting it into this org would be a cross-tenant leak — refuse with a
        # generic 409 (never reveal the other org).
        if org_id is not None and existing.org_id != org_id:
            raise ScimError(409, "userName already in use.", "uniqueness")
        updated = _apply_user_fields(
            existing.id, org_id, actor, name=name, active=active, external_id=ext
        )
        audit.log(
            actor=actor,
            action="scim.user.update",
            target=email,
            detail="adopted existing account via SCIM create",
        )
        return user_resource(updated, base), False

    # Fresh account — funnel through the shared seam (email validation, org stamping).
    role = _default_role()
    try:
        u = security.create_user(email, name, role, "", auth_source="scim", org_id=org_id)
    except ValueError as exc:
        raise ScimError(400, str(exc), "invalidValue") from None
    # Creation and lifecycle stamping are separate commits. Another request may
    # already have removed the other admin, so active=false needs the same guard.
    row = _apply_user_fields(u.id, org_id, actor, active=active, external_id=ext)
    result = user_resource(row, base)
    audit.log(actor=actor, action="scim.user.create", target=email, detail=f"role={role}")
    return result, True


def _apply_user_fields(uid: int, org_id, actor: str, *, name=None, active=None, external_id=None):
    """Apply the mapped SCIM fields to an EXISTING user (adoption / PUT / PATCH commit).
    Role and email/userName are NEVER changed here (v1 policy). Deactivation routes
    through the last-active-admin guard (a 409). Returns the refreshed User."""
    with Session(engine) as s:
        lock_user_mutations(s)
        u = _user_in_org(s, uid, org_id)
        if u is None:
            raise ScimError(404, f"User {uid} not found.")
        # active transition FIRST (it owns the last-admin guard).
        scim_tokens: list[str] = []
        if active is not None and bool(active) != bool(u.active):
            if not active and security.is_last_active_admin(s, uid, org_id):
                raise ScimError(
                    409,
                    "Refusing to deactivate the last active administrator of this organization.",
                    "mutability",
                )
            u.active = bool(active)
            if not active:
                # #422: deactivation ends the sessions and API keys (same transaction),
                # so a later active=true never revives a pre-deactivation credential.
                s.add(u)
                # #422 gate: a SCIM token this account owns dies with it (possibly the
                # very token of this request); say so in the audit trail.
                scim_tokens = security.scim_token_prefixes(s, uid)
                security.end_credentials(s, uid, org_id=org_id)
        if name is not None:
            u.name = name
        if external_id is not None:
            u.external_id = external_id
        s.add(u)
        s.commit()
        s.refresh(u)
        security.audit_scim_tokens_ended(actor, u.email, scim_tokens)
        return u


def replace_user(org_id, actor: str, raw_id, payload: dict, base: str = "") -> dict:
    """PUT /Users/{id}: full replace of the MAPPED fields (name, active, externalId).
    userName (email) is immutable in this mapping, and role is never changed."""
    uid = _int_id(raw_id)
    name = _display_name(payload)
    active = _payload_active(payload)
    ext = (payload.get("externalId") or "").strip() or None
    u = _apply_user_fields(uid, org_id, actor, name=name, active=active, external_id=ext)
    audit.log(actor=actor, action="scim.user.update", target=u.email, detail="PUT replace")
    return user_resource(u, base)


def _patch_ops(payload: dict) -> list[dict]:
    ops = payload.get("Operations") or payload.get("operations")
    if not isinstance(ops, list):
        raise ScimError(400, "PATCH body must carry an Operations array.", "invalidValue")
    return ops


_CORE_USER_PREFIX = SCHEMA_USER.lower() + ":"


def _core_attr(name) -> str:
    """A PATCH attribute path or pathless key, lowercased, with the core User schema URN
    prefix dropped: RFC 7644 3.10 allows the fully qualified form
    (`urn:ietf:params:scim:schemas:core:2.0:User:active`), and ignoring it would make a
    deprovisioning PATCH a silent no-op (#481 gate)."""
    low = str(name).strip().lower()
    if low.startswith(_CORE_USER_PREFIX):
        low = low[len(_CORE_USER_PREFIX) :]
    return low


def patch_user(org_id, actor: str, raw_id, payload: dict, base: str = "") -> dict:
    """PATCH /Users/{id} (RFC 7644 §3.5.2). Supports replace/add on `active`,
    `name.formatted`/`name.*`, `displayName`, `externalId` — with Entra's Operations /
    op casing tolerated. An unsupported path is ignored (a no-op), never a 500."""
    uid = _int_id(raw_id)
    name = active = ext = None
    for op in _patch_ops(payload):
        if not isinstance(op, dict):
            continue
        verb = str(op.get("op") or op.get("Op") or "").lower()
        if verb not in ("replace", "add"):
            # `remove` on a User attribute is not meaningful for our mapped fields.
            continue
        path = (op.get("path") or op.get("Path") or "").strip()
        value = op.get("value")
        if not path:
            # No path: value is an object of attributes (Azure AD style).
            if isinstance(value, dict):
                low = {_core_attr(k): v for k, v in value.items()}
                if "active" in low:
                    active = _as_bool(low["active"])
                if "displayname" in low:
                    name = (low["displayname"] or "").strip()
                if "name" in low and isinstance(low["name"], dict):
                    name = _display_name({"name": low["name"]})
                if "externalid" in low:
                    ext = (low["externalid"] or "").strip() or None
            continue
        pl = _core_attr(path)
        if pl == "active":
            active = _as_bool(value)
        elif pl in ("displayname", "name.formatted") or pl.startswith("name"):
            if pl == "name.formatted" or pl.startswith("name."):
                name = (
                    (value or "").strip()
                    if isinstance(value, str)
                    else _display_name({"name": value} if isinstance(value, dict) else {})
                )
            else:  # displayName or bare "name"
                name = (
                    (value or "").strip()
                    if isinstance(value, str)
                    else _display_name(value if isinstance(value, dict) else {})
                )
        elif pl == "externalid":
            ext = (value or "").strip() or None if isinstance(value, str) else None
    u = _apply_user_fields(uid, org_id, actor, name=name, active=active, external_id=ext)
    audit.log(actor=actor, action="scim.user.update", target=u.email, detail="PATCH")
    return user_resource(u, base)


def _as_bool(v) -> bool:
    """Strict SCIM boolean: a real bool or the strings "true"/"false" (Entra sends
    those). Anything else is a 400, never a guess: reading garbage as True would
    silently fail a deprovision, reading it as False would deactivate on a typo (#481)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise ScimError(400, '"active" must be a boolean (true or false).', "invalidValue")


def _payload_active(payload: dict):
    """`active` from a POST/PUT body: None when omitted (keep the stored value; a PUT
    that leaves it out never re-activates an account), else the strict boolean."""
    if "active" not in payload:
        return None
    return _as_bool(payload["active"])


def deactivate_user(org_id, actor: str, raw_id) -> None:
    """DELETE /Users/{id} => SOFT deactivate (never a row delete; GDPR erase is human).
    The last-active-admin guard is surfaced as a 409."""
    uid = _int_id(raw_id)
    with Session(engine) as s:
        lock_user_mutations(s)
        u = _user_in_org(s, uid, org_id)
        if u is None:
            raise ScimError(404, f"User {raw_id} not found.")
        if u.active and security.is_last_active_admin(s, uid, org_id):
            raise ScimError(
                409,
                "Refusing to deactivate the last active administrator of this organization.",
                "mutability",
            )
        email = u.email
        result = security.set_active(s, uid, False, actor=actor, org_id=org_id)
    if result.get("ok"):
        audit.log(actor=actor, action="scim.user.deactivate", target=email, detail="SCIM DELETE")


# --------------------------------------------------------------------------- #
# /Groups (directory metadata only — ZERO authorization effect in v1)
# --------------------------------------------------------------------------- #
def list_groups(org_id, filter_raw, start_index, count, base: str = "") -> dict:
    filt = parse_filter(filter_raw, ("displayName", "externalId"))
    si, c = _page_params(start_index, count)
    with Session(engine) as s:
        stmt = select(UserGroup)
        if org_id is not None:
            stmt = stmt.where(UserGroup.org_id == org_id)
        if filt:
            attr, val = filt
            if attr == "displayName":
                stmt = stmt.where(UserGroup.name == val)
            else:
                stmt = stmt.where(UserGroup.external_id == val)
        rows = list(s.exec(stmt.order_by(UserGroup.id)).all())
        total = len(rows)
        page = rows[si - 1 : (si - 1 + c) if c is not None else None]
        resources = [group_resource(g, _group_members(s, g.id), base) for g in page]
    return _list_response(resources, total, si)


def get_group(org_id, raw_id, base: str = "") -> dict:
    gid = _int_id(raw_id)
    with Session(engine) as s:
        g = _group_in_org(s, gid, org_id)
        if g is None:
            raise ScimError(404, f"Group {raw_id} not found.")
        return group_resource(g, _group_members(s, gid), base)


def _member_ids(payload: dict) -> list[int]:
    """The user ids in a SCIM group `members` array (each {value: "<id>"}), DEDUPED with
    first-seen order preserved. Membership is idempotent by design (a user is in the group
    or not), so a payload repeating an id — Okta/Entra do emit members:[{value:"2"},
    {value:"2"}] — must be one edge, not a uniqueness collision. Deduping HERE is the single
    upstream point that heals every consumer: create_group and replace_group feed this list
    straight into _set_members (two identical (group_id,user_id) rows would otherwise hit the
    ux_usergroupmember_group_user unique index at commit and get MISREPORTED as a name-409 by
    the IntegrityError handler); patch_group also parses through here, and its _add_members
    already dedups against the present set, so a deduped input is at worst a no-op there — no
    double-application."""
    out: list[int] = []
    seen: set[int] = set()
    for m in payload.get("members") or []:
        if isinstance(m, dict) and m.get("value") not in (None, ""):
            try:
                uid = int(str(m["value"]))
            except (TypeError, ValueError):
                raise ScimError(
                    400, f"member value {m.get('value')!r} is not a user id.", "invalidValue"
                ) from None
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


def _set_members(s: Session, gid: int, org_id, user_ids: list[int], actor: str) -> None:
    """Reconcile the group's membership to EXACTLY `user_ids` (org-validated), idempotently.
    Implemented as a set diff (delete edges no longer wanted, add only missing ones), NOT a
    blanket delete-all-then-re-add. The naive delete+re-add churns a RETAINED member's edge
    in a single flush; because UserGroupMember->UserGroup has no declared ORM relationship,
    SQLAlchemy's unit-of-work can emit that member's INSERT BEFORE its DELETE, violating the
    ux_usergroupmember_group_user unique index at commit -> an IntegrityError that
    _commit_group_or_409 then MISLABELS as a name-409. Real Okta/Entra PUTs send the full
    member list (retained members included), so the churn fired on the common path. Diffing
    touches only genuinely-changed edges -> no self-collision, and (with _member_ids already
    deduping) a repeated id is a clean no-op. Retained edges keep their original added_by."""
    for uid in user_ids:
        if _user_in_org(s, uid, org_id) is None:
            raise ScimError(
                400, f"member {uid} is not a user in this organization.", "invalidValue"
            )
    target = set(user_ids)
    present: set[int] = set()
    for row in s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all():
        if row.user_id in target:
            present.add(row.user_id)  # keep the retained edge untouched (no churn)
        else:
            s.delete(row)  # drop an edge no longer in the target set
    for uid in user_ids:
        if uid not in present:
            s.add(UserGroupMember(group_id=gid, user_id=uid, added_by=actor))
            present.add(uid)


def create_group(org_id, actor: str, payload: dict, base: str = "") -> dict:
    name = (payload.get("displayName") or "").strip()
    if not name:
        raise ScimError(400, "displayName is required.", "invalidValue")
    ext = (payload.get("externalId") or "").strip() or None
    member_ids = _member_ids(payload)
    with Session(engine) as s:
        # name unique per org (the functional unique index backs this; pre-check for a
        # clean 409 instead of an IntegrityError 500).
        stmt = select(UserGroup).where(UserGroup.name == name)
        if org_id is not None:
            stmt = stmt.where(UserGroup.org_id == org_id)
        else:
            stmt = stmt.where(UserGroup.org_id.is_(None))
        if s.exec(stmt).first() is not None:
            raise ScimError(409, f"A group named {name!r} already exists.", "uniqueness")
        g = UserGroup(name=name, org_id=org_id, external_id=ext)
        s.add(g)
        # ATOMICITY: flush (NOT commit) so the group INSERT gets an id (g.id) while staying
        # in the SAME uncommitted transaction as the membership. _set_members validates ALL
        # member ids up front and raises a 400 BEFORE it mutates, so a create carrying a bad /
        # cross-org member fails with NOTHING persisted. The pre-fix code committed the group
        # FIRST and only then set members, so a rejected member left an ORPHAN empty group in
        # the DB AND burned the (now-taken) name -> every retry then 409'd. flush-then-a-single
        # -commit makes the group + its membership land together or not at all.
        try:
            s.flush()  # INSERT the group -> g.id; a raced duplicate name raises IntegrityError here
            _set_members(s, g.id, org_id, member_ids, actor)
            s.commit()  # one commit: group row + membership edges are one atomic unit
        except IntegrityError:
            # A concurrent create won the ux_usergroup_org_name race: a clean 409, never a 500.
            s.rollback()
            raise ScimError(409, f"A group named {name!r} already exists.", "uniqueness") from None
        except Exception:
            # Any other failure (notably _set_members' 400 for a bad/cross-org member) rolls the
            # WHOLE create back -> no orphan group, name stays free -> re-raise unchanged.
            s.rollback()
            raise
        s.refresh(g)
        result = group_resource(g, _group_members(s, g.id), base)
    audit.log(
        actor=actor, action="scim.group.create", target=name, detail=f"members={len(member_ids)}"
    )
    return result


def _name_taken(s: Session, org_id, name: str, exclude_id: int) -> bool:
    """Whether ANOTHER group (not exclude_id) in this org already holds `name` — the
    pre-check that turns a rename collision into a clean 409 BEFORE the pending name
    change can autoflush into an IntegrityError mid-query (which would escape the
    commit-time guard). Mirrors create_group's pre-check."""
    stmt = select(UserGroup).where(UserGroup.name == name, UserGroup.id != exclude_id)
    stmt = (
        stmt.where(UserGroup.org_id == org_id)
        if org_id is not None
        else stmt.where(UserGroup.org_id.is_(None))
    )
    return s.exec(stmt).first() is not None


def _commit_group_or_409(s: Session, group_name: str) -> None:
    """Commit a group mutation, turning a ux_usergroup_org_name / ux_usergroupmember_group_user
    unique-index violation (a raced rename / membership add) into a clean 409 uniqueness
    instead of an unhandled 500 — the concurrent-race backstop behind the _name_taken
    pre-check. On the alembic/prod path the index exists, so this is the live guard; on the
    SQLite path it now exists too (db._ensure_indexes)."""
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise ScimError(
            409, f"A group named {group_name!r} already exists.", "uniqueness"
        ) from None


def replace_group(org_id, actor: str, raw_id, payload: dict, base: str = "") -> dict:
    gid = _int_id(raw_id)
    name = (payload.get("displayName") or "").strip()
    ext = (payload.get("externalId") or "").strip() or None
    member_ids = _member_ids(payload)
    with Session(engine) as s:
        g = _group_in_org(s, gid, org_id)
        if g is None:
            raise ScimError(404, f"Group {raw_id} not found.")
        if name and name != g.name and _name_taken(s, org_id, name, gid):
            raise ScimError(409, f"A group named {name!r} already exists.", "uniqueness")
        if name:
            g.name = name
        g.external_id = ext
        s.add(g)
        # no_autoflush: _set_members runs SELECTs, and a pending g.name change would
        # otherwise AUTOFLUSH mid-query and raise the unique violation THERE (escaping the
        # commit-time backstop -> 500). Deferring the flush to the explicit commit funnels
        # every violation through _commit_group_or_409 -> a clean 409 (the true-race path
        # the _name_taken pre-check cannot cover).
        with s.no_autoflush:
            _set_members(s, gid, org_id, member_ids, actor)
        _commit_group_or_409(s, name or g.name)
        s.refresh(g)
        result = group_resource(g, _group_members(s, gid), base)
    audit.log(actor=actor, action="scim.group.update", target=g.name, detail="PUT replace")
    return result


_MEMBER_PATH_RE = re.compile(r'^members\[\s*value\s+eq\s+"(.*)"\s*\]$', re.IGNORECASE)


def patch_group(org_id, actor: str, raw_id, payload: dict, base: str = "") -> dict:
    """PATCH /Groups/{id}: add/remove members and replace displayName (RFC 7644). Entra
    sends member removes as `path: members[value eq "<id>"]`; Okta sends `remove` with a
    `members` value array. Both are handled."""
    gid = _int_id(raw_id)
    with Session(engine) as s:
        g = _group_in_org(s, gid, org_id)
        if g is None:
            raise ScimError(404, f"Group {raw_id} not found.")
        # no_autoflush over the whole op loop: a rename op followed by a member op would
        # otherwise AUTOFLUSH the pending g.name change during the member query's SELECT and
        # raise the unique violation THERE (escaping the commit-time backstop -> 500).
        # Deferring the flush to the explicit commit funnels every violation through
        # _commit_group_or_409 -> a clean 409 (the true-race path _name_taken cannot cover).
        with s.no_autoflush:
            for op in _patch_ops(payload):
                if not isinstance(op, dict):
                    continue
                verb = str(op.get("op") or op.get("Op") or "").lower()
                path = (op.get("path") or op.get("Path") or "").strip()
                value = op.get("value")
                pl = path.lower()
                if pl == "members" or (not path and isinstance(value, dict) and "members" in value):
                    vals = value if path else value.get("members")
                    ids = _member_ids({"members": vals or []})
                    if verb == "add":
                        _add_members(s, gid, org_id, ids, actor)
                    elif verb == "remove":
                        _remove_members(s, gid, ids)
                    elif verb == "replace":
                        _set_members(s, gid, org_id, ids, actor)
                elif _MEMBER_PATH_RE.match(path):
                    # members[value eq "X"] — a targeted single-member op.
                    target = _int_id(_MEMBER_PATH_RE.match(path).group(1))
                    if verb == "remove":
                        _remove_members(s, gid, [target])
                    elif verb in ("add", "replace"):
                        _add_members(s, gid, org_id, [target], actor)
                elif pl == "displayname" and verb in ("add", "replace"):
                    if isinstance(value, str) and value.strip():
                        new_name = value.strip()
                        if new_name != g.name and _name_taken(s, org_id, new_name, gid):
                            raise ScimError(
                                409, f"A group named {new_name!r} already exists.", "uniqueness"
                            )
                        g.name = new_name
                        s.add(g)
                elif pl == "externalid" and verb in ("add", "replace"):
                    g.external_id = (
                        (value or "").strip() or None if isinstance(value, str) else None
                    )
                    s.add(g)
        _commit_group_or_409(s, g.name)
        s.refresh(g)
        result = group_resource(g, _group_members(s, gid), base)
    audit.log(actor=actor, action="scim.group.update", target=g.name, detail="PATCH")
    return result


def _pending_members(s: Session, gid: int) -> tuple[dict[int, UserGroupMember], list]:
    """Membership edges of group `gid` as this session currently sees them: committed rows
    (patch_group runs under no_autoflush, so they ignore the ops applied earlier in the
    same PATCH) minus the rows already marked for delete, plus the rows already added.
    Returns ({user_id: edge}, pending_deleted_edges)."""
    deleted = [o for o in s.deleted if isinstance(o, UserGroupMember) and o.group_id == gid]
    gone = {id(o) for o in deleted}
    edges: dict[int, UserGroupMember] = {}
    for r in s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all():
        if id(r) not in gone:
            edges[r.user_id] = r
    for o in s.new:
        if isinstance(o, UserGroupMember) and o.group_id == gid:
            edges[o.user_id] = o
    return edges, deleted


def _add_members(s: Session, gid: int, org_id, user_ids: list[int], actor: str) -> None:
    present, deleted = _pending_members(s, gid)
    revive = {o.user_id: o for o in deleted}
    for uid in user_ids:
        if _user_in_org(s, uid, org_id) is None:
            raise ScimError(
                400, f"member {uid} is not a user in this organization.", "invalidValue"
            )
        if uid in present:
            continue
        if uid in revive:
            # remove then add of the same member in one PATCH: cancel the pending
            # delete (a delete + insert pair would hit the unique index at flush).
            s.expunge(revive.pop(uid))
        else:
            s.add(UserGroupMember(group_id=gid, user_id=uid, added_by=actor))
        present[uid] = None


def _remove_members(s: Session, gid: int, user_ids: list[int]) -> None:
    present, _deleted = _pending_members(s, gid)
    for uid in user_ids:
        row = present.get(uid)
        if row is None:
            continue
        if row in s.new:
            s.expunge(row)  # added earlier in this same PATCH: never persisted
        else:
            s.delete(row)


def delete_group(org_id, actor: str, raw_id) -> None:
    """DELETE /Groups/{id}. A group is pure directory metadata (no authz), so removing
    it (and its membership edges) is safe. The USERS are untouched."""
    gid = _int_id(raw_id)
    with Session(engine) as s:
        g = _group_in_org(s, gid, org_id)
        if g is None:
            raise ScimError(404, f"Group {raw_id} not found.")
        name = g.name
        for row in s.exec(select(UserGroupMember).where(UserGroupMember.group_id == gid)).all():
            s.delete(row)
        # FK ORDERING: UserGroupMember->UserGroup is NOT declared as an ORM relationship/
        # cascade, so at commit SQLAlchemy's unit-of-work has no dependency edge and can emit
        # the parent (usergroup) DELETE BEFORE the child (usergroupmember) DELETEs -> "FOREIGN
        # KEY constraint failed" 500 whenever the group has members (Okta group-unpush hits
        # this live). flush() forces the child DELETEs to the DB first, THEN delete the parent.
        s.flush()
        s.delete(g)
        s.commit()
    audit.log(actor=actor, action="scim.group.delete", target=name, detail="SCIM DELETE")


# --------------------------------------------------------------------------- #
# Read-only summaries for the CLI + web surfaces (in-process, trusted operator)
# --------------------------------------------------------------------------- #
def managed_user_count(org_id=None) -> int:
    with Session(engine) as s:
        stmt = select(User).where(User.auth_source == "scim")
        if org_id is not None:
            stmt = stmt.where(User.org_id == org_id)
        return len(list(s.exec(stmt).all()))


def group_count(org_id=None) -> int:
    with Session(engine) as s:
        stmt = select(UserGroup)
        if org_id is not None:
            stmt = stmt.where(UserGroup.org_id == org_id)
        return len(list(s.exec(stmt).all()))


def managed_users(org_id=None) -> list[dict]:
    """SCIM-managed accounts as SAFE metadata rows (id/email/name/active/external_id) for
    the `scim users` CLI + the Users-page badge helper. A user is 'SCIM-managed' when its
    auth_source is 'scim' OR it carries an external_id (adopted accounts)."""
    with Session(engine) as s:
        stmt = select(User).where((User.auth_source == "scim") | (User.external_id.is_not(None)))
        if org_id is not None:
            stmt = stmt.where(User.org_id == org_id)
        return [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "active": u.active,
                "external_id": u.external_id,
                "auth_source": u.auth_source,
            }
            for u in s.exec(stmt.order_by(User.email)).all()
        ]


def groups_with_members(org_id=None) -> list[dict]:
    """Every group + its member emails, for the `scim groups` CLI + the read-only web
    Groups section."""
    with Session(engine) as s:
        stmt = select(UserGroup)
        if org_id is not None:
            stmt = stmt.where(UserGroup.org_id == org_id)
        out = []
        for g in s.exec(stmt.order_by(UserGroup.name)).all():
            out.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "external_id": g.external_id,
                    "members": [m.email for m in _group_members(s, g.id)],
                }
            )
        return out


def is_scim_managed(user: User) -> bool:
    return security.auth_source_of(user) == "scim" or bool(getattr(user, "external_id", None))
