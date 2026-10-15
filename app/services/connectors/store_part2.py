"""store: part 2 (layered pure move)

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    Connection,
    ConnStatus,
    DocStatus,
    Document,
    Project,
    TransMode,
    User,
)
from app.services import audit
from app.services.connectors import connector_class, oauth
from app.services.connectors.base import Connector, ConnectorError
from app.services.connectors.store_common import (
    CONFIG_LOCK,
    SECRET_LOCK,
    _add_missing_locales,
    _canonical_locale,
    _coerce_id,
    _confine_customer_id,
    _kind_enabled,
    _log,
    _looks_like_auth_error,
    _refresh_lock,
    _resolve_content_type_id,
    _set_status,
    _token_fresh,
    auth_mode,
    connection_project_archived,
    dec_secret,
    effective_locales,
    enc_secret,
    get,
)
from app.services.connectors.tenant_host import TENANT_HOSTS, tenant_host
from app.services.credential_origin import endpoint_origin

log = logging.getLogger("loc_tms.connectors")


def _validate_config_urls(cfg, previous=None):
    from app.output_safety import InvalidOutputURL, safe_http_url

    for key, value in (cfg or {}).items():
        if previous is not None and value == previous.get(key):
            continue
        if key == "base_url" and value not in (None, "") and not safe_http_url(value):
            raise InvalidOutputURL(
                f"{key} must be an absolute http:// or https:// URL with a host."
            )


def is_oauth(conn) -> bool:
    """True when a Connection's stored secret is an OAuth (v2) secret."""
    if not conn:
        return False
    return auth_mode(dec_secret(conn.secret_enc)) == "oauth"


def set_oauth_secret(cid: int, oauth: dict, *, expected_scope=None, user=None) -> bool:
    """Persist an OAuth (v2) secret for a connection under SECRET_LOCK.

    Used by the callback (chunk 3) and reused by refresh persistence: re-reads the
    row inside the lock and REPLACES secret_enc wholesale with the v2 shape.
    Callbacks supply expected_scope: authorization and CONNECTED status then
    share the credential transaction, serialized with connection scope edits.
    Trusted internal/API credential writers retain their existing contract."""
    with SECRET_LOCK, Session(engine) as s:
        row = _locked_connection(s, cid)
        if not row:
            return False
        if expected_scope is not None:
            from app.services import security
            from app.services.connectors.oauth import connection_authorized

            if security.auth_required() and user is not None:
                user = s.exec(select(User).where(User.id == user.id).with_for_update()).first()
            if not connection_authorized(row, user, expected_scope):
                return False
        # ffadmin Wave 5 (HIGH backstop): a direct credential write for a DISABLED
        # kind is refused. set_oauth_secret bypasses build/build_fresh (it is a plain
        # DB write), so without this a disabled kind could still have a fresh token
        # persisted -- e.g. an OAuth flow started before the kind was disabled and
        # whose callback lands after. complete_authorize refuses FIRST (before any
        # token egress), so this is defense-in-depth for any other writer.
        if not _kind_enabled(row.kind):
            raise ConnectorError(
                f"connector kind '{row.kind}' is disabled (settings connector_disabled_kinds)."
            )
        row.secret_enc = enc_secret({"auth_mode": "oauth", "oauth": oauth or {}})
        if expected_scope is not None:
            row.status = ConnStatus.CONNECTED
            row.last_message = "Connected via OAuth."
            row.last_test = datetime.utcnow()
        s.add(row)
        s.commit()
        return True


def content_type_attachable(ct_id, org_id, customer_id) -> bool:
    """True iff `ct_id` may be attached to a connection scoped to (org_id, customer_id)
    -- the SAME resolution create()/update() use internally via
    `_resolve_content_type_id`/`content_types.attachable`. Exposed read-only (no DB
    write) so a caller can PRE-FLIGHT an explicitly-requested content type before
    create()/update()'s own contract silently drops an invalid/out-of-scope id to None
    (SPEC-content-types phase-3 re-review LOW: the CLI wants a clean error on an
    EXPLICIT flag; create()/update()'s silent-drop contract itself is UNCHANGED by
    this helper -- it only lets a caller ask the seam's answer ahead of time).

    `customer_id` accepts the same raw forms create()/update() do (int, digit string,
    blank/None) and is coerced identically via `_coerce_id`. Blank/None `ct_id` ->
    True (nothing to validate -- matches "leave untyped")."""
    if not ct_id:
        return True
    with Session(engine) as s:
        from app.services import content_types

        return (
            content_types.attachable(s, ct_id, org_id=org_id, customer_id=_coerce_id(customer_id))
            is not None
        )


def _drop_server_owned(cfg) -> dict:
    """Caller-supplied config minus the keys only a run may write. `*_branch_state` is the
    git core's record of the commit it last wrote (git_core._choose_branch): a forged one
    would let a run build on a branch it never created."""
    return {k: v for k, v in (cfg or {}).items() if not str(k).endswith("_branch_state")}


def _normalize_secret(kind: str, secret):
    """Validate and canonicalise a credential at the ONE seam every writer passes (create,
    update, rotate). Google Docs: the service account key is checked (service_account type,
    client_email, private_key, token_uri pinned to Google) and stored as one line of minified
    JSON, so a pretty-printed paste, a single-line form field and a CLI stdin value are the same
    thing at rest. Anything else is returned untouched. Raises ConnectorError without echoing
    the pasted text."""
    if kind != "gdocs" or not isinstance(secret, dict):
        return secret
    raw = secret.get("service_account_json")
    if not str(raw or "").strip():
        return secret
    from app.services.connectors.gdocs_google import normalize_service_account

    return {**secret, "service_account_json": normalize_service_account(raw)}


def create(
    kind: str,
    name: str,
    cfg: dict,
    secret: dict,
    actor: str = "",
    org_id=None,
    customer_id=None,
    content_type_id=None,
    bound_customer_id=None,
) -> Connection:
    """Create a connection. `org_id`, when given (the creating user's org in
    multi-org mode — leak vector 10), stamps the new row so it is bound to the
    creator's org and its /connection/{cid}/* routes become org-scoped; None (the
    gate off) leaves org_id NULL for the Default-org backfill, byte-identical to
    today.

    `customer_id` (P4, item 5), when a digit/int, scopes the connection to ONE
    customer — it's then only offered inside that customer's contexts; None/blank
    (the default, and every pre-P4 caller) leaves it global/unassigned, byte-
    identical to today.

    `bound_customer_id` (task_bcae962f), when given, is the CALLER's own customer
    binding — the connection's customer scope is then CONFINED to it (a bound caller
    can never scope a connection to a DIFFERENT customer or to global, even with a
    forged customer_id). None (internal staff / the trusted in-process CLI operator /
    auth off) leaves `customer_id` unchanged — byte-identical to today. See
    _confine_customer_id; this is THE seam that gives web, CLI and /api a uniform fix.

    `content_type_id` (SPEC-content-types phase 3, §6), when a digit/int, is the
    default content type copied onto the auto-created sync project in _ensure_project;
    None/blank leaves the auto-created project untyped (General). It is VALIDATED at this
    seam (Findings 1+2) against the connection's resolved (org_id, customer_id) — an
    out-of-scope id is dropped to None so it can never be planted cross-customer/-org."""
    cfg = _drop_server_owned(cfg)
    _validate_config_urls(cfg)
    _refuse_unreadable_destination(kind, cfg)
    secret = _normalize_secret(kind, secret)
    if kind == "helpscout":
        # MED-3: reject an invalid collection_map partition at the store seam, so web +
        # CLI + api all inherit the same guard (raises ConnectorError loudly).
        from app.services.connectors.helpscout import validate_config as _hs_validate_config

        _hs_validate_config(cfg or {})
    cust_id = _confine_customer_id(customer_id, bound_customer_id)
    # #73: _resolve_content_type_id -> content_types.attachable now fails closed on a bare
    # None org_id under an active gate, so the CLI --operator create door threads
    # access.UNSCOPED (op_org_scope) here -- resolution keeps the raw scope (UNSCOPED
    # sweeps every org), while the row is STAMPED via org_scope_value (which collapses the
    # UNSCOPED sentinel back to None; identity on a real id / -1 / None) so the persisted
    # org is byte-identical for every web/api caller.
    from app.services import access

    with Session(engine) as s:
        ct_id = _resolve_content_type_id(s, _coerce_id(content_type_id), org_id, cust_id)
        conn = Connection(
            kind=kind,
            name=name or kind.capitalize(),
            config=cfg or {},
            secret_enc=enc_secret(secret),
            created_by=actor,
            org_id=access.org_scope_value(org_id),
            customer_id=cust_id,
            content_type_id=ct_id,
        )
        s.add(conn)
        s.commit()
        s.refresh(conn)
        return conn


_UNSET = object()

# Config destinations used by each connector's token-auth request construction.
_CREDENTIAL_HOST_KEYS = {
    "strapi": "base_url",
    "akeneo": "base_url",
    "drupal": "base_url",
    "wordpress": "base_url",
    "servicenow": "base_url",
    "zoho": "api_domain",
    "jira": "base_url",
    "confluence": "base_url",
    # Salesforce WAS excluded here on the reasoning that "the REST host comes from the
    # token exchange, not config". That is exactly backwards: `login_url` IS config, and
    # it is the host the token exchange talks to, so a linguist who repoints it receives
    # the assertion carrying `client_id` (a declared secret_field) and then supplies the
    # `instance_url` that every later Knowledge read and write uses. The token exchange is
    # the attacker at that point. Three review passes cleared this before it was proven.
    "salesforce": "login_url",
    # PROTOTYPE (proto/mcp-connector): the CMS's own MCP endpoint. Its tool MAPPING is pinned
    # too (see _pin_secret_to_host): which tools the token drives is part of its scope.
    "mcp_cms": "mcp_url",
    # The GitHub API root. Blank means api.github.com (see _credential_raw), so setting the
    # default explicitly is not a move; the repository is pinned too (_pin_secret_to_host).
    "github": "base_url",
    # GitLab: gitlab.com when blank (see _credential_raw); the project path is pinned too.
    "gitlab": "base_url",
    # Sitecore XM Cloud: the CM host receives the minted bearer token, so a base_url move
    # drops the stored client id and secret (the token exchange host is fixed).
    "sitecore": "base_url",
}

# Kinds that genuinely have no config-controlled credential destination: their host is a
# module constant or comes from a fixed vendor gateway, so there is nothing to pin. Listed
# EXPLICITLY rather than left to fall through, because a silent fall-through is how the
# Freshdesk, Zendesk, Shopify and Salesforce holes all survived earlier rounds. Adding a
# connector kind without classifying it fails `test_every_kind_is_classified`.
_FIXED_HOST_KINDS = {
    # kind -> the host(s) it is ALLOWED to reach. A set, not a bare list of kind names.
    #
    # GATE R4 LOW-1: asserting mere MEMBERSHIP would let a future author green the suite by
    # dropping a base_url-driven kind in here, leaving it unpinned AND blessed by a passing
    # test, which is worse than the silent fall-through this ratchet replaced. Naming the
    # expected host lets the test DRIVE each connector and check where it actually goes.
    "airtable": frozenset({"api.airtable.com"}),
    # GATE R5 MED-1: derived from contentful_part1.py:37-38, the ONLY place these hosts
    # are defined. The map used to declare "cdn.contentful.com", which appears nowhere in
    # the tree, and to OMIT "api.eu.contentful.com", which a legitimate region=EU
    # connection actually reaches. Declare what the code does, never what sounds right.
    "contentful": frozenset({"api.contentful.com", "api.eu.contentful.com"}),
    # Google Docs (gdocs): Drive v3, Docs v1 and the service-account token endpoint are module
    # constants (gdocs_google.DRIVE_BASE / DOCS_BASE / TOKEN_URL); nothing else is ever contacted
    # (a Drive copy and a Docs batchUpdate are plain API calls, no file host). What the stored
    # service account key REACHES (the two folders, the user it acts as) is pinned separately,
    # below, through `gdocs.scope_fingerprint`.
    "gdocs": frozenset({"www.googleapis.com", "docs.googleapis.com", "oauth2.googleapis.com"}),
    "notion": frozenset({"api.notion.com"}),
    "hubspot": frozenset({"api.hubapi.com"}),
    # GATE R5 MED-1: helpscout.py:78 has a single global host. "api.helpscout.net" was
    # invented; an allowlist entry nothing reaches is a hole waiting for a future caller.
    "helpscout": frozenset({"docsapi.helpscout.net"}),
    "intercom": frozenset({"api.intercom.io", "api.eu.intercom.io", "api.au.intercom.io"}),
    # document360.py builds every request host from its closed _HOSTS table keyed by the
    # `region` config (global / us / ca), so a hostile region value is refused, never used as a
    # host. These are the vendor's documented API hosts (apidocs.document360.com).
    "document360": frozenset(
        {"apihub.document360.io", "apihub.us.document360.io", "apihub.ca.document360.io"}
    ),
    # Microsoft 365 Files (msgraph): the API and the app-only token endpoint are module constants
    # (msgraph_graph.GRAPH_BASE / LOGIN_BASE). The Graph-issued download and upload-session URLs
    # are followed only on the drive's own tenant SharePoint hosts (msgraph_graph.file_hosts)
    # and never receive the token. What the stored app secret REACHES (tenant, app, drive,
    # folders) is pinned separately, below, through `msgraph.scope_fingerprint`.
    "msgraph": frozenset({"graph.microsoft.com", "login.microsoftonline.com"}),
    # Plytix: both hosts are module constants in plytix.py (_API_ROOT, _AUTH_URL); no config
    # field names a destination. The key+password exchange goes to auth.plytix.com only.
    "plytix": frozenset({"pim.plytix.com", "auth.plytix.com"}),
    # Bitbucket Cloud: the API host is a module constant (BitbucketConnector._api_root); the
    # only config-controlled part is the workspace/repository, pinned in _pin_secret_to_host.
    "bitbucket": frozenset({"api.bitbucket.org"}),
}


def _credential_raw(kind: str, cfg: dict) -> str:
    """The raw destination value a connection's credential follows, normalized the way
    the connector itself reads it (Zoho bare domain, Salesforce sandbox default). For a
    tenant-host kind it is the operator handle. "" for fixed-host kinds."""
    if kind in TENANT_HOSTS:
        key = TENANT_HOSTS[kind][0]
        return str((cfg or {}).get(key) or "")
    key = _CREDENTIAL_HOST_KEYS.get(kind)
    # str() before .strip(): `cfg` is unvalidated caller JSON (api_connector_update
    # checks only isinstance(cfg, dict)), so a non-string base_url -- {"base_url": 123}
    # -- would raise AttributeError out of the store as an unhandled 500. It is also
    # read from the STORED config, so one legacy non-string value would otherwise brick
    # every later update of that row, including an admin's.
    raw = str(cfg.get(key) or "").strip() if key else ""
    # Zoho accepts a bare service domain and prepends https:// in _base().
    if kind == "zoho" and raw and not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    if kind == "github":
        # Blank resolves to github.com, exactly as GitHubConnector._api_root() reads it.
        raw = (raw or "https://api.github.com").rstrip("/")
    if kind == "gitlab":
        # Blank resolves to gitlab.com, exactly as GitLabConnector._api_root() reads it; a
        # pasted /api/v4 suffix is the same server.
        raw = (raw or "https://gitlab.com").rstrip("/")
        if raw.lower().endswith("/api/v4"):
            raw = raw[: -len("/api/v4")]
    if kind == "salesforce":
        # GATE R6 LOW-1: the earlier comment here had this backwards. Mirror
        # Salesforce._login_url()'s own defaulting, so a BLANK login_url resolves to the
        # same sandbox host the connector would actually call. Without the defaulting the
        # blank reads as "", so setting login_url to that same sandbox host for the first
        # time would compare "" against the host and count as a repoint, clearing a
        # credential that is still pointed exactly where it always was. This is an
        # availability guard against a FALSE clear, not the thing that catches a real
        # repoint: a genuine move away from the sandbox host is caught either way.
        from app.services.connectors.salesforce import _LOGIN_SANDBOX

        raw = (raw or _LOGIN_SANDBOX).rstrip("/")
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
    return raw


def _refuse_unreadable_destination(kind: str, cfg: dict | None) -> None:
    """Gate round 2 MEDIUM-1: refuse, at every config writer, a credential destination
    URL whose origin cannot be read (httpx reaches `https://h:+443`, urlsplit refuses its
    port), so the pin and the wire can never disagree. Only checked when THIS write
    supplies the key, so a grandfathered row stays editable (the pin fails closed on it)."""
    key = _CREDENTIAL_HOST_KEYS.get(kind)
    if not key or key not in (cfg or {}):
        return
    raw = _credential_raw(kind, cfg)
    if raw and not endpoint_origin(raw):
        raise ConnectorError(
            f"{key} must look like https://host or https://host:port with a plain numeric "
            "port; this one could not be read."
        )


def _credential_host(kind: str, cfg: dict) -> str:
    if kind in TENANT_HOSTS:
        try:
            return tenant_host(kind, cfg)
        except ConnectorError:
            # Invalid destinations cannot issue requests. Moving to one still clears
            # any credential previously pinned to a valid destination.
            return ""
    raw = _credential_raw(kind, cfg)
    # #265: pin the ORIGIN (scheme, host, effective port), not the hostname. A
    # hostname-only pin let an https -> http downgrade, or a move to another port on the
    # same host, keep the credential. Shared with engines, the local slot and vendors.
    return endpoint_origin(raw)


def _pin_secret_to_host(conn, old_cfg: dict, new_cfg: dict):
    """Drop a stored credential when a config write moves the connection's credential
    DESTINATION host. Returns (old_host, new_host, host_changed, dropped).

    THE reason this is a shared helper rather than inline in update(): `update()` is not
    the only writer of `Connection.config`. `patch_config()` is a second one, and
    `POST /connectors/{kind}/oauth/start` (app/web/connectors.py) feeds it a
    caller-supplied `base_url` behind only `_ai_block` (= can_tune_ai = ADMIN or
    LINGUIST) -- the exact role #165 is about. Guarding only `update()` left that route
    able to repoint a connection at an attacker host with the live credential intact.
    Every config writer that can move a host must call this.

    Clearing is UNCONDITIONAL on a host change and happens before any secret merge reads
    the old value, so a partial re-submission cannot carry an inherited credential to the
    new host."""
    old_host = _credential_host(conn.kind, old_cfg or {})
    new_host = _credential_host(conn.kind, new_cfg or {})
    # Atlassian OAuth pins to the API gateway and never reads config base_url, so a
    # base_url edit there moves nothing and must not destroy a working token.
    # Gate round 2 MEDIUM-1: fail CLOSED. An unreadable destination ("") is never "the
    # same server": when either side is "" and the raw value changed, it is a move.
    moved = old_host != new_host or (
        not (old_host and new_host)
        and _credential_raw(conn.kind, old_cfg or {}) != _credential_raw(conn.kind, new_cfg or {})
    )
    if conn.kind == "mcp_cms" and not moved:
        # PROTOTYPE: remapping which MCP tools the stored token calls (or their argument
        # templates) widens what it can do as surely as a new host, so it clears the token.
        from app.services.connectors.mcp_cms import mapping_fingerprint

        moved = mapping_fingerprint(old_cfg) != mapping_fingerprint(new_cfg)
    if conn.kind == "gdocs" and not moved:
        # Which folders (and which delegated user) the stored key reaches is its scope: editing
        # any of them widens or moves what it can read and write as surely as a new host.
        from app.services.connectors.gdocs import scope_fingerprint as _gdocs_scope

        moved = _gdocs_scope(old_cfg) != _gdocs_scope(new_cfg)
    if conn.kind == "msgraph" and not moved:
        # Which tenant, app, drive and folders the stored secret reaches is its scope: editing
        # any of them widens or moves what it can read and write as surely as a new host.
        from app.services.connectors.msgraph import scope_fingerprint

        moved = scope_fingerprint(old_cfg) != scope_fingerprint(new_cfg)
    if conn.kind in ("github", "gitlab", "bitbucket") and not moved:
        # Pointing the stored token at a different repository widens what it writes to just
        # as surely as a new host does (a classic token reaches every repo its owner can).
        moved = (
            str((old_cfg or {}).get("repo") or "").strip().lower()
            != str((new_cfg or {}).get("repo") or "").strip().lower()
        )
    host_changed = moved and not (conn.kind in ("jira", "confluence") and is_oauth(conn))
    dropped = bool(host_changed and _has_credential(conn))
    if dropped:
        conn.secret_enc = ""
    return old_host, new_host, host_changed, dropped


def _has_credential(conn) -> bool:
    """True when the connection actually holds a usable credential.

    `bool(conn.secret_enc)` is NOT this test, and using it was a real defect:
    `enc_secret({})` produces a NON-EMPTY Fernet token and `create()` always writes one
    (`:164`), so every row has a truthy `secret_enc`. Testing it meant a credential-less
    connection -- e.g. the row the OAuth-start path creates with a literal `{}` -- got
    flagged `error` with "Stored credentials were cleared" and an audit row claiming
    that the credential had been cleared, for what was really just a typo'd URL fix.

    The web form also stores BLANK strings for fields the operator left untouched, so an
    all-blank dict is "no credential" too. `auth_mode` is metadata, not a credential, so
    it never counts on its own; an OAuth secret nests its real material one level down."""
    for key, value in (dec_secret(conn.secret_enc) or {}).items():
        if key == "auth_mode":
            continue
        if isinstance(value, dict):
            if any(str(v or "").strip() for v in value.values()):
                return True
        elif str(value or "").strip():
            return True
    return False


# Prefix of the operator-facing message set when a credential is dropped. update() keys
# the reset off it so re-entering credentials clears the flag, WITHOUT clobbering an
# unrelated error (a failed test, an expired token) that happens to be on the row.
_CLEARED_PREFIX = "Stored credentials were cleared"


def _locked_connection(session: Session, cid: int):
    """Lock before reading so config/credential writes serialize across processes."""
    if session.get_bind().dialect.name == "sqlite":
        session.exec(text("BEGIN IMMEDIATE"))
    return session.exec(select(Connection).where(Connection.id == cid).with_for_update()).first()


def _mark_credential_cleared(conn) -> None:
    """Flag a connection whose credential was dropped and NOT replaced in the same call.

    Called only once the final state is known, so a caller that supplies a replacement
    secret in the same write does not get flagged. Do NOT use `_set_status` here: it opens
    its own Session, and this runs inside an uncommitted transaction holding CONFIG_LOCK.
    Setting the fields on the live row keeps the status atomic with the credential drop.

    The hosts are deliberately NOT named here: `last_message` renders on the connection
    page to a VIEWER, who cannot otherwise see `base_url`. The old and new host are in the
    audit row instead, which is admin-only."""
    conn.status = ConnStatus.ERROR
    conn.last_message = (
        f"{_CLEARED_PREFIX} because the connection's destination host changed. "
        "Re-enter them to reconnect."
    )[:300]


# sentinel: "customer_id argument not supplied" (leave scope as-is)
def update(
    cid: int,
    name: str,
    cfg: dict,
    secret: dict | None,
    customer_id: object = _UNSET,
    content_type_id: object = _UNSET,
    bound_customer_id=None,
    actor: str = "",
    can_add_languages: bool = True,
) -> None:
    """Merge cfg over the current config (never a wholesale replace, so keys
    owned by other cards / the scheduler survive) and update credentials only
    when a non-empty secret is supplied. A changed credential destination HOST
    clears stored credentials in the same transaction unless a new nonblank secret
    is supplied; same-host URL edits preserve them. Atlassian OAuth ignores config
    base_url (its gateway is pinned), so it is excluded. Audit records include the
    old/new hosts and whether the stored secret was cleared.

    `customer_id` (P4, item 5) is left untouched unless explicitly passed: a digit/
    int scopes the connection to that customer, "" clears it (makes it global). The
    sentinel default means pre-P4 callers never change the scope.

    `bound_customer_id` (task_bcae962f) is the CALLER's own customer binding. On UPDATE
    it does NOT force-to-own (that's the CREATE contract); instead — MED-3 (adversarial
    review, SETTLED): a customer-BOUND caller may NEVER change an EXISTING connection's
    customer binding in either direction, because forcing-to-own would let a bound
    caller privatize a shared GLOBAL (customer NULL) connection into their own customer
    (capture), silently removing it from every other customer's view. So a bound
    caller's submitted `customer_id` is IGNORED here and the connection keeps its
    current scope. Only an UNBOUND caller (internal staff / trusted in-process CLI /
    auth off — bound_customer_id None) may re-scope, exactly as before (byte-identical).

    `content_type_id` (SPEC-content-types phase 3) mirrors the same sentinel contract:
    left untouched unless explicitly passed; a digit/int sets the default content type,
    "" clears it. Changing it affects only FUTURE auto-created sync projects, not the
    connection's existing project (whose content type is a project setting).

    Language settings of a connection a project runs on (connected projects, decision 4): the
    change flows into the PROJECT's language list (`_language_plan`); `can_add_languages`
    False (a caller below manager) refuses one that would add a language."""
    existing = get(cid)
    if existing is not None:
        secret = _normalize_secret(existing.kind, secret)
    with CONFIG_LOCK, SECRET_LOCK, Session(engine) as s:
        conn = _locked_connection(s, cid)
        if not conn:
            return
        conn.name = name or conn.name
        _refuse_unreadable_destination(conn.kind, cfg)
        old_cfg = dict(conn.config or {})
        conn.config = {**old_cfg, **_drop_server_owned(cfg)}
        _validate_config_urls(conn.config)
        lang_plan = _language_plan(cid, old_cfg, conn.config, can_add_languages)
        new_secret = bool(secret and any((v or "").strip() for v in secret.values()))
        # _has_credential, not bool(secret_enc): enc_secret({}) is a non-empty token and
        # create() always writes one, so bool(secret_enc) is true for EVERY row.
        old_secret_present = _has_credential(conn)
        old_status, old_message = conn.status, conn.last_message or ""
        old_host, new_host, host_changed, _dropped = _pin_secret_to_host(conn, old_cfg, conn.config)
        # _pin_secret_to_host cleared the stored credential above if the destination
        # moved, BEFORE the merge below reads it. That ordering is the #165 fix: gating
        # the clear on `not new_secret` would leave the hole open on a PARTIAL
        # re-submission, because a Jira/Confluence form posts {"email", "api_token"} and
        # a caller supplying a junk email with a BLANK token makes `new_secret` true
        # while the merge still inherits the old api_token from dec_secret(secret_enc).
        # Clearing first makes the merge base empty (dec_secret("") == {}), so only
        # values supplied in THIS call can reach the new host.
        # `secret_cleared` is narrower than `_dropped`: it means "a credential was
        # dropped and NOT replaced in the same call", which is what the audit and the
        # operator-facing message report.
        secret_cleared = bool(host_changed and old_secret_present and not new_secret)
        if secret_cleared:
            _mark_credential_cleared(conn)
        elif (
            new_secret
            and old_status == ConnStatus.ERROR
            and old_message.startswith(_CLEARED_PREFIX)
        ):
            # The flag's own message tells the operator to re-enter credentials, so doing
            # that must clear it. Keyed off the message prefix so an UNRELATED error on the
            # row (a failed test, an expired token) is left alone. NEW, not CONNECTED: the
            # new credential has not been proven against the new host yet.
            conn.status = ConnStatus.NEW
            conn.last_message = ""
        if conn.kind == "helpscout":
            # MED-3: validate the MERGED config so an update can't introduce an
            # invalid collection_map partition (raises before commit -> no persist).
            from app.services.connectors.helpscout import validate_config as _hs_validate_config

            _hs_validate_config(conn.config)
        if new_secret:
            merged = {
                **dec_secret(conn.secret_enc),
                **{k: v for k, v in secret.items() if (v or "").strip()},
            }
            conn.secret_enc = enc_secret(merged)
        if customer_id is not _UNSET and _coerce_id(bound_customer_id) is None:
            # UNBOUND caller only: re-scope as requested (today's behaviour). A
            # BOUND caller's re-scope is dropped here (MED-3) — the connection's
            # existing customer_id is preserved, so a shared global can't be
            # captured and no connection can be re-homed across customers.
            conn.customer_id = _coerce_id(customer_id)
        if content_type_id is not _UNSET:
            # Validated at the seam (Findings 1+2) against the connection's RESOLVED
            # customer (the just-applied one above, else the existing scope) + org,
            # so an out-of-scope id is dropped rather than planted cross-scope.
            conn.content_type_id = _resolve_content_type_id(
                s, _coerce_id(content_type_id), conn.org_id, conn.customer_id
            )
        s.add(conn)
        s.commit()
        audit.log(
            actor=actor,
            action="connector.update",
            target=f"connection:{cid}",
            detail=f"config keys: {', '.join(sorted(cfg or {})) or 'none'}"
            + ("; secret changed" if new_secret else "")
            + (f"; host: {old_host} -> {new_host}" if host_changed else "")
            + f"; secret_cleared={secret_cleared}",  # pregate: allow-secret audit field
            customer_id=conn.customer_id,
        )
    _apply_language_plan(lang_plan, actor)

    # NOTE (adversarial-review H2/M3): config-map orphan pruning is NOT auto-run here.
    # Dropping a locale can leave "<id>:<locale>" hash entries behind, but automatically
    # deleting them on every save was unsafe (a --locales-scoped run, or a field-keyed
    # map, could be swept). Pruning is now the EXPLICIT, dry-run-by-default operator verb
    # `connector prune-config` (store.prune_orphaned_config). See its docstring.


def _language_plan(cid, old_cfg: dict, new_cfg: dict, can_add: bool = True):
    """What a change of this connection's language settings means for the project it runs for
    (project_automation.connection_language_plan): None for a plain connection or no language
    change. A refusal (languages managed by other projects, an unknown code, an add below
    manager, a change that would stop every language) is a ConnectorError, raised BEFORE the
    change is committed, so nothing is saved."""
    from app.services import project_automation

    try:
        return project_automation.connection_language_plan(cid, old_cfg, new_cfg, can_add=can_add)
    except project_automation.AutomationError as exc:
        err = ConnectorError(str(exc))
        err.status = exc.status  # 409 managed elsewhere, 403 below manager, 400 bad code
        raise err from exc


def _apply_language_plan(plan, actor: str) -> None:
    """After the commit (and outside CONFIG_LOCK: the project's add / stop service writes the
    connection's languages back through patch_config)."""
    if plan:
        from app.services import project_automation

        project_automation.apply_connection_language_plan(plan, actor=actor)


def patch_config(
    cid: int,
    updates: dict | None = None,
    remove=(),
    actor: str = "",
    can_add_languages: bool = True,
) -> None:
    """Atomically apply a partial change to Connection.config: re-read the row
    under CONFIG_LOCK, drop `remove` keys, layer `updates` on top, commit. The
    only sanctioned way to change config without risking a stale full-snapshot
    write clobbering a concurrent update.

    #165: this is the SECOND writer of Connection.config, and
    `POST /connectors/{kind}/oauth/start` reaches it with a caller-supplied `base_url`
    behind only `_ai_block` (can_tune_ai = ADMIN or LINGUIST). It therefore carries the
    same host pin as `update()`: moving the credential destination drops the stored
    credential in the same transaction, so a config-only edit can never repoint a live
    token at another host.

    A change to the language settings of a connection a project runs on flows into the
    project's language list, exactly as in `update()` (same refusals, same `can_add_languages`)."""
    lang_plan = None
    with CONFIG_LOCK, SECRET_LOCK, Session(engine) as s:
        conn = _locked_connection(s, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        old_cfg = dict(cfg)
        for key in remove:
            cfg.pop(key, None)
        _refuse_unreadable_destination(conn.kind, updates)
        cfg.update(updates or {})
        _validate_config_urls(cfg, old_cfg)
        lang_plan = _language_plan(cid, old_cfg, cfg, can_add_languages)
        conn.config = cfg
        old_host, new_host, _changed, dropped = _pin_secret_to_host(conn, old_cfg, cfg)
        if dropped:
            # patch_config takes no secret, so a drop here is always unreplaced.
            _mark_credential_cleared(conn)
        s.add(conn)
        s.commit()
        if dropped:
            cleared_note = "secret_cleared=True"  # pregate: allow-secret audit field name
            audit.log(
                actor=actor,
                action="connector.update",
                target=f"connection:{cid}",
                detail=(
                    f"credential host changed: {old_host or 'unset'} -> "
                    f"{new_host or 'unset'}; {cleared_note}"
                ),
                customer_id=conn.customer_id,
            )
    _apply_language_plan(lang_plan, actor)


def patch_config_merge(
    cid: int,
    hashes_key: str,
    hashes_delta: dict | None = None,
    watermark_key: str | None = None,
    watermark_at_least: int | None = None,
    hashes_remove=None,
    remove_casefold: bool = False,
) -> None:
    """Locked read-MERGE-write for state a long-running connector loop accumulates
    incrementally (zendesk_translate's per-run hash delta + watermark).

    `hashes_remove` (#666): keys THIS run wrote incompletely. Their stored hash describes
    content the target no longer holds as a whole, so it is popped (before the delta is
    applied, so an entry this run also wrote completely wins). `remove_casefold` also pops
    stored keys equal to one of them ignoring case (Confluence's legacy mixed-case keys are
    lower-cased into the pair on read, so leaving one would bring the stale hash back).

    Unlike patch_config (whole-value overwrite of the given keys), this re-reads
    the CURRENT config under CONFIG_LOCK and:
      - dict.update()s `hashes_key`'s stored dict with ONLY `hashes_delta` (the
        entries THIS run actually wrote/verified) — never replaces the map, so a
        concurrent run's own delta (persisted before or after this one) is kept.
      - sets `watermark_key` to max(stored, watermark_at_least) — a watermark can
        only advance, never regress, regardless of write order between two
        concurrent runs.

    Fixes a review finding: zendesk_translate used to read the whole zendesk_hashes
    map + zendesk_watermark at run start, mutate in memory through the (long,
    per-article) translate loop, then patch_config-write the ENTIRE snapshot at the
    end. A webhook-triggered run (plain jobs.submit) and a scheduled/manual run
    (submit_unique) CAN execute concurrently (WORKER_COUNT=2) against the SAME
    connection; the last one to finish clobbered the other's new hashes and could
    regress the watermark. Tracking only the delta + taking max() here makes the
    persist commutative regardless of interleaving."""
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        if hashes_delta or hashes_remove:
            merged_hashes = dict(cfg.get(hashes_key) or {})
            drop = {str(k) for k in (hashes_remove or ())}
            if remove_casefold:
                low = {k.lower() for k in drop}
                drop |= {k for k in merged_hashes if str(k).lower() in low}
            for k in drop:
                merged_hashes.pop(k, None)
            merged_hashes.update(hashes_delta or {})
            cfg[hashes_key] = merged_hashes
        if watermark_key is not None and watermark_at_least is not None:
            cfg[watermark_key] = max(int(cfg.get(watermark_key) or 0), int(watermark_at_least))
        _validate_config_urls(cfg, conn.config or {})
        conn.config = cfg
        s.add(conn)
        s.commit()


def patch_config_nested(cid: int, changes: dict | None = None) -> None:
    """Atomic locked read-merge-write for MULTIPLE nested config maps at once.

    `changes` = {config_key: {"add": {k: v}, "remove": [k, ...], "union": {k: [items]}}}.
    For each key it re-reads the stored nested dict under CONFIG_LOCK, pops the `remove`
    keys, dict.update()s the `add` entries, then for each `union` sub-key merges the given
    items into the stored LIST (sorted set union) — never replacing a map wholesale, so a
    concurrent run's own delta survives (same discipline as patch_config_merge). `union`
    reads its baseline INSIDE the lock (Shopify L1), so two runs unioning the same list key
    cannot lose each other's additions. Doing every key in ONE locked op removes the
    cross-key crash window two sequential patch_config_merge calls have (e.g. Contentful's
    hash + block markers, or Shopify's registered-key set, must land together)."""
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        for key, ch in (changes or {}).items():
            m = dict(cfg.get(key) or {})
            for rk in ch.get("remove") or ():
                m.pop(rk, None)
            m.update(ch.get("add") or {})
            for uk, items in (ch.get("union") or {}).items():
                m[uk] = sorted(set(m.get(uk) or []) | set(items or ()))
            cfg[key] = m
        _validate_config_urls(cfg, conn.config or {})
        conn.config = cfg
        s.add(conn)
        s.commit()


_CFG_LOCALE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")


_PRUNABLE_HASH_MAPS = frozenset(
    {
        "contentful_hashes",
        "d360_hashes",
        "fd_hashes",
        "helpscout_hashes",
        "hubspot_hashes",
        "intercom_hashes",
        "notion_hashes",
        "sanity_hashes",
        "sf_hashes",
        "shopify_hashes",
        "strapi_hashes",
        "zendesk_hashes",
        "zoho_hashes",
        "jira_hashes",
        "confluence_hashes",
        "wordpress_hashes",
    }
)


# --- config-map orphan pruning (recorded chip: "connector config-map pruning") -----
# A locale token as it appears in a "<record_id>:...:<locale>" hash-map key (BCP-47-ish,
# lower-cased — the flow engine stores hash keys with the wire-form lowered locale).
# EXPLICIT ALLOW-LIST of the PURE per-(record, locale) CONTENT-HASH maps whose keys end in
# ":<locale>" (flow.py hash_key + the non-flow content-hash maps). Pruning is confined to
# THESE by NAME (adversarial-review H2/M3): a broad "*_hashes" match would sweep
# `airtable_written_hashes` (keyed "<ext_id>:<field>" where a field named "src"/"url"
# matches the locale regex -> pruning it would make the next run overwrite a human's manual
# edit), and pruning `*_children` / `*_written` / id-recording maps deletes LIVE tracking
# (a pruned confluence child-page id -> a DUPLICATE child page). Those are NEVER in this set.
# flow-engine content-hash maps (TranslateFlowSpec.hash_key)
# non-flow content-hash maps (same "<id>...:<locale>" shape; jira also carries
# "<issue>:comment:<cid>:<loc>" keys -- the trailing token is still the locale)
def prune_orphaned_config(cid: int, *, apply: bool = False, actor: str = "system") -> dict:
    """Prune stale per-(record, locale) content-hash entries from a connection's config
    (the recorded chip). Connectors accumulate content-hash maps (zendesk_hashes,
    jira_hashes, confluence_hashes, ...) keyed "<record_id>...:<locale>", written
    APPEND-ONLY (patch_config_merge only `.update()`s, never removes -- store.py:363). When
    a target locale is dropped from the connection, its "<id>:<locale>" entries are DEAD
    forever: content-hash suppression only ever consults the connection's CURRENTLY
    configured locales (flow.py stage 6), so those entries can never be read again -- they
    only grow.

    EXPLICIT, OPERATOR-DRIVEN, DRY-RUN BY DEFAULT (adversarial-review H2/M3). This is NOT
    automatic: it is the `connector prune-config` verb + POST /api/connectors/{id}/prune-
    config (admin-gated). With `apply=False` (the default) it MUTATES NOTHING -- it returns
    exactly what WOULD be removed, per map, for the operator to review; `apply=True` executes
    and audits (connector.prune_config).

    PROVABLY SAFE, no external call (config-only). An entry "<x>:<loc>" is a candidate ONLY
    when ALL hold:
      * its MAP NAME is in the `_PRUNABLE_HASH_MAPS` allow-list -- pure content-hash maps
        only; `*_children` / `*_written` / `airtable_written_hashes` / id maps are NEVER
        touched (they record LIVE tracking, and some are field-keyed not locale-keyed);
      * <loc> (the trailing ":"-token) matches a locale token; and
      * <loc> is ABSENT from the KEEP set = the connection's configured effective locales
        UNIONED with its primary/source locale (so a source-locale hash and every live
        target is retained). A removed target locale is the only thing that qualifies, and
        because that is the operator's own dry-run-reviewed call, the `--locales` override
        edge (a run scoped to a subset) is never silently swept.
    Returns {"connection_id", "applied", "total", "removed": [{"map","keys","count"}, ...]}.
    Whole-connection DELETE is already clean (store.delete drops the row + its config).
    Best-effort: never raises."""
    report = {"connection_id": cid, "applied": False, "total": 0, "removed": []}
    try:
        with CONFIG_LOCK, Session(engine) as s:
            conn = s.get(Connection, cid)
            if conn is None:
                return report
            # DEEP copy so no inner map is shared with the ORM's loaded snapshot (the
            # patch_config_nested inner-dict-copy discipline: reassigning conn.config to
            # a fresh tree is what persists an apply).
            cfg = json.loads(json.dumps(conn.config or {}))
            # KEEP set = configured target locales UNION the primary target locale UNION
            # the SOURCE locale (re-review R2: primary_locale is a TARGET resolver, so the
            # source is added EXPLICITLY -- both the connection's config source_locale and
            # its sync project's source_locale -- so a "<id>:<source>" hash is never swept).
            keep = {l.strip().lower() for l in effective_locales(conn) if l}  # noqa: E741
            prim = (primary_locale(conn) or "").strip().lower()
            if prim:
                keep.add(prim)
            src_cfg = (cfg.get("source_locale") or cfg.get("source_language") or "").strip().lower()
            if src_cfg:
                keep.add(src_cfg)
            if conn.project_id:
                proj = s.get(Project, conn.project_id)
                proj_src = (getattr(proj, "source_locale", "") or "").strip().lower()
                if proj_src:
                    keep.add(proj_src)
            changed = False
            for key in list(cfg.keys()):
                if key not in _PRUNABLE_HASH_MAPS:
                    continue  # allow-list ONLY -- never children/written/id maps
                val = cfg.get(key)
                if not isinstance(val, dict):
                    continue
                dead = []
                for k in list(val.keys()):
                    if not isinstance(k, str) or ":" not in k:
                        continue
                    loc = k.rsplit(":", 1)[1].strip().lower()
                    if _CFG_LOCALE_RE.match(loc) and loc not in keep:
                        dead.append(k)
                if not dead:
                    continue
                report["removed"].append({"map": key, "keys": sorted(dead), "count": len(dead)})
                report["total"] += len(dead)
                if apply:
                    for k in dead:
                        del val[k]
                    cfg[key] = val
                    changed = True
            if apply and changed:
                conn.config = cfg
                s.add(conn)
                s.commit()
                report["applied"] = True
        if apply and report["total"]:
            log.info(
                "Pruned %d orphaned config-map entr%s from connection %s",
                report["total"],
                "y" if report["total"] == 1 else "ies",
                cid,
            )
            try:
                audit.log(
                    actor=actor or "system",
                    action="connector.prune_config",
                    target=f"connection:{cid}",
                    detail=f"removed {report['total']} orphaned content-hash entr"
                    f"{'y' if report['total'] == 1 else 'ies'} for dropped "
                    f"locales",
                )
            except Exception:
                log.exception("Audit log failed for connector.prune_config on %s", cid)
    except Exception:
        log.warning("prune_orphaned_config failed for connection %s (ignored)", cid, exc_info=True)
    return report


def prune_all_orphaned_config(*, apply: bool = False, actor: str = "system", org_id=None) -> dict:
    """Run prune_orphaned_config over EVERY connection and AGGREGATE the reports (the
    `connector prune-config --all` operator convenience). DRY-RUN by default, best-effort.
    `org_id` (#515) confines the sweep to that org's connections (the CLI --org scope);
    None = every connection (single-tenant, or the key-verified --operator door).
    Returns {"applied", "total", "connections": [<per-connection report>, ...]}."""
    from app.services import access

    agg = {"applied": apply, "total": 0, "connections": []}
    # #73 converged law: a bare None under an ACTIVE gate sweeps nothing; the CLI passes
    # access.UNSCOPED (op_org_scope) for the key-verified --operator door.
    if access.org_scope_blocked(org_id):
        return agg
    org_id = access.org_scope_value(org_id)
    try:
        with Session(engine) as s:
            # ffadmin Wave 5: EXCLUDE connections of a disabled kind -- the `--all` sweep
            # must never NAME a disabled-kind connection id in its report (non-enumerable).
            # Config pruning is DB-only (no egress), so a hidden connection's stale hashes
            # simply wait until the kind is re-enabled; nothing is lost.
            stmt = select(Connection)
            if org_id is not None:
                stmt = stmt.where(Connection.org_id == org_id)
            cids = [c.id for c in s.exec(stmt).all() if _kind_enabled(c.kind)]
        for cid in cids:
            rep = prune_orphaned_config(cid, apply=apply, actor=actor)
            if rep["total"]:
                agg["connections"].append(rep)
                agg["total"] += rep["total"]
    except Exception:
        log.warning("prune_all_orphaned_config failed (ignored)", exc_info=True)
    return agg


def _refuse_archived_sync(cid: int, *, actor: str = "", op: str = "translate"):
    """SHARED archive gate for EVERY connector ingest entrypoint (residual sweep M6,
    re-review R1). If the connection's SYNC PROJECT is archived, record a clean skip
    (status + SyncLog) and return the message string; else return None. Called at the TOP
    of translate_now, EACH <kind>_translate (flow + non-flow), pull/push/sync -- so a
    DIRECT CLI (`connector jira translate`) / API (POST /api/connectors/{id}/pull) call
    that bypasses translate_now still refuses. No paid MT / external write into a retired
    project. NOTE: inbox ROUTING still delivers into an archived inbox project (a documented
    twin-creation hazard, R3); this only stops the connector's own pull/translate/push."""
    name = connection_project_archived(cid)
    if name is None:
        return None
    label = {"pull": "Pull", "push": "Push"}.get(op, "Sync")
    msg = (
        f"{label} skipped: the connection's project '{name}' is archived (unarchive it to resume)."
    )
    try:
        _set_status(cid, ConnStatus.CONNECTED, msg, synced=False)
        _log(cid, op, True, 0, msg, actor)
    except Exception:  # noqa: S110 -- status/history write only; the skip message is returned
        pass
    return msg


def _refuse_disabled_sync(cid: int, *, actor: str = "", op: str = "translate"):
    """ffadmin Wave 5 -- refuse an ingest entrypoint for a DISABLED connector kind at the
    TOP, BEFORE any build/egress/bookkeeping. Returns the switch-named message (else None).

    THE WAVE-3 MEDIUM-3 LESSON, applied: a feature refusal must NEVER stamp a HEALTHY
    object ERROR. The realistic way a disabled kind reaches an ingest entrypoint is a
    connector_translate job that was ENQUEUED while the kind was enabled and RUNS after it
    was disabled (routes/CLI/scheduler all refuse a disabled kind up front). If that job
    fell through to build_fresh, pull_connection's except-clause would _set_status(ERROR)
    -- marking a perfectly healthy connection failed merely because the FEATURE is off. So
    this early refusal returns the readable message and DOES NOT touch conn.status; the
    caller returns a clean (False, msg, <empty>) so the JOB fails readably (naming the
    switch) while the CONNECTION is left untouched and returns on re-enable. A SyncLog
    history row is written (ok=True skip -- observability only, not a status change).
    Mirrors _refuse_archived_sync's placement at every ingest entrypoint."""
    conn = get(cid)
    if conn is None or _kind_enabled(conn.kind):
        return None
    msg = f"connector kind '{conn.kind}' is disabled (settings connector_disabled_kinds)."
    try:  # noqa: SIM105
        _log(cid, op, True, 0, msg, actor)  # history only -- never _set_status (no ERROR)
    except Exception:  # noqa: S110 -- SyncLog history row only; the refusal is still returned
        pass
    return msg


def _refuse_disabled_kind(kind: str) -> None:
    if not _kind_enabled(kind):
        raise ConnectorError(
            f"connector kind '{kind}' is disabled (settings connector_disabled_kinds)."
        )


def build(conn: Connection, transport=None) -> Connector:
    cls = connector_class(conn.kind)
    if not cls:
        raise ValueError(f"Unknown connector kind: {conn.kind}")
    _refuse_disabled_kind(conn.kind)
    c = cls(conn.config or {}, dec_secret(conn.secret_enc), transport=transport)
    # Stable connection identity for the T2 write-serialization lock (audit D1):
    # keys the striped mutex in SubResourceStrategy.write_target per connection.
    c.connection_id = conn.id
    return c


def ensure_fresh_token(cid: int, transport=None, force: bool = False) -> dict:
    """Return the decrypted secret for `cid`, refreshing an OAuth access token
    first when it's near expiry (or when force=True).

    Not an OAuth secret -> returned as-is (legacy token creds pass straight
    through; zero token-endpoint traffic). Fresh OAuth token -> returned without a
    network call. Otherwise this is the SINGLE-FLIGHT refresh:

      acquire _refresh_lock(cid) -> RE-READ the row inside the lock (double-checked
      locking — MANDATORY: two racing refreshes each consume a rotating refresh
      token, so the loser's freshly-persisted token would already be invalid; the
      re-read lets the second thread see the first thread's just-written token and
      skip its own refresh) -> refresh -> persist under SECRET_LOCK.

    On refresh FAILURE the connection is marked ConnStatus.REAUTH and the STALE
    secret is returned: downstream then hits a normal 401 that surfaces as an
    ordinary (False, msg) through each flow's existing try/except — never a crash.
    """
    conn = get(cid)
    if not conn:
        return {}
    secret = dec_secret(conn.secret_enc)
    if auth_mode(secret) != "oauth":
        return secret  # legacy token creds, untouched
    oauth_dict = secret.get("oauth") or {}
    if not force and _token_fresh(oauth_dict):
        return secret

    with _refresh_lock(cid):
        # Double-checked: re-read the row INSIDE the lock. A thread that lost the
        # race sees the winner's freshly-persisted token here and returns it
        # without issuing its own (rotating-token-invalidating) refresh.
        conn = get(cid)
        if not conn:
            return {}
        secret = dec_secret(conn.secret_enc)
        if auth_mode(secret) != "oauth":
            return secret
        oauth_dict = secret.get("oauth") or {}
        if not force and _token_fresh(oauth_dict):
            return secret

        prior_status = conn.status
        new_oauth, err = oauth.refresh(conn.kind, oauth_dict, transport=transport)
        if err or not new_oauth:
            _set_status(
                cid,
                ConnStatus.REAUTH,
                f"Reconnect needed -- token refresh failed: {err or 'unknown error'}",
            )
            return secret  # STALE secret; a 401 surfaces normally
        # F7: re-check the row is STILL oauth-mode before overwriting secret_enc.
        # After F1 (rotate refuses OAuth) the rotate-interleave that could flip a
        # row to token-mode mid-refresh is unreachable, but this hardens the
        # persist path against any future write path racing the refresh — never
        # clobber a token-mode secret with a v2 OAuth blob.
        current = get(cid)
        if not current or auth_mode(dec_secret(current.secret_enc)) != "oauth":
            log.warning(
                "Skipping OAuth refresh persist for connection %s — no "
                "longer oauth-mode (raced by another write).",
                cid,
            )
            return {"auth_mode": "oauth", "oauth": new_oauth}
        set_oauth_secret(cid, new_oauth)
        # A successful refresh proves the connection can talk to the provider again,
        # so self-heal a prior REAUTH: clear it back to CONNECTED. (Only touch
        # status when it WAS reauth — don't disturb an ordinary CONNECTED/ERROR.)
        if prior_status == ConnStatus.REAUTH:
            _set_status(cid, ConnStatus.CONNECTED, "Reconnected - token refreshed.")
        return {"auth_mode": "oauth", "oauth": new_oauth}


def build_fresh(cid: int, transport=None) -> Connector:
    """build() for a stored connection id, but with a proactive OAuth refresh first
    (ensure_fresh_token). This is the seam every LIVE-OP call site uses instead of
    build(): a token connection is unaffected; an OAuth connection gets a valid
    access token minted before the connector is instantiated."""
    conn = get(cid)
    if not conn:
        raise ValueError(f"No such connection: {cid}")
    cls = connector_class(conn.kind)
    if not cls:
        raise ValueError(f"Unknown connector kind: {conn.kind}")
    _refuse_disabled_kind(conn.kind)  # ffadmin Wave 5 deep seam (see build())
    secret = ensure_fresh_token(cid, transport=transport)
    c = cls(conn.config or {}, secret, transport=transport)
    c.connection_id = cid  # see build(): stable identity for the D1 write lock
    return c


def _with_oauth_retry(cid, op, transport=None):
    """Reactive one-shot 401 retry for the SIMPLE flows (test_connection + generic
    pull/sync). `op` is a zero-arg callable returning (ok, msg[, ...]); when the
    connection is OAuth and the first attempt fails on what looks like an auth
    error, force a refresh once and retry exactly once.

    The RICH flows (dynamic_translate, jira_translate, confluence_translate) do
    NOT use this reactive wrapper: they instead re-mint the OAuth token and REBUILD
    the connector between the fetch phase and the write phase (see each flow's F4
    build_fresh before its write loop), so the write can't 401 on a token that
    expired during a long MT phase. A hard 401 that still slips through surfaces as
    an ordinary (False, msg) plus a clean REAUTH status via ensure_fresh_token."""
    result = op()
    if not is_oauth(get(cid)):
        return result
    ok = result[0] if isinstance(result, tuple) else result
    msg = result[1] if isinstance(result, tuple) and len(result) > 1 else ""
    if ok or not _looks_like_auth_error(msg):
        return result
    # Force one refresh, then retry the op exactly once.
    ensure_fresh_token(cid, transport=transport, force=True)
    return op()


# --- operations ---
def test_connection(cid: int, actor: str = "", transport=None) -> tuple[bool, str]:
    conn = get(cid)
    if not conn:
        return False, "No such connection."
    # Proactive refresh (build_fresh) + reactive one-shot 401 retry: for an OAuth
    # connection whose token went stale between the freshness check and the call,
    # a single forced refresh + retry recovers without user action.
    ok, msg = _with_oauth_retry(
        cid, lambda: build_fresh(cid, transport).test(), transport=transport
    )
    _set_status(cid, ConnStatus.CONNECTED if ok else ConnStatus.ERROR, msg, tested=True)
    _log(cid, "test", ok, 0, msg, actor)
    return ok, msg


def rotate_secret(cid: int, new_secret: dict, actor: str = "", transport=None) -> tuple[bool, str]:
    """Replace a connection's credentials, but only after proving the NEW ones
    work — a bad rotation must never disturb the old, still-working secret.

    Builds a THROWAWAY connector from the connection's CURRENT config plus the
    NEW secret and calls test(). On failure, nothing is written: the stored
    secret_enc is untouched and the caller gets (False, msg) to show inline.
    On success, re-reads the row under SECRET_LOCK and REPLACES secret_enc
    wholesale (never merges the new fields over the old ones): a partial
    "rotate just the token, keep the old email" write is a footgun for
    basic-auth-style connectors (Jira/Confluence — email + api_token) because a
    stale email paired with a fresh token silently authenticates as the wrong
    account, or fails in a way that support has to reverse-engineer. Rotation
    is a deliberate all-or-nothing swap of everything the form submitted.

    Compare the tested destination inside the database write transaction. A config
    edit in another CLI/web process must not restore a credential after clearing it."""
    conn = get(cid)
    if not conn:
        return False, "No such connection."
    # OAuth connections have no manual secret to rotate — their tokens are minted
    # (and rotated) by the provider via the reconnect flow. Overwriting secret_enc
    # here with a form-submitted secret would silently DOWNGRADE a v2 OAuth secret
    # to a v1 token secret (or blank it), so refuse outright.
    if is_oauth(conn):
        return False, "OAuth connections reconnect via the provider, not manual rotation."
    cls = connector_class(conn.kind)
    if not cls:
        return False, f"Unknown connector kind: {conn.kind}"
    # ffadmin Wave 5 deep seam (site 3): a disabled kind builds no live candidate + does
    # no test() egress. Returns the clean (False, msg) contract this function already uses
    # rather than raising, so a caller that somehow reaches here gets a readable refusal.
    if not _kind_enabled(conn.kind):
        return False, (
            f"connector kind '{conn.kind}' is disabled (settings connector_disabled_kinds)."
        )
    try:
        new_secret = _normalize_secret(conn.kind, new_secret)
    except ConnectorError as exc:
        return False, str(exc)
    tested_host = _credential_host(conn.kind, conn.config or {})
    candidate = cls(conn.config or {}, new_secret or {}, transport=transport)
    ok, msg = candidate.test()
    if not ok:
        _log(cid, "rotate", False, 0, msg, actor)
        return False, msg
    with CONFIG_LOCK, SECRET_LOCK, Session(engine) as s:
        row = _locked_connection(s, cid)
        if not row:
            return False, "No such connection."
        if (
            row.kind != conn.kind
            or is_oauth(row)
            or _credential_host(row.kind, row.config or {}) != tested_host
        ):
            return False, (
                "The connection's destination changed while the credential was being tested; "
                "try again."
            )
        row.secret_enc = enc_secret(new_secret or {})
        row.status = ConnStatus.CONNECTED
        row.last_message = (msg or "")[:300]
        row.last_test = datetime.utcnow()
        s.add(row)
        s.commit()
    _log(cid, "rotate", True, 0, msg, actor)
    audit.log(
        actor=actor,
        action="connector.rotate_credentials",
        target=f"connection:{cid}",
        detail=f"kind={conn.kind}",
        customer_id=getattr(conn, "customer_id", None),
    )
    return True, msg


def browse_containers_unsaved(kind: str, cfg: dict, secret: dict, transport=None) -> list[dict]:
    """Containers from a THROWAWAY connector built off caller-supplied creds/config.

    For the add-connection flow, where no Connection row exists yet: the user has
    typed a token (and, for Atlassian, a base URL) but not saved, so we build a
    connector in-memory (never persisted) and list its containers. An unknown kind
    returns []. containers() is itself best-effort, so a bad token just yields []."""
    cls = connector_class(kind)
    if not cls:
        return []
    # ffadmin Wave 5 deep seam (site 4): a disabled kind builds no client + does no
    # containers() egress. The web /connectors/{kind}/browse route already refuses a
    # disabled kind upstream; this is the service-layer backstop for any other caller.
    _refuse_disabled_kind(kind)
    from app.services.connectors.base import UNSAVED_RESPONSE_MAX_BYTES

    candidate = cls(cfg or {}, secret or {}, transport=transport)
    candidate.response_max_bytes = UNSAVED_RESPONSE_MAX_BYTES
    return candidate.containers()


def primary_locale(conn) -> str:
    """The single locale the manual pull/push/sync round-trip handles.

    An explicit (non-blank) config target_locale always wins — exactly the
    pre-unification behavior, so every existing config that has target_locale set
    (including ones that ALSO carry a Language Set or target_locales) keeps
    round-tripping the locale it always did, and documents pulled before a config
    edit stay pushable. Only when target_locale is BLANK does the round-trip fall
    back to the first effective locale (the Language Set's first language, or the
    first free-text target_locales entry) instead of the old hardcoded fr-FR —
    the one intended locale fix of the Phase-2 unification."""
    explicit = ((conn.config or {}).get("target_locale") or "").strip()
    if explicit:
        return explicit
    locs = effective_locales(conn)
    return locs[0] if locs else "fr-FR"  # non-empty in practice; guard odd configs


def _ensure_project(s: Session, conn: Connection) -> Project:
    """The Localization OS project this connection syncs into (created on first pull)."""
    if conn.project_id:
        proj = s.get(Project, conn.project_id)
        if proj:
            return proj
    cfg = conn.config or {}
    # MULTI-ORG (leak vector 10 — connectors): the sync project inherits the
    # connection's org so a connector-created project lands in the right org rather
    # than NULL (quarantined). Connection.org_id exists from P0 (backfilled to the
    # Default org today); P3 wires real org binding on the Connection itself + the
    # /connection/{cid}/* authz — until then the project simply inherits whatever
    # org the connection carries, which is a strict no-op single-tenant (all rows
    # share the Default org).
    proj = Project(
        name=f"{conn.name} ({conn.kind})",
        # #659: HubSpot (and any connector that names its authoring language
        # `source_language`) carries it under that key; `source_locale` wins when both
        # are set, and a connection with neither stays en-US.
        source_locale=(cfg.get("source_locale") or cfg.get("source_language") or "en-US").strip()
        or "en-US",
        default_provider=config.DEFAULT_PROVIDER,
        created_by=conn.created_by,
        org_id=getattr(conn, "org_id", None),
        # ADJACENT FIX (SPEC-content-types phase 3 recon): the sync project
        # inherited org_id but NOT the connection's customer_id, so a
        # CUSTOMER-SCOPED connection auto-created an UNSCOPED (customer NULL)
        # project — its content, TM and glossaries then escaped the customer's
        # R2-3 isolation boundary. Inherit customer_id too so the auto-created
        # project is correctly customer-scoped. NULL (a global connection) stays
        # NULL = byte-identical to today for the global case.
        customer_id=getattr(conn, "customer_id", None),
        # SPEC-content-types phase 3 (§6): the auto-created sync project
        # inherits the connection's default content type, so imported
        # content lands in the right domain (its TM/glossary scope follows).
        # NULL = untyped/General (byte-identical to today).
        content_type_id=getattr(conn, "content_type_id", None),
    )
    s.add(proj)
    s.commit()
    s.refresh(proj)
    # #690 gate: a connector project created from now on starts with comment translation
    # OFF like every project_create project; only pre-option projects (no row) keep Word on.
    from app.services import comment_option as _comment_option

    _comment_option.stamp_new_project(proj, s)
    conn.project_id = proj.id
    s.add(conn)
    s.commit()
    # multilingual: every locale in the set. A connector's target_locales config is its
    # provider wire format (Zendesk 'pt-br', Akeneo 'fr_FR'): canonicalize ONLY the
    # TargetLocale row (catalog spelling when the catalog knows the code), never the value
    # used for the connector's own API calls. Same rule as the shared flow's stage 5, so the
    # first pull and the flow agree on ONE row per language (a config naming pt-br used to
    # leave both pt-br and pt-BR rows, which a bound project then ran twice).
    _add_missing_locales(s, proj.id, [_canonical_locale(loc) for loc in effective_locales(conn)])
    s.commit()
    # Connected projects (spec C3): a project a connector creates on its first pull is a
    # CONNECTED project bound to this same connection (not scheduled, nothing enabled). It
    # never raises into the pull; with the automation module off it stays the legacy project.
    from app.services import connected_projects as _connected

    _connected.adopt_new_project(s, proj, conn)
    return proj


def _new_connector_doc(
    s: Session,
    conn: Connection,
    units,
    filename: str,
    actor: str,
    locales=None,
    extra_skeleton: dict | None = None,
    context: dict | None = None,
) -> Document:
    """Shared setup for every connector-created document: ensure the backing
    project exists, register any missing TargetLocale rows for `locales`, and
    create the Document shell (fmt = connection kind, segmented, skeleton with
    format/connector_id/units plus any flow-specific extras). The skeleton
    deliberately gets NO "mode" key here: only the flows that historically stored
    one (pull + dynamic) pass it via extra_skeleton, keeping each flow's persisted
    skeleton shape identical to pre-refactor rows. Segment seeding stays with each
    caller — the flows intentionally seed differently
    (per unit / per locale x unit / per locale: title then body units).

    `context` (SPEC-context-metadata Phase 3A connector harvest) is stamped onto
    `Document.context` — the 2nd-highest resolve() layer, injected into every
    segment prompt with ZERO per-connector translate change (translate.py:355).
    CONTRACT (mirrors ingest_document / web api.py:947): the caller passes an
    ALREADY-VALIDATED dict — the clean output of context_pkg.validate_and_sanitize
    — because this seam does NOT re-validate. Pass None (never {}) when there is
    nothing to harvest so Document.context stays None and the prompt is
    byte-identical to a pre-harvest run (harvest.build returns None for that case)."""
    proj = _ensure_project(s, conn)
    _add_missing_locales(s, proj.id, locales)
    skeleton = {"format": conn.kind, "connector_id": conn.id}
    if units is not None:
        skeleton["units"] = units
    skeleton.update(extra_skeleton or {})
    # Issue #142: the title comes from a REMOTE system (an Airtable cell, a Jira summary,
    # a Zendesk article title), so a `/` or `..` in it is that system's business, not a
    # refusal case — basename + neutralise, exactly like the upload seam, so a connector
    # can never store a filename a filesystem sink could compose a path from.
    from app.services.content_part2 import safe_document_filename

    doc = Document(
        project_id=proj.id,
        filename=safe_document_filename(filename),
        fmt=conn.kind,
        mode=TransMode.SEGMENTED,
        status=DocStatus.NEW,
        skeleton=skeleton,
        context=context,
        created_by=actor or conn.created_by,
    )
    s.add(doc)
    s.commit()
    s.refresh(doc)
    return doc


def jira_record_pending(cid: int, issue_keys) -> list[str]:
    """Merge issue key(s) a verified push named into the connection's PENDING set
    (config `jira_pending`, a de-duplicated list preserving first-seen order). Used
    when auto-translate is OFF: the work is queued for a human to release with the
    "Translate pending now" action rather than billed immediately. Returns the full
    pending list after the merge. Concurrency-safe under CONFIG_LOCK (read-merge-
    write of the whole list — patch_config would clobber a concurrent push)."""
    keys = [str(k).strip() for k in (issue_keys or []) if str(k).strip()]
    if not keys:
        conn = get(cid)
        return list((conn.config or {}).get("jira_pending") or []) if conn else []
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return []
        cfg = dict(conn.config or {})
        pending = list(cfg.get("jira_pending") or [])
        seen = set(pending)
        for k in keys:
            if k not in seen:
                pending.append(k)
                seen.add(k)
        # Cap the pending list so a chatty (but signed) sender can't grow it
        # unbounded; the newest keys win (a human clears them anyway).
        if len(pending) > 500:
            pending = pending[-500:]
        cfg["jira_pending"] = pending
        conn.config = cfg
        s.add(conn)
        s.commit()
        return list(pending)


def jira_pending(cid: int) -> list[str]:
    """The connection's current pending issue keys (see jira_record_pending)."""
    conn = get(cid)
    return list((conn.config or {}).get("jira_pending") or []) if conn else []


def wordpress_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> wordpress.wordpress_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    _dis = _refuse_disabled_sync(cid, actor=actor)  # ffadmin Wave 5 (before any egress/bookkeeping)
    if _dis is not None:
        return False, _dis, {}
    _arch = _refuse_archived_sync(cid, actor=actor)  # M6/R1
    if _arch is not None:
        return True, _arch, {"created": 0, "updated": 0, "skipped_archived": True}
    from app.services.connectors import wordpress as _m

    return _m.wordpress_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )
