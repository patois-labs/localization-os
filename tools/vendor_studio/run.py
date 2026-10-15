"""Vendor Studio uvicorn entry point (default 127.0.0.1:8100).

    python run.py                # local
    VSTUDIO_HOST=0.0.0.0 python run.py   # container (behind a proxy)

Config is env-based (see vstudio/config.py). This adds the package dir to sys.path so
`import vstudio` resolves without an install.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

PLATFORM_PROXY_UNSET_ERROR = (
    "[vendor-studio] ERROR: running behind a platform proxy with VSTUDIO_TRUSTED_PROXY_IPS "
    "unset: every caller shares the proxy address, so one stranger can use up the API-key "
    "login rate limit for all vendors. Read the peer address of public requests and of health "
    "checks in the access log, set VSTUDIO_TRUSTED_PROXY_IPS to those /32s, and redeploy."
)


def main() -> int:
    import uvicorn
    from vstudio.config import parse_trusted_proxy_ips, settings

    # Issue #331: never trust X-Forwarded-For implicitly. uvicorn's default runs
    # ProxyHeadersMiddleware with forwarded_allow_ips=127.0.0.1 (and ::1 since 0.53), so a
    # same-host peer could rotate its apparent IP past the per-IP /login/apikey rate limit
    # and pollute the signature-reject log (both key on request.client.host). Proxy headers
    # go ON only when the explicit trusted-proxy setting names a proxy (never loopback
    # implicitly); otherwise the socket peer IS the client IP, like the main app.
    #
    # Validated BEFORE anything is served, and never handed over raw: a wildcard or a
    # default-route network in that setting would make uvicorn trust every peer, and garbage
    # would look configured while trusting nothing. parse_trusted_proxy_ips() refuses to
    # start on any of those, naming the bad entry (the main app's rule).
    trusted_proxies = parse_trusted_proxy_ips(settings.trusted_proxy_ips)
    # FLY_APP_NAME is how the platform announces that a managed proxy fronts the process.
    if os.environ.get("FLY_APP_NAME", "").strip() and not trusted_proxies:
        # Behind a platform proxy every socket peer is the proxy, so with no trusted proxy
        # configured every caller shares ONE /login/apikey rate-limit key and every reject
        # row carries the proxy address. The /32s differ per machine and cannot be guessed
        # here: the access log below shows the peer address of each request.
        sys.stderr.write(PLATFORM_PROXY_UNSET_ERROR + "\n")
    sys.stderr.write(
        f"[vendor-studio] serving on {settings.host}:{settings.port} "
        f"(advisor mode={settings.advisor_mode}, "
        f"locstudio={'set' if settings.locstudio_url else 'unset'}, "
        f"signing={'set' if settings.signing_secret else 'UNSET'})\n"
    )
    proxy_kwargs = (
        {"proxy_headers": True, "forwarded_allow_ips": ",".join(trusted_proxies)}
        if trusted_proxies
        else {"proxy_headers": False}
    )
    uvicorn.run(
        "vstudio.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        **proxy_kwargs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
