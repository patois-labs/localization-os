"""MCP OAuth authorization server (#753): pure constants and setting validators.

No database, no settings import: `app/services/settings.py` registers the validators below in
VALUE_VALIDATORS (every write door: web, `config set`, /api config, MCP set_config), and
`app/services/mcp_as.py` uses the same helpers at run time, so a value that passes the door is
the value the server reasons about.

Documented in: docs/architecture/mcp-oauth.md
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.services.settings_common import InvalidSettingValue

#: The OAuth scopes an MCP client may be granted. They are the per-user API key grants of the
#: same names (app/services/apikeys.py ALL_SCOPES minus admin and scim, which are never
#: grantable to a remote client). "api" is always implied.
GRANTABLE_SCOPES: tuple = ("api", "docs", "keys")

#: Settings keys (all host-only over /api: REMOTE_IMMUTABLE_KEYS, and confirm-to-set).
SETTING_KEYS: tuple = (
    "mcp_oauth_enabled",
    "mcp_oauth_resource",
    "mcp_oauth_dcr_enabled",
    "mcp_oauth_cimd_hosts",
    "mcp_oauth_rs_secret_hash",
)

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_HOST_RE = re.compile(
    r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)
_HEX64 = re.compile(r"[0-9a-f]{64}")


def is_loopback_host(host: str) -> bool:
    h = (host or "").lower().strip("[]")
    return h in _LOOPBACK or h.endswith(".localhost")


def resource_problem(value) -> str:
    """Why `value` cannot be the canonical MCP server URL (RFC 8707 / RFC 9728 `resource`),
    or "" when it can. Absolute https (http only on a loopback host, for local testing), a
    host, no userinfo, query or fragment, at most 512 characters."""
    v = str(value if value is not None else "").strip()
    if not v:
        return "is empty"
    if len(v) > 512 or any(ord(c) < 0x21 or ord(c) == 0x7F for c in v):
        return "is not a usable URL"
    try:
        p = urlsplit(v)
        _ = p.port
    except ValueError:
        return "is not a usable URL"
    if not p.hostname:
        return "must be an absolute URL with a host"
    if p.scheme == "http" and is_loopback_host(p.hostname):
        pass
    elif p.scheme != "https":
        return "must use https (http is accepted only on localhost)"
    if p.username or p.password or p.query or p.fragment:
        return "may not carry credentials, a query or a fragment"
    return ""


def normalize_resource(value) -> tuple:
    """Comparison key for a resource indicator: scheme and host case-folded (MCP 2026-07-28:
    servers SHOULD accept uppercase scheme/host), default port made explicit, one trailing
    slash ignored. Used only on the SERVER side to accept a client's `resource`; issuer
    comparison elsewhere stays exact."""
    v = str(value if value is not None else "").strip()
    try:
        p = urlsplit(v)
        port = p.port
    except ValueError:
        return ("", "", None, v)
    scheme = (p.scheme or "").lower()
    if port is None:
        port = {"https": 443, "http": 80}.get(scheme)
    return (scheme, (p.hostname or "").lower(), port, p.path.rstrip("/"))


def _v_mcp_oauth_resource(key, value):
    if not str(value if value is not None else "").strip():
        return  # blank = not configured (the server stays off)
    problem = resource_problem(value)
    if problem:
        raise InvalidSettingValue(f"{key} {problem} (example: https://mcp.example.com/mcp).")


def parse_hosts(value) -> list:
    """The CIMD host trust list: comma/space separated host names, each exact or `*.domain`."""
    return [h for h in re.split(r"[\s,]+", str(value or "").strip().lower()) if h]


def _v_mcp_oauth_cimd_hosts(key, value):
    hosts = parse_hosts(value)
    if len(hosts) > 100:
        raise InvalidSettingValue(f"{key}: at most 100 host names.")
    for h in hosts:
        if len(h) > 253 or not _HOST_RE.fullmatch(h):
            raise InvalidSettingValue(
                f"{key}: {h!r} is not a host name (use exact names or *.example.com)."
            )


def host_trusted(host: str, trust: list) -> bool:
    """Blank trust list = any public host; otherwise an exact or `*.suffix` match."""
    if not trust:
        return True
    h = (host or "").lower()
    for t in trust:
        if t.startswith("*."):
            if h.endswith(t[1:]) and h != t[2:]:
                return True
        elif h == t:
            return True
    return False


def _v_mcp_oauth_rs_secret_hash(key, value):
    v = str(value if value is not None else "").strip()
    if v and not _HEX64.fullmatch(v):
        raise InvalidSettingValue(
            f"{key} is the SHA-256 hex digest of the resource-server secret; generate the "
            "secret with `mcp-oauth rs-secret` or the MCP OAuth page instead of setting it."
        )
