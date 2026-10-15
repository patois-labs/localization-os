"""Trust-host-auth — the embedded profile's tokenless auth door (Train 0b).

Per the composable-core decision record (#4, LOCKED): an embedded install may sit
BEHIND a host application whose own boundary already authenticates every caller, so
per-request `X-Loc-Token` headers on `/api` would be redundant. In that — and ONLY
that — shape, this module lets a TOKENLESS `/api` request resolve to a configured
trust principal instead of a 401.

THE DOUBLE CONDITION (both required, see `active()`):
  * `config.IS_EMBEDDED` — a boot-time env flag (LOC_TMS_EMBEDDED), frozen at import;
  * the `embedded_trust_host_auth` Setting == "1" — HOST-ONLY (on REMOTE_IMMUTABLE_KEYS,
    so no /api config route can ever flip the perimeter over the API it guards).

TWO INDEPENDENT LEVERS decide what the principal can DO (there is no single "scopes"
lever — the /api surface is gated by BOTH an API SCOPE and an RBAC ROLE, and they are
checked separately):
  * `LOC_TMS_TRUST_SCOPES` — the API-key SCOPE grant (space/comma list; default
    "api docs keys"). Gates the scope-checked surface: `docs` unlocks the document
    endpoints, `keys` the Delta-API/string-key endpoints, `admin` the R2/R3 admin
    scope-gate. Default excludes `admin`.
  * `LOC_TMS_TRUST_ROLE` — the RBAC ROLE (default "viewer" — least privilege). Gates
    the role-checked surface: `_write_block` (>= translator) guards content writes,
    `_manage_block` (>= manager) customer/admin-ops, `_ai_block` (admin/linguist) AI
    setup, `_admin_block`/`is_admin` the sharp admin verbs (user + apikey management,
    deletes). A `viewer` default therefore CANNOT reach ANY role-gated write — the
    default principal is READ-ONLY (plus whatever its scopes read). Set the role UP
    explicitly (`translator` for content writes, up to `admin` for full administration).
  * COUPLING LAW: the `admin` role is permitted ONLY when `admin` is ALSO in the
    scopes (else the boot refuses). So the sole way to reach the admin surface is an
    EXPLICIT role=admin + scopes-includes-admin opt-in; a leaked/default principal can
    reach neither the role-only-gated admin routes (e.g. `POST /api/apikeys/issue`,
    `is_admin` + docs scope — the "mint a durable key" perimeter escape) NOR the
    scope-gated ones. Even a full admin principal keeps the irreversible verbs behind
    their host-only `remote_admin_enabled` lock (unchanged, immutable-remote).

WHY A REAL, AUTO-PROVISIONED USER ROW (not a synthetic ident) — decided on HONESTY of
two seams:
  * audit attribution: every /api mutation is audited to `ident["actor"]`, and the
    multi-org audit view confines rows by `AuditLog.org_id`, stamped at write time from
    the actor's account (audit._actor_org_id, #487). A real User row makes the trust
    principal's email a first-class actor with an org, so attribution is honest AND
    org-scoped for free; a synthetic string would have no org and match no org's view.
  * org binding + RBAC: `deps._ident_org_id` reads `ident["org_id"]` (the token path
    derives it as `access.current_org_id(user)`), and every `_role_block` reads
    `ident["user"].role`. Reusing a real User row means org binding AND the entire role
    ladder flow through the EXACT same code as a normal key — no second, hand-rolled
    derivation to drift out of sync.
So `principal()` returns a dict SHAPED byte-for-byte like the per-user-key branch of
`deps.api_identity` (kind "user", a real User in `user`, org_id via current_org_id),
plus an `auth_kind: "trust"` marker the audit middleware reflects into the action so
trust-mode requests are distinguishable in the trail, plus an optional `on_behalf_of`
key (attribution annotation, present only when (b) is armed and a valid
`X-Loc-Trust-User` was sent — see `annotate_forwarded_actor`).

THE PRINCIPAL IS A DEDICATED IDENTITY, NOT A LOGIN. It is provisioned with
`auth_source="trust"` and no password. `configure_at_boot()`:
  * REFUSES to adopt an existing NON-trust row (a human/local/SSO/LDAP account) — a
    trust principal must be its own dedicated email, so it can never silently promote a
    real person's account to the trust role/org (only a row THIS module created before,
    auth_source=="trust", is re-asserted);
  * DEACTIVATES any superseded trust-source rows (a previous principal after a
    `LOC_TMS_TRUST_PRINCIPAL` rotation) so rotation never accumulates dormant admins.
The SSO/LDAP login seams additionally REFUSE to adopt a trust-source row (it is not a
login identity), so an IdP login as that email can never take over the principal.

FAIL LOUDLY AT BOOT, never silently at request time: `configure_at_boot()` (called from
app.main._startup on an embedded boot) validates the env and provisions the User, and
RAISES if the switch is on but the principal email is missing/invalid, an existing row
would be adopted, the role/scope coupling is violated, or multi-org is on with no valid
single-org binding. A boot that can't honour the switch does not start.

Env inputs (read at boot only):
  * LOC_TMS_TRUST_PRINCIPAL — the trust actor's email (REQUIRED when the switch is on;
    format-validated exactly like the SSO/LDAP identity seam).
  * LOC_TMS_TRUST_SCOPES — space/comma scope list; default "api docs keys".
  * LOC_TMS_TRUST_ROLE — the RBAC role; default "viewer". `admin` requires `admin` in
    the scopes.
  * LOC_TMS_TRUST_ORG_ID — REQUIRED when multi_org_enabled is on: the single org the
    principal binds to. Must name an existing Organization, or the boot refuses (no
    org-widening through the trust door). Ignored when multi-org is off.

Documented in: docs/architecture/identity-and-access.md
"""

import hashlib
import logging
import os
import secrets
import time

from app import config
from app.services import ids, settings

_logger = logging.getLogger("loc_tms.trust_auth")

#: The default per-surface scopes a trust principal carries. Deliberately EXCLUDES
#: "admin". Mirrors the init --issue-key default set.
DEFAULT_SCOPES = "api docs keys"

#: The default RBAC role — least privilege. A viewer-role principal cannot pass ANY
#: `_role_block`, so the default principal is read-only; an operator opts UP explicitly.
DEFAULT_ROLE = "viewer"

#: Boot-resolved trust configuration, frozen once by configure_at_boot():
#:   {"user_id": int, "email": str, "scopes": str}
#: or None when trust mode is not configured at boot. The role lives on the User row.
#: Read-only afterward — the request path (principal()) only ever reads it.
_TRUST: dict | None = None

#: (b) The boot-frozen shared-secret source restriction (phase-3 hardening), as the SHA-256
#: DIGESTS of an ACCEPT-LIST of concurrently-valid secrets (LOW-a: digests are frozen at boot,
#: never recomputed per request -- kills the per-request length-timing leak and is faster).
#: Empty tuple () when trust mode is off OR no secret is configured — every check tests
#: TRUTHINESS `if not _TRUST_HEADER_SECRET_DIGESTS` (an empty tuple is falsy), never a
#: sentinel, so a blank never re-opens the source gate. NEVER logged.
#:
#: TWO ENVS, back-compatible (MED-1). `LOC_TMS_TRUST_HEADER_SECRET` (SINGULAR) is a LITERAL
#: single value -- commas are part of the secret and are NEVER split, so upgrading an install
#: whose existing secret contains a comma keeps authenticating as the WHOLE value (the earlier
#: single-env comma-split silently broke a comma-bearing secret into independently-valid weak
#: fragments -> a proxy break + auth downgrade). `LOC_TMS_TRUST_HEADER_SECRETS` (PLURAL) is the
#: comma-separated ACCEPT-LIST for zero-cutover rotation (SPEC §4.5): configure "old,new",
#: migrate the proxy to "new", then drop "old" next restart -- BOTH honored during the overlap
#: (closes the R1-F9 rolling-restart 401 window). Precedence: SECRETS (if set non-blank) wins,
#: else SECRET. The multiplicity lives entirely on the APP's accept side; the proxy still
#: injects exactly ONE `X-Loc-Trust-Secret` header (M-7 still 401s two copies). Resolved once
#: at boot by `_resolve_header_secrets()`; see it for the per-element ASCII + fail-closed laws.
_TRUST_HEADER_SECRET_DIGESTS: tuple[bytes, ...] = ()

#: Throttle STATE (same shape/law as _REJECT_WARN/_DROP_WARN -- MED-3) for the "switch on at
#: REQUEST time but NOT resolved at boot" diagnostic (flipped on locally after startup, an
#: unsupported ordering). We fail closed (no principal -> 401) but say so, re-armed on the
#: monotonic window: a permanent one-shot could be PRE-BURNED before a real misconfiguration
#: needs diagnosing. Reset in configure_at_boot alongside the other throttles.
_UNCONFIGURED_WARN = {"next_at": 0.0, "suppressed": 0}

#: The coarse re-arm window (seconds, monotonic clock) for the two per-request
#: diagnostics below. A permanent per-boot latch was flagged by BOTH Identity re-gate
#: reviewers: an end-user behind an APPENDING proxy can PRE-BURN a one-shot latch early
#: (ride a bad secret / duplicate forwarded header) and silence the ops signal for the
#: entire boot — exactly when an operator later needs it to diagnose "why are my proxy's
#: calls 401ing". A time-based re-arm restores a live signal (at most one line per window,
#: each carrying the count suppressed during the window) while KEEPING the flood-safety the
#: latch existed for: an exposed port still produces at most one line per window, never
#: per-request. 10 minutes: rare enough never to flood a log, frequent enough that an
#: operator sees a fresh line within one window. NOT operator-configurable (surface hold).
_WARN_REARM_SECONDS = 600.0

#: Throttle STATE for the (c-reject) source-rejection diagnostic: when the (b) secret gate
#: rejects a tokenless request, warn (so an operator can diagnose "why are my proxy's calls
#: 401ing") at most once per _WARN_REARM_SECONDS, then re-arm — never per-request, which
#: would be a log-flood DoS on an exposed port. `next_at` is the monotonic deadline before
#: which further rejects are silently counted into `suppressed`. Reset in-place at the top
#: of configure_at_boot alongside the secret (so a fresh boot warns immediately).
_REJECT_WARN = {"next_at": 0.0, "suppressed": 0}

#: Throttle STATE (identical shape/law to _REJECT_WARN) for the dropped-forwarded-value
#: diagnostic (LOW-8-ii): when a PRESENT `X-Loc-Trust-User` value is dropped (invalid/
#: reserved/multi-copy), warn so an operator can diagnose a proxy that appends rather than
#: overwrites (an end-user could otherwise null their own audit attribution by riding a
#: duplicate header alongside the proxy's copy). Absent forwarded value != a drop, so no
#: warning. Throttled (not per-request) to avoid a log-flood DoS on an exposed port.
_DROP_WARN = {"next_at": 0.0, "suppressed": 0}


class TrustConfigError(RuntimeError):
    """Raised AT BOOT when embedded_trust_host_auth is on but the deployment cannot
    honour it (missing/invalid principal email, an existing non-trust row that would be
    adopted, a role/scope coupling violation, or multi-org on with no valid single-org
    binding). Fails the boot loudly rather than degrading silently at request time."""


def switch_on() -> bool:
    """The `embedded_trust_host_auth` Setting is "1" (read LIVE — it is a runtime
    Setting, unlike the frozen IS_EMBEDDED env flag)."""
    return settings.get("embedded_trust_host_auth") == "1"


def active() -> bool:
    """THE DOUBLE CONDITION: an embedded boot AND the switch on. Both required. When
    False, everything in this module is inert and api_identity's tokenless branch is
    byte-identical to before (returns None -> 401)."""
    return config.IS_EMBEDDED and switch_on()


# --------------------------------------------------------------------------- #
# Boot: validate + provision. Loud on misconfiguration.
# --------------------------------------------------------------------------- #
def configure_at_boot() -> None:
    """Resolve, validate and provision the trust principal when the double condition
    holds. Idempotent (safe on every boot). Called from app.main._startup AFTER the
    login wall is enabled (so multi_org_active reads correctly). Raises TrustConfigError
    on any misconfiguration — a boot that cannot honour the switch must not start.

    When trust mode is OFF (switch off, or a non-embedded boot) this is a strict no-op
    for behavior: ALL boot-frozen trust state (_TRUST, _TRUST_HEADER_SECRET_DIGESTS, and the
    _REJECT_WARN / _DROP_WARN / _UNCONFIGURED_WARN throttles) is reset to its inert default and
    no trust env is read, so a normal / Train-0a embedded boot is unaffected."""
    global _TRUST, _TRUST_HEADER_SECRET_DIGESTS
    # Reset ALL boot-frozen trust state unconditionally at the TOP (M-6): the suite calls
    # this ~20x/run, so a stale secret (or a live warning throttle) from a prior arm would
    # cause order-dependence. The (b) source gate is inert whenever _TRUST_HEADER_SECRET_DIGESTS
    # is empty, so a trust-off boot never enforces it. The throttle dicts are mutated in place
    # (not reassigned) so no `global` is needed for them and the module-level identity holds.
    _TRUST = None
    _TRUST_HEADER_SECRET_DIGESTS = ()
    _REJECT_WARN.update(next_at=0.0, suppressed=0)
    _DROP_WARN.update(next_at=0.0, suppressed=0)
    _UNCONFIGURED_WARN.update(next_at=0.0, suppressed=0)
    if not active():
        return

    from app.services import security

    raw_email = os.environ.get("LOC_TMS_TRUST_PRINCIPAL") or ""
    if not raw_email.strip():
        raise TrustConfigError(
            "embedded_trust_host_auth is ON but LOC_TMS_TRUST_PRINCIPAL is not set. "
            "Trust mode attributes EVERY tokenless /api request to this principal's "
            "email, so it cannot be inferred — set LOC_TMS_TRUST_PRINCIPAL to the host "
            "application's service identity, or turn the switch off on the host with "
            "`config set embedded_trust_host_auth 0`."
        )
    # Format-validate BEFORE anything is written (MEDIUM 4): reject CRLF/control-char/
    # over-long/malformed emails exactly like the SSO+LDAP identity seam, so a forged
    # value can never enter a User row or AuditLog.actor.
    try:
        email = security.validate_email(raw_email)
    except ValueError as exc:
        raise TrustConfigError(f"LOC_TMS_TRUST_PRINCIPAL is not a valid email: {exc}") from exc
    # A system/synthetic actor email would let the principal impersonate the scheduler/
    # system in the audit trail — refuse exactly like create_user does.
    if security.is_reserved_actor(email):
        raise TrustConfigError(
            f"LOC_TMS_TRUST_PRINCIPAL={email!r} is a reserved system identity and cannot be a "
            "trust principal (it would impersonate a system actor in the audit log)."
        )

    scopes = _resolve_scopes()
    role = _resolve_role(scopes)  # validates the role<->scope coupling; may raise
    org_id = _resolve_org()  # validates the multi-org binding; may raise

    # (b) source restriction (phase-3 hardening): resolve + VALIDATE the boot-frozen shared
    # secret(s) the host proxy injects on every tokenless request. Read ONCE at boot (like all
    # LOC_TMS_TRUST_* env); NEVER logged, never returned, never stored in the DB (env-only).
    # `_resolve_header_secrets` applies the MED-1 two-env back-compat, the MED-2 fail-closed on
    # a configured-but-empty accept-list, and the per-element ASCII refusal (may raise).
    accepted_secrets = _resolve_header_secrets()

    user_id = _provision_user(email, role, org_id)
    superseded = _deactivate_superseded_trust_users(user_id)
    # LOW-7 fail-closed ordering: freeze the source-gate digests IMMEDIATELY BEFORE arming
    # _TRUST, with NO statement that can raise between the two assignments -- so no future
    # edit can ever leave trust ARMED (_TRUST set) while the source gate is still wide open
    # (_TRUST_HEADER_SECRET_DIGESTS empty). Anything that can raise (_provision_user,
    # _deactivate_*, _resolve_header_secrets) runs above, before either global is set. LOW-a:
    # freeze the SHA-256 digests here so source_permitted never re-hashes a secret per request
    # (no per-request length-timing leak). sha256().digest() over an ASCII-validated str cannot
    # raise, so the fail-closed no-raise window holds.
    _TRUST_HEADER_SECRET_DIGESTS = tuple(
        hashlib.sha256(s.encode("utf-8", "surrogatepass")).digest() for s in accepted_secrets
    )
    _TRUST = {"user_id": user_id, "email": email, "scopes": scopes}
    _logger.info(
        "trust-host-auth ENABLED: tokenless /api requests resolve to %r (role=%s, "
        "scopes=%r, org_id=%s). This install trusts the host application's boundary to "
        "authenticate every caller.",
        email,
        role,
        scopes,
        org_id,
    )
    if superseded:
        _logger.warning(
            "trust-host-auth: deactivated %d superseded trust principal(s) after a "
            "LOC_TMS_TRUST_PRINCIPAL change: %s",
            len(superseded),
            ", ".join(superseded),
        )
    if not _TRUST_HEADER_SECRET_DIGESTS:
        # (c-boot) no-secret warning — per configure_at_boot() call, NO latch (M-8). One
        # boot = one call, so no latch is needed and the ~20 harness calls stay testable.
        _logger.warning(
            "trust-host-auth is ARMED without LOC_TMS_TRUST_HEADER_SECRET(S): every tokenless "
            "caller that can reach /api is trusted. Set the secret (a long random value the "
            "host proxy injects on every request) unless a network boundary guarantees only "
            "the host proxy reaches this port."
        )
    elif any(len(s) < 32 for s in accepted_secrets):
        # Weak-secret nudge (M-5), per accepted value: a boot WARNING, not a raise — a short
        # secret is brute-forceable if the port is ever exposed. Fires when ANY accepted value
        # is short (an accept-list is only as strong as its weakest member).
        _logger.warning(
            "trust-host-auth: a configured trust header secret is shorter than 32 characters. "
            "Use long, random secret(s) (each is the only thing standing between a "
            "directly-reachable /api port and a trusted request)."
        )


def _resolve_header_secrets() -> list:
    """Resolve the (b) source-restriction accept-list from env, VALIDATED. Returns the list of
    accepted secret strings (0..N) or raises TrustConfigError. Read ONCE at boot; never logged.

    TWO ENVS, back-compatible (MED-1):
      * ``LOC_TMS_TRUST_HEADER_SECRET`` (SINGULAR) -- a LITERAL single value. Commas are part
        of the secret and are NEVER split, so upgrading an install whose existing secret
        CONTAINS a comma keeps authenticating as the whole value (the earlier single-env
        comma-split silently broke a comma-bearing secret into independently-valid weak
        fragments -> proxy break + auth downgrade). Blank-after-strip => not configured.
      * ``LOC_TMS_TRUST_HEADER_SECRETS`` (PLURAL) -- the comma-separated ACCEPT-LIST for
        zero-cutover rotation (SPEC §4.5). Blank elements (a trailing/double comma) are dropped.

    PRECEDENCE: if SECRETS is set to a non-blank value it is the SOLE source; else SECRET.

    FAIL CLOSED (MED-2): a SECRETS value that is present-and-non-blank but yields ZERO elements
    after dropping blanks (``","`` / ``",,,"`` / ``" , "``) is a configured-but-empty accept-list
    -- it must NOT silently disarm the gate, so the boot is REFUSED.

    PER-ELEMENT ASCII refusal (MED-3/F-3): each accepted value must be ``isascii() AND
    isprintable()``. Starlette latin-1-decodes the wire header, so a non-ASCII configured secret
    can never match the re-encoded bytes; and an un-sendable ASCII control char (a C0 control,
    newline, or DEL) that ``.strip()`` leaves in the interior can never appear in a real header
    value either -- both would silently BRICK that secret (boot clean, then 401 forever). Refuse
    loudly at boot; the request-time digest compare therefore only ever sees ASCII secrets."""

    def _refuse_nonascii(value: str, env_name: str) -> None:
        if not (value.isascii() and value.isprintable()):
            raise TrustConfigError(
                "{} must be ASCII printable characters only. A non-ASCII secret cannot be "  # noqa: UP032
                "compared against the latin-1-decoded wire header, and an un-sendable ASCII "
                "control character (a C0 control, newline, or DEL -- .strip() does not remove "
                "one from the interior) can never appear in a real header value either; both "
                "would silently brick trust auth. Use a long, random ASCII printable secret.".format(  # noqa: E501
                    env_name
                )
            )

    raw_plural = os.environ.get("LOC_TMS_TRUST_HEADER_SECRETS")
    if raw_plural is not None and raw_plural.strip() != "":
        # The PLURAL accept-list env is configured -> it is the SOLE source (precedence).
        out: list = []
        for element in raw_plural.split(","):
            element = element.strip()
            if not element:
                continue  # blank element (trailing/double comma) dropped
            _refuse_nonascii(element, "LOC_TMS_TRUST_HEADER_SECRETS")
            out.append(element)
        if not out:
            raise TrustConfigError(  # MED-2 fail-closed
                "LOC_TMS_TRUST_HEADER_SECRETS is set but contains no non-blank value (only "
                "commas/whitespace). A configured-but-empty accept-list would silently DISARM "
                "the (b) source gate -- the boot is refused instead. Provide at least one "
                "secret, or unset LOC_TMS_TRUST_HEADER_SECRETS (use LOC_TMS_TRUST_HEADER_SECRET "
                "for a single literal value)."
            )
        return out
    # SINGULAR literal (commas are part of the value, NEVER split -- MED-1 back-compat).
    raw_single = (os.environ.get("LOC_TMS_TRUST_HEADER_SECRET") or "").strip()
    if not raw_single:
        return []  # not configured => 0b + c-boot warning
    _refuse_nonascii(raw_single, "LOC_TMS_TRUST_HEADER_SECRET")
    return [raw_single]


def _resolve_scopes() -> str:
    """The configured trust scopes, validated against ALL_SCOPES. Blank env -> the
    DEFAULT_SCOPES set. An unknown scope token is a loud boot failure (never a silent
    partial grant)."""
    from app.services import apikeys

    raw = (os.environ.get("LOC_TMS_TRUST_SCOPES") or "").strip()
    scopes = raw if raw else DEFAULT_SCOPES
    unknown = sorted(apikeys.scope_set(scopes) - apikeys.ALL_SCOPES)
    if unknown:
        raise TrustConfigError(
            "LOC_TMS_TRUST_SCOPES lists unknown scope(s): {}. Valid scopes are: {}.".format(
                ", ".join(unknown), ", ".join(sorted(apikeys.ALL_SCOPES))
            )
        )
    return scopes


def _resolve_role(scopes: str) -> str:
    """The configured RBAC role, validated against Role.ALL. Blank env -> DEFAULT_ROLE
    (viewer, least privilege). THE COUPLING LAW: the `admin` role is permitted only when
    `admin` is ALSO in the granted scopes — so the admin surface (role-gated AND
    scope-gated) is reachable only by an explicit both-levers opt-in, never by a
    default/partial principal."""
    from app.models import Role
    from app.services import apikeys

    role = (os.environ.get("LOC_TMS_TRUST_ROLE") or "").strip().lower() or DEFAULT_ROLE
    if role not in Role.ALL:
        raise TrustConfigError(
            "LOC_TMS_TRUST_ROLE={!r} is not a valid role. Valid roles are: {}.".format(
                role, ", ".join(Role.ALL)
            )
        )
    if role == Role.ADMIN and "admin" not in apikeys.scope_set(scopes):
        raise TrustConfigError(
            "LOC_TMS_TRUST_ROLE=admin requires the `admin` scope in LOC_TMS_TRUST_SCOPES "
            "(the admin surface is gated by BOTH the admin role and the admin scope; "
            "granting the role without the scope is refused so the coupling stays "
            "explicit). Add `admin` to LOC_TMS_TRUST_SCOPES, or choose a lower role."
        )
    return role


def _resolve_org() -> int | None:
    """The single org the trust principal binds to, or None with multi-org off.

    Multi-org OFF (the default / single-tenant): None — no scoping, byte-identical to a
    normal install. Multi-org ON: LOC_TMS_TRUST_ORG_ID is REQUIRED and must name an
    EXISTING Organization; anything else is a loud boot failure. This is the "bind to
    one configured org or refuse at boot" rule — there is no way to widen the principal
    across orgs through the trust door."""
    if settings.get("multi_org_enabled") != "1":
        return None
    raw = (os.environ.get("LOC_TMS_TRUST_ORG_ID") or "").strip()
    if not raw:
        raise TrustConfigError(
            "multi_org_enabled is ON and embedded_trust_host_auth is ON, but "
            "LOC_TMS_TRUST_ORG_ID is not set. A trust principal must bind to exactly ONE "
            "org (no org-widening through the trust door) — set LOC_TMS_TRUST_ORG_ID to "
            "the org id the host application acts within."
        )
    org_id = ids.coerce_id(raw)
    if org_id is None:
        raise TrustConfigError(f"LOC_TMS_TRUST_ORG_ID={raw!r} is not a valid organization id.")
    if not _org_exists(org_id):
        raise TrustConfigError(
            "LOC_TMS_TRUST_ORG_ID=%d does not match any organization; the trust "  # noqa: UP031
            "principal cannot bind to a non-existent org." % org_id
        )
    return org_id


def _org_exists(org_id: int) -> bool:
    from sqlmodel import Session

    from app.db import engine
    from app.models import Organization

    with Session(engine) as s:
        return s.get(Organization, org_id) is not None


def _provision_user(email: str, role: str, org_id: int | None) -> int:
    """Get-or-create the trust principal's User row: `role`, active, bound to `org_id`
    (None single-tenant), no password (auth_source "trust").

    NEVER ADOPTS A NON-TRUST ROW (HIGH 2): if an existing row with this email is NOT a
    trust identity (a human/local/SSO/LDAP account) the boot REFUSES — a trust principal
    must be a dedicated email, so it can never silently promote a real person's account
    to the trust role/org or re-activate a disabled human account. Only a row THIS module
    previously created (auth_source == "trust") is re-asserted into the current shape
    (role/active/org), so a boot can never inherit a stale demoted/disabled trust row.
    Returns the user id."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import User

    # #74: normalize at the seam that CONSTRUCTS the row -- the caller already routes
    # LOC_TMS_TRUST_PRINCIPAL through security.validate_email, so this is a
    # by-construction guard, not a behaviour change.
    from app.services import security as _security

    email = _security.normalize_email(email)
    with Session(engine) as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if (
            user is not None
            and user.auth_source != "trust"
            and _security.auth_source_of(user) == "trust"
        ):
            # Gate sso-localpw round 3, LOW-1: only an EXACT "trust" row (one this module
            # wrote) is re-asserted. A hand-edited spelling such as " Trust " is refused, not
            # repaired: re-asserting sets active=True, so repairing would re-activate a row
            # someone disabled by hand. The operator fixes the value and restarts.
            raise TrustConfigError(
                f"LOC_TMS_TRUST_PRINCIPAL={email!r} names an account whose sign-in source is "
                f"{user.auth_source!r}, not exactly 'trust'. That value was not written by "
                "this app. Set it to exactly 'trust' (or choose another email) and restart."
            )
        if user is not None and _security.auth_source_of(user) != "trust":
            raise TrustConfigError(
                f"LOC_TMS_TRUST_PRINCIPAL={email!r} already belongs to a '{user.auth_source}' account. A trust "  # noqa: E501
                "principal must be a DEDICATED service identity, never an adopted human/"
                "local/SSO/LDAP account (adopting it would force it to the trust role/org "
                "and could re-activate a disabled account). Choose an email that is not an "
                "existing login."
            )
        if user is None:
            if _security.lookalike_account_id(email) is not None:
                raise TrustConfigError(
                    f"LOC_TMS_TRUST_PRINCIPAL={email!r}: " + _security.LOOKALIKE_EMAIL_MESSAGE
                )
            user = User(
                email=email,
                name=email,
                role=role,
                active=True,
                org_id=org_id,
                auth_source="trust",
                password_hash="",
            )
            created = True
        else:
            user.role = role
            user.active = True
            user.org_id = org_id
            user.auth_source = "trust"
            created = False
        s.add(user)
        s.commit()
        s.refresh(user)
        _logger.info(
            "trust-host-auth: %s trust principal row for %r (role=%s)",
            "created" if created else "re-asserted",
            email,
            role,
        )
        return user.id


def _deactivate_superseded_trust_users(keep_id: int) -> list:
    """Deactivate every OTHER active trust-source User row (HIGH 3): after a
    LOC_TMS_TRUST_PRINCIPAL rotation the previous principal(s) must not linger as live
    admin/SSO-adoptable rows. Returns the deactivated emails (for the boot log). Only
    touches rows whose normalized auth_source is "trust" — a real account is never affected."""
    from sqlmodel import Session, func, select

    from app.db import engine
    from app.models import User
    from app.services.security import auth_source_of

    deactivated = []
    with Session(engine) as s:
        # Match on the NORMALIZED source (gate sso-localpw round 3, INFO-2), so a hand-edited
        # " Trust " row that is no longer the principal is deactivated too. The SQL narrows
        # to rows that contain the word; auth_source_of decides.
        candidates = s.exec(
            select(User).where(
                func.lower(User.auth_source).contains("trust"),
                User.id != keep_id,
                User.active == True,  # noqa: E712
            )
        ).all()
        rows = [r for r in candidates if auth_source_of(r) == "trust"]
        for r in rows:
            r.active = False
            s.add(r)
            deactivated.append(r.email)
        if deactivated:
            s.commit()
    return deactivated


# --------------------------------------------------------------------------- #
# Request: resolve the principal for a tokenless call.
# --------------------------------------------------------------------------- #
def principal() -> dict | None:
    """The trust principal for a TOKENLESS /api request, or None when trust mode is not
    active (byte-identical to the historical tokenless -> None -> 401 path).

    The returned dict is shaped byte-for-byte like the per-user-key branch of
    deps.api_identity (kind "user", real User in `user`, scopes, org_id via
    access.current_org_id), plus `auth_kind: "trust"`. It reloads the User each call and
    re-checks active + org-active EXACTLY as the token path does (deps.py P7), so a
    demoted/disabled/suspended-org principal is rejected the same way a token would be.
    The principal's RBAC ROLE lives on the User row, so every `_role_block` sees it."""
    if not active():
        return None
    if _TRUST is None:
        # The switch is on now but was NOT resolved at boot — it was flipped on locally
        # after startup (an unsupported ordering; the perimeter switch is a boot-time
        # decision). Fail CLOSED (no principal -> 401), but say so — THROTTLED, not a
        # permanent one-shot (MED-3): a pre-burned latch would go silent on the real
        # misconfiguration an operator later needs to diagnose. _warn_due re-arms on the
        # monotonic window and carries a suppressed-count.
        n = _warn_due(_UNCONFIGURED_WARN)
        if n is not None:
            _logger.warning(
                "embedded_trust_host_auth is ON but no trust principal was provisioned "
                "at boot (the switch was flipped on after startup). Tokenless /api "
                "requests will keep getting 401. Set the switch on the host BEFORE boot "
                "(and restart) so LOC_TMS_TRUST_PRINCIPAL is validated + provisioned. "
                "(This warning is throttled to at most one line per %d minutes%s.)",
                int(_WARN_REARM_SECONDS // 60),
                (" (%d more suppressed)" % n) if n else "",  # noqa: UP031
            )
        return None

    from sqlmodel import Session

    from app.db import engine
    from app.models import User
    from app.services import access

    with Session(engine) as s:
        user = s.get(User, _TRUST["user_id"])
    if user is None or not user.active:
        return None
    # Mirror the token path's multi-org P7 check: a SUSPENDED org's principal is rejected
    # (treated like an unknown token). No-op with the gate off (user_org_active -> True).
    if not access.user_org_active(user):
        return None
    return {
        "kind": "user",
        "actor": user.email,
        "user": user,
        "scopes": _TRUST["scopes"],
        # HOME org (linchpin): None with the multi-org gate off, resolved off the
        # real User row when on — identical to the per-user-key path.
        "org_id": access.current_org_id(user),
        # Distinguishes trust-mode requests in the audit trail (the middleware
        # reflects this into the audited action). Absent on every token identity.
        "auth_kind": "trust",
    }


def _warn_due(state: dict) -> "int | None":
    """Coarse monotonic re-arm gate for a per-request diagnostic (replaces a permanent
    one-shot latch). Returns the count SUPPRESSED since the last emitted line (>= 0) when a
    line is DUE — the first call, or once _WARN_REARM_SECONDS have elapsed since the last —
    and re-arms the window; returns None to SUPPRESS (a further event inside the same
    window), counting it so the next due line can report it.

    Cheap + lock-free: reached ONLY on the reject/drop error path (the armed-off and the
    happy paths return before any warn), so a default install never touches it. The suppress
    branch is one monotonic read + one int increment, no allocation. No lock is taken: a
    benign concurrent race can at worst under/over-count the `suppressed` integer in a log
    line or emit a redundant line — never a crash, never a flood (the window still bounds
    it). `next_at` starts at 0.0, so the very first call (monotonic() >= 0) always emits."""
    now = time.monotonic()
    if now < state["next_at"]:
        state["suppressed"] += 1
        return None
    suppressed = state["suppressed"]
    state["suppressed"] = 0
    state["next_at"] = now + _WARN_REARM_SECONDS
    return suppressed


# --------------------------------------------------------------------------- #
# (b) Source restriction + (a) attribution annotation (phase-3 hardening).
# Both live ENTIRELY inside the tokenless trust branch (deps.api_identity). Neither
# widens the identity swap surface: source_permitted is a bool GUARD (resolves no
# identity, not registrable) and annotate_forwarded_actor only mutates an already-
# resolved dict — the Train-6 "not a peer adapter" guarantee rests on the registry
# refusal + get()-only, not on any function's arity.
# --------------------------------------------------------------------------- #
def source_permitted(request) -> bool:
    """(b) The boot-frozen shared-secret source gate for the tokenless trust branch.

    Returns True (permit — behave exactly as 0b) UNLESS a secret is configured AND the
    request's `X-Loc-Trust-Secret` header matches NONE of the accept-list values. This helper
    can only turn a would-be-trusted request into a 401, NEVER the reverse (directionality
    law): a False makes deps' tokenless branch return None -> 401.

    B-2: the FIRST condition is `if not _TRUST_HEADER_SECRET_DIGESTS: return True` (the
    boot-FROZEN digests, not a live Setting read) — NO active() short-circuit. When trust is
    off, the boot reset _TRUST_HEADER_SECRET_DIGESTS to (), so this returns True and principal()
    alone produces the 401. Dropping the active() read removes a second live read and its
    TOCTOU; a boot-armed-then-switched-off install firing the throttled reject warning on an
    already-doomed 401 is correct diagnostics, not a bug.

    ROTATION (SPEC §4.5 accept-list): the header is compared against EVERY value in the
    boot-frozen accept-list; a match on ANY (old OR new mid-rotation) permits. The compare is
    a FIXED-LENGTH SHA-256 DIGEST compare (H-2): Starlette decodes headers latin-1, and a raw
    `secrets.compare_digest(str, str)` RAISES TypeError on any non-ASCII input -> an
    unauthenticated 500, an is-armed enumeration oracle, and a non-ASCII *configured* secret
    would brick all of /api. Hashing the header to an equal-length digest and comparing it
    against the BOOT-FROZEN accept-list digests (LOW-a: computed once at boot, never here) makes
    every input comparable in constant time AND removes the length side-channel; iterating
    without an early return keeps the MATCH POSITION out of the timing too. (The same latent
    compare_digest-on-latin-1 bug pre-existed at deps.py's connector-token compare and main.py
    — chipped as task_3cc0e09a, fixed as a sibling follow-up.)

    The header is read via getlist: MISSING, blank-after-strip, MISMATCH-of-all, or >1 copy
    all => False (M-5/M-7) — a client-injected duplicate riding alongside the proxy's copy
    cannot smuggle a value in (the multiplicity that rotation needs lives on the APP's
    accept-list, never on a second header copy — the §4.5 two-value-overlap footgun)."""
    if not _TRUST_HEADER_SECRET_DIGESTS:
        return True
    sent = None
    values = request.headers.getlist("X-Loc-Trust-Secret")
    if len(values) == 1:
        sent = (values[0] or "").strip()
    if sent:
        sent_digest = hashlib.sha256(sent.encode("utf-8", "surrogatepass")).digest()
        matched = False
        for accepted_digest in _TRUST_HEADER_SECRET_DIGESTS:
            # No early return: compare against EVERY boot-frozen accept-list DIGEST so the MATCH
            # POSITION is not exposed by timing (belt-and-suspenders — the values are
            # independent, high-entropy, and the list is tiny). A match on ANY permits.
            if secrets.compare_digest(sent_digest, accepted_digest):
                matched = True
        if matched:
            return True
    # missing / blank / mismatch-of-all / multi-copy => reject.
    n = _warn_due(_REJECT_WARN)
    if n is not None:
        # (c-reject) throttled to at most one line per _WARN_REARM_SECONDS, then re-armed —
        # never per-request (a log-flood DoS on an exposed port) and never a permanent
        # one-shot (an appending-proxy end-user could otherwise pre-burn it). Carries the
        # count suppressed during the window so the signal stays honest.
        _logger.warning(
            "trust-host-auth: rejecting a tokenless /api request because LOC_TMS_TRUST_"
            "HEADER_SECRET is configured but the request's X-Loc-Trust-Secret header is "
            "missing, wrong, or duplicated. The host proxy MUST inject the matching secret "
            "(overwrite, not append) on every request. This warning is throttled to at most "
            "one line per %d minutes to avoid a log-flood DoS on an exposed port%s.",
            int(_WARN_REARM_SECONDS // 60),
            (" (%d more suppressed since the last line)" % n) if n else "",  # noqa: UP031
        )
    return False


def annotate_forwarded_actor(ident: dict, request) -> None:
    """(a) The attribution-only audit annotation, HARD-COUPLED to (b) (H-1).

    Records the host-supplied end-user identity (fixed header `X-Loc-Trust-User`) as an
    ADDITIVE `ident["on_behalf_of"]` key, which the audit middleware records in the audit
    `detail` column (HIGH-1 -- never the attacker-influenceable free-form `action`). It
    grants ZERO authority: scopes/role/user/org_id/auth_kind all come
    from the boot-frozen _TRUST + the real principal User row, and `ident["actor"]` stays
    the trust principal (org-filtering, job-org isolation, inbox placement, and budget
    routing all keep saying the principal — they are ownership/routing/isolation keys, not
    labels). Mutation ONLY; nothing else on the dict changes.

    HARD-COUPLED to (b) (H-1, the sharpest finding): return IMMEDIATELY unless the (b)
    secret is configured. Without this, every existing 0b install would start honoring
    `X-Loc-Trust-User` the moment this train merges, letting an inside attacker seal a
    named colleague into the hash-chained audit record (a repudiation/framing vector) and
    breaking §8 byte-neutrality. So the forwarded label is only ever read on a request the
    trusted proxy already gate-stamped.

    The value runs the TWO-gate sequence exactly like configure_at_boot (validate_email
    AND the RESERVED_ACTORS refusal — H-3): validate_email tolerates a bare word, so an
    unguarded `X-Loc-Trust-User: system` would pass and impersonate a system actor in the
    trail (0b refused exactly this). ANY failure (CRLF/control/over-long/reserved/malformed/
    absent/blank/multi-copy) => SILENT drop, no annotation, NEVER a raise (a per-request
    raise is a DoS). A PRESENT-but-dropped value additionally fires the throttled drop
    diagnostic (LOW-8-ii); an ABSENT header is not a drop and warns nothing.

    `request` must be a real (Starlette) request exposing `headers.getlist` — the only
    caller is deps.api_identity's tokenless branch, which always passes one (F9: a
    dict-headers duck-typed stub would AttributeError here; that is intentional — the
    guard rests on a real request, and swallowing it with getattr would mask a real bug).
    (F-B7: dict-headers request stubs DO exist elsewhere — verify_multiorg.py:1094/1105/
    1289 — but are safe only INCIDENTALLY: each carries an `X-Loc-Token`, so it resolves on
    the token path and never enters this tokenless trust branch, and `source_permitted`
    short-circuits on the empty accept-list before it ever touches headers. A FUTURE
    tokenless dict-stub caller WOULD hit the AttributeError here — pass a real request.)"""
    if not _TRUST_HEADER_SECRET_DIGESTS:
        return
    values = request.headers.getlist("X-Loc-Trust-User")
    if not values:
        # absent => nothing was forwarded; NOT a drop (no warning).
        return
    if len(values) != 1:
        # multi-copy (>1) => no annotation (M-7); a present-but-dropped value.
        _warn_dropped_obo()
        return
    raw = (values[0] or "").strip()
    if not raw:
        _warn_dropped_obo()
        return
    from app.services import security

    try:
        email = security.validate_email(raw)
    except ValueError:
        # CRLF/control/over-long/malformed => ignored (never written, never a raise).
        _warn_dropped_obo()
        return
    if security.is_reserved_actor(email):
        # A reserved system identity would impersonate the scheduler/system in the trail.
        _warn_dropped_obo()
        return
    ident["on_behalf_of"] = email


def _warn_dropped_obo() -> None:
    """Fire the (LOW-8-ii) diagnostic when a PRESENT `X-Loc-Trust-User` value is dropped.
    Throttled to at most one line per _WARN_REARM_SECONDS then re-armed (never per-request,
    to avoid a log-flood DoS on an exposed port; never a permanent one-shot, so an
    appending-proxy end-user cannot pre-burn it) — like the (c-reject) warning. The message
    names NO value (the dropped input may be attacker-controlled and must never be logged)."""
    n = _warn_due(_DROP_WARN)
    if n is None:
        return
    _logger.warning(
        "trust-host-auth: dropped a forwarded X-Loc-Trust-User value (invalid, reserved, "
        "or duplicated); no on-behalf-of attribution was recorded for it. If your proxy "
        "APPENDS rather than overwrites X-Loc-Trust-User, a client copy can ride alongside "
        "it and null the attribution; the proxy MUST overwrite (not append) the header. "
        "This warning is throttled to at most one line per %d minutes to avoid a log-flood "
        "DoS on an exposed port%s.",
        int(_WARN_REARM_SECONDS // 60),
        (" (%d more suppressed since the last line)" % n) if n else "",  # noqa: UP031
    )
