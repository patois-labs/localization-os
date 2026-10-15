"""Shared validation and destination derivation for vendor tenant hosts.

The three vendors whose request host is COMPOSED from an operator-supplied handle rather
than read from a config URL. The handle must match a single ASCII DNS label, so it can
never smuggle in a path, a port, a second host, or a lookalike separator: that was #165,
where a LINGUIST could repoint a connection and carry the admin-set credential with it.

Documented in: docs/architecture/connectors.md
"""

import re

from app.services.connectors.base import ConnectorError

TENANT_HOSTS = {
    "freshdesk": ("domain", "freshdesk.com"),
    "zendesk": ("subdomain", "zendesk.com"),
    "shopify": ("store_name", "myshopify.com"),
    "sanity": ("project_id", "api.sanity.io"),
}
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def tenant_handle(raw, suffix: str) -> str:
    """Accept a DNS label or its exact vendor FQDN, without URL normalization."""
    if not isinstance(raw, str) or not raw.isascii():
        raise ConnectorError("Invalid tenant handle: use an ASCII DNS label.")
    value = raw.lower()
    if value.endswith("." + suffix):
        value = value[: -len(suffix) - 1]
    if not _LABEL.fullmatch(value):
        raise ConnectorError(
            "Invalid tenant handle: use 1-63 ASCII letters, digits or interior hyphens."
        )
    return value


def tenant_host(kind: str, cfg: dict) -> str:
    key, suffix = TENANT_HOSTS[kind]
    return f"{tenant_handle(cfg.get(key), suffix)}.{suffix}"
