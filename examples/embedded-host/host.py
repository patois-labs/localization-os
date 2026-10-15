"""Independent ASGI host. Configure environment and bootstrap before importing."""

import os
import secrets

from starlette.responses import JSONResponse

if os.environ.get("LOC_TMS_EMBEDDED") != "1" or not os.environ.get("LOC_TMS_DATA_DIR"):
    raise RuntimeError("Set LOC_TMS_EMBEDDED=1 and a dedicated LOC_TMS_DATA_DIR before boot")

if not os.environ.get("HOST_AUTH_TOKEN", "ascii").isascii() and os.environ.get("HOST_AUTH_TOKEN"):
    raise RuntimeError("HOST_AUTH_TOKEN must contain only ASCII characters before boot")
if not os.environ.get("HOST_USER", "ascii").isascii() and os.environ.get("HOST_USER"):
    raise RuntimeError("HOST_USER must contain only ASCII characters before boot")
if not os.environ.get("LOC_TMS_TRUST_HEADER_SECRET", "ascii").isascii() and os.environ.get(
    "LOC_TMS_TRUST_HEADER_SECRET"
):
    raise RuntimeError("LOC_TMS_TRUST_HEADER_SECRET must contain only ASCII characters before boot")

from app.main import app as product


async def app(scope, receive, send):
    scope = dict(scope)
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in (b"x-loc-trust-secret", b"x-loc-trust-user")
    ]
    if "headers" in scope:
        scope["headers"] = headers
    # Forward startup AND shutdown, including product recovery and storage lifecycle.
    if scope["type"] != "http":
        await product(scope, receive, send)
        return
    path = scope["path"]
    if path == "/host/projects":
        # Tiny single-user host authentication example. A real host substitutes its
        # authenticated session identity here, never a caller-provided user header.
        expected = os.environ.get("HOST_AUTH_TOKEN", "")
        user = os.environ.get("HOST_USER", "")
        secret = os.environ.get("LOC_TMS_TRUST_HEADER_SECRET", "")
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        if not (
            expected
            and user
            and secret
            and len(auth) == 1
            and secrets.compare_digest(auth[0], ("Bearer " + expected).encode("ascii"))
        ):
            await JSONResponse({"detail": "Host authentication required"}, status_code=401)(
                scope, receive, send
            )
            return
        headers.extend(
            [(b"x-loc-trust-secret", secret.encode("ascii")), (b"x-loc-trust-user", user.encode())]
        )
        path = "/api/projects"
    elif path == "/host/tick":
        path = "/api/admin/tick"
    elif path == "/host/retention/purge":
        path = "/api/admin/retention/purge"
    # Cron requests retain the caller's X-Loc-Token and body. Product role/scope,
    # retention, remote-admin and confirmation gates all still run.
    scope["headers"] = headers
    if path != scope["path"]:
        scope.update(path=path, raw_path=path.encode("ascii"))
    await product(scope, receive, send)
