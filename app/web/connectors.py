"""Connectors UI (connections CRUD + actions) and language sets.

Transitional compatibility shim for one release (added 2026-09-14, remove the re-export block after
the next tagged release): `__all__` re-exports _safe_int_id, _secret_display, _customers,
_content_types, _public_base_url, ArchivedCustomerScopeError, _valid_customer_id,
PLANNED_CONNECTORS, connectors_index, connectors_manage, connector_kind_toggle, connectors_catalog,
connector_type, _form_to_cfg_secret, connector_oauth_callback, _notion_idmap, _helpscout_idmap,
_wordpress_idmap, _servicenow_idmap, contentful_blocked_list, _langset_or_404, languages_index from
app.web.connectors_common.

Documented in: docs/architecture/connectors.md
"""

import secrets
from copy import copy as _srp_copy
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.kernel import (
    access,
    audit,
    connected_projects,
    ids,
    jobs,
    languages,
    scheduler,
    security,
    settings,
)
from app.kernel import connectors as connectors_registry
from app.kernel.connectors import oauth as conn_oauth
from app.kernel.connectors import store as conn_store
from app.kernel.providers import provider_is_live
from app.models import ConnStatus, Customer, Project
from app.services.connectors.base import ConnectorError
from app.web import deps
from app.web.connectors_common import (
    PLANNED_CONNECTORS,
    ArchivedCustomerScopeError,
    _content_types,
    _customers,
    _form_to_cfg_secret,
    _helpscout_idmap,
    _langset_or_404,
    _notion_idmap,
    _public_base_url,
    _safe_int_id,
    _secret_display,
    _servicenow_idmap,
    _valid_customer_id,
    _wordpress_idmap,
    allow_oauth_form_action,
    connector_kind_toggle,
    connector_oauth_callback,
    connector_type,
    connectors_catalog,
    connectors_index,
    connectors_manage,
    contentful_blocked_list,
    languages_index,
)
from app.web.connectors_common import router as connectors_common_router
from app.web.deps import (
    _actor_email,
    _admin_block,
    _ai_block,
    _error_page,
    _ip,
    _my_customer_id,
    _my_org_id,
    _org_block,
    connector_api_token,
    templates,
)

__all__ = [
    "PLANNED_CONNECTORS",
    "ArchivedCustomerScopeError",
    "ConnStatus",
    "Customer",
    "_admin_block",
    "_content_types",
    "_customers",
    "_form_to_cfg_secret",
    "_helpscout_idmap",
    "_ip",
    "_langset_or_404",
    "_notion_idmap",
    "_public_base_url",
    "_safe_int_id",
    "_secret_display",
    "_servicenow_idmap",
    "_valid_customer_id",
    "_wordpress_idmap",
    "access",
    "audit",
    "config",
    "connector_kind_toggle",
    "connector_oauth_callback",
    "connector_type",
    "connectors_catalog",
    "connectors_index",
    "connectors_manage",
    "contentful_blocked_list",
    "ids",
    "languages_index",
    "provider_is_live",
    "select",
]

router = APIRouter()


def _conn_action_redirect(cid: int, ok, msg: str) -> RedirectResponse:
    """Redirect back to a connection page carrying an ok/msg flash (#501).

    The message is percent-encoded so a provider error can never corrupt the
    query: interpolated raw, a '#' truncates everything after it and a stray
    '&ok=1' inside the text adds a SECOND 'ok' that Starlette's
    QueryParams.get returns -- so a failure renders as a green success banner
    with a cut-off message. urlencode guarantees exactly one 'ok' and that msg
    round-trips intact (the same reason the OAuth callback encodes, #270).
    """
    return RedirectResponse(
        f"/connectors/connection/{cid}?" + urlencode({"ok": int(ok), "msg": msg}),
        status_code=303,
    )


def _conn_or_404(request, cid):
    """Fetch a connection and org-gate it in one shot (leak vector 10 — connectors
    had ZERO object authz before P3). Returns (conn, blocked): `blocked` is a
    ready-to-return 404 when the connection is missing OR belongs to another org
    (a scoped user must not learn a foreign cid exists — 404, not 403); else
    (conn, None). The stored project must also be visible to the caller, including
    for global connections on single-tenant installs (#370). Callers that historically
    redirected to /connectors on a missing row keep that behaviour by passing the
    404 straight back only when they need HTML — the JSON pickers return 404 too."""
    conn = conn_store.get(cid)
    if conn is None:
        return None, _error_page(request, 404, "Not found.")
    blocked = _org_block(request, getattr(conn, "org_id", None))
    if blocked:
        return None, blocked
    # R2-3: a customer-bound user may reach only its OWN customer's connection or a
    # global/unassigned one (customer_id NULL) — a DIFFERENT customer's connection
    # is 404 (not enumerable), matching the org rule. No-op for internal staff.
    my_cust = _my_customer_id(request)
    conn_cust = getattr(conn, "customer_id", None)
    if my_cust is not None and conn_cust is not None and conn_cust != my_cust:
        return None, _error_page(request, 404, "Not found.")
    if conn.project_id is not None:
        with Session(engine) as s:
            project = s.get(Project, conn.project_id)
            if project is None or not access.can_view_project(
                s, security.session_user(request), project
            ):
                return None, _error_page(request, 404, "Not found.")
    # ffadmin Wave 5: a connection whose KIND is disabled on this install is 404 on every
    # connection page + action route (this is THE chokepoint every /connectors/connection/
    # {cid}/* route funnels through). Preserved-but-hidden: the row is untouched and
    # returns when the kind is re-enabled.
    if not connectors_registry.kind_enabled(conn.kind):
        return None, _error_page(request, 404, "Not found.")
    return conn, None


_srp_piece_router = connectors_common_router
connectors_common_router = _srp_copy(connectors_common_router)
connectors_common_router.routes = connectors_common_router.routes[0:5]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(connectors_common_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
connectors_common_router = _srp_piece_router


@router.post("/connectors/{kind}")
async def connector_create(kind: str, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    sp = connectors_registry.spec(kind)
    # ffadmin Wave 5 -- DOCTRINE: a DISABLED kind is byte-identical to an unknown one on
    # create too (same redirect), so creation is refused non-enumerably (indistinguishable
    # from a typo), together-with the spec() check and BEFORE the archived branch.
    if not sp or not connectors_registry.kind_enabled(kind):
        return RedirectResponse("/connectors", status_code=303)
    # §6b: an archived connector accepts NO new connections. Existing connections
    # keep working — only creation is refused, with a clear message on its page.
    if connectors_registry.is_archived(kind):
        from urllib.parse import quote

        return RedirectResponse(
            f"/connectors/{kind}?msg="
            + quote(
                f"The {sp.name} connector is archived and no longer accepts new "
                f"connections. Existing {sp.name} connections keep working."
            ),
            status_code=303,
        )
    form = await request.form()
    cfg, secret = _form_to_cfg_secret(sp, form)
    imp_err = conn_store.gdocs_impersonation_error(
        kind,
        {},
        cfg,
        caller_is_admin=(not security.auth_required()) or security.is_admin(_me),
    )
    if imp_err:
        from urllib.parse import quote

        return RedirectResponse(f"/connectors/{kind}?msg=" + quote(imp_err), status_code=303)
    # Review gate 2026-10-10: live-edit opt-ins (ServiceNow update_published) are admin only.
    scope_err = conn_store.scope_admin_error(
        kind,
        None,
        cfg,
        caller_is_admin=(not security.auth_required()) or security.is_admin(_me),
        actor=_actor_email(request),
    )
    if scope_err:
        from urllib.parse import quote

        return RedirectResponse(f"/connectors/{kind}?msg=" + quote(scope_err), status_code=303)
    # Stamp the creating user's org so this connection + its /connection/{cid}/*
    # routes are org-scoped (leak vector 10). No-op (org_id NULL) with the gate off.
    # customer_id (P4, item 5) scopes the connection to one customer (blank = global).
    # content_type_id is passed RAW; store.create validates it at the seam against the
    # connection's RESOLVED customer/org (Findings 1+2), so it can't be planted cross-
    # scope regardless of surface. customer_id is pre-validated by _valid_customer_id
    # (same-org + caller-binding + T2-01 archived check) AND re-confined at the store
    # seam via bound_customer_id (task_bcae962f) — a bound caller can never plant a
    # foreign customer scope on either layer.
    try:
        conn = await deps.offload_blocking(
            conn_store.create,
            kind,
            (form.get("name") or "").strip(),
            cfg,
            secret,
            actor=_actor_email(request),
            org_id=_my_org_id(request),
            customer_id=_valid_customer_id(request, form.get("customer_id")),
            bound_customer_id=_my_customer_id(request),
            content_type_id=form.get("content_type_id"),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    except (ArchivedCustomerScopeError, ConnectorError) as exc:
        # ConnectorError: a refused destination URL (gate round 2, an unreadable origin).
        # T2-01: an archived (or otherwise not-selectable) explicit customer_id is a
        # clean redirect back to the add-form, never a silently-global connection.
        from urllib.parse import quote

        return RedirectResponse(f"/connectors/{kind}?msg=" + quote(str(exc)), status_code=303)
    # Connected projects: a connection made from a project's setup menu goes back to that
    # project with the new connection preselected (validated in-site path only).
    back = connected_projects.safe_return_to(form.get("return_to"))
    if back:
        return RedirectResponse(
            connected_projects.with_params(back.split("#", 1)[0], connection=conn.id) + "#source",
            status_code=303,
        )
    return RedirectResponse(f"/connectors/connection/{conn.id}", status_code=303)


# --- OAuth connect flow (Airtable PKCE + Atlassian 3LO) -------------------------
# begin_authorize's `state` is the callback's CSRF defense AND the binding to the
# connection that started it (a callback whose state doesn't match the session, or
# whose bound cid differs, can't finish). The callback lives at
# /connectors/oauth/callback — session-authed under the NORMAL middleware, NOT
# under /api (that prefix is session-blind by design) and NOT under /auth (the
# OIDC-only exemption). See app/main.py's carve-out notes and oauth.py.
def _mcp_consent_page(url: str):
    """A tiny page that sends the browser on to the (already SSRF-validated) authorization
    endpoint, naming the host first so the operator sees where consent happens."""
    from html import escape
    from urllib.parse import urlsplit

    from fastapi.responses import HTMLResponse

    host = escape(urlsplit(url).netloc)
    link = escape(url, quote=True)
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={link}">'
        "<title>Continue to authorize</title></head><body>"
        f'<p>Continue to <a href="{link}" rel="noreferrer">{host}</a> to approve access.</p>'
        "</body></html>"
    )
    return HTMLResponse(
        body, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
    )


@router.post("/connectors/{kind}/oauth/start")
async def connector_oauth_start(kind: str, request: Request):
    """Start the OAuth authorization-code flow for a connector kind.

    Creates the Connection row FIRST (status NEW) so `state` binds a real cid, then
    redirects to the provider's authorize URL. The only config we read from the
    add-form here is name + base_url (Atlassian needs the Site URL to (a) resolve
    the right cloudid among multiple sites and (b) let the user still round-trip a
    friendly site link); the rest of the config is filled in on the connection page
    after connecting. No secret is stored yet — the callback writes the v2 OAuth
    secret when the provider returns tokens.

    Create-OR-REUSE: when the form carries a `cid` for an EXISTING connection of
    this same kind (the "Reconnect with OAuth" button on connection.html), that
    connection is reused as-is — no new row is created — so a reconnect re-runs
    the same connection's authorization instead of spawning a duplicate. Any
    `base_url` submitted alongside a reused cid is folded into that connection's
    config the same way a fresh create would set it, since a reconnect's hidden
    form carries the connection's existing base_url along for the Atlassian
    host-hint (there's no live add-form to read it from otherwise)."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    sp = connectors_registry.spec(kind)
    # ffadmin Wave 5 -- DOCTRINE: a DISABLED kind is byte-identical to an unknown / non-
    # OAuth one here (same redirect), together-with the spec()/supports_oauth check.
    if (
        not sp
        or not getattr(sp, "supports_oauth", False)
        or not connectors_registry.kind_enabled(kind)
    ):
        return RedirectResponse("/connectors", status_code=303)
    if not conn_oauth.provider_app_configured(kind):
        return RedirectResponse(f"/connectors/{kind}?created=oauth_unconfigured", status_code=303)
    form = await request.form()
    base_url = (form.get("base_url") or "").strip()
    raw_cid = (form.get("cid") or "").strip()
    conn = None
    reuse_cid = _safe_int_id(raw_cid)
    if reuse_cid is not None:
        # HIGH-2 (task_bcae962f review): the reused cid must pass the FULL object
        # predicate _conn_or_404 (org + R2-3 customer ring), not just same_org — else a
        # bound-A caller could name a foreign-B cid and this reconnect would
        # patch_config() it now and let the OAuth callback OVERWRITE customer B's
        # credentials with the attacker's tokens. A cid the caller may not reach (wrong
        # org OR foreign customer) is treated as if it didn't exist -> _conn_or_404
        # returns (None, blocked), so `existing` is None and we fall through to create,
        # never mutating the foreign row. A malformed cid (non-digit, a Unicode
        # digit like '2' that int() rejects, or an oversized value SQLite rejects
        # at the bind layer) coerces to None here and takes the same fall-through-
        # to-create path as a stale id, never an unhandled 500.
        existing, _notfound = _conn_or_404(request, reuse_cid)
        if existing and existing.kind == kind:
            conn = existing
            # mcp_cms never takes a posted base_url: its OAuth discovery and token traffic go
            # only to the SAVED connection's MCP URL, and a reconnect form must not be able to
            # rewrite any config on the way (gate 2026-10-10).
            if (
                kind != "mcp_cms"
                and base_url
                and base_url != (existing.config or {}).get("base_url")
            ):
                # actor threaded so the host-change audit row names the caller: this is the
                # one path where a LINGUIST moves a connection's credential destination.
                try:
                    await deps.offload_blocking(
                        conn_store.patch_config,
                        existing.id,
                        {"base_url": base_url},
                        actor=_actor_email(request),
                    )
                except deps.WorkerBusy as exc:
                    return deps.busy_response(str(exc))
                except ConnectorError as exc:  # an unreadable base URL (gate round 2)
                    from urllib.parse import quote

                    return RedirectResponse(
                        f"/connectors/connection/{existing.id}?ok=0&msg=" + quote(str(exc)),
                        status_code=303,
                    )
    if kind == "mcp_cms":
        # MCP authorization needs the connection's own MCP URL, so it can only RECONNECT an
        # existing connection (never mint one here). Discovery makes guarded network calls, so
        # it runs off the event loop. The browser is handed an interstitial link instead of a
        # 303: the authorization server is chosen by the MCP server, so it cannot be named in
        # the form-action CSP of the page that posted the form.
        from urllib.parse import quote

        if conn is None:
            return RedirectResponse(
                "/connectors/mcp_cms?msg="
                + quote("Create the connection first, then use Connect with OAuth on its page."),
                status_code=303,
            )
        try:
            url = await deps.offload_blocking(
                conn_oauth.begin_authorize, request, kind, conn.id, connection=conn
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except (ConnectorError, ValueError) as exc:
            return RedirectResponse(
                f"/connectors/connection/{conn.id}?ok=0&msg=" + quote(str(exc)), status_code=303
            )
        return _mcp_consent_page(url)
    if conn is None:
        # §6b (HIGH-1 fix): the archived gate is on CREATION only. We reach here
        # with conn is None exactly when no reusable existing connection resolved,
        # i.e. this call would MINT a new row — refuse that for an archived kind
        # (e.g. Confluence, which supports OAuth). RE-AUTHORIZATION of an EXISTING
        # archived connection took the reuse branch above (conn is not None) and is
        # NOT refused, so the connection.html Reconnect button keeps working for a
        # connection in reauth/expired state — the "existing connections keep
        # working untouched" guarantee.
        if connectors_registry.is_archived(kind):
            from urllib.parse import quote

            return RedirectResponse(
                f"/connectors/{kind}?msg="
                + quote(
                    f"The {sp.name} connector is archived and no longer accepts new "
                    f"connections. Existing {sp.name} connections keep working."
                ),
                status_code=303,
            )
        cfg = {}
        if base_url:
            cfg["base_url"] = base_url
        # F6 (accepted): if the user abandons the provider consent screen this NEW
        # row is left behind with an empty secret_enc. That's harmless by design —
        # a row with no stored secret can never authenticate (build/test/refresh all
        # fail on empty creds), so it's inert clutter, not a security or data risk.
        # No cleanup mechanism is provided; the user can delete it from the UI.
        # task_bcae962f (review fold-in): a NEW connection created by a customer-BOUND
        # caller is force-to-own'd to their own customer via bound_customer_id — the
        # SAME create-seam idiom connector_create uses — so a bound caller's OAuth
        # connection is correctly scoped, not left global. No-op for unbound callers.
        try:
            conn = await deps.offload_blocking(
                conn_store.create,
                kind,
                (form.get("name") or "").strip(),
                cfg,
                {},
                actor=_actor_email(request),
                org_id=_my_org_id(request),
                bound_customer_id=_my_customer_id(request),
            )
        except deps.WorkerBusy as exc:
            return deps.busy_response(str(exc))
        except ConnectorError as exc:  # an unreadable base URL (gate round 2)
            from urllib.parse import quote

            return RedirectResponse(f"/connectors/{kind}?msg=" + quote(str(exc)), status_code=303)
    # For Atlassian, bind the connection's Site URL host into the cloudid hook so a
    # multi-site app resolves THIS site's cloudid (falls back to the first site).
    host = ""
    if base_url:
        from urllib.parse import urlsplit

        host = urlsplit(base_url if "://" in base_url else "https://" + base_url).hostname or ""
    try:
        url = conn_oauth.begin_authorize(
            request, kind, conn.id, base_url_host=host, connection=conn
        )
    except ValueError:
        return RedirectResponse(f"/connectors/{kind}?created=oauth_unavailable", status_code=303)
    # Connected projects: "Connect a new system" from a project's setup menu. The in-site path to
    # come back to rides in the server-side OAuth session state (never in the provider URL), is
    # validated here and again at the callback.
    # A flow started without one must never inherit the return_to of an earlier, abandoned one.
    request.session.pop("conn_oauth_return", None)
    back = connected_projects.safe_return_to(form.get("return_to"))
    if back:
        request.session["conn_oauth_return"] = back
    return RedirectResponse(url, status_code=303)


_srp_piece_router = connectors_common_router
connectors_common_router = _srp_copy(connectors_common_router)
connectors_common_router.routes = connectors_common_router.routes[5:6]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(connectors_common_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
connectors_common_router = _srp_piece_router


def _languages_owner(request, conn):
    """Connected projects: (the project whose language list this connection follows, or None;
    the projects that copy this connection through their own lane, those the caller may see).
    The connection page says so above its Languages card."""
    from app.models import ProjectAutomation

    me = security.session_user(request)
    with Session(engine) as s:
        pid = conn_store.languages_project_id(conn.id)
        owner = s.get(Project, pid) if pid is not None else None
        feeds = []
        if owner is None:
            for row in s.exec(
                select(ProjectAutomation).where(ProjectAutomation.connection_id == conn.id)
            ).all():
                p = s.get(Project, row.project_id)
                if p is not None and access.can_view_project(s, me, p):
                    feeds.append(p)
    return owner, feeds


@router.get("/connectors/connection/{cid}")
def connection_detail(cid: int, request: Request):
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    sp = connectors_registry.spec(conn.kind)
    allow_oauth_form_action(request, conn.kind)
    project = None
    if conn.project_id:
        with Session(engine) as s:
            project = s.get(Project, conn.project_id)
    languages_project, languages_feeds = _languages_owner(request, conn)
    schedule_minutes = scheduler.interval_minutes(conn)
    schedule_options = list(scheduler.PRESETS)
    if schedule_minutes and schedule_minutes not in [m for m, _ in schedule_options]:
        schedule_options.append((schedule_minutes, f"Every {schedule_minutes} minutes (custom)"))
        schedule_options.sort()
    secret = conn_store.dec_secret(conn.secret_enc)
    oauth_dict = secret.get("oauth") or {}
    expires_at = oauth_dict.get("expires_at") or 0
    expires_display = (
        datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
        if expires_at
        else "never (no expiry returned)"
    )
    # D7: the template gets a masked view (see _secret_display), never the raw
    # decrypted dict — `is_oauth`/`oauth_dict` are derived from the raw secret
    # right here, above, before it goes out of scope.
    is_oauth = conn_store.auth_mode(secret) == "oauth"
    secret_display = _secret_display(sp, secret)
    return templates.TemplateResponse(
        request,
        "connection.html",
        {
            "request": request,
            "conn": conn,
            "spec": sp,
            "project": project,
            "customers": _customers(request),
            "content_types": _content_types(request),
            "secret": secret_display,
            "is_oauth": is_oauth,
            "oauth_scope": oauth_dict.get("scope", ""),
            "oauth_expires_display": expires_display,
            "history": conn_store.history(cid),
            "can_manage": security.can_tune_ai(security.session_user(request)),
            # ADMIN only, separate from the template's can_manage: see connectors_common
            # for why, including GATE R5 HIGH-1, where this predicate was can_manage and
            # let a MANAGER read the token and then bypass can_tune_ai through /api.
            "can_see_token": security.is_admin(security.session_user(request)),
            "base_url": settings.external_base_url(request),
            "api_token": (
                connector_api_token()
                # ADMIN, NOT can_tune_ai and NOT can_manage: a customer-BOUND linguist
                # passes can_tune_ai, and this token resolves to
                # {"kind":"connector","org_id":None} with no user, so _ident_customer_id is
                # None and the token is NOT customer-confined. Handing it to a bound
                # linguist lets them escape the R2-3 customer ring across the whole
                # /api/connectors surface; handing it to a manager is a can_tune_ai bypass.
                if security.is_admin(security.session_user(request))
                else ""
            ),
            # PUBLIC base URL for push intake (Automation web request / webhooks): a
            # provider in Atlassian's cloud must reach us over public HTTPS, so this is
            # the configured app_base_url (a tunnel / Fly URL), independent of the request Host
            # (which is localhost in dev). Empty/localhost -> the template shows the
            # tunnel-required guidance instead of an unreachable URL.
            "public_base_url": _public_base_url(),
            "jira_pending": conn_store.jira_pending(cid) if conn.kind == "jira" else [],
            "language_sets": languages.list_sets(org_id=_my_org_id(request)),
            "language_set_id": (conn.config or {}).get("language_set_id", ""),
            "effective_locales": conn_store.effective_locales(conn),
            "languages_project": languages_project,
            "languages_feeds": languages_feeds,
            "schedule_minutes": schedule_minutes,
            "schedule_options": schedule_options,
            "scheduler_on": scheduler.enabled_globally(),
            "airtable_webhook": (
                conn_store.airtable_webhook_status(cid) if conn.kind == "airtable" else None
            ),
            "notion_idmap": _notion_idmap(conn),
            "helpscout_idmap": _helpscout_idmap(conn),
            "wordpress_idmap": _wordpress_idmap(conn),
            "servicenow_idmap": _servicenow_idmap(conn),
            "contentful_blocked": contentful_blocked_list(conn),
            "msg": request.query_params.get("msg"),
            "ok": request.query_params.get("ok"),
        },
    )


@router.post("/connectors/connection/{cid}")
async def connection_update(cid: int, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    sp = connectors_registry.spec(conn.kind)
    form = await request.form()
    cfg, secret = _form_to_cfg_secret(sp, form)
    # Security review 2026-10 (connectors-01 residual): the configured Airtable
    # base/table is the scope every per-call override is checked against; a
    # non-admin may move it only while also entering their own credential.
    retarget_err = conn_store._airtable_retarget_error(
        conn,
        cfg,
        caller_is_admin=(not security.auth_required()) or security.is_admin(_me),
        new_secret_supplied=any((secret or {}).values()),
        actor=_actor_email(request),
    ) or conn_store.scope_admin_error(
        conn.kind,
        conn,
        cfg,
        caller_is_admin=(not security.auth_required()) or security.is_admin(_me),
        actor=_actor_email(request),
    )
    if retarget_err:
        from urllib.parse import quote

        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=" + quote(retarget_err), status_code=303
        )
    imp_err = conn_store.gdocs_impersonation_error(
        conn.kind,
        conn.config,
        cfg,
        caller_is_admin=(not security.auth_required()) or security.is_admin(_me),
    )
    if imp_err:
        from urllib.parse import quote

        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=" + quote(imp_err), status_code=303
        )
    # The form carries only spec fields; update() merges server-side over the
    # freshly-read config, so keys owned by other cards (language_set_id, sync_*)
    # survive without racing a stale client-side snapshot. customer_id (P4) is only
    # changed when the scope field is present in the POST.
    kwargs = {}
    if "customer_id" in form:
        # item 6 / task_bcae962f / T2-01: a cross-org OR ARCHIVED customer id (forged
        # POST, or a customer archived after the dropdown was rendered) now RAISES a
        # clean error instead of silently dropping to '' (global); a customer-BOUND
        # caller is still confined to its own customer — both here (_valid_customer_id)
        # AND at the store seam (bound_customer_id below). No-op with the gate off /
        # for internal staff.
        try:
            kwargs["customer_id"] = _valid_customer_id(request, form.get("customer_id"))
        except ArchivedCustomerScopeError as exc:
            from urllib.parse import quote

            return RedirectResponse(
                f"/connectors/connection/{cid}?ok=0&msg=" + quote(str(exc)), status_code=303
            )
    if "content_type_id" in form:
        # SPEC-content-types phase 3: passed RAW; store.update validates it at the seam
        # against the connection's resolved customer/org (Findings 1+2) — a forged/
        # foreign id is dropped there, never planted.
        kwargs["content_type_id"] = form.get("content_type_id")
    try:
        await deps.offload_blocking(
            conn_store.update,
            cid,
            (form.get("name") or "").strip(),
            cfg,
            secret,
            bound_customer_id=_my_customer_id(request),
            actor=_actor_email(request),
            # connected projects: a language added here is added to the project it runs for,
            # which needs a manager or higher (decision 8)
            can_add_languages=(not security.auth_required()) or security.can_manage(_me),
            **kwargs,
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    except ConnectorError as exc:  # an unreadable base URL (gate round 2), a language refusal
        from urllib.parse import quote

        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=" + quote(str(exc)), status_code=303
        )
    return RedirectResponse(f"/connectors/connection/{cid}?ok=1&msg=Saved.", status_code=303)


@router.post("/connectors/connection/{cid}/rotate")
async def connection_rotate(cid: int, request: Request):
    """Rotate this connection's credentials: the NEW secret is tested before it
    replaces the old one — a failed test leaves the stored credentials
    untouched. Config (base URL, JQL, etc.) is not part of this form; only the
    spec's secret fields are read."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    sp = connectors_registry.spec(conn.kind)
    if not sp:
        return RedirectResponse("/connectors", status_code=303)
    form = await request.form()
    _cfg, secret = _form_to_cfg_secret(sp, form)
    try:
        ok, msg = await deps.offload_blocking(
            conn_store.rotate_secret, cid, secret, actor=_actor_email(request)
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/test")
def connection_test(cid: int, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    _conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    ok, msg = conn_store.test_connection(cid, actor=_actor_email(request))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/pull")
def connection_pull(cid: int, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    _conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    ok, msg, doc_id = conn_store.pull_connection(cid, actor=_actor_email(request))
    if ok and doc_id:
        return RedirectResponse(f"/documents/{doc_id}", status_code=303)
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/push")
def connection_push(cid: int, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    _conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    ok, msg, _n = conn_store.push_connection(cid, actor=_actor_email(request))
    return _conn_action_redirect(cid, ok, msg)


def _jira_batch_web_block(request: Request, cid: int, kind: str):
    """Web twin of the API's Jira batch admin gate (#517): a saved-JQL run or a
    pending release is paid and org-wide, so only an admin (or single-operator mode)
    may start it."""
    if kind != "jira" or not security.auth_required():
        return None
    if security.is_admin(security.session_user(request)):
        return None
    return RedirectResponse(
        f"/connectors/connection/{cid}?ok=0&msg=Running a Jira batch needs an admin account.",
        status_code=303,
    )


@router.post("/connectors/connection/{cid}/sync")
def connection_sync(cid: int, request: Request):
    """One-click 'Translate now' — enqueued as a background job (an Airtable base
    with hundreds of records × locales runs for minutes; the request returns now).
    Deduped: if a run for this connection is already in flight, land the user on
    that job's progress instead of stacking a second live run."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    _conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    blocked = _jira_batch_web_block(request, cid, getattr(_conn, "kind", ""))
    if blocked:
        return blocked
    jid, _created = jobs.submit_unique(
        "connector_translate",
        {"connection_id": cid, "actor": _actor_email(request)},
        actor=_actor_email(request),
    )
    return RedirectResponse(f"/connectors/connection/{cid}?job={jid}", status_code=303)


@router.post("/connectors/connection/{cid}/retry")
async def connection_retry(cid: int, request: Request):
    """Re-run just the failed items of a prior connector job. The failures are
    read from that job's stored result (job_id form field), so the UI doesn't
    round-trip the list back to us. Enqueued as a connector_translate job with a
    retry_failures payload, so submit_unique dedup still prevents stacking it
    behind a full sync of the same connection."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    _conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    form = await request.form()
    try:
        job_id = int((form.get("job_id") or "0").strip())
    except ValueError:
        job_id = 0
    job = jobs.get(job_id) if job_id else None
    # Cross-connection guard: the job's failures must belong to THIS connection,
    # and the job must be a connector_translate job — never let a job_id from
    # another connection (or another kind) seed a retry against this one.
    if (
        not job
        or job.kind != "connector_translate"
        or (job.payload or {}).get("connection_id") != cid
    ):
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Job does not belong to this connection.",
            status_code=303,
        )
    # Retry the run's real failures AND any INCOMPLETE pairs (review M1 — Contentful
    # partial writes where some fields were held back). `incomplete` is absent for every
    # other connector, so this union is a no-op for them; store.retry_failed passes these
    # pairs to the flow as only_pairs, and the flow engine bypasses content-hash
    # suppression for them (flow.py stage 6), so a held/partial pair genuinely re-runs.
    result = job.result or {}
    failures = (
        (result.get("failures") or [])
        + (result.get("incomplete") or [])
        + (result.get("blocked") or [])
    )
    if not failures:
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=No failed items to retry.", status_code=303
        )
    jid, created = jobs.submit_unique(
        "connector_translate",
        {"connection_id": cid, "actor": _actor_email(request), "retry_failures": failures},
        actor=_actor_email(request),
    )
    url = f"/connectors/connection/{cid}?job={jid}"
    if not created:
        # A full sync for this connection is already in flight — the retry was
        # NOT queued; point the user at the live job so they still see progress.
        url += "&msg=A sync is already running - retry not queued"
    return RedirectResponse(url, status_code=303)


@router.post("/connectors/connection/{cid}/retry-blocked")
async def connection_retry_blocked(cid: int, request: Request):
    """Re-attempt a Contentful connection's PERSISTENT validation-blocked pairs (review
    round-3 finding 2b). Unlike /retry (which reads a specific job result), this reads
    the durable block worklist from conn.config and enqueues a retry for exactly those
    pairs — store.retry_failed passes them to the flow as only_pairs, and the flow engine
    bypasses content-hash suppression for them (flow.py stage 6), so a genuine re-attempt
    runs (a plain re-run would be hash-suppressed, and a blocked pair's validation-block is
    consulted only on a hash-match, which the retry bypasses). The unblock-only path the
    block message points operators to."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    pairs = contentful_blocked_list(conn)
    if not pairs:
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=No blocked pairs to retry.", status_code=303
        )
    failures = [{"article_id": p["aid"], "locale": p["loc"], "error": p["reason"]} for p in pairs]
    jid, created = jobs.submit_unique(
        "connector_translate",
        {"connection_id": cid, "actor": _actor_email(request), "retry_failures": failures},
        actor=_actor_email(request),
    )
    url = f"/connectors/connection/{cid}?job={jid}"
    if not created:
        url += "&msg=A sync is already running - retry not queued"
    return RedirectResponse(url, status_code=303)


@router.post("/connectors/connection/{cid}/translate-pending")
def connection_translate_pending(cid: int, request: Request):
    """Jira Phase 2: release the connection's PENDING issue keys (recorded by a
    verified push while auto_translate_on_change is OFF — the button-trigger
    posture). Enqueued as a background job (a pending set can hold up to 500 keys);
    deduped on (connection_id, "jira_translate_pending") so a double-click doesn't
    stack two concurrent releases (F2, adversarial review — two rapid clicks used
    to enqueue two jobs, each posting its own labelled comments and doubling MT
    spend, since WORKER_COUNT=2 lets both run at once and the per-run hash gate
    can't protect against them racing each other). Redirects to the job's progress."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    blocked = _jira_batch_web_block(request, cid, getattr(conn, "kind", ""))
    if blocked:
        return blocked
    if not conn or conn.kind != "jira" or not conn_store.jira_pending(cid):
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=No pending issues to translate.",
            status_code=303,
        )
    # submit_unique keyed on the COMPOUND (connection_id, jira_translate_pending)
    # — coalesces only a second pending-release for this connection, not a
    # concurrent full sync/retry (that would key-match on connection_id alone and
    # could swallow this job's jira_translate_pending flag, leaving the pending
    # set uncleared — see jobs.submit_unique's docstring).
    jid, created = jobs.submit_unique(
        "connector_translate",
        {"connection_id": cid, "actor": _actor_email(request), "jira_translate_pending": True},
        actor=_actor_email(request),
        dedupe_key=("connection_id", "jira_translate_pending"),
    )
    url = f"/connectors/connection/{cid}?job={jid}"
    if not created:
        url += "&msg=A pending-release is already running - not queued again"
    return RedirectResponse(url, status_code=303)


@router.post("/connectors/connection/{cid}/salesforce/rediscover-fields")
def connection_salesforce_rediscover_fields(cid: int, request: Request):
    """Re-run Salesforce Describe and persist the per-record-type translatable field
    map (the 'rediscover fields' action). Runs synchronously via the store; web/CLI/
    api all call conn_store.salesforce_rediscover_fields."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "salesforce":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not a Salesforce connection.", status_code=303
        )
    ok, msg, _summary = conn_store.salesforce_rediscover_fields(cid, actor=_actor_email(request))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/notion/resolve-link")
async def connection_notion_resolve_link(cid: int, request: Request):
    """Adopt/ignore a Notion needs_link id-map row (SPEC-t3 rule 6): a pre-existing
    target page the map never created. web/CLI/api all call conn_store.notion_resolve_link;
    it never overwrites content, only edits the id map."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "notion":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not a Notion connection.", status_code=303
        )
    form = await request.form()
    try:
        ok, msg, _s = await deps.offload_blocking(
            conn_store.notion_resolve_link,
            cid,
            (form.get("source_id") or "").strip(),
            (form.get("target_locale") or "").strip(),
            (form.get("action") or "").strip(),
            actor=_actor_email(request),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/helpscout/resolve-link")
async def connection_helpscout_resolve_link(cid: int, request: Request):
    """Adopt/ignore a Help Scout needs_link id-map row (SPEC-t3 rule 6): a pre-existing
    target article the map never created. web/CLI/api all call
    conn_store.helpscout_resolve_link; it never overwrites content, only edits the id
    map."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "helpscout":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not a Help Scout connection.", status_code=303
        )
    form = await request.form()
    try:
        ok, msg, _s = await deps.offload_blocking(
            conn_store.helpscout_resolve_link,
            cid,
            (form.get("source_id") or "").strip(),
            (form.get("target_locale") or "").strip(),
            (form.get("action") or "").strip(),
            actor=_actor_email(request),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/wordpress/resolve-link")
async def connection_wordpress_resolve_link(cid: int, request: Request):
    """Adopt/ignore a WordPress needs_link id-map row (SPEC-t3 rule 6): a pre-existing
    Polylang-linked target post the map never created. web/CLI/api all call
    conn_store.wordpress_resolve_link; it never overwrites content, only edits the id map."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "wordpress":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not a WordPress connection.", status_code=303
        )
    form = await request.form()
    try:
        ok, msg, _s = await deps.offload_blocking(
            conn_store.wordpress_resolve_link,
            cid,
            (form.get("source_id") or "").strip(),
            (form.get("target_locale") or "").strip(),
            (form.get("action") or "").strip(),
            actor=_actor_email(request),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/servicenow/resolve-link")
async def connection_servicenow_resolve_link(cid: int, request: Request):
    """Adopt/ignore a ServiceNow needs_link id-map row (SPEC-t3 rule 6): a translation that
    already exists under the source's Parent but that the map never created. web/CLI/api all
    call conn_store.servicenow_resolve_link; it never overwrites content, only edits the id
    map."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "servicenow":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not a ServiceNow connection.", status_code=303
        )
    form = await request.form()
    try:
        ok, msg, _s = await deps.offload_blocking(
            conn_store.servicenow_resolve_link,
            cid,
            (form.get("source_id") or "").strip(),
            (form.get("target_locale") or "").strip(),
            (form.get("action") or "").strip(),
            actor=_actor_email(request),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/airtable-webhook/register")
def connection_airtable_webhook_register(cid: int, request: Request):
    """Register the official Airtable webhook for this connection (Phase 2). Refuses
    with an actionable message when app_base_url is not a public https URL (Airtable
    cannot reach localhost)."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "airtable":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not an Airtable connection.", status_code=303
        )
    ok, msg = conn_store.register_airtable_webhook(cid, actor=_actor_email(request))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/airtable-webhook/unregister")
def connection_airtable_webhook_unregister(cid: int, request: Request):
    """Delete the official Airtable webhook + clear its stored state."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "airtable":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not an Airtable connection.", status_code=303
        )
    ok, msg = conn_store.unregister_airtable_webhook(cid, actor=_actor_email(request))
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/airtable-webhook/translate-pending")
def connection_airtable_webhook_translate_pending(cid: int, request: Request):
    """Translate the records a ping recorded as pending (auto-translate-on-change
    off). Runs synchronously via the store (drains pending + pulls newer payloads)."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "airtable":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not an Airtable connection.", status_code=303
        )
    ok, msg, _summary = conn_store.consume_airtable_payloads(
        cid, actor=_actor_email(request), force=True
    )
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/airtable-webhook/health")
def connection_airtable_webhook_health(cid: int, request: Request):
    """Probe the official webhook's health (round-2 #6): detect + surface a
    watched-field-deleted / paused error state Airtable does not 404. Persists the
    state so the connection page shows it."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn.kind != "airtable":
        return RedirectResponse(
            f"/connectors/connection/{cid}?ok=0&msg=Not an Airtable connection.", status_code=303
        )
    ok, msg, _info = conn_store.check_airtable_webhook_health(cid)
    return _conn_action_redirect(cid, ok, msg)


@router.post("/connectors/connection/{cid}/schedule")
def connection_schedule(cid: int, request: Request, every_minutes: str = Form("0")):
    """Enable/disable the automatic sync schedule. The first run is one full
    interval out — never immediate (that's what "Translate now" is for)."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    _conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    try:
        minutes = max(0, int((every_minutes or "0").strip()))
    except ValueError:
        minutes = 0
    if minutes > 0:
        # Turning on a Jira schedule starts recurring org-wide batches (#517 gate).
        blocked = _jira_batch_web_block(request, cid, getattr(_conn, "kind", ""))
        if blocked:
            return blocked
    scheduler.set_schedule(cid, minutes, actor=_actor_email(request))
    msg = (
        "Scheduled sync turned off."
        if minutes == 0
        else f"Scheduled sync saved — runs every {minutes} min (first run in {minutes} min)."
    )
    return _conn_action_redirect(cid, 1, msg)


@router.post("/connectors/connection/{cid}/delete")
def connection_delete(cid: int, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    from urllib.parse import quote

    kind = conn.kind if conn else ""
    retained = conn_store.delete(cid)
    # Surface the retention truth (audit Finding 4): the connection's synced project +
    # its pulled content are NOT deleted with the connection — full erasure needs a
    # separate project delete. Parity with the CLI's identical wording.
    if retained:
        msg = (
            f"Connection deleted. Project '{retained}' and its pulled content are "
            "retained; delete the project separately for full erasure."
        )
    else:
        msg = "Connection deleted."
    dest = f"/connectors/{kind}" if kind else "/connectors"
    return RedirectResponse(f"{dest}?msg={quote(msg)}", status_code=303)


# --- browse (live pickers) ---
# These layer dropdowns over the existing text inputs: the JS asks the connector
# what bases/tables/projects/spaces exist so the user picks instead of typing IDs.
# They're reads (never a mutation), so no audit row is needed — but they DO spend
# a token / hit the external API, so keep the same strict _ai_block gate as every
# other live-network route. All POST + JSON, per the codebase convention that no
# GET does outbound network I/O.
@router.post("/connectors/connection/{cid}/browse/containers")
def connection_browse_containers(cid: int, request: Request):
    """List the connection's pickable scopes (Airtable bases+tables, Jira
    projects, Confluence spaces) for the settings-form pickers."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    # HIGH-1 (task_bcae962f review): route through the SHARED _conn_or_404 predicate
    # (org + R2-3 customer ring) — the hand-rolled same_org check here used to miss
    # the customer ring, letting a bound linguist read a FOREIGN customer's schema
    # (and spend its token budget). We keep the JSON 404 contract the picker JS reads
    # (status + ok) rather than _conn_or_404's HTML page.
    _conn, notfound = _conn_or_404(request, cid)
    if notfound:
        return JSONResponse({"ok": False, "error": "Connection not found."}, status_code=404)
    return JSONResponse({"ok": True, "containers": conn_store.containers(cid)})


@router.post("/connectors/connection/{cid}/browse/fields")
async def connection_browse_fields(
    cid: int, request: Request, base: str = Form(""), table: str = Form("")
):
    """Field names of an Airtable table, for the source-field picker. Airtable-only
    (only that connector has a schema); a ConnectorError (bad base/table, missing
    scope) surfaces as a 400 with the message so the UI can show it inline."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    # HIGH-1: same shared org+customer predicate as browse/containers (see there).
    conn, notfound = _conn_or_404(request, cid)
    if notfound:
        return JSONResponse({"ok": False, "error": "Connection not found."}, status_code=404)
    if conn.kind != "airtable":
        return JSONResponse(
            {"ok": False, "error": "Field browsing is Airtable-only."}, status_code=400
        )
    try:
        fields = await deps.offload_blocking(
            conn_store.schema_fields, cid, base.strip(), table.strip()
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    except ConnectorError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "fields": fields})


@router.post("/connectors/{kind}/browse")
async def connector_browse_unsaved(kind: str, request: Request):
    """Containers for the ADD-connection form — no Connection row exists yet, so we
    build a throwaway connector from the secret (and, for Atlassian, base_url) the
    user has typed but not saved. Invalid kind -> 404."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    sp = connectors_registry.spec(kind)
    # ffadmin Wave 5 -- DOCTRINE: a DISABLED kind returns the SAME "Unknown connector." 404
    # an unknown slug does (byte-identical -- indistinguishable from never-built), together-
    # with the spec() check and before any client build.
    if not sp or not connectors_registry.kind_enabled(kind):
        return JSONResponse({"ok": False, "error": "Unknown connector."}, status_code=404)
    form = await request.form()
    secret = {f.name: (form.get(f.name) or "").strip() for f in sp.secret_fields}
    # Only the config keys a browse actually needs (Atlassian's base_url); the rest
    # of the config isn't relevant to listing containers.
    cfg = {
        f.name: (form.get(f.name) or "").strip()
        for f in sp.config_fields
        if form.get(f.name) is not None
    }
    try:
        containers = await deps.offload_blocking(
            conn_store.browse_containers_unsaved, kind, cfg, secret
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": True, "containers": containers})


@router.post("/connectors/token/regenerate")
def connector_token_regenerate(request: Request):
    # GATE R4 MED-1. Admin, not can_tune_ai, and audited.
    #
    # This rotates an INSTALL-WIDE, confinement-exempt machine credential: every rotation
    # breaks the Airtable Extension, the Airtable Automation webhook and the Jira Forge
    # app until each is re-pasted. It was the weakest and only unaudited of its three
    # writers (the /api twin requires _r3_gate and audits; the CLI requires require_local),
    # and a LINGUIST could fire it, leaving no record of who did.
    #
    # This train made the CONSEQUENCE worse, which is why it is fixed here rather than
    # filed: before the disclosure was narrowed (first to can_manage, then in round 5 to
    # is_admin, which is where it stands now), a linguist who rotated the token could at
    # least read the new one and re-paste it. Now the same button is pure destruction that
    # only an admin can repair.
    _me, blocked = _admin_block(request)
    if blocked:
        return blocked
    settings.set_many({"connector_api_token": "lsk_" + secrets.token_urlsafe(24)})
    audit.log(
        actor=_actor_email(request),
        action="connector.token_regenerate",
        target="connector_api_token",
        detail="shared connector token rotated; every companion app must be re-paired",
    )
    from app.web.form_errors import referer_local_path

    return RedirectResponse(referer_local_path(request, "/connectors"), status_code=303)


_srp_piece_router = connectors_common_router
connectors_common_router = _srp_copy(connectors_common_router)
connectors_common_router.routes = connectors_common_router.routes[6:7]
_srp_router_options = router.prefix, router.tags, router.dependencies
router.prefix, router.tags, router.dependencies = "", [], []
router.include_router(connectors_common_router)
router.prefix, router.tags, router.dependencies = _srp_router_options
connectors_common_router = _srp_piece_router


def _langset_write_block(request):
    """#468: a language set is an org-level shared asset (no customer column), so a
    customer-bound user may read sets but never create, change or delete one (403, the
    shared message). No-op for internal staff / auth off."""
    if access.customer_write_blocks(_my_customer_id(request), None):
        return _error_page(request, 403, access.SHARED_ASSET_READ_ONLY)
    return None


@router.post("/languages")
def languages_create(
    request: Request,
    name: str = Form(...),
    source_locale: str = Form("en-US"),
    target_locales: list[str] = Form(default=[]),
):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    blocked = _langset_write_block(request)
    if blocked:
        return blocked
    # Wave 3B review finding 2: the langpicker has no native `required`, and an
    # EMPTY set is a footgun downstream (store.effective_locales treats it as
    # unset and silently falls back to fr-FR for a connection using it). Reject
    # an empty submission with the same ?err= banner pattern as a bad locale.
    # parse_locales(+shim) sees through both POST shapes (repeated keys / legacy
    # comma string), so a submit of only blanks/separators is also caught.
    if not languages.parse_locales(languages._normalize_target_input(target_locales)):
        from urllib.parse import quote

        return RedirectResponse(
            "/languages?err=" + quote("Pick at least one target language."), status_code=303
        )
    # Stamp the creating user's org (leak vector 2). No-op (NULL) with the gate off.
    try:
        languages.create(
            name,
            source_locale,
            target_locales,
            actor=_actor_email(request),
            org_id=_my_org_id(request),
        )
    except languages.InvalidLocaleError as exc:
        from urllib.parse import quote

        return RedirectResponse("/languages?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/languages?saved=1", status_code=303)


@router.post("/languages/{sid}")
def languages_update(
    sid: int,
    request: Request,
    name: str = Form(...),
    source_locale: str = Form("en-US"),
    target_locales: list[str] = Form(default=[]),
):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    blocked = _langset_write_block(request)
    if blocked:
        return blocked
    _ls, blocked = _langset_or_404(request, sid)
    if blocked:
        return blocked
    try:
        languages.update(sid, name, source_locale, target_locales)
    except languages.InvalidLocaleError as exc:
        from urllib.parse import quote

        return RedirectResponse("/languages?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/languages?saved=1", status_code=303)


@router.post("/languages/{sid}/delete")
def languages_delete(sid: int, request: Request):
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    blocked = _langset_write_block(request)
    if blocked:
        return blocked
    _ls, blocked = _langset_or_404(request, sid)
    if blocked:
        return blocked
    # SPEC-rename-delete §7: fails closed while a RoutingRule / Connection config still
    # references the set. Surface the itemized message, never a 500.
    try:
        languages.delete(sid)
    except languages.LanguageSetInUseError as exc:
        from urllib.parse import quote

        return RedirectResponse("/languages?err=" + quote(str(exc)), status_code=303)
    return RedirectResponse("/languages?saved=deleted", status_code=303)


@router.post("/connectors/connection/{cid}/languages")
def connection_languages(
    cid: int, request: Request, language_set_id: str = Form(""), target_locales: str = Form("")
):
    """Set which languages this connection fans out to — a saved set or a custom list."""
    _me, blocked = _ai_block(request)
    if blocked:
        return blocked
    conn, blocked = _conn_or_404(request, cid)
    if blocked:
        return blocked
    if conn:
        # Patch only the keys this card owns — language_set_id always, and
        # target_locales only when non-blank (a blank submit keeps the stored
        # custom list, matching the pre-atomic-write semantics).
        updates = {"language_set_id": language_set_id.strip()}
        if target_locales.strip():
            updates["target_locales"] = target_locales.strip()
        # Connected projects: on a connection a project runs on, the change flows into the
        # project's language list (adding one needs a manager, decision 8); a connection that
        # only feeds other projects refuses it, naming them.
        try:
            conn_store.patch_config(
                cid,
                updates,
                actor=_actor_email(request),
                can_add_languages=(not security.auth_required()) or security.can_manage(_me),
            )
        except ConnectorError as exc:
            from urllib.parse import quote

            return RedirectResponse(
                f"/connectors/connection/{cid}?ok=0&msg=" + quote(str(exc)), status_code=303
            )
    return RedirectResponse(
        f"/connectors/connection/{cid}?ok=1&msg=Languages updated.", status_code=303
    )
