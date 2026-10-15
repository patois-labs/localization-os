"""connectors: shared leaf helpers (layered pure move)

Documented in: docs/architecture/connectors.md
"""

import re
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.kernel import (
    access,
    audit,
    connected_projects,
    ids,
    languages,
    resource_visibility,
    security,
    settings,
)
from app.kernel import connectors as connectors_registry
from app.kernel.connectors import oauth as conn_oauth
from app.kernel.connectors import store as conn_store
from app.kernel.providers import provider_is_live
from app.models import Customer
from app.web import page_guard
from app.web.deps import (
    _actor_email,
    _admin_block,
    _error_page,
    _ip,
    _my_customer_id,
    _my_org_id,
    _org_block,
    connector_api_token,
    templates,
)

router = APIRouter()


def _safe_int_id(raw) -> int | None:
    """A positive, DB-safe row id or None -- the single app-wide id law
    (app/services/ids.coerce_id). A malformed id (a Unicode digit int() rejects,
    or a value SQLite rejects at the bind layer) falls through to the same
    not-found / stale-id path any nonexistent id takes, never a 500."""
    return ids.coerce_id(raw)


def _secret_display(sp, secret: dict) -> dict:
    """A template-safe view of a connection's decrypted secret dict (D7 fix): the
    RAW secret dict (which can carry an `oauth` sub-dict with access/refresh
    tokens) never reaches the Jinja context. Only the fields declared on the
    connector's spec.secret_fields are echoed, and only per-field:
      - secret=True  (real credential, e.g. api_token) -> a bool "is it set"
        flag, matching the engines.py `key_set` masked pattern. The template's
        password inputs only need presence to decide the placeholder text.
      - secret=False (e.g. an account email — not sensitive, prefilled so the
        operator doesn't retype it) -> the actual value, same as today.
    A future FieldSpec that mismarks a real token as secret=False still can't
    leak an oauth token specifically, because oauth tokens are never field
    values here at all (they live under secret['oauth'], which this function
    never touches)."""
    out = {}
    for f in sp.secret_fields:
        v = secret.get(f.name, "")
        out[f.name] = bool(v) if f.secret else v
    return out


def _customers(request):
    """Org-scoped customer list for the connection scope dropdown (P4, item 5).
    org_filter is a no-op with the multi-org gate off (byte-identical to today).
    R2-3: a customer-bound user only ever sees its OWN customer here (the dropdown
    renders on connector pages, so an unfiltered list would leak customer names)."""
    from app.kernel import customers as customer_service

    me = security.session_user(request)
    with Session(engine) as s:
        # SPEC-customer-archive: this is a scope PICKER — archived customers are
        # retired from it (active_only), so a connection can't be freshly scoped to a
        # retired customer. Existing connection scopes are untouched.
        q = customer_service.active_only(
            access.org_filter(select(Customer), Customer, me)
        ).order_by(Customer.name)
        my_cust = access.current_customer_id(me)
        if my_cust is not None:
            q = q.where(Customer.id == my_cust)
        return s.exec(q).all()


def _content_types(request):
    """Content-type options for the connection default-content-type dropdown (SPEC-
    content-types phase 3, §6).

    A customer-BOUND caller sees globals + their own customer's types (the only scopes
    they can legally attach). An UNBOUND staff/admin caller sees EVERY type in the org
    (globals + all customers') so they can scope a new connection to ANY customer AND
    still pick that customer's type — the functional case a caller-binding check broke
    (Finding 1a). The store.create/update SEAM re-validates the chosen type against the
    connection's ACTUAL target customer, so offering the staff superset here is safe: a
    mismatched pick is dropped at the seam, never planted."""
    from app.kernel import content_types as _ct

    me = security.session_user(request)
    org = access.current_org_id(me)
    binding = access.current_customer_id(me)
    with Session(engine) as s:
        if binding is not None:
            return _ct.options_for_project(s, org_id=org, customer_id=binding)
        return _ct.list_types(s, org_id=org)


def _public_base_url() -> str:
    """The configured PUBLIC base URL (app_base_url setting) for push intake, or ''
    when unset or pointed at localhost. Automation web requests / raw webhooks
    originate from Atlassian's cloud and cannot reach localhost, so a localhost value
    is treated as "no public URL" and the connection page shows the tunnel guidance.
    Returned WITHOUT a trailing slash."""
    base = (settings.get("app_base_url") or "").strip().rstrip("/")
    if not base:
        return ""
    from urllib.parse import urlsplit

    host = (urlsplit(base).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local"):  # noqa: S104 -- host comparison, not a bind
        return ""
    return base


class ArchivedCustomerScopeError(ValueError):
    """T2-01: a connector create/update named an ARCHIVED customer_id via an
    EXPLICIT form field. Surfaced by the route as a clean redirect error, never
    silently dropped to global/unscoped and never stamped onto the connection."""


def _valid_customer_id(request, raw) -> str:
    """Return `raw` (a customer_id string, possibly '') only if it names a customer
    the caller may scope to; RAISE ArchivedCustomerScopeError specifically for a
    same-org customer that exists but is ARCHIVED (T2-01).

    R2-3 / task_bcae962f (the caller-binding gap): a customer-BOUND caller is
    CONFINED to its OWN customer — a forged/foreign/blank value can never scope a
    connection to a DIFFERENT customer or to global. It returns the caller's binding
    id (as a string), mirroring content_types._valid_customer_id. The store.create/
    update SEAM re-confines with the same binding (bound_customer_id=), so this is the
    web layer of a two-layer, fail-closed guarantee (repo law: the seam is
    authoritative; this pre-validates so the form's own dropdown never even offers a
    foreign id). No-op for internal staff (current_customer_id is None).

    UNBOUND staff caller: same-org validation (item 6) is UNCHANGED — a cross-org id
    (which the dropdown never offers, but a forged POST could carry), a nonexistent
    id, or a non-digit value all still drop to '' (global) exactly as before
    (verify_connector_isolation_gaps.py's test_d_cross_org_fail_closed depends on
    this byte-identical fail-closed-to-global contract; it is a deliberate design
    choice, not the T2-01 bug). ONLY the genuinely new case — a customer that IS
    same-org-visible but archived — is refused outright instead of silently
    stamped (the pre-fix gap: no archived_at check existed at all here). A blank
    customer_id (global, the legitimate no-scope case) still returns '' unchanged."""
    me = security.session_user(request)
    binding = access.current_customer_id(me)
    if binding is not None:
        return str(binding)  # bound caller: forced to own customer, never foreign/global
    raw = (raw or "").strip()
    cid = _safe_int_id(raw)
    if cid is None:
        return ""
    with Session(engine) as s:
        cust = s.get(Customer, cid)
    if cust is None or not access.same_org(me, getattr(cust, "org_id", None)):
        return ""  # unchanged (item 6): cross-org / nonexistent -> silent drop to global
    from app.kernel import customers as customer_service

    if not customer_service.is_active(cust):
        raise ArchivedCustomerScopeError(
            f"customer {raw} ({cust.name}) is archived and cannot be selected."
        )
    # L5: return the VALIDATED id (str(cid)), not the raw form value -- int()
    # accepts shapes str.isdigit() rejected ("+12", "1_0"), so the value handed to
    # store.create must match what was validated. The error message above keeps the
    # ORIGINAL raw for a faithful echo of what the operator typed.
    return str(cid)


PLANNED_CONNECTORS = [
    {"name": "Figma", "icon": "🎨", "cat": "Design", "desc": "Pull copy from design frames."},
    {"name": "GitHub", "icon": "🐙", "cat": "Repos", "desc": "Localize JSON/Markdown via PRs."},
    {
        "name": "Slack",
        "icon": "💬",
        "cat": "Notifications",
        "desc": "Job + LQA alerts to channels.",
    },
    {
        "name": "Smartling API",
        "icon": "🌐",
        "cat": "TMS facade",
        "desc": "Expose a Smartling-dialect API so existing connectors target this TMS.",
    },
]


# --- Connectors ---
# Integrations that aren't real connectors yet — shown as "Planned" tiles.
@router.get("/connectors")
def connectors_index(request: Request):
    live = {p: provider_is_live(p) for p in config.PROVIDERS}
    by_kind: dict[str, list] = {}
    # Multi-org: only the caller's own org's connections (leak vector 2/10).
    # R2-3: a customer-bound user sees only its OWN customer's connections + global
    # (list_connections(customer_id=) = own + unassigned, never another customer's).
    for conn in _project_visible_connections(
        request,
        conn_store.list_connections(
            org_id=_my_org_id(request), customer_id=_my_customer_id(request)
        ),
    ):
        by_kind.setdefault(conn.kind, []).append(conn)
    # §6b: the add-connection picker offers ACTIVE connectors only; archived ones
    # render in a separate, non-addable "Archived" section (an operator still sees
    # they exist and that any existing connection of that kind keeps running).
    # ffadmin Wave 5: render ONLY the ENABLED kinds -- a disabled kind has no tile in
    # EITHER grid (it vanishes entirely). The roadmap/planned tiles moved to the
    # /connectors/catalog page. Enabled-ness and archived-ness are orthogonal, so we
    # split active/archived within the enabled set.
    enabled = connectors_registry.enabled_specs()
    specs = [s for s in enabled if not getattr(s, "archived", False)]
    archived = [s for s in enabled if getattr(s, "archived", False)]
    return templates.TemplateResponse(
        request,
        "connectors.html",
        {
            "request": request,
            "specs": specs,
            "archived_specs": archived,
            "by_kind": by_kind,
            "providers_live": live,
            "models": config.DEFAULT_MODELS,
            "is_admin": security.is_admin(security.session_user(request)),
            "can_manage": security.can_tune_ai(security.session_user(request)),
        },
    )


@router.get("/connectors/manage")
def connectors_manage(request: Request):
    """ffadmin Wave 5 (ADMIN) -- the per-connector-kind enable/disable console. Lists
    EVERY kind in REGISTRY (enabled + disabled) with a toggle per kind. A disable
    confirm names any existing connections that will be hidden. Toggles ride the same
    audited settings write as the Wave-1 feature card. 'Get more connectors' links to
    the catalog."""
    blocked = _admin_block(request)[1]
    if blocked:
        return blocked
    # existing-connection counts per kind (this org / customer ring), so the disable
    # confirm can name how many connections a kind will hide.
    counts: dict[str, int] = {}
    for conn in conn_store.list_connections(
        org_id=_my_org_id(request), customer_id=_my_customer_id(request)
    ):
        counts[conn.kind] = counts.get(conn.kind, 0) + 1
    rows = []
    for cls in connectors_registry.REGISTRY.values():
        sp = cls.spec
        rows.append(
            {
                "kind": sp.kind,
                "name": sp.name,
                "icon": sp.icon,
                "cat": sp.cat,
                "blurb": sp.blurb,
                "archived": bool(getattr(sp, "archived", False)),
                "enabled": connectors_registry.kind_enabled(sp.kind),
                "connections": counts.get(sp.kind, 0),
            }
        )
    return templates.TemplateResponse(
        request, "connectors_manage.html", {"request": request, "rows": rows}
    )


@router.post("/connectors/manage/{kind}")
def connector_kind_toggle(kind: str, request: Request, enabled: str = Form("")):
    """Enable/disable one connector kind from the manage page or the catalog (ADMIN).
    Mirrors the Wave-1 Settings feature toggle: CSRF via the form include, 400 on a
    bad value, 404 on an unknown kind, and a no-op resubmit SKIPS the write + audit
    (set_kind_enabled recomputes the same canonical CSV, which set_many no-ops).
    Audited as 'connector_kind.toggle' with the kind + value; the write rides
    settings.set_many so a running server picks the flip up within ~2s (the K3
    property)."""
    me, blocked = _admin_block(request)
    if blocked:
        return blocked
    if kind not in connectors_registry.REGISTRY:
        return _error_page(request, 404, "Not found.")
    if enabled not in ("0", "1"):
        return _error_page(request, 400, "A connector toggle is 0 or 1.")
    on = enabled == "1"
    # Skip the write + audit entirely on a same-state resubmit (the Wave-1 idiom).
    if connectors_registry.kind_enabled(kind) != on:
        connectors_registry.set_kind_enabled(kind, on)
        audit.log(
            actor=getattr(me, "email", "system") or "system",
            action="connector_kind.toggle",
            target=kind,
            detail=f"={enabled}",
            ip=_ip(request),
        )
    from app.web.form_errors import referer_local_path

    dest = referer_local_path(request, "/connectors/manage")
    return RedirectResponse(dest, status_code=303)


@router.get("/connectors/catalog")
def connectors_catalog(request: Request):
    """ffadmin Wave 5 -- the full connector catalog: EVERY connector developed, whatever
    its install state. Active kinds (enabled + disabled, each badged), archived kinds
    (badged, per the code tier), and the roadmap/planned tiles (moved here off the main
    page). An admin gets an Enable button for each disabled kind (same toggle POST);
    non-admins may VIEW the catalog but see no toggle."""
    is_admin = security.is_admin(security.session_user(request))
    active, archived = [], []
    for cls in connectors_registry.REGISTRY.values():
        sp = cls.spec
        row = {
            "kind": sp.kind,
            "name": sp.name,
            "icon": sp.icon,
            "cat": sp.cat,
            "blurb": sp.blurb,
            "enabled": connectors_registry.kind_enabled(sp.kind),
        }
        if getattr(sp, "archived", False):
            archived.append(row)
        else:
            active.append(row)
    return templates.TemplateResponse(
        request,
        "connectors_catalog.html",
        {
            "request": request,
            "active": active,
            "archived": archived,
            "planned": PLANNED_CONNECTORS,
            "is_admin": is_admin,
        },
    )


def _project_visible_connections(request, rows):
    """#474: drop connections bound to a project the session user cannot see (the
    project rule the per-connection page already applies, #370). Unrestricted users
    (staff, auth off) keep every row."""
    with Session(engine) as s:
        return resource_visibility.visible_connections(s, security.session_user(request), rows)


@router.get("/connectors/{kind}")
def connector_type(kind: str, request: Request):
    # ffadmin Wave 5 -- DOCTRINE: a DISABLED kind is byte-identical to a NEVER-BUILT
    # (unknown) one. Both take the SAME `spec is None`-style redirect, so a disabled kind
    # cannot be distinguished from a typo by response code/body (non-enumerable). The
    # kind_enabled check sits TOGETHER WITH spec(), never after it.
    sp = connectors_registry.spec(kind)
    if not sp or not connectors_registry.kind_enabled(kind):
        return RedirectResponse("/connectors", status_code=303)
    allow_oauth_form_action(request, kind)
    return templates.TemplateResponse(
        request,
        "connector_type.html",
        {
            "request": request,
            "spec": sp,
            # §6b: an archived kind still shows its existing connections + creds help,
            # but the add-connection form (token + OAuth) is hidden and creation refused.
            "archived": connectors_registry.is_archived(kind),
            "connections": _project_visible_connections(
                request,
                conn_store.list_connections(
                    kind, org_id=_my_org_id(request), customer_id=_my_customer_id(request)
                ),
            ),
            "customers": _customers(request),
            "content_types": _content_types(request),
            "can_manage": security.can_tune_ai(security.session_user(request)),
            # Separate from the template's `can_manage` on purpose: that one is bound to
            # can_tune_ai above and still governs the ordinary connector affordances a
            # linguist legitimately has, while the install-wide token row is ADMIN only.
            #
            # GATE R5 HIGH-1: this said `security.can_manage`, and the comment beside it
            # claimed can_manage meant admin. It does not: can_manage is rank >= MANAGER.
            # MANAGER is the ONE role that is can_manage WITHOUT being can_tune_ai, so a
            # manager could read the install-wide token off this page and then use it as
            # `X-Loc-Token` to update and delete connections through /api/connectors, the
            # very writes the can_tune_ai gate had just refused them in the web UI.
            "can_see_token": security.is_admin(security.session_user(request)),
            "base_url": settings.external_base_url(request),
            "api_token": (
                connector_api_token()
                # ADMIN, not can_tune_ai and not can_manage: the token is not
                # customer-confined (kind="connector", no user, so _ident_customer_id is
                # None), so giving it to a customer-BOUND linguist lets them escape the
                # R2-3 customer ring on the whole /api/connectors surface, and giving it
                # to a manager hands them a can_tune_ai bypass.
                if security.is_admin(security.session_user(request))
                else ""
            ),
            "oauth_configured": conn_oauth.provider_app_configured(kind),
            "created": request.query_params.get("created"),
            "msg": request.query_params.get("msg"),
            # Connected projects: "Connect a new system" from a project's setup menu carries
            # a validated in-site path to return to once the connection exists ("" otherwise).
            "return_to": connected_projects.safe_return_to(request.query_params.get("return_to")),
        },
    )


def allow_oauth_form_action(request: Request, kind: str) -> None:
    """Form POST redirects must allow the configured provider's authorization origin."""
    from app.web.security_headers import allow_form_action

    provider = conn_oauth.get_provider(kind)
    if provider and conn_oauth.provider_app_configured(kind):
        allow_form_action(request, provider.authorize_url)


def _form_to_cfg_secret(spec, form) -> tuple[dict, dict]:
    cfg = {f.name: (form.get(f.name) or "").strip() for f in spec.config_fields}
    secret = {f.name: (form.get(f.name) or "").strip() for f in spec.secret_fields}
    return cfg, secret


@router.get("/connectors/oauth/callback")
def connector_oauth_callback(request: Request):
    """Finish the OAuth flow: validate state (one-shot, in complete_authorize),
    exchange the code, persist the v2 OAuth secret, mark CONNECTED.

    Privileged-gated: this is a session-authed GET, so re-check can_tune_ai on the
    session user (an anonymous/translator hitting the callback URL is bounced to
    /connectors). The provider's `state` is the CSRF defense (it must match the
    one-shot session value); an error or state mismatch redirects with a message
    rather than 500ing."""
    if not security.can_tune_ai(security.session_user(request)):
        return RedirectResponse("/connectors", status_code=303)
    # Read the bound kind BEFORE any session-clear so failure/cancel can land the
    # user on the specific connector's page (which renders `msg`), not the generic
    # /connectors index where the feedback is lost. Fall back to /connectors when
    # the session has no kind (a truly orphaned callback).
    kind = request.session.get("conn_oauth_kind") or ""
    # Connected projects: where a connection made from a project's setup menu returns to. Read
    # BEFORE complete_authorize clears the one-shot session keys; validated again here.
    back = connected_projects.safe_return_to(request.session.get("conn_oauth_return"))
    # ffadmin Wave 5 (HIGH): if the bound kind was DISABLED while this OAuth flow was in
    # flight, refuse the callback BEFORE reaching complete_authorize -- clear the one-shot
    # session keys (so no stale state lingers) and land on /connectors. complete_authorize
    # + set_oauth_secret ALSO refuse at the service seam (no egress, no secret write), so
    # this route check is the clean-UX front line, not the security boundary.
    if kind and not connectors_registry.kind_enabled(kind):
        for k in conn_oauth._SESSION_KEYS:
            request.session.pop(k, None)
        return RedirectResponse("/connectors", status_code=303)

    def _fail(msg: str):
        # F5: surface OAuth error/cancel on the connector-type page (it renders
        # `msg`). Security review 2026-09 (#270, r2-connectors-10): the message is
        # percent-encoded (a raw "&ok=1" can no longer restyle it as a success) and
        # recorded for this session by the page guard, because this is a GET anyone can
        # link to: only text this route wrote itself is recorded, never request text.
        dest = f"/connectors/{kind}" if kind else "/connectors"
        page_guard.attest(request, "msg", msg)
        return RedirectResponse(f"{dest}?msg={quote(msg, safe='')}", status_code=303)

    err = request.query_params.get("error")
    if err:
        # provider-side denial/cancel (e.g. access_denied) — clear the one-shot
        # session keys so a stale state can't be reused, then report. The provider's
        # value is named only when it is an OAuth error CODE (RFC 6749 4.1.2.1).
        mcp_facts = request.session.get("conn_oauth_mcp") if kind == conn_oauth.MCP_KIND else None
        for k in conn_oauth._SESSION_KEYS:
            request.session.pop(k, None)
        if mcp_facts is not None:
            # MCP 2026-07-28 / RFC 9207: the iss check applies to error responses too; on a
            # mismatch the error code is neither acted on nor shown (#758).
            refusal = conn_oauth.mcp_iss_refusal(mcp_facts, request.query_params.get("iss"))
            if refusal:
                return _fail(refusal)
        code = err if re.fullmatch(r"[a-z][a-z_]{0,63}", err) else "not a standard error code"
        return _fail(f"OAuth was cancelled or failed: {code}")
    code = request.query_params.get("code") or ""
    state = request.query_params.get("state") or ""
    cid = _safe_int_id(request.session.get("conn_oauth_cid"))
    scope = request.session.get("conn_oauth_scope")
    conn = conn_store.get(cid) if cid is not None else None
    if not conn_oauth.connection_authorized(conn, security.session_user(request), scope):
        for k in conn_oauth._SESSION_KEYS:
            request.session.pop(k, None)
        if cid is None:
            return _fail("OAuth state mismatch - please start the connection again.")
        return _error_page(request, 404, "Not found.")
    result, error = conn_oauth.complete_authorize(request, code, state)
    if error or not result:
        return _fail(error or "OAuth failed - please try again.")
    result_cid, oauth_dict = result
    if result_cid != cid or not conn_store.set_oauth_secret(
        cid, oauth_dict, expected_scope=scope, user=security.session_user(request)
    ):
        return _error_page(request, 404, "Not found.")
    audit.log(
        actor=_actor_email(request),
        action="connector.oauth_connected",
        target=f"connection:{cid}",
        detail=f"kind={kind}",
        ip=_ip(request),
        customer_id=audit.customer_for_connection(cid),
    )
    # A GET redirect carrying server-written text: record it for this session or the page
    # guard strips the success message (the failure path already does, in _fail).
    page_guard.attest(request, "msg", "Connected via OAuth.")
    if back:
        return RedirectResponse(
            connected_projects.with_params(back.split("#", 1)[0], connection=cid) + "#source",
            status_code=303,
        )
    return RedirectResponse(
        f"/connectors/connection/{cid}?ok=1&msg=Connected via OAuth.", status_code=303
    )


def _notion_idmap(conn) -> dict:
    """The Notion connection's id-map status summary + the needs_link worklist for
    the connection page (SPEC-t3 rule 6). None-safe for non-Notion connections."""
    if not conn or conn.kind != "notion":
        return {"counts": {}, "needs_link": []}
    from app.kernel.connectors import idmap

    return {
        "counts": idmap.status_counts(conn.id),
        "needs_link": idmap.list_for_connection(conn.id, status=idmap.NEEDS_LINK),
    }


def _helpscout_idmap(conn) -> dict:
    """The Help Scout connection's id-map status summary + needs_link worklist (the
    SAME T3 rule-6 surface as Notion). None-safe for non-Help-Scout connections."""
    if not conn or conn.kind != "helpscout":
        return {"counts": {}, "needs_link": []}
    from app.kernel.connectors import idmap

    return {
        "counts": idmap.status_counts(conn.id),
        "needs_link": idmap.list_for_connection(conn.id, status=idmap.NEEDS_LINK),
    }


def _wordpress_idmap(conn) -> dict:
    """The WordPress connection's id-map status summary + needs_link worklist (the SAME T3
    rule-6 surface as Notion/Help Scout) plus the persisted Polylang mode label (gotcha #2 —
    operator-visible degraded-mode surface, no live call). None-safe for non-WordPress
    connections."""
    if not conn or conn.kind != "wordpress":
        return {"counts": {}, "needs_link": [], "mode": "", "mode_label": ""}
    from app.kernel.connectors import idmap
    from app.services.connectors.wordpress import WordPressConnector

    mode = (conn.config or {}).get("wordpress_polylang_mode") or ""
    return {
        "counts": idmap.status_counts(conn.id),
        "needs_link": idmap.list_for_connection(conn.id, status=idmap.NEEDS_LINK),
        "mode": mode,
        "mode_label": WordPressConnector.mode_label(mode) if mode else "",
    }


def _servicenow_idmap(conn) -> dict:
    """The ServiceNow connection's id-map status summary + needs_link worklist (the SAME T3
    rule-6 surface as Notion/Help Scout/WordPress) plus the pairs HELD by workflow (a
    translation no longer Draft), read from persisted config (no live call). None-safe."""
    if not conn or conn.kind != "servicenow":
        return {"counts": {}, "needs_link": [], "held": []}
    from app.kernel.connectors import idmap

    why = (conn.config or {}).get("servicenow_parked_why") or {}
    held = []
    for pair, reason in sorted(why.items()):
        aid, _sep, loc = str(pair).rpartition(":")
        held.append({"aid": aid, "loc": loc, "reason": str(reason)})
    return {
        "counts": idmap.status_counts(conn.id),
        "needs_link": idmap.list_for_connection(conn.id, status=idmap.NEEDS_LINK),
        "held": held,
    }


def contentful_blocked_list(conn) -> list[dict]:
    """The Contentful connection's PERSISTENT validation-blocked pairs (review round-3
    finding 2b), rendered from `conn.config['contentful_blocked']` — NOT from the last
    job result, so the worklist survives across sessions until a fixed source or a retry
    clears it. Each item: {aid, loc, reason}. None-safe for non-Contentful connections."""
    if not conn or conn.kind != "contentful":
        return []
    out = []
    for pair, reason in sorted(((conn.config or {}).get("contentful_blocked") or {}).items()):
        aid, _sep, loc = str(pair).partition(":")
        out.append({"aid": aid, "loc": loc, "reason": str(reason)})
    return out


# --- language sets (reusable target-locale profiles) ---
def _langset_or_404(request, sid):
    """Fetch a language set and org-gate it (leak vector 2). Returns (ls, blocked):
    a foreign / missing set is a 404 (not enumerable). No-op with the gate off."""
    ls = languages.get(sid)
    if ls is None:
        return None, _error_page(request, 404, "Not found.")
    blocked = _org_block(request, getattr(ls, "org_id", None))
    if blocked:
        return None, blocked
    return ls, None


@router.get("/languages")
def languages_index(request: Request):
    from app.kernel import languages_catalog

    return templates.TemplateResponse(
        request,
        "languages.html",
        {
            "request": request,
            "sets": languages.list_sets(org_id=_my_org_id(request)),
            "can_manage": security.can_tune_ai(security.session_user(request)),
            # Wave 3B: the New / Edit forms render the langpicker macro instead of a
            # comma text field. Same source as the project picker (project.html) —
            # the enabled subset offered by default, the full catalog behind "show all".
            "enabled_locales": languages_catalog.enabled_language_rows(),
            "all_locales": languages_catalog.all_locales(),
            "saved": request.query_params.get("saved"),
            "err": request.query_params.get("err"),
        },
    )
