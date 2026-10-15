"""Tailnet routing seam — route LOCAL/self-hosted ENGINE http through the
Tailscale sidecar's SOCKS5 proxy, and ONLY for tailnet destinations.

WHY THIS EXISTS (opt-in): a hosted image can carry a userspace
Tailscale sidecar (see docker-entrypoint.sh) so the cloud machine can privately
reach a home LM Studio / self-hosted model server on a private tailnet — no public
exposure, no port-forward. Outbound HTTP for an ENGINE call whose base URL points
at a tailnet address must go through the sidecar's SOCKS5 proxy (localhost:1055);
EVERYTHING else — the cloud providers (Anthropic/OpenAI/Gemini) and every connector
— must be left completely untouched.

The whole seam is two pure, unit-testable pieces:

  * is_tailnet_host(base_url)   — classify a base URL as a tailnet destination or
                                  not (100.64.0.0/10 CGNAT IP literal, or a *.ts.net
                                  MagicDNS host). Pure; no I/O, no env.
  * tailnet_client_kwargs(url)  — the httpx client kwargs to use for that URL:
                                  {"proxy": <socks5 url>} ONLY when the URL is a
                                  tailnet host AND a sidecar proxy is configured
                                  (config.TAILNET_PROXY non-empty); {} otherwise.

Because the proxy is applied ONLY for tailnet URLs AND only when the sidecar exists,
the default install (no TS_AUTHKEY / no LOC_TMS_TAILNET_PROXY, or any non-tailnet
base URL) gets {} and is byte-identical to before — no proxy, no new dependency
path exercised.

SCOPE: this is deliberately ENGINE-only (the local / openai_compatible provider
clients + their probes). It is NOT wired into any connector path — connectors keep
their own SSRF/egress posture (connectors/base._validate_target) untouched. The
CGNAT 100.64.0.0/10 range that the connector SSRF guard deliberately BLOCKS is
allowed HERE only for engine calls, which is the point of the sidecar.

Documented in: docs/architecture/deployment.md
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app import config

# The shared CGNAT range Tailscale hands out node addresses from (100.64.0.0/10 =
# 100.64.0.0 .. 100.127.255.255). Note ipaddress.is_private is False for this range
# (verified on 3.13), so it is checked explicitly — the same reasoning the connector
# SSRF guard documents for BLOCKING it there.
_TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_DNS_SUFFIX = ".ts.net"


def is_tailnet_host(base_url: str) -> bool:
    """True iff base_url points at a Tailscale tailnet destination:

      * an IP LITERAL in 100.64.0.0/10 (the CGNAT range Tailscale assigns), or
      * a host ending in `.ts.net` (Tailscale MagicDNS).

    Pure and offline — never resolves DNS, never reads env. Anything else (a public
    host, a LAN IP like 192.168.x, localhost, an empty/garbage URL) is False, so the
    caller leaves those on their normal direct path.

    SCOPE: IPv4 CGNAT literals (100.64.0.0/10) and *.ts.net hostnames only; Tailscale
    IPv6 (fd7a:115c:a1e0::/48) is not matched — use the 100.x IPv4 address."""
    host = (urlsplit((base_url or "").strip()).hostname or "").strip().lower()
    if not host:
        return False
    if host.endswith(_TAILNET_DNS_SUFFIX):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a non-.ts.net hostname is not a tailnet literal
    return ip in _TAILNET_CGNAT


def tailnet_proxy_url() -> str | None:
    """The configured SOCKS5 proxy URL for the Tailscale sidecar, or None when no
    sidecar is present. Read through config.TAILNET_PROXY (which is set from
    TS_AUTHKEY / LOC_TMS_TAILNET_PROXY — see config._tailnet_proxy_default) so it
    reflects the runtime env. None == byte-identical default behavior."""
    proxy = (getattr(config, "TAILNET_PROXY", "") or "").strip()
    return proxy or None


def tailnet_client_kwargs(base_url: str) -> dict:
    """httpx client kwargs to reach `base_url`:

      * {"proxy": <socks5 url>}  when base_url is a tailnet host AND a sidecar proxy
        is configured — route this ENGINE call through the Tailscale SOCKS5 proxy.
      * {}                       otherwise — direct, unchanged.

    Both conditions must hold, so a tailnet URL with no sidecar (dev box, no
    TS_AUTHKEY) and a non-tailnet URL with a sidecar both get {} — only a genuine
    tailnet destination on a machine that actually has the sidecar is proxied."""
    if not is_tailnet_host(base_url):
        return {}
    proxy = tailnet_proxy_url()
    if not proxy:
        return {}
    return {"proxy": proxy}
