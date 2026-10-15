"""store: shared leaf helpers (layered pure move)

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports log, _fernet, CONFIG_LOCK, SECRET_LOCK,
_REFRESH_LOCKS, _REFRESH_LOCKS_GUARD, _refresh_lock, auth_mode, list_connections, get, _coerce_id,
_resolve_content_type_id, connection_project_archived, WriteBudget, delete, _log from
app.services.connectors.store_common_leaf.

Documented in: docs/architecture/connectors.md
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.models import (
    Connection,
    Document,
    Project,
    SyncLog,
    TargetLocale,
    WebhookEvent,
)
from app.services import audit, ids
from app.services.connectors import oauth
from app.services.connectors.store_common_leaf import (
    _REFRESH_LOCKS,
    _REFRESH_LOCKS_GUARD,
    CONFIG_LOCK,
    SECRET_LOCK,
    WriteBudget,
    _coerce_id,
    _fernet,
    _log,
    _refresh_lock,
    _resolve_content_type_id,
    auth_mode,
    connection_project_archived,
    delete,
    get,
    list_connections,
)

__all__ = [
    "CONFIG_LOCK",
    "SECRET_LOCK",
    "_REFRESH_LOCKS",
    "_REFRESH_LOCKS_GUARD",
    "Fernet",
    "Project",
    "WebhookEvent",
    "WriteBudget",
    "_coerce_id",
    "_fernet",
    "_log",
    "_refresh_lock",
    "_resolve_content_type_id",
    "auth_mode",
    "config",
    "connection_project_archived",
    "delete",
    "get",
    "ids",
    "list_connections",
    "threading",
]

log = logging.getLogger("loc_tms.connectors")


def enc_secret(d: dict) -> str:
    return _fernet().encrypt(json.dumps(d or {}).encode()).decode()


def dec_secret(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except Exception as e:
        # Loudly distinguish "no credentials" from "credentials unreadable" —
        # this usually means data/.enc_key was lost or replaced.
        log.warning(
            "Connector credential decrypt failed (%s) — the encryption key "
            "may have changed; re-enter the connection's API token.",
            type(e).__name__,
        )
        return {}


def _confine_customer_id(requested, bound_customer_id):
    """Resolve a NEW connection's customer scope, CONFINED to the CALLER's binding
    (task_bcae962f — the connector-isolation gap the Content Types phase-3 review
    recorded but did not introduce). CREATE-only: update() deliberately does NOT
    force-to-own (MED-3 — that would let a bound caller capture a shared GLOBAL
    connection); it preserves the existing connection's scope instead. See update().

    THE SEAM (repo law: org/customer scope confinement lives at the store.create/
    update seam, never hand-rolled per surface — the same idiom the content-types
    work used for content_type_id via _resolve_content_type_id). Putting it here is
    what makes web, CLI and /api inherit the fix UNIFORMLY: a surface that forgets to
    pre-validate still cannot plant a foreign customer scope on a new connection.

    `bound_customer_id` is the caller's OWN customer binding when the caller is a
    customer-BOUND account (a web-session User or a per-user API key — see
    access.current_customer_id / deps._ident_customer_id), else None (internal staff,
    the trusted in-process CLI operator, or auth off — see the cmd_template.py
    precedent that the local CLI is a non-bound operator identity).

    Semantics mirror project_create(bound_customer_id=) and content_types.
    _valid_customer_id: a bound caller is FORCED to its own customer, so a forged /
    foreign / blank `requested` can never scope the connection to a DIFFERENT customer
    or to global (fail-closed). The confinement is SILENT (force-to-own, not an
    exception) to match this module's established customer_id / content_type_id
    silent-drop contract — the connector web/CLI callers have no ForeignCustomerError
    handling, so a raise would 500 rather than correct the scope. An UNBOUND caller
    (bound_customer_id None) keeps `requested` exactly as before — byte-identical to
    today for every pre-R2-3 / single-tenant / CLI-operator path."""
    bound = _coerce_id(bound_customer_id)
    if bound is not None:
        return bound
    return _coerce_id(requested)


def history(cid: int, limit: int = 20) -> list[SyncLog]:
    with Session(engine) as s:
        return list(
            s.exec(
                select(SyncLog)
                .where(SyncLog.connection_id == cid)
                .order_by(SyncLog.ts.desc())
                .limit(limit)
            ).all()
        )


# --- build a live connector from a stored Connection ---
# ffadmin Wave 5 -- THE DEEP SEAM for per-kind visibility. There are FOUR service-layer
# sites that construct a live connector client (and can therefore egress), and ALL FOUR
# refuse a disabled kind via _refuse_disabled_kind, so a disabled kind cannot reach a
# provider even through a future unanticipated caller a URL/CLI sweep never sees (the
# Wave-3 HIGH-1 lesson -- a service function reached by import, invisible to a route
# sweep):
#   1. build(conn)                 -- the plain constructor (offline test path, etc.)
#   2. build_fresh(cid)            -- build() + a proactive OAuth refresh; the seam every
#                                     LIVE-OP path uses (test/pull/push/sync/translate/
#                                     containers/schema)
#   3. rotate_secret(cid, ...)     -- builds a THROWAWAY candidate from NEW creds + test()
#   4. browse_containers_unsaved   -- builds a THROWAWAY client from caller-typed creds
# The refusal names the setting, uniform with the web/api/CLI gates; the pull/translate
# flows also refuse EARLY (top of every ingest entrypoint, _refuse_disabled_sync) so a
# queued job fails readably WITHOUT stamping the healthy connection ERROR, and this deep
# seam is the last-line backstop that guarantees no egress.
def _kind_enabled(kind: str) -> bool:
    from app.services import connectors as _reg

    return _reg.kind_enabled(kind)


# --- OAuth token lifecycle (refresh single-flight, proactive freshness) ---
def _token_fresh(oauth_dict: dict) -> bool:
    """A stored OAuth token is fresh when it has more than the skew buffer of life
    left. expires_at == 0 (a token endpoint that returned no expires_in) is treated
    as always-fresh — nothing to proactively refresh; a hard 401 handles expiry."""
    import time as _time

    exp = (oauth_dict or {}).get("expires_at") or 0
    if not exp:
        return True
    return (exp - int(_time.time())) > oauth.SKEW_SECONDS


def _looks_like_auth_error(msg: str) -> bool:
    """Heuristic: does a flow's error string look like an auth/401 failure? Kept
    deliberately loose — a false positive costs one wasted refresh + retry, a
    false negative just means the user sees the REAUTH status and reconnects."""
    m = (msg or "").lower()
    return (
        "401" in m
        or "unauthor" in m
        or "invalid_grant" in m
        or "invalid token" in m
        or "expired" in m
        or "forbidden" in m
        or "403" in m
    )


def _set_status(cid: int, status: str, message: str, *, tested=False, synced=False) -> None:
    with Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        conn.status = status
        conn.last_message = message[:300]
        if tested:
            conn.last_test = datetime.utcnow()
        if synced:
            conn.last_sync = datetime.utcnow()
        s.add(conn)
        s.commit()


def config_locales(cfg: dict | None, default: str = "fr-FR") -> list[str]:
    """The locales a connection CONFIG names: its selected Language Set, else its free-text
    target_locales, else its single target_locale, else `default` ("" gives an empty list)."""
    from app.services import languages

    cfg = cfg or {}
    ls = languages.get(cfg.get("language_set_id"))
    if ls and ls.target_locales:
        return list(ls.target_locales)
    return languages.parse_locales(cfg.get("target_locales") or cfg.get("target_locale") or default)


def dedupe_locales(locales) -> list[str]:
    """`locales` with later spellings of an already-listed language dropped (compared by
    languages_catalog.normalize, so pt-br and pt-BR are one language). Order kept."""
    from app.services import languages_catalog

    out, seen = [], set()
    for loc in locales or ():
        key = languages_catalog.normalize(loc)
        if key and key not in seen:
            seen.add(key)
            out.append(loc)
    return out


def in_connection_spelling(locales, connection_locales) -> list[str]:
    """`locales` (a project's languages) in the spelling the connection already uses for the same
    language (its wire format: Akeneo fr_FR, Zendesk pt-br), else as given; one entry per
    language. The connection's first spelling of a language wins."""
    from app.services import languages_catalog

    spelled: dict[str, str] = {}
    for loc in connection_locales or ():
        spelled.setdefault(languages_catalog.normalize(loc), loc)
    return dedupe_locales(
        spelled.get(languages_catalog.normalize(loc), loc) for loc in locales or ()
    )


def languages_project_id(cid) -> int | None:
    """The project whose language list a connection's languages follow: the project it RUNS for
    (connected projects, decision 4), else None (a plain connection, or one that only feeds other
    projects through their own copies)."""
    if cid is None:
        return None
    from app.models import ProjectAutomation

    with Session(engine) as s:
        return s.exec(
            select(ProjectAutomation.project_id).where(ProjectAutomation.run_connection_id == cid)
        ).first()


def _config_locales(conn, override=None) -> list[str]:
    """The locales a connection names for itself: an explicit override, else its selected
    Language Set, else its free-text target_locales. (The pre-project-language behavior.)"""
    from app.services import languages

    if override:
        return languages.parse_locales(override)
    return config_locales(conn.config or {})


def bound_project_locales(conn) -> list[str] | None:
    """The ACTIVE target languages of the project a connection RUNS for, or None when the
    connection is not the run connection of a project automation (every plain connection).

    Connected projects (2026-10-13, decision 4): for a bound project the project's own
    language list is the only source of truth. A stopped language (TargetLocale.stopped_at)
    is not in the list. None also when the project has no active language at all, so a
    degenerate project falls back to the connection's own configuration instead of running
    on nothing.

    One entry per language: two rows that are spellings of one language (pt-br and pt-BR, a
    legacy duplicate) give one entry, the first row's spelling."""
    cid = getattr(conn, "id", None)
    if cid is None:
        return None
    from app.models import ProjectAutomation

    with Session(engine) as s:
        pid = s.exec(
            select(ProjectAutomation.project_id).where(ProjectAutomation.run_connection_id == cid)
        ).first()
        if pid is None:
            return None
        rows = s.exec(
            select(TargetLocale)
            .where(TargetLocale.project_id == pid, TargetLocale.stopped_at.is_(None))
            .order_by(TargetLocale.id)
        ).all()
        out = dedupe_locales(r.locale for r in rows if (r.locale or "").strip())
    return out or None


def effective_locales(conn, override=None) -> list[str]:
    """Resolve the target locales for a connector run: an explicit override wins,
    else the connection's selected Language Set, else its free-text target_locales.
    This is how 'one request → many languages' is configured per connection.

    A connection that runs for a project with a content source (a ProjectAutomation) takes
    its languages from the PROJECT instead: its active target languages, in the connection's
    own spelling where it names the same language (wire formats differ per kind). An explicit
    override can only narrow that list."""
    raw = _config_locales(conn, override)
    bound = bound_project_locales(conn)
    if bound is None:
        return raw
    from app.services import languages_catalog

    active = in_connection_spelling(bound, _config_locales(conn))
    if not override:
        return active
    want = {languages_catalog.normalize(l) for l in raw}  # noqa: E741
    return [l for l in active if languages_catalog.normalize(l) in want]  # noqa: E741


def locale_is_active(conn, locale) -> bool:
    """False only when `conn` runs for a project with a content source and `locale` is not one
    of that project's active languages (stopped, or never added). True for every other
    connection, so callers that filter on it leave a plain connection untouched."""
    bound = bound_project_locales(conn)
    if bound is None:
        return True
    from app.services import languages_catalog

    return languages_catalog.normalize(str(locale or "")) in {
        languages_catalog.normalize(l)
        for l in bound  # noqa: E741
    }


def _add_missing_locales(s: Session, project_id: int, locales) -> None:
    """Register any TargetLocale rows the project doesn't have yet.

    Compared by languages_catalog.normalize, never the raw string: a project that holds
    `pt-BR` already has `pt-br` (a connector's wire spelling), and a second row for one
    language makes two segment sets, two run entries and a UNIQUE failure on segment insert.
    The existing row's spelling wins (rows are never renamed); a stopped row stays stopped."""
    if not locales:
        return
    from app.services import languages_catalog

    existing = {
        languages_catalog.normalize(tl.locale)
        for tl in s.exec(select(TargetLocale).where(TargetLocale.project_id == project_id)).all()
    }
    for loc in locales:
        key = languages_catalog.normalize(loc)
        if key and key not in existing:
            s.add(TargetLocale(project_id=project_id, locale=loc))
            existing.add(key)


def _latest_doc(s: Session, conn: Connection) -> Document | None:
    if not conn.project_id:
        return None
    return s.exec(
        select(Document)
        .where(Document.project_id == conn.project_id, Document.fmt == conn.kind)
        .order_by(Document.created_at.desc())
    ).first()


# ---------------------------------------------------------------------------
# Record CREATE / DELETE (round-2 #1; bb-io AddRecord / DeleteRecord).
#
# A narrowly-scoped, MANUAL/API-triggered capability — deliberately NOT wired into
# the auto-translate / webhook loop, and untouched by dynamic_translate's PATCH
# write-back. base/table come from the call or the connection config. Each op runs
# under the shared WriteBudget (a create or a delete is one external write), so a
# runaway bulk request stops cleanly at the same ceiling every other push path uses.
# ---------------------------------------------------------------------------
def _airtable_scope_target(
    cfg: dict, base_id: str | None, table: str | None
) -> tuple[str, str, str]:
    """Resolve + SCOPE-CHECK the base/table a create/delete targets (taxonomy A,
    review MEDIUM). A caller-supplied override MUST match the connection's configured
    base/table EXACTLY (case-exact) — an org-authorized caller of a connection pinned
    to base B1 must not be able to create or (destructively) DELETE in a different
    base B2 the connection's token happens to reach. Fail-closed:

      * no override           -> use the connection's configured base/table.
      * override == config     -> allowed (returns the value).
      * override != config     -> REFUSED with a clear message (empty base/table +
                                   the error string; the caller aborts before any
                                   network op).
    Returns (base, table, error) — error is "" on success. A connection with NO
    configured base/table cannot be targeted by an override (config is ""), which is
    the intended fail-closed posture for the destructive delete path."""
    cfg_base = (cfg.get("base_id") or "").strip()
    cfg_tbl = (cfg.get("table") or "").strip()
    ov_base = (base_id or "").strip()
    ov_tbl = (table or "").strip()
    # Security review 2026-10 (connectors-01/02): the SAME guard now also fronts
    # dynamic_translate (the convention webhook), register_airtable_webhook and
    # schema_fields (the field picker / `connector browse --table`), so no
    # caller-supplied base/table reaches the stored credential outside the
    # connection's configured scope on any surface.
    unset_hint = (
        " Set the base and table on the connection first (Connectors, open the"
        " connection, Default base ID and Default table)."
    )
    if ov_base and ov_base != cfg_base:
        return (
            "",
            "",
            (
                f"this connection is scoped to base {cfg_base or '(none)'!r}; the "
                f"request targeted {ov_base!r}; refused." + ("" if cfg_base else unset_hint)
            ),
        )
    if ov_tbl and ov_tbl != cfg_tbl:
        return (
            "",
            "",
            (
                f"this connection is scoped to table {cfg_tbl or '(none)'!r}; the "
                f"request targeted {ov_tbl!r}; refused." + ("" if cfg_tbl else unset_hint)
            ),
        )
    return (ov_base or cfg_base), (ov_tbl or cfg_tbl), ""


IMPERSONATE_REFUSED = (
    "Only an admin can set or change 'Impersonate user' on a Google Docs connection. It makes the "
    "service account act as that person through domain-wide delegation, so everything the person "
    "can reach in Drive becomes reachable. Ask an admin."
)


def gdocs_impersonation_error(kind, old_cfg, new_cfg, *, caller_is_admin: bool) -> str:
    """'' when allowed, else the refusal text. Domain-wide delegation lets a service account act
    as any user of the Workspace, so only an admin may turn it on or point it at another user
    (clearing it is always fine: it only narrows). Other kinds and admins are unaffected."""
    if kind != "gdocs" or caller_is_admin or "impersonate_user" not in (new_cfg or {}):
        return ""
    new = str((new_cfg or {}).get("impersonate_user") or "").strip().lower()
    old = str((old_cfg or {}).get("impersonate_user") or "").strip().lower()
    return IMPERSONATE_REFUSED if new and new != old else ""


AIRTABLE_RETARGET_REFUSED = (
    "Only an admin can change the base or table of an Airtable connection that holds "
    "a saved credential. To point it at a different base yourself, enter your own "
    "Airtable token in the same save (a connection signed in with OAuth needs an admin)."
)


def _airtable_retarget_error(
    conn,
    new_cfg: dict | None,
    *,
    caller_is_admin: bool,
    new_secret_supplied: bool,
    actor: str = "",
) -> str:
    """The configured base/table IS the scope _airtable_scope_target enforces, so
    moving it is the one remaining way to aim a stored credential at another base
    (security review 2026-10, connectors-01 residual). A non-admin may not move it
    while the connection holds a saved credential, unless the same save supplies a
    fresh credential (their own token, so nothing admin-provisioned is reused).
    Returns "" when allowed, else the refusal message (and writes an audit row).
    Non-Airtable kinds, admins, and connections with no stored credential are
    unaffected.

    Gate fix (i419): a supplied token only counts when it becomes the EFFECTIVE
    credential. On an OAuth-mode secret the connector authenticates with
    secret['oauth']['access_token']; a form api_key is merged beside it and never
    used, so a junk PAT must not unlock the move (the admin's OAuth token would be
    aimed at the new base). Such a connection needs an admin to retarget it."""
    if getattr(conn, "kind", "") != "airtable" or caller_is_admin:
        return ""
    stored = dec_secret(getattr(conn, "secret_enc", "") or "")
    if not any(stored.values()):
        return ""
    if new_secret_supplied and auth_mode(stored) != "oauth":
        return ""
    old = conn.config or {}
    for key in ("base_id", "table"):
        if key in (new_cfg or {}) and (
            str(new_cfg.get(key) or "").strip() != str(old.get(key) or "").strip()
        ):
            audit.log(
                actor=actor or "system",
                action="connector.retarget_refused",
                target=f"connection:{conn.id}",
                detail=f"non-admin change of Airtable {key} refused",
                customer_id=getattr(conn, "customer_id", None),
            )
            return AIRTABLE_RETARGET_REFUSED
    return ""


# Config keys that define WHAT a stored credential reaches (scope) or whether a run writes
# straight to content readers see (live edit), per kind. ServiceNow and Notion shipped
# with it (review gates 2026-10-10); Jira jql and Confluence space_key joined on 2026-10-11
# (#419 residual, the maintainer's B-scope decision).
SCOPE_ADMIN_FIELDS = {
    "servicenow": ("knowledge_base", "kb_category"),
    # Notion: the root decides which shared pages the stored token reads and writes under
    # (every Notion write is live), 2026-10-10 Notion gate.
    "notion": ("parent_page_id",),
    # #419 (2026-10-11 B-scope): the Jira JQL and the Confluence space key are the
    # declared scope every caller request is ANDed against; blank means "anything the
    # stored token can read", so clearing one widens it as much as changing it.
    "jira": ("jql",),
    # Confluence cql (gate 2026-10-11): a scope CQL with no space key is the whole scope,
    # so it is as wide a lever as space_key.
    "confluence": ("space_key", "cql"),
}
# Fields compared exactly (only outer whitespace ignored): a JQL string literal or a
# Confluence personal space key (~accountid) / CQL literal is not safe to fold to lower case.
SCOPE_CASE_SENSITIVE = {("jira", "jql"), ("confluence", "space_key"), ("confluence", "cql")}
LIVE_EDIT_ADMIN_FIELDS = {"servicenow": ("update_published",)}

SCOPE_CHANGE_REFUSED = (
    "Only an admin can change the {label} of a {name} connection that holds a saved "
    "credential (it decides what that credential reads and writes)."
)
LIVE_EDIT_REFUSED = (
    "Only an admin can turn on {label}: with it on, a run edits content readers see at once, "
    "with no review step."
)


def _truthy_cfg(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def scope_admin_error(
    kind: str,
    conn,
    new_cfg: dict | None,
    *,
    caller_is_admin: bool,
    actor: str = "",
) -> str:
    """Refuse a non-admin config write that widens a connection's scope or turns on live
    editing. Returns "" when allowed, else the refusal (and writes an audit row).

    * LIVE_EDIT_ADMIN_FIELDS: turning the flag ON (create or update) is admin only, with
      or without a stored credential; leaving it as it is, or turning it off, is open (the
      web form re-posts every field on each save).
    * SCOPE_ADMIN_FIELDS: on an existing connection that holds a credential, a non-admin
      may not change the value at all (no own-credential escape: a partial re-entry would
      merge over the stored secret). Creating a connection with your own credential is
      unaffected."""
    if caller_is_admin or not isinstance(new_cfg, dict):
        return ""
    from app.services import connectors as _reg

    sp = _reg.spec(kind)
    labels = {f.name: f.label for f in (sp.config_fields if sp else [])}
    name = sp.name if sp else kind
    old = (getattr(conn, "config", None) or {}) if conn is not None else {}
    refusal = ""
    for key in LIVE_EDIT_ADMIN_FIELDS.get(kind, ()):
        if key in new_cfg and _truthy_cfg(new_cfg.get(key)) and not _truthy_cfg(old.get(key)):
            refusal = LIVE_EDIT_REFUSED.format(label=repr(labels.get(key, key)))
            break
    if not refusal and conn is not None:
        from app.services.connectors.store_part2 import _has_credential

        if _has_credential(conn):
            for key in SCOPE_ADMIN_FIELDS.get(kind, ()):
                new_v = str(new_cfg.get(key) or "").strip()
                old_v = str(old.get(key) or "").strip()
                if (kind, key) not in SCOPE_CASE_SENSITIVE:
                    new_v, old_v = new_v.lower(), old_v.lower()
                if key in new_cfg and new_v != old_v:
                    refusal = SCOPE_CHANGE_REFUSED.format(
                        label=labels.get(key, key).lower(), name=name
                    )
                    break
    if refusal:
        audit.log(
            actor=actor or "system",
            action="connector.scope_change_refused",
            target=f"connection:{conn.id}" if conn is not None else f"kind:{kind}",
            detail=f"non-admin {kind} scope/live-edit change refused",
            customer_id=getattr(conn, "customer_id", None),
        )
    return refusal


# ---------------------------------------------------------------------------
# Airtable official-Webhooks-API subsystem -> relocated to airtable_webhooks.py
# (Wave 3 store.py decomposition). Thin delegators keep store.<fn> importable for
# the web/CLI/api entry points, the scheduler + job worker, and the verify suites.
# ---------------------------------------------------------------------------
def refresh_webhooks(cid: int, now: datetime | None = None, transport=None) -> tuple[int, str]:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw.refresh_webhooks(cid, now=now, transport=transport)


def register_airtable_webhook(
    cid: int,
    base_id: str | None = None,
    table: str | None = None,
    source_field: str | None = None,
    actor: str = "",
    transport=None,
) -> tuple[bool, str]:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw.register_airtable_webhook(
        cid,
        base_id=base_id,
        table=table,
        source_field=source_field,
        actor=actor,
        transport=transport,
    )


def unregister_airtable_webhook(cid: int, actor: str = "", transport=None) -> tuple[bool, str]:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw.unregister_airtable_webhook(cid, actor=actor, transport=transport)


def airtable_webhook_status(cid: int) -> dict:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw.airtable_webhook_status(cid)


def check_airtable_webhook_health(cid: int, transport=None) -> tuple[bool, str, dict]:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw.check_airtable_webhook_health(cid, transport=transport)


def consume_airtable_payloads(
    cid: int, actor: str = "", transport=None, force: bool = False
) -> tuple[bool, str, dict]:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw.consume_airtable_payloads(cid, actor=actor, transport=transport, force=force)


def _public_base_url() -> tuple[str, str]:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw._public_base_url()


def _add_pending_records(cid: int, record_ids: list[str]) -> None:
    from app.services.connectors import airtable_webhooks as _aw

    return _aw._add_pending_records(cid, record_ids)


def _jira_harvest_fields(issues: list) -> dict:
    """Aggregate the per-issue metadata (jira.fetch_issues -> issue['meta']) into ONE
    harvest-input dict for a jira_translate run's single Document (which spans EVERY
    matched issue). Ambiguity discipline:

      * domain (project name) / content_kind (issuetype name): supplied ONLY when the
        whole run shares exactly ONE distinct value (the normal scoped-connection
        case — a connection is usually one project). Mixed values -> omitted (a merged
        or first-wins guess would be misleading context).
      * source.title / source.labels: supplied ONLY for a single-issue run — an
        aggregate title across many issues is meaningless, and unioned labels are noise.

    Everything omitted here degrades to None in harvest.build (byte-stable). Every
    value is still routed through validate_and_sanitize by harvest.build downstream."""
    projects = {m for i in issues if (m := (i.get("meta") or {}).get("project"))}
    itypes = {m for i in issues if (m := (i.get("meta") or {}).get("issuetype"))}
    f: dict = {}
    if len(projects) == 1:
        f["domain"] = next(iter(projects))
    if len(itypes) == 1:
        f["content_kind"] = next(iter(itypes))
    if len(issues) == 1:
        only = issues[0]
        f["title"] = only.get("summary") or None
        f["labels"] = (only.get("meta") or {}).get("labels") or None
    return f


# --- Jira outbound attachments -> relocated to jira.py (Wave 3 store.py decomposition).
# Thin delegators: _jira_translate_attachments is called by jira_translate above and by
# the verify suites (cstore._jira_translate_attachments); _attachment_allowlist by the
# verify suites. See jira.py for the full guard inventory (SSRF caps + allowlist etc.).
def _attachment_allowlist(cfg) -> set:
    from app.services.connectors import jira as _j

    return _j._attachment_allowlist(cfg)


def _jira_translate_attachments(
    cid: int,
    issue_keys,
    *,
    locales=None,
    target_locales=None,
    actor: str = "",
    transport=None,
    budget=None,
    jr=None,
) -> dict:
    from app.services.connectors import jira as _j

    return _j._jira_translate_attachments(
        cid,
        issue_keys,
        locales=locales,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        budget=budget,
        jr=jr,
    )


def jira_auto_translate_on(conn) -> bool:
    """True iff this Jira connection opted into auto-translate on a verified push.
    Default OFF (button-trigger posture). Accepts the usual truthy config spellings."""
    if not conn:
        return False
    v = (conn.config or {}).get("auto_translate_on_change", "0")
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _canonical_locale(loc: str) -> str:
    """Catalog-canonical display form of a Zendesk wire-format locale code (e.g.
    'pt-br' -> 'pt-BR', 'es' -> 'es-ES' when the catalog's canonical form for that
    bare language differs), for STORING TargetLocale rows only. Uses the same
    public catalog canonicalization `pair_providers._canon` uses
    (`languages_catalog.validate_codes`) rather than duplicating or importing that
    private helper. Falls back to the original Zendesk code, unchanged, when the
    catalog doesn't recognize it — an unknown code is still better stored as-is
    than guessed at, and provider resolution never depends on this shape anyway
    (it always compares via languages_catalog.normalize / pair_providers._norm).
    NEVER use this for a Zendesk API call — the connector's own wire codes
    (lowercase, e.g. 'pt-br') are what Guide expects and must round-trip
    unchanged; only the stored TargetLocale.locale row is canonicalized."""
    from app.services import languages_catalog

    valid, _invalid = languages_catalog.validate_codes([loc])
    return valid[0] if valid else loc


# ===========================================================================
# Per-connector translate flows — thin delegators over the shared flow engine.
#
# FABLE-ARCH-REVIEW-2026-07-07 Finding 1 collapsed the near-identical T2/T3 flows
# onto ONE implicit pipeline (resolve locales -> egress guard -> discover ->
# change-gate -> ensure project/doc -> pretranslate -> refusal-gated write-back ->
# WriteBudget ceiling -> record hashes/watermark -> log + attribution) living once
# in flow.run_translate_flow; each connector supplies only its vendor-specific hooks
# via a flow.TranslateFlowSpec. Wave 3 (store.py decomposition) then RELOCATED each
# `_<kind>_*` hook bundle + `<kind>_translate` body INTO the connector's own module;
# the thin delegators below keep `store.<kind>_translate` importable + monkeypatchable
# (jobs webhook dispatch, CLI getattr, /api, web, and the verify suites all use it).
# Public signatures, message wording, result-dict keys, and persisted config state
# stay byte-identical to the hand-written flows (tests/verify_flow_engine_parity.py).
# jira/confluence/airtable dynamic flows stay in store.py (the Wave-4 flow.py
# generalization target).
# ===========================================================================


def zendesk_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> zendesk.zendesk_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import zendesk as _m

    return _m.zendesk_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def document360_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> document360.document360_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import document360 as _m

    return _m.document360_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def freshdesk_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> freshdesk.freshdesk_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import freshdesk as _m

    return _m.freshdesk_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def zoho_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> zoho.zoho_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import zoho as _m

    return _m.zoho_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def intercom_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> intercom.intercom_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import intercom as _m

    return _m.intercom_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def salesforce_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> salesforce.salesforce_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import salesforce as _m

    return _m.salesforce_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def salesforce_rediscover_fields(
    cid: int, actor: str = "", transport=None
) -> tuple[bool, str, dict]:
    """Thin delegator -> salesforce.salesforce_rediscover_fields (Wave 3)."""
    from app.services.connectors import salesforce as _m

    return _m.salesforce_rediscover_fields(cid, actor=actor, transport=transport)


def notion_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> notion.notion_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import notion as _m

    return _m.notion_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def notion_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Thin delegator -> notion.notion_resolve_link (Wave 3)."""
    from app.services.connectors import notion as _m

    return _m.notion_resolve_link(cid, source_id, target_locale, action, actor=actor)


def helpscout_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> helpscout.helpscout_translate (Wave 3: body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/webhook/tests."""
    from app.services.connectors import helpscout as _m

    return _m.helpscout_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def helpscout_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Thin delegator -> helpscout.helpscout_resolve_link (Wave 3)."""
    from app.services.connectors import helpscout as _m

    return _m.helpscout_resolve_link(cid, source_id, target_locale, action, actor=actor)


def wordpress_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Thin delegator -> wordpress.wordpress_resolve_link (SPEC-t3 rule 6 adopt/ignore)."""
    from app.services.connectors import wordpress as _m

    return _m.wordpress_resolve_link(cid, source_id, target_locale, action, actor=actor)


def strapi_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> strapi.strapi_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import strapi as _m

    return _m.strapi_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def contentful_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> contentful.contentful_translate (body lives in the connector
    module). Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import contentful as _m

    return _m.contentful_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def shopify_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> shopify.shopify_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import shopify as _m

    return _m.shopify_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def hubspot_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> hubspot.hubspot_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import hubspot as _m

    return _m.hubspot_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def akeneo_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> akeneo.akeneo_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import akeneo as _m

    return _m.akeneo_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def mcp_cms_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> mcp_cms.mcp_cms_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import mcp_cms as _m

    return _m.mcp_cms_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def github_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> github.github_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import github as _m

    return _m.github_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def confluence_translate(
    cid: int,
    page_ids: list | None = None,
    space_key: str | None = None,
    cql: str | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    article_ids: list | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> confluence.confluence_translate (parity rebuild: the body lives in
    the connector module and rides the shared flow engine). `page_ids` / `space_key` / `cql`
    are Confluence's own request shape (API, CLI, webhook job); kept importable and
    monkeypatchable on store."""
    from app.services.connectors import confluence as _m

    return _m.confluence_translate(
        cid,
        page_ids=page_ids,
        space_key=space_key,
        cql=cql,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        article_ids=article_ids,
        source=source,
    )


def msgraph_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> msgraph.msgraph_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import msgraph as _m

    return _m.msgraph_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def plytix_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> plytix.plytix_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import plytix as _m

    return _m.plytix_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def sitecore_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> sitecore.sitecore_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import sitecore as _m

    return _m.sitecore_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def sanity_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> sanity.sanity_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import sanity as _m

    return _m.sanity_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def gdocs_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> gdocs.gdocs_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import gdocs as _m

    return _m.gdocs_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def drupal_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> drupal.drupal_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import drupal as _m

    return _m.drupal_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def bitbucket_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> bitbucket.bitbucket_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import bitbucket as _m

    return _m.bitbucket_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def gitlab_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> gitlab.gitlab_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import gitlab as _m

    return _m.gitlab_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def servicenow_translate(
    cid: int,
    article_ids: list | None = None,
    target_locales=None,
    actor: str = "",
    transport=None,
    only_pairs: set | None = None,
    source: str = "manual",
) -> tuple[bool, str, dict]:
    """Thin delegator -> servicenow.servicenow_translate (body lives in the connector module).
    Kept importable/monkeypatchable on store for jobs/CLI/tests."""
    from app.services.connectors import servicenow as _m

    return _m.servicenow_translate(
        cid,
        article_ids=article_ids,
        target_locales=target_locales,
        actor=actor,
        transport=transport,
        only_pairs=only_pairs,
        source=source,
    )


def servicenow_resolve_link(
    cid: int, source_id: str, target_locale: str, action: str, actor: str = ""
) -> tuple[bool, str, dict]:
    """Thin delegator -> servicenow.servicenow_resolve_link (SPEC-t3 rule 6 adopt/ignore)."""
    from app.services.connectors import servicenow as _m

    return _m.servicenow_resolve_link(cid, source_id, target_locale, action, actor=actor)


# factory:anchor store.delegator  (scripts/factory inserts new *_translate delegators above this line)  # noqa: E501
