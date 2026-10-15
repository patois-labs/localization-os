"""The ``GET /utcp`` route — the UTCP discovery manual (BACKLOG 6b, Option B).

A THIN route over ``app.services.utcp_manual``: it resolves the base URL, asks the
service to render the manual from the LIVE OpenAPI schema, and returns it as JSON. All
policy — the read-only allow-list security boundary, tool naming, determinism — lives
in the service, so it is unit-testable without HTTP and cannot drift between the route
and the tests.

UNAUTHENTICATED, by design. ``/utcp`` is a machine discovery document (in
``app.main._ALLOW_EXACT``) that a client fetches before it holds any credential. It exposes
only a strict READ-ONLY SUBSET (allow-listed GET tools) of the /api surface. The FULL schema
at ``/openapi.json`` needs a session or an API key when the login wall is on (issue #286);
this subset stays public. (``/help/llm``, the HUMAN-facing HTML driver's manual, is
login-walled.)

BASE URL — do NOT trust the Host header blindly (adversarial review #3). A forged Host
would otherwise render every tool URL (and the /help/llm link) at an attacker's host,
and a UTCP client would then send the real API key in X-Loc-Token to that host —
cache-poisoning credential exfiltration. So we PREFER the operator-configured,
HTTP(S)-validated ``app_base_url`` setting (the same one used to build absolute webhook/
email links) and fall back to the request base URL only when it is unset. On top of
that the response carries ``Cache-Control: no-store`` + ``Vary: Host`` so a shared cache
can never serve one requester's host-derived manual to another.

KILL SWITCH. ``utcp_manual_enabled`` off -> 404 (never 403), rendered by the same
``_error_page`` path an unknown URL takes, so a switched-off manual is non-enumerable.

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.kernel import settings, utcp_manual
from app.web.deps import feature_block

router = APIRouter()


def _resolve_base_url(request: Request) -> str:
    """The absolute base for every tool URL, trailing slash stripped. Prefer the
    operator-set ``app_base_url`` (host-controlled, HTTP(S)-validated); fall back to the
    request's own base URL only when it is blank. See the module docstring for why the
    raw Host header is not trusted when a configured value exists."""
    return settings.external_base_url(request)


@router.get("/utcp")
def utcp_discovery_manual(request: Request):
    """The UTCP manual for this install, generated from the OpenAPI schema."""
    # 404, not 403: a switched-off capability must be non-enumerable. Layer-2 delegated
    # to the ONE shared deps.feature_block (the middleware in app/main is layer 1 and
    # answers /utcp first when the flag is off); both render error.html via _error_page,
    # so this is indistinguishable from an unknown path.
    blocked = feature_block(request, "utcp")
    if blocked:
        return blocked
    # request.app.openapi() is cached by FastAPI and generated from the route table, so
    # the manual can never list a route the app does not actually serve.
    schema = request.app.openapi()
    manual = utcp_manual.build_manual(schema, _resolve_base_url(request))
    # no-store + Vary: Host so a shared cache never serves one caller's host-derived
    # manual to another (defense-in-depth alongside preferring app_base_url).
    return JSONResponse(manual, headers={"Cache-Control": "no-store", "Vary": "Host"})
