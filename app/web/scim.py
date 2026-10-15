"""SCIM 2.0 provisioning router, mounted at /scim/v2 (Train I).

The thin HTTP shell over app.services.scim (reached via the app.kernel facade). Every
route: (1) 404s with a SCIM error body when scim_enabled is off (non-enumerable — the
kill-switch law, in SCIM error shape); (2) AUTHENTICATES FIRST (Bearer or X-Loc-Token
through the ApiKey path — scim scope + active admin owner) BEFORE parsing any body;
(3) delegates to the service, which owns the mapping, org confinement and audit. All
errors are RFC 7644 §3.12 (a 401 carries WWW-Authenticate: Bearer).

Auth is EXEMPT from the customer app's session/CSRF wall in app/main.py exactly like
/api (token-authed, session-blind); no /scim route reads request.session.

Body-bearing routes are `async def` (to read the JSON body) and push every blocking step
(token lookup, service call, `lock_user_mutations`) to the threadpool, so a slow SCIM write
never stalls the event loop or /ready (#495).

Documented in: docs/architecture/sso-provisioning.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import branding
from app.kernel import scim, settings
from app.web.deps import WorkerBusy, offload_parse

router = APIRouter()

_MEDIA = "application/scim+json"


def _base(request: Request) -> str:
    return settings.external_url(request, "/scim/v2")


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=_MEDIA)


def _error(e: scim.ScimError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
    if e.status == 503:
        # A busy bounded worker pool (#495): retryable, so tell the IdP when to retry.
        from app.web.streaming import retry_after

        headers = {"Retry-After": str(retry_after())}
    return JSONResponse(e.to_dict(), status_code=e.status, media_type=_MEDIA, headers=headers)


async def _offload(func, *args, **kwargs):
    """Blocking SCIM work in the reviewed bounded worker pool (never the event loop, never
    Starlette's unbounded default pool); no free worker in time is a retryable RFC 7644 503."""
    try:
        return await offload_parse(func, *args, **kwargs)
    except WorkerBusy as exc:
        raise scim.ScimError(503, str(exc)) from None


def _extract_token(request: Request) -> str:
    """A Bearer token (the SCIM norm) OR the app's own X-Loc-Token header — accepted ONLY
    on this /scim mount; every other surface keeps rejecting Bearer."""
    authz = request.headers.get("authorization") or ""
    if authz[:7].lower() == "bearer ":
        return authz[7:].strip()
    return (request.headers.get("x-loc-token") or "").strip()


def _authorize(request: Request) -> dict:
    """Flag check (404 when off) THEN auth (401/403). Order matters: a disabled feature
    is non-enumerable even to a valid caller."""
    if not scim.enabled():
        raise scim.ScimError(404, "SCIM is not enabled on this install.")
    return scim.resolve_identity(_extract_token(request))


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise scim.ScimError(400, "Request body is not valid JSON.", "invalidSyntax") from None
    if not isinstance(data, dict):
        raise scim.ScimError(400, "Request body must be a JSON object.", "invalidSyntax")
    return data


# --------------------------------------------------------------------------- #
# Discovery (static)
# --------------------------------------------------------------------------- #
@router.get("/scim/v2/ServiceProviderConfig")
def service_provider_config(request: Request):
    try:
        _authorize(request)
    except scim.ScimError as e:
        return _error(e)
    body = {
        "schemas": [scim.SCHEMA_SPCONFIG],
        "documentationUri": "",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        # A LIMITED filter (userName/externalId/displayName eq) — advertised honestly.
        "filter": {"supported": True, "maxResults": scim._MAX_COUNT},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": f"Authentication via a {branding.brand_name()} API key with the "
                "'scim' scope (Authorization: Bearer, or X-Loc-Token).",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{_base(request)}/ServiceProviderConfig",
        },
    }
    return _json(body)


@router.get("/scim/v2/ResourceTypes")
def resource_types(request: Request):
    try:
        _authorize(request)
    except scim.ScimError as e:
        return _error(e)
    base = _base(request)
    types = [
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": scim.SCHEMA_USER,
            "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/User"},
        },
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": scim.SCHEMA_GROUP,
            "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/Group"},
        },
    ]
    return _json(scim._list_response(types, len(types), 1))


@router.get("/scim/v2/Schemas")
def schemas(request: Request):
    try:
        _authorize(request)
    except scim.ScimError as e:
        return _error(e)
    core = [
        {"id": scim.SCHEMA_USER, "name": "User", "description": "SCIM core User"},
        {"id": scim.SCHEMA_GROUP, "name": "Group", "description": "SCIM core Group"},
    ]
    return _json(scim._list_response(core, len(core), 1))


# --------------------------------------------------------------------------- #
# /Users
# --------------------------------------------------------------------------- #
@router.get("/scim/v2/Users")
def list_users(request: Request):
    try:
        ident = _authorize(request)
        q = request.query_params
        body = scim.list_users(
            ident["org_id"], q.get("filter"), q.get("startIndex"), q.get("count"), _base(request)
        )
        return _json(body)
    except scim.ScimError as e:
        return _error(e)


@router.post("/scim/v2/Users")
async def create_user(request: Request):
    try:
        ident = await _offload(_authorize, request)
        payload = await _body(request)
        resource, created = await _offload(
            lambda: scim.create_user(ident["org_id"], ident["actor"], payload, _base(request))
        )
        return _json(resource, 201 if created else 200)
    except scim.ScimError as e:
        return _error(e)


@router.get("/scim/v2/Users/{uid}")
def get_user(uid: str, request: Request):
    try:
        ident = _authorize(request)
        return _json(scim.get_user(ident["org_id"], uid, _base(request)))
    except scim.ScimError as e:
        return _error(e)


@router.put("/scim/v2/Users/{uid}")
async def replace_user(uid: str, request: Request):
    try:
        ident = await _offload(_authorize, request)
        payload = await _body(request)
        return _json(
            await _offload(
                lambda: scim.replace_user(
                    ident["org_id"], ident["actor"], uid, payload, _base(request)
                )
            )
        )
    except scim.ScimError as e:
        return _error(e)


@router.patch("/scim/v2/Users/{uid}")
async def patch_user(uid: str, request: Request):
    try:
        ident = await _offload(_authorize, request)
        payload = await _body(request)
        return _json(
            await _offload(
                lambda: scim.patch_user(
                    ident["org_id"], ident["actor"], uid, payload, _base(request)
                )
            )
        )
    except scim.ScimError as e:
        return _error(e)


@router.delete("/scim/v2/Users/{uid}")
def delete_user(uid: str, request: Request):
    try:
        ident = _authorize(request)
        scim.deactivate_user(ident["org_id"], ident["actor"], uid)
        return Response(status_code=204)
    except scim.ScimError as e:
        return _error(e)


# --------------------------------------------------------------------------- #
# /Groups
# --------------------------------------------------------------------------- #
@router.get("/scim/v2/Groups")
def list_groups(request: Request):
    try:
        ident = _authorize(request)
        q = request.query_params
        body = scim.list_groups(
            ident["org_id"], q.get("filter"), q.get("startIndex"), q.get("count"), _base(request)
        )
        return _json(body)
    except scim.ScimError as e:
        return _error(e)


@router.post("/scim/v2/Groups")
async def create_group(request: Request):
    try:
        ident = await _offload(_authorize, request)
        payload = await _body(request)
        return _json(
            await _offload(
                lambda: scim.create_group(ident["org_id"], ident["actor"], payload, _base(request))
            ),
            201,
        )
    except scim.ScimError as e:
        return _error(e)


@router.get("/scim/v2/Groups/{gid}")
def get_group(gid: str, request: Request):
    try:
        ident = _authorize(request)
        return _json(scim.get_group(ident["org_id"], gid, _base(request)))
    except scim.ScimError as e:
        return _error(e)


@router.put("/scim/v2/Groups/{gid}")
async def replace_group(gid: str, request: Request):
    try:
        ident = await _offload(_authorize, request)
        payload = await _body(request)
        return _json(
            await _offload(
                lambda: scim.replace_group(
                    ident["org_id"], ident["actor"], gid, payload, _base(request)
                )
            )
        )
    except scim.ScimError as e:
        return _error(e)


@router.patch("/scim/v2/Groups/{gid}")
async def patch_group(gid: str, request: Request):
    try:
        ident = await _offload(_authorize, request)
        payload = await _body(request)
        return _json(
            await _offload(
                lambda: scim.patch_group(
                    ident["org_id"], ident["actor"], gid, payload, _base(request)
                )
            )
        )
    except scim.ScimError as e:
        return _error(e)


@router.delete("/scim/v2/Groups/{gid}")
def delete_group(gid: str, request: Request):
    try:
        ident = _authorize(request)
        scim.delete_group(ident["org_id"], ident["actor"], gid)
        return Response(status_code=204)
    except scim.ScimError as e:
        return _error(e)
