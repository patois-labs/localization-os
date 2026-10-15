"""Vendor Studio configuration — env-based, no secrets in code.

Production-bound (PROPOSAL-vendor-platform decision 4): the app ships live, so every
secret and endpoint is read from the environment and NEVER hard-coded, logged, or
rendered. Reads once at import.

Env vars:
  VSTUDIO_SIGNING_SECRET   shared HMAC secret (== the Localization OS Vendor entity's
                           signing_secret). Verifies inbound dispatch signatures and
                           signs outbound returns. REQUIRED for the wire to work.
  VSTUDIO_SSO_SECRET       HMAC secret for the SSO handoff token from Localization OS.
  VSTUDIO_ORG_ID           pin this Studio to ONE Localization OS org (#557). REQUIRED when the
                           Localization OS install is multi-org: an SSO token or API key from
                           any other org, or one that does not state its org, is refused.
  VSTUDIO_ROLE_RECHECK_SECONDS  a session older than this must sign in again so the role is
                           re-read from Localization OS (default 3600; 0 = off, only the
                           VSTUDIO_SESSION_MAX_AGE limit applies).
  VSTUDIO_LOCSTUDIO_URL    base URL of the Localization OS instance (return webhook +
                           the API-key verification read live under it).
  VSTUDIO_DB_PATH          SQLite file path (default: <data>/vendor_studio.db).
  VSTUDIO_SESSION_SECRET   signs Vendor Studio's own session cookie (auto-generated
                           per-process if unset — fine for a single machine/dev).
  VSTUDIO_ADVISOR_BASE_URL LM Studio / OpenAI-compatible base URL (live advisor).
  VSTUDIO_ADVISOR_MODEL    advisor model id.
  VSTUDIO_ADVISOR_MODE     "mock" (default, deterministic + hermetic) | "live".
  VSTUDIO_HOST / VSTUDIO_PORT   bind (default 127.0.0.1 : 8100).
  VSTUDIO_TRUSTED_PROXY_IPS   comma-separated IPs/CIDRs of a fronting proxy whose
                           X-Forwarded-For may be trusted (validated and passed to uvicorn
                           as forwarded_allow_ips, with proxy_headers enabled). EMPTY by
                           default: proxy headers are fully OFF and the socket peer IS
                           the client IP (issue #331: the /login/apikey rate limit and
                           the signature-reject log key on it; never trust loopback
                           implicitly). Set ONLY when a real proxy fronts the app. A
                           wildcard (`*`), an unparseable entry, or any network broader
                           than IPv4 /8 or IPv6 /32 REFUSES TO START (see
                           parse_trusted_proxy_ips): those are caller-controlled identity.
"""

from __future__ import annotations

import ipaddress
import os
import secrets

# Where the SQLite DB lives. Default beside a data dir so a container volume mount at
# /data just works.
_DEFAULT_DATA = os.environ.get("VSTUDIO_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)


class Settings:
    """One resolved settings snapshot. Instantiated once as `settings` below."""

    def __init__(self, env: dict | None = None) -> None:
        e = os.environ if env is None else env
        self.signing_secret: str = e.get("VSTUDIO_SIGNING_SECRET", "") or ""
        self.sso_secret: str = e.get("VSTUDIO_SSO_SECRET", "") or ""
        self.org_id: str = (e.get("VSTUDIO_ORG_ID", "") or "").strip()
        # Only an explicit 0 turns the re-check off; a negative or unparsable value is bad
        # config and falls back to the default (fail closed), never to "off".
        try:
            recheck = int(e.get("VSTUDIO_ROLE_RECHECK_SECONDS", "") or 3600)
        except (TypeError, ValueError):
            recheck = 3600
        self.role_recheck_seconds: int = recheck if recheck >= 0 else 3600
        self.locstudio_url: str = (e.get("VSTUDIO_LOCSTUDIO_URL", "") or "").rstrip("/")
        self.db_path: str = e.get("VSTUDIO_DB_PATH", "") or os.path.join(
            _DEFAULT_DATA, "vendor_studio.db"
        )
        # A per-process random cookie secret is acceptable ONLY for a loopback dev bind;
        # the startup guard HARD-FAILS a non-loopback bind without an explicit secret.
        raw_secret = e.get("VSTUDIO_SESSION_SECRET", "") or ""
        self.session_secret_explicit: bool = bool(raw_secret)
        self.session_secret: str = raw_secret or secrets.token_urlsafe(32)
        # M1: cookies are Secure by default; VSTUDIO_COOKIE_INSECURE=1 is the local-dev /
        # loopback-http opt-out ONLY. Session cookies also carry a max-age (timed signer).
        self.cookie_secure: bool = not _truthy(e.get("VSTUDIO_COOKIE_INSECURE", ""))
        try:
            self.session_max_age: int = int(e.get("VSTUDIO_SESSION_MAX_AGE", "") or 12 * 3600)
        except (TypeError, ValueError):
            self.session_max_age = 12 * 3600
        # The /autopilot/run HTTP route delivers adversarial payloads (blank targets,
        # source echoes, corrupted ids) that bypass the QA gate BY DESIGN — a test harness
        # affordance, NOT a production surface. Default OFF (route 404s), mirroring the main
        # app's remote_admin_enabled master switch. The CLI (`python -m vstudio.autopilot`)
        # and the roundtrip suite (which sets this env for its subprocess) keep full access.
        self.autopilot_enabled: bool = _truthy(e.get("VSTUDIO_AUTOPILOT_ENABLED", ""))
        self.advisor_base_url: str = e.get("VSTUDIO_ADVISOR_BASE_URL", "") or ""
        self.advisor_model: str = e.get("VSTUDIO_ADVISOR_MODEL", "") or "local-model"
        self.advisor_mode: str = (e.get("VSTUDIO_ADVISOR_MODE", "") or "mock").lower()
        self.host: str = e.get("VSTUDIO_HOST", "") or "127.0.0.1"
        try:
            self.port: int = int(e.get("VSTUDIO_PORT", "") or 8100)
        except (TypeError, ValueError):
            self.port = 8100
        # Issue #331: uvicorn's ProxyHeadersMiddleware trusts X-Forwarded-For from loopback
        # peers by default, and the /login/apikey rate limit + the signature-reject log key
        # on request.client.host - a same-host peer could rotate its apparent IP. run.py
        # enables proxy headers ONLY when this explicit list names the proxy (mirrors the
        # main app's trusted_proxy_ips; empty = the socket peer is the client IP). The raw
        # string is NEVER what uvicorn gets: run.py validates it with
        # parse_trusted_proxy_ips() and hands over the normalized networks.
        self.trusted_proxy_ips: str = (e.get("VSTUDIO_TRUSTED_PROXY_IPS", "") or "").strip()
        # The webhook path on the Localization OS side (contract-fixed).
        self.return_webhook_path: str = "/webhooks/vendor-returns"
        # Vendor Wave E — the QUOTE-submission webhook on the Localization OS side (contract-fixed,
        # but env-overridable for a non-standard mount, mirroring the return sibling).
        self.quote_webhook_path: str = (
            e.get("VSTUDIO_QUOTE_WEBHOOK_PATH", "") or "/webhooks/vendor-quotes"
        )

    @property
    def return_webhook_url(self) -> str:
        return f"{self.locstudio_url}{self.return_webhook_path}"

    @property
    def quote_webhook_url(self) -> str:
        return f"{self.locstudio_url}{self.quote_webhook_path}"

    @property
    def advisor_live(self) -> bool:
        return self.advisor_mode == "live"

    @property
    def host_is_loopback(self) -> bool:
        h = (self.host or "").strip().lower()
        return h in ("127.0.0.1", "localhost", "::1", "")

    def assert_production_ready(self) -> None:
        """Startup guard (M1/M7). A NON-loopback bind is a real deployment, so it MUST
        carry an explicit session secret and a HTTPS Localization OS URL. A loopback dev bind is
        exempt. Raises RuntimeError with an actionable message."""
        if self.host_is_loopback:
            return
        if not self.session_secret_explicit:
            raise RuntimeError(
                "VSTUDIO_SESSION_SECRET must be set for a non-loopback bind (a random "
                "per-process secret would invalidate every session on restart and is not "
                "safe for a real deployment)."
            )
        if self.locstudio_url and not self.locstudio_url.lower().startswith("https://"):
            raise RuntimeError(
                "VSTUDIO_LOCSTUDIO_URL must use https:// for a non-loopback bind (an API "
                "key is posted to it over the wire)."
            )

    def reload(self) -> None:
        """Re-read env (tests toggle env then call this)."""
        self.__init__()


def parse_trusted_proxy_ips(value) -> list[str]:
    """Validate VSTUDIO_TRUSTED_PROXY_IPS; return the normalized network strings to hand
    uvicorn (empty list = proxy headers OFF). The rule is the main app's
    `app/services/security.py::proxy_networks`, COPIED and not imported (Vendor Studio is a
    separate tool): reject broad trust - a default route recreates wildcard, caller-controlled
    identity. Raises ValueError naming the bad entry for a wildcard (`*` never parses), an
    unparseable entry, or any network broader than IPv4 /8 or IPv6 /32; run.py refuses to
    start on it instead of handing uvicorn a trust-everything `forwarded_allow_ips`."""
    networks = []
    for entry in [p.strip() for p in str(value or "").split(",") if p.strip()]:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"VSTUDIO_TRUSTED_PROXY_IPS: invalid host or IP/CIDR list: {entry!r} ({exc})"
            ) from exc
        if network.prefixlen < (8 if network.version == 4 else 32):
            raise ValueError(
                f"VSTUDIO_TRUSTED_PROXY_IPS: {entry!r} is too broad: trusted proxies require "
                "IPv4 /8 or narrower and IPv6 /32 or narrower; broad networks permit forged "
                "client identity"
            )
        networks.append(str(network))
    return networks


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


settings = Settings()
