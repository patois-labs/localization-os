"""Project notifications: pluggable channels (Slack, Microsoft Teams, signed webhook, email).

Part B of the project-automation MVP (SPEC-project-automation-2026-10-13, section 5; owner's
10-13 decision 7: managers may create channels too).

THE ONE ENTRY POINT other code calls::

    from app.services import notify
    notify.notify("run.finished", project, {"run_id": 42, "counts": {"written": 38, "held": 6}})

`notify(event, project, payload)` never raises and never blocks the caller on a network call. Part A
(project automation runs) plugs in by calling it from its settle step: `event` is one of
`notify.EVENTS`, `project` is a Project row or id, `payload` may carry `run_id` (dedups to one
message per (run, event)), `counts` (a dict of integers: created, updated, written, held, failed,
locales), `limit` (`budget` | `write_ceiling` | `spend_cap`), `band` (80 | 100) and `at` (a UTC
datetime). Nothing else is read: a message NEVER carries source or target text, record titles or
cost figures, only the project and customer names, small integers and a status link.

Design (what a reviewer should check):

* Channels are org-owned (`NotifyChannel`). The destination URL (an incoming-webhook URL is a bearer
  credential) and the webhook signing secret live in `secret_enc` (Fernet, like Vendor and
  Connection) and are write-only: a surface gets a masked tail, never the value.
* Routing (`NotifyRoute`): project, then content type, then project type, then customer, then the
  org default. Per channel KIND the most specific level that has a route covering the event wins. A
  project mute row (channel NULL) silences the project. A route with no event list means the
  default-on events.
* Delivery: `notify()` writes `NotifyOutbox` rows (the delivery log, unique per (channel, dedup
  key)), then a short-lived daemon thread delivers them. A transient failure retries on the
  scheduler tick at 1 minute, 5 minutes and 30 minutes, then the row is `failed` and visible on the
  channel page. A failure never fails the run.
* Egress: every HTTP send goes through the SSRF guard (`egress.guarded_transport_kwargs`, subsystem
  `notify`), HTTPS only, no redirects, a 10 second timeout, about one message per second per
  channel. Slack is pinned to hooks.slack.com and Teams to the Power Platform / Logic Apps hosts.
* Kill switch: the `notify_outbound_enabled` setting (default on). Off means no channel sends and
  nothing is queued; email paths in `notifications.py` are unchanged. The retry sweep sits under
  `sync_scheduler_enabled` like every other egress sweep.
* Audit (fail-closed chain): `notify.channel.create|update|delete|test`, `notify.route.set` and
  `notify.delivery_failed`. Secrets never appear in a row.

Documented in: docs/architecture/notifications.md
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, text
from sqlmodel import Session, select

from app import branding, config
from app.db import engine
from app.models import (
    NotifyChannel,
    NotifyChannelKind,
    NotifyOutbox,
    NotifyRoute,
    Project,
)
from app.services import access, audit, egress, ids, mailer, settings

log = logging.getLogger("loc.notify")

# --- vocabulary --------------------------------------------------------------------------------
EVENTS = (
    "run.started",
    "run.finished",
    "run.failed",
    "run.needs_review",
    "run.limit_hit",
    "automation.paused",
    "automation.blocked",
    "project.created",
    "project.completed",
)
#: Posted when a route names no events (the spec's "default on" column).
DEFAULT_ON = frozenset(
    {
        "run.finished",
        "run.failed",
        "run.needs_review",
        "run.limit_hit",
        "automation.paused",
        "automation.blocked",
    }
)
EVENT_LABELS = {
    "run.started": "A run started",
    "run.finished": "A run finished",
    "run.failed": "A run failed",
    "run.needs_review": "Work is waiting for review",
    "run.limit_hit": "A limit was reached (budget, write ceiling or spend cap)",
    "automation.paused": "Automation was paused",
    "automation.blocked": "A run is blocked by another project's items",
    "project.created": "A project was created",
    "project.completed": "A project was completed",
}
#: Most specific first.
SCOPES = ("project", "content_type", "project_type", "customer", "org")
PROJECT_TYPES = ("document", "key_based")

#: Retry delays after the first failed attempt: 1 minute, 5 minutes, 30 minutes, then `failed`.
RETRY_DELAYS = (60, 300, 1800)
SEND_TIMEOUT_S = 10.0
#: Minimum gap between two sends to one channel (Slack allows about one message per second).
MIN_INTERVAL_S = 1.0
STALE_SENDING_S = 600
MAX_NAME = 80
MAX_RECIPIENTS = 20
SWEEP_LIMIT = 50

ST_PENDING, ST_RETRY, ST_SENDING, ST_SENT, ST_FAILED, ST_SKIPPED = (
    "pending",
    "retry",
    "sending",
    "sent",
    "failed",
    "skipped",
)

#: Teams hosts: Workflows (Power Automate) webhooks. UNVERIFIED against a live tenant, so the list
#: is deliberately the documented Microsoft suffixes only; the legacy Office 365 connector host is
#: kept because tenants still hold such URLs until Microsoft retires them.
TEAMS_HOST_SUFFIXES = (".logic.azure.com", ".powerplatform.com", ".webhook.office.com")
SLACK_HOST = "hooks.slack.com"
#: Shown instead of the guard's own text, which can name an internal address a name resolved to.
REFUSED_TEXT = "that destination is not allowed (private, internal or unresolvable address)."
STALE_AFTER = timedelta(hours=24)  # a row older than this is not sent late (kill switch re-enable)
PRUNE_SENT_DAYS, PRUNE_FAILED_DAYS = 30, 90
SWEEP_BUDGET_S = 20.0
TEST_LIMIT, TEST_WINDOW_S = 5, 3600
WEBHOOK_PORTS = (None, 443, 8443)


class NotifyError(Exception):
    """A refusal or a failed send. `retryable` marks a transient one (network, 408, 429, 5xx)."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        http_status: int | None = None,
        sent_to: list | None = None,
    ):
        super().__init__(message)
        self.sent_to = sent_to or []
        self.retryable = retryable
        self.http_status = http_status


#: Test seams: a fake httpx transport (never the wire) and synchronous delivery.
_TEST_TRANSPORT: httpx.BaseTransport | None = None
_SYNC = False
_LAST_SENT: dict[int, float] = {}
_LAST_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None
_SWEEP_LOCK = threading.Lock()
_FAIL_AUDIT_AT: dict[int, float] = {}
_TEST_TIMES: dict[int, list[float]] = {}
_LAST_PRUNE = [0.0]


def set_test_transport(transport: httpx.BaseTransport | None) -> None:
    global _TEST_TRANSPORT
    _TEST_TRANSPORT = transport


def set_sync(value: bool) -> None:
    """Deliver inside notify() instead of on a thread (tests only)."""
    global _SYNC
    _SYNC = bool(value)


def _utcnow() -> datetime:
    return datetime.utcnow()


def outbound_enabled() -> bool:
    """The `notify_outbound_enabled` kill switch (default on; a no-op with no channels)."""
    return settings.get("notify_outbound_enabled", "1") == "1"


# --- secret at rest (Fernet, exactly like Vendor / Connection credentials) ---------------------
def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(config.ENCRYPTION_KEY.encode())


def _enc(d: dict) -> str:
    return _fernet().encrypt(json.dumps(d or {}).encode()).decode()


def _dec(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except Exception:
        return {}


def secret_dict(channel: NotifyChannel) -> dict:
    """Decrypted {url, signing_secret}. SERVER SIDE ONLY: never hand this to a surface."""
    return _dec(channel.secret_enc if channel else "")


# --- small helpers -----------------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x1f\x7f\u0085  ]")
_EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def _clean(value, limit: int = MAX_NAME) -> str:
    """A display name made safe to print: control and format characters dropped, whitespace
    collapsed, bounded."""
    s = "".join(c for c in str(value or "") if unicodedata.category(c) not in ("Cc", "Cf"))
    s = " ".join(s.split())
    return s[:limit]


def slack_escape(value: str) -> str:
    """Slack mrkdwn control characters (&, <, >) so a project named `<!channel>` cannot ping."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def teams_escape(value: str) -> str:
    """Adaptive Card TextBlock markdown: backslash-escape the characters that format or link."""
    return re.sub(r"([\\`*_{}\[\]<>()#+!|~])", r"\\\1", value)


def _same_org(a, b) -> bool:
    return access.normalized_org_id(a) == access.normalized_org_id(b)


def _mask(url: str) -> str:
    """The write-only destination hint: host and the last six characters of the URL."""
    if not url:
        return ""
    host = urlsplit(url).hostname or ""
    return f"{host}/...{url[-6:]}"


def _status_url(project_id) -> str:
    base = settings.external_base_url()
    if not base or project_id is None:
        return ""
    return f"{base}/projects/{int(project_id)}"


# --- destination validation --------------------------------------------------------------------
def validate_url(kind: str, url: str) -> str:
    """The destination URL for `kind`, or NotifyError. HTTPS only, no credentials in the URL,
    Slack and Teams pinned to their vendor hosts, the SSRF guard consulted for every kind."""
    url = (url or "").strip()
    if not url or len(url) > 2048 or _CTRL.search(url):
        raise NotifyError("A destination URL is required.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise NotifyError("The destination must be an https:// URL.")
    if parts.username or parts.password:
        raise NotifyError("The destination URL must not carry a user name or password.")
    host = (parts.hostname or "").lower()
    if not host:
        raise NotifyError("The destination URL has no host.")
    if kind == NotifyChannelKind.WEBHOOK and parts.port not in WEBHOOK_PORTS:
        raise NotifyError("A webhook must use port 443 or 8443.")
    if kind in (NotifyChannelKind.SLACK, NotifyChannelKind.TEAMS) and parts.port not in (None, 443):
        raise NotifyError("Only the standard https port is allowed for this channel kind.")
    if kind == NotifyChannelKind.SLACK and host != SLACK_HOST:
        raise NotifyError(f"A Slack incoming webhook must be on {SLACK_HOST}.")
    if kind == NotifyChannelKind.TEAMS and not host.endswith(TEAMS_HOST_SUFFIXES):
        raise NotifyError(
            "A Teams webhook must be a Workflows (Power Automate) URL on "
            + ", ".join(s.lstrip(".") for s in TEAMS_HOST_SUFFIXES)
            + "."
        )
    try:
        egress.validate_target(url, subsystem="notify")
    except egress.EgressBlocked as exc:
        log.info("notify destination refused at save: %s", exc)
        raise NotifyError(REFUSED_TEXT) from exc
    return url


def _recipients(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,;\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise NotifyError("At least one email recipient is required.")
    out: list[str] = []
    for item in raw:
        addr = str(item or "").strip()
        if not _EMAIL.match(addr) or len(addr) > 254 or _CTRL.search(addr):
            raise NotifyError("One of the email recipients is not a valid address.")
        if addr.lower() not in {a.lower() for a in out}:
            out.append(addr)
    if len(out) > MAX_RECIPIENTS:
        raise NotifyError(f"At most {MAX_RECIPIENTS} email recipients per channel.")
    return out


# --- visibility --------------------------------------------------------------------------------
def visible(session: Session, cid, *, org_id=None, customer_id=None) -> NotifyChannel | None:
    """The channel IFF this caller may see it: same org, and a customer-bound caller sees only its
    own customer's channels. A foreign or missing id answers the same (None)."""
    if access.org_scope_blocked(org_id):
        return None
    org_id = access.org_scope_value(org_id)
    n = ids.coerce_id(cid)
    ch = session.get(NotifyChannel, n) if n is not None else None
    if ch is None:
        return None
    if org_id is not None and not _same_org(ch.org_id, org_id):
        return None
    if customer_id is not None and ch.customer_id != customer_id:
        return None
    return ch


def list_channels(session: Session, *, org_id=None, customer_id=None) -> list[NotifyChannel]:
    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    rows = list(session.exec(select(NotifyChannel).order_by(NotifyChannel.name, NotifyChannel.id)))
    return [
        c
        for c in rows
        if (org_id is None or _same_org(c.org_id, org_id))
        and (customer_id is None or c.customer_id == customer_id)
    ]


def to_dict(session: Session, ch: NotifyChannel) -> dict:
    """The surface shape. NEVER the URL or the signing secret: a masked hint and presence flags."""
    sec = secret_dict(ch)
    failed = session.exec(
        select(func.count())
        .select_from(NotifyOutbox)
        .where(NotifyOutbox.channel_id == ch.id, NotifyOutbox.status == ST_FAILED)
    ).one()
    return {
        "id": ch.id,
        "name": ch.name,
        "kind": ch.kind,
        "enabled": bool(ch.enabled),
        "customer_id": ch.customer_id,
        "destination": _mask(sec.get("url", "")),
        "recipients": list((ch.config or {}).get("recipients") or []),
        "has_signing_secret": bool(sec.get("signing_secret")),
        "last_test_at": ch.last_test_at.isoformat() if ch.last_test_at else None,
        "last_test_ok": ch.last_test_ok,
        "last_test_message": ch.last_test_message or "",
        "failed_deliveries": int(failed or 0),
        "created_by": ch.created_by,
    }


def can_modify(ch: NotifyChannel, user, actor: str) -> bool:
    """Who may edit, re-point or delete a channel: an admin, or the person who created it. (Any
    manager may create one and attach any visible channel to a project; 10-13.) With the
    login wall off every caller is the operator."""
    from app.services import security

    if not security.auth_required():
        return True
    return bool(security.is_admin(user)) or (bool(ch.created_by) and ch.created_by == actor)


def _alert_admins(ch: NotifyChannel, actor: str, what: str) -> None:
    """Email the org's admins that a channel's destination or signing secret changed. Never
    carries the URL. Best effort: a mail failure never blocks the change."""
    try:
        from app import branding
        from app.services import notifications

        with Session(engine) as s:
            rcpts = notifications._admin_recipients(s, ch.org_id)
        subject = f"[{branding.org_name()}] Notification channel changed: {_clean(ch.name)}"
        body = (
            f"The {what} of the {ch.kind} notification channel '{_clean(ch.name)}' was changed "
            f"by {_clean(actor) or 'an operator'}.\n\nIf this was not expected, review the "
            "channel under Settings > Notifications.\n"
        )
        for to in rcpts:
            mailer.send(to, subject, body)
    except Exception:
        log.exception("channel change alert failed")


# --- channel CRUD ------------------------------------------------------------------------------
def _unique_name(session, name: str, org_id, exclude_id=None) -> None:
    for c in session.exec(select(NotifyChannel)).all():
        if (
            c.id != exclude_id
            and _same_org(c.org_id, org_id)
            and c.name.strip().lower() == name.lower()
        ):
            raise NotifyError(f"A channel named {name!r} already exists.")


def _check_name(raw) -> str:
    try:
        name = _clean(ids.clean_name(raw, field="name", max_len=MAX_NAME), MAX_NAME)
    except ids.InvalidNameError as exc:
        raise NotifyError(str(exc)) from exc
    if not name:
        raise NotifyError("A channel name is required.")
    return name


def create_channel(
    session: Session,
    *,
    kind: str,
    name: str,
    url: str = "",
    recipients=None,
    customer_id: int | None = None,
    org_id=None,
    created_by: str = "",
    ip: str = "",
) -> tuple[NotifyChannel, str]:
    """Create a channel. Returns (channel, signing_secret): the secret is non-empty ONLY for a
    generic webhook and is shown to the caller exactly once (it is never readable again)."""
    if kind not in NotifyChannelKind.ALL:
        raise NotifyError(
            f"Unknown channel kind {kind!r} (use {', '.join(NotifyChannelKind.ALL)})."
        )
    name = _check_name(name)
    org_id = access.stampable_org_id(org_id)
    _unique_name(session, name, org_id)
    secret: dict = {}
    cfg: dict = {}
    shown = ""
    if kind == NotifyChannelKind.EMAIL:
        cfg["recipients"] = _recipients(recipients)
    else:
        secret["url"] = validate_url(kind, url)
        if kind == NotifyChannelKind.WEBHOOK:
            shown = secrets.token_urlsafe(32)
            secret["signing_secret"] = shown
    ch = NotifyChannel(
        org_id=org_id,
        customer_id=customer_id,
        kind=kind,
        name=name,
        config=cfg or None,
        secret_enc=_enc(secret) if secret else "",
        created_by=created_by,
    )
    session.add(ch)
    session.commit()
    session.refresh(ch)
    audit.log(
        actor=created_by or "system",
        action="notify.channel.create",
        target=f"notify-channel {ch.id}",
        detail=f"kind={kind} name={name}",
        ip=ip,
        customer_id=customer_id,
        org_id=org_id,
    )
    return ch, shown


def update_channel(
    session: Session,
    ch: NotifyChannel,
    *,
    name: str | None = None,
    url: str | None = None,
    recipients=None,
    enabled: bool | None = None,
    rotate_signing_secret: bool = False,
    actor: str = "",
    ip: str = "",
) -> str:
    """Change a channel. Returns a NEW signing secret (shown once) when one was rotated, else ''."""
    changed: list[str] = []
    shown = ""
    if name is not None:
        new = _check_name(name)
        _unique_name(session, new, ch.org_id, exclude_id=ch.id)
        ch.name = new
        changed.append("name")
    if enabled is not None:
        ch.enabled = bool(enabled)
        changed.append("enabled" if enabled else "disabled")
    sec = secret_dict(ch)
    if url:
        if ch.kind == NotifyChannelKind.EMAIL:
            raise NotifyError("An email channel has recipients, not a URL.")
        sec["url"] = validate_url(ch.kind, url)
        changed.append("url")
    if rotate_signing_secret:
        if ch.kind != NotifyChannelKind.WEBHOOK:
            raise NotifyError("Only a webhook channel has a signing secret.")
        shown = secrets.token_urlsafe(32)
        sec["signing_secret"] = shown
        changed.append("signing_secret")
    if recipients is not None:
        if ch.kind != NotifyChannelKind.EMAIL:
            raise NotifyError("Only an email channel has recipients.")
        ch.config = {**(ch.config or {}), "recipients": _recipients(recipients)}
        changed.append("recipients")
    if sec:
        ch.secret_enc = _enc(sec)
    ch.updated_at = _utcnow()
    session.add(ch)
    session.commit()
    audit.log(
        actor=actor or "system",
        action="notify.channel.update",
        target=f"notify-channel {ch.id}",
        detail="changed=" + ",".join(changed),
        ip=ip,
        customer_id=ch.customer_id,
        org_id=ch.org_id,
    )
    if "url" in changed or "signing_secret" in changed:
        _alert_admins(ch, actor, "destination" if "url" in changed else "signing secret")
    return shown


def delete_channel(session: Session, ch: NotifyChannel, *, actor: str = "", ip: str = "") -> None:
    cid, name, cust, org = ch.id, ch.name, ch.customer_id, ch.org_id
    for model in (NotifyRoute, NotifyOutbox):
        for row in session.exec(select(model).where(model.channel_id == cid)).all():
            session.delete(row)
        session.flush()
    session.delete(ch)
    session.commit()
    audit.log(
        actor=actor or "system",
        action="notify.channel.delete",
        target=f"notify-channel {cid}",
        detail=f"name={name}",
        ip=ip,
        customer_id=cust,
        org_id=org,
    )


# --- routes ------------------------------------------------------------------------------------
def _scope_ref(scope: str, ref) -> str:
    if scope not in SCOPES:
        raise NotifyError(f"Unknown scope {scope!r} (use {', '.join(SCOPES)}).")
    if scope == "org":
        return ""
    if scope == "project_type":
        r = str(ref or "").strip().lower()
        if r not in PROJECT_TYPES:
            raise NotifyError(f"project_type must be one of {', '.join(PROJECT_TYPES)}.")
        return r
    n = ids.coerce_id(ref)
    if n is None:
        raise NotifyError(f"A {scope.replace('_', ' ')} id is required.")
    return str(n)


def _events(raw) -> list[str] | None:
    if raw in (None, "", [], ()):
        return None
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    bad = [e for e in raw if e not in EVENTS]
    if bad:
        raise NotifyError(f"Unknown event(s): {', '.join(map(str, bad))}. Use {', '.join(EVENTS)}.")
    return sorted(set(raw), key=EVENTS.index)


def set_route(
    session: Session,
    *,
    scope: str,
    ref,
    channel: NotifyChannel,
    events=None,
    project: Project | None = None,
    created_by: str = "",
    ip: str = "",
) -> NotifyRoute:
    """Attach `channel` to a scope (idempotent: the same (channel, scope, ref) updates its events).
    A channel pinned to a customer only routes that customer's projects."""
    ref_s = _scope_ref(scope, ref)
    evs = _events(events)
    if channel.customer_id is not None:
        if scope in ("org", "content_type", "project_type"):
            raise NotifyError(
                "A channel pinned to a customer can only be attached to that customer or one of "
                "its projects."
            )
        if scope == "customer" and ref_s != str(channel.customer_id):
            raise NotifyError("That channel belongs to a different customer.")
        if scope == "project" and (project is None or project.customer_id != channel.customer_id):
            raise NotifyError("That channel belongs to a different customer.")
    row = session.exec(
        select(NotifyRoute).where(
            NotifyRoute.channel_id == channel.id,
            NotifyRoute.scope == scope,
            NotifyRoute.scope_ref == ref_s,
        )
    ).first()
    if row is None:
        row = NotifyRoute(
            org_id=channel.org_id,
            channel_id=channel.id,
            scope=scope,
            scope_ref=ref_s,
            created_by=created_by,
        )
    row.events = evs
    session.add(row)
    session.commit()
    session.refresh(row)
    audit.log(
        actor=created_by or "system",
        action="notify.route.set",
        target=f"notify-route {row.id}",
        detail=f"channel={channel.id} scope={scope} ref={ref_s} events={','.join(evs or [])}",
        ip=ip,
        customer_id=channel.customer_id,
        org_id=channel.org_id,
    )
    return row


def clear_route(
    session: Session,
    row: NotifyRoute,
    *,
    actor: str = "",
    ip: str = "",
    customer_id: int | None = None,
) -> None:
    rid, scope, ref, chan, org = row.id, row.scope, row.scope_ref, row.channel_id, row.org_id
    session.delete(row)
    session.commit()
    audit.log(
        actor=actor or "system",
        action="notify.route.set",
        target=f"notify-route {rid}",
        detail=f"removed channel={chan} scope={scope} ref={ref}",
        ip=ip,
        customer_id=customer_id,
        org_id=org,
    )


def set_project_muted(
    session: Session, project: Project, muted: bool, *, actor: str = "", ip: str = ""
) -> bool:
    """A project can turn notifications off (a mute row beats every other level)."""
    rows = session.exec(
        select(NotifyRoute).where(
            NotifyRoute.channel_id == None,  # noqa: E711
            NotifyRoute.scope == "project",
            NotifyRoute.scope_ref == str(project.id),
        )
    ).all()
    row = rows[0] if rows else None
    if muted and row is None:
        session.add(
            NotifyRoute(
                org_id=project.org_id,
                channel_id=None,
                scope="project",
                scope_ref=str(project.id),
                created_by=actor,
            )
        )
    elif not muted and row is not None:
        for r in rows:
            session.delete(r)
    else:
        return muted
    session.commit()
    audit.log(
        actor=actor or "system",
        action="notify.route.set",
        target=f"project {project.id}",
        detail="muted" if muted else "unmuted",
        ip=ip,
        customer_id=project.customer_id,
        org_id=project.org_id,
    )
    return muted


def route_to_dict(row: NotifyRoute, channel: NotifyChannel | None = None) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "scope_ref": row.scope_ref,
        "channel_id": row.channel_id,
        "channel": channel.name if channel is not None else None,
        "kind": channel.kind if channel is not None else None,
        "events": list(row.events or []),
        "muted": row.channel_id is None,
    }


def _candidate_refs(project: Project) -> dict[str, str]:
    refs = {"project": str(project.id), "project_type": str(project.project_type or "document")}
    if project.content_type_id is not None:
        refs["content_type"] = str(project.content_type_id)
    if project.customer_id is not None:
        refs["customer"] = str(project.customer_id)
    refs["org"] = ""
    return refs


def _routes_for(
    session: Session, project: Project
) -> list[tuple[NotifyRoute, NotifyChannel | None]]:
    refs = _candidate_refs(project)
    out: list[tuple[NotifyRoute, NotifyChannel | None]] = []
    # Only the candidate (scope, ref) rows, in SQL: this runs on every notify() and nine times
    # per project page render, so it must not scan every route of the install (review gate 10-13).
    cond = None
    for scope, ref in refs.items():
        c = (NotifyRoute.scope == scope) & (NotifyRoute.scope_ref == ref)
        cond = c if cond is None else (cond | c)
    for row in session.exec(select(NotifyRoute).where(cond)).all():
        if refs.get(row.scope) != row.scope_ref or not _same_org(row.org_id, project.org_id):
            continue
        ch = session.get(NotifyChannel, row.channel_id) if row.channel_id is not None else None
        if row.channel_id is not None and ch is None:
            continue
        out.append((row, ch))
    return out


def _covers(row: NotifyRoute, event: str) -> bool:
    return event in (set(row.events) if row.events else DEFAULT_ON)


def resolve(session: Session, project: Project, event: str) -> list[tuple[NotifyChannel, str]]:
    """[(channel, level)] to post `event` to. Per channel kind the most specific level whose route
    covers the event wins (every route of that level and kind is used). A project mute wins over
    everything; a disabled channel and a channel pinned to another customer are skipped."""
    routes = _routes_for(session, project)
    if any(r.channel_id is None and r.scope == "project" for r, _ in routes):
        return []
    best: dict[str, list[tuple[NotifyChannel, str, int]]] = {}
    for row, ch in routes:
        if ch is None or not ch.enabled or not _covers(row, event):
            continue
        if ch.customer_id is not None and ch.customer_id != project.customer_id:
            continue
        rank = SCOPES.index(row.scope)
        cur = best.setdefault(ch.kind, [])
        if cur and rank < cur[0][2]:
            cur.clear()
        if not cur or rank == cur[0][2]:
            cur.append((ch, row.scope, rank))
    seen: set[int] = set()
    out: list[tuple[NotifyChannel, str]] = []
    for items in best.values():
        for ch, level, _ in items:
            if ch.id not in seen:
                seen.add(ch.id)
                out.append((ch, level))
    return out


def effective(session: Session, project: Project) -> dict:
    """What a project card shows: whether it is muted and, per event, the channels and the level
    each one came from."""
    routes = _routes_for(session, project)
    muted = any(r.channel_id is None and r.scope == "project" for r, _ in routes)
    events = {}
    for ev in EVENTS:
        events[ev] = (
            []
            if muted
            else [
                {"channel_id": ch.id, "channel": ch.name, "kind": ch.kind, "from": level}
                for ch, level in resolve(session, project, ev)
            ]
        )
    return {
        "project_id": project.id,
        "muted": muted,
        "routes": [route_to_dict(r, c) for r, c in routes if r.channel_id is not None],
        "events": events,
        "outbound_enabled": outbound_enabled(),
    }


# --- message building --------------------------------------------------------------------------
_VERBS = {
    "run.started": "started a run",
    "run.finished": "finished a run",
    "run.failed": "had a run fail",
    "run.needs_review": "has work waiting for review",
    "run.limit_hit": "hit a limit",
    "automation.paused": "had its automation paused",
    "automation.blocked": "had a run blocked by another project",
    "project.created": "was created",
    "project.completed": "was completed",
    "test": "is only a test",
}
_COUNT_WORDS = (
    ("created", "created"),
    ("updated", "updated"),
    ("written", "written"),
    ("held", "waiting for review"),
    ("failed", "failed"),
    ("locales", "languages"),
    ("pairs", "items held by another project"),
)


def _limit_phrase(payload: dict) -> str:
    kind = payload.get("limit")
    if kind == "budget":
        band = payload.get("band")
        return "passed 80% of its monthly budget" if band == 80 else "reached its monthly budget"
    if kind == "write_ceiling":
        return "stopped at the per-run write ceiling"
    if kind == "spend_cap":
        return "stopped at the per-run spend cap"
    return ""


def _facts(event: str, project: Project | None, payload: dict, customer_name: str = "") -> dict:
    """The neutral facts that go into the outbox row: names, small integers, a status link."""
    counts = {}
    for key, _ in _COUNT_WORDS:
        val = (payload.get("counts") or {}).get(key)
        if isinstance(val, int) and not isinstance(val, bool) and 0 <= val < 10**9:
            counts[key] = val
    at = payload.get("at")
    at = at if isinstance(at, datetime) else _utcnow()
    facts = {
        "event": event,
        "project_id": getattr(project, "id", None),
        "project": _clean(getattr(project, "name", "") or "a project"),
        "customer": _clean(customer_name),
        "counts": counts,
        "limit": payload.get("limit")
        if payload.get("limit") in ("budget", "write_ceiling", "spend_cap")
        else "",
        "band": payload.get("band") if payload.get("band") in (80, 100) else None,
        "at": at.replace(microsecond=0).isoformat(),
        "url": _status_url(getattr(project, "id", None)),
    }
    if event == "automation.blocked":
        # the other project's NAME only (the caller leaves it empty across customers) and which
        # side this message is for; never an item title or any content
        facts["other_project"] = _clean(payload.get("other") or "")
        facts["role"] = "owner" if payload.get("role") == "owner" else "blocked"
    return facts


def _when(facts: dict) -> str:
    try:
        dt = datetime.fromisoformat(facts["at"])
    except Exception:
        return ""
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{dt.strftime('%M %p')} UTC"


def render_text(facts: dict, escape=lambda s: s) -> tuple[str, str]:
    """(headline, detail) plain strings; `escape` makes names safe for the target format."""
    if facts["event"] == "test":
        return f"Test message from {branding.brand_name()}: this channel is connected.", ""
    name = escape(facts["project"])
    cust = f" ({escape(facts['customer'])})" if facts.get("customer") else ""
    ev = facts["event"]
    verb = _VERBS.get(ev, "had an update")
    if ev == "run.limit_hit":
        phrase = _limit_phrase(facts)
        verb = phrase or verb
    if ev == "automation.blocked":
        other = escape(facts.get("other_project") or "") or "another project"
        if facts.get("role") == "owner":
            verb = f"holds items that block {other}"
        else:
            verb = f"was blocked by {other}"
    head = f"{name}{cust} {verb}."
    bits = [f"{n} {word}" for key, word in _COUNT_WORDS if (n := facts["counts"].get(key))]
    when = _when(facts)
    parts = []
    if bits:
        parts.append(", ".join(bits) + ".")
    if when:
        parts.append(when + ".")
    return head, " ".join(parts)


def render_slack(facts: dict) -> dict:
    head, detail = render_text(facts, slack_escape)
    text_ = head
    if detail:
        text_ += "\n" + detail
    url = facts.get("url")
    if url and "|" not in url and ">" not in url and "<" not in url:
        text_ += f"\n<{url}|Open project>"
    else:
        text_ += f"\nOpen {branding.brand_name()} to view it."
    return {"text": text_, "mrkdwn": True}


def render_teams(facts: dict) -> dict:
    head, detail = render_text(facts, teams_escape)
    body = [{"type": "TextBlock", "text": head, "weight": "Bolder", "wrap": True}]
    if detail:
        body.append({"type": "TextBlock", "text": detail, "wrap": True})
    card: dict = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    if facts.get("url"):
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open project", "url": facts["url"]}]
    else:
        body.append(
            {
                "type": "TextBlock",
                "text": f"Open {branding.brand_name()} to view it.",
                "wrap": True,
            }
        )
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def render_webhook(facts: dict, delivery_id: str) -> dict:
    head, detail = render_text(facts)
    out = {
        "version": 1,
        "id": delivery_id,
        "event": facts["event"],
        "occurred_at": facts["at"] + "Z",
        "project": {
            "id": facts["project_id"],
            "name": facts["project"],
            "customer": facts["customer"],
            "url": facts["url"],
        },
        "counts": facts["counts"],
        "limit": facts["limit"],
        "band": facts["band"],
        "summary": (head + " " + detail).strip(),
    }
    if facts["event"] == "automation.blocked":
        out["other_project"] = facts.get("other_project", "")
        out["role"] = facts.get("role", "blocked")
    return out


def render_email(facts: dict) -> tuple[str, str]:
    head, detail = render_text(facts)
    subject = f"[{branding.org_name()}] {head}"
    body = head + "\n" + (detail + "\n" if detail else "")
    body += "\n" + (facts.get("url") or f"Open {branding.brand_name()} to view the project.") + "\n"
    return subject, body


def sign_body(secret: str, body: bytes, ts: int | None = None) -> str:
    """The X-LocOS-Signature value: `t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + body)>`. The
    receiver recomputes it over the raw body and should refuse a timestamp more than 5 minutes off
    (the same scheme Part A's inbound project trigger uses)."""
    ts = int(time.time()) if ts is None else int(ts)
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


# --- sending -----------------------------------------------------------------------------------
def _post(url: str, body: bytes, headers: dict) -> httpx.Response:
    """POST through the SSRF guard (HTTPS only, no redirects, bounded). The seam
    verify_egress_coverage inspects: the client and the guard are built in this one function."""
    try:
        with httpx.Client(
            timeout=SEND_TIMEOUT_S,
            follow_redirects=False,
            **egress.guarded_transport_kwargs(
                "notify", transport=_TEST_TRANSPORT, error_type=NotifyError
            ),
        ) as client:
            return client.post(url, content=body, headers=headers)
    except NotifyError as exc:
        log.info("notify egress refused: %s", exc)
        raise NotifyError("destination refused: " + REFUSED_TEXT, retryable=False) from exc
    except httpx.HTTPError as exc:
        raise NotifyError(f"delivery failed: {type(exc).__name__}", retryable=True) from exc


def _classify(resp: httpx.Response) -> None:
    code = resp.status_code
    if 200 <= code < 300:
        return
    retry = code in (408, 425, 429) or code >= 500
    raise NotifyError(f"destination answered HTTP {code}", retryable=retry, http_status=code)


def _throttle(channel_id: int) -> None:
    if MIN_INTERVAL_S <= 0:
        return
    with _LAST_LOCK:
        last = _LAST_SENT.get(channel_id, 0.0)
        wait = MIN_INTERVAL_S - (time.monotonic() - last)
        _LAST_SENT[channel_id] = time.monotonic() + max(wait, 0.0)
    if wait > 0:
        time.sleep(min(wait, MIN_INTERVAL_S))


def send_facts(channel: NotifyChannel, facts: dict, delivery_id: str) -> int | None:
    """Send one rendered message on `channel`. Returns the HTTP status (None for email). Raises
    NotifyError (retryable or not). Does not touch the database."""
    _throttle(channel.id)
    if channel.kind == NotifyChannelKind.EMAIL:
        subject, body = render_email(facts)
        rcpts = list((channel.config or {}).get("recipients") or [])
        if not rcpts:
            raise NotifyError("the email channel has no recipients")
        if not mailer.configured():
            raise NotifyError("SMTP is not configured on this install")
        done = list(facts.get("_sent_to") or [])
        failed = []
        for r in rcpts:
            if r in done:
                continue
            if mailer.send(r, subject, body):
                done.append(r)
            else:
                failed.append(r)
        if failed:
            raise NotifyError(
                f"mail could not be sent to {len(failed)} recipient(s)",
                retryable=True,
                sent_to=done,
            )
        return None
    sec = secret_dict(channel)
    url = sec.get("url") or ""
    if not url:
        raise NotifyError("the channel has no destination")
    headers = {"content-type": "application/json", "user-agent": "LocalizationOS-Notify/1"}
    if channel.kind == NotifyChannelKind.SLACK:
        payload = render_slack(facts)
    elif channel.kind == NotifyChannelKind.TEAMS:
        payload = render_teams(facts)
    else:
        payload = render_webhook(facts, delivery_id)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if channel.kind == NotifyChannelKind.WEBHOOK:
        headers["x-locos-event"] = facts["event"]
        headers["x-locos-delivery"] = delivery_id
        if sec.get("signing_secret"):
            headers["x-locos-signature"] = sign_body(sec["signing_secret"], body)
    resp = _post(url, body, headers)
    _classify(resp)
    return resp.status_code


# --- outbox ------------------------------------------------------------------------------------
def notify(
    event: str, project, payload: dict | None = None, *, session: Session | None = None
) -> int:
    """Queue `event` for every channel the project routes it to and start delivery. Returns the
    number of messages queued. NEVER raises, never blocks on the network.

    With `session` the rows join the caller's transaction (no lost or phantom events: they commit
    or roll back with the caller) and are delivered by the next scheduler tick; without it a row is
    committed at once and a short-lived thread sends it."""
    try:
        if event not in EVENTS or not outbound_enabled():
            return 0
        payload = dict(payload or {})
        own = session is None
        s = session or Session(engine)
        try:
            return _enqueue(s, event, project, payload, own)
        finally:
            if own:
                s.close()
    except Exception:
        log.exception("notify(%s) failed", event)
        return 0


def _enqueue(s: Session, event: str, project, payload: dict, own: bool) -> int:
    pid = ids.coerce_id(getattr(project, "id", project))
    proj = s.get(Project, pid) if pid is not None else None
    if proj is None:
        return 0
    if not s.exec(select(func.count()).select_from(NotifyChannel)).one():
        return 0  # nothing configured: a cheap no-op
    targets = resolve(s, proj, event)
    if not targets:
        return 0
    cust = ""
    if proj.customer_id is not None:
        from app.models import Customer

        c = s.get(Customer, proj.customer_id)
        cust = getattr(c, "name", "") or ""
    facts = _facts(event, proj, payload, cust)
    run_key = payload.get("dedup") or payload.get("run_id") or payload.get("job_id")
    dedup = f"{event}:{run_key}" if run_key is not None else f"{event}:{uuid.uuid4().hex}"
    queued: list[int] = []
    for ch, _level in targets:
        if s.exec(
            select(NotifyOutbox.id).where(
                NotifyOutbox.channel_id == ch.id, NotifyOutbox.dedup_key == dedup
            )
        ).first():
            continue
        row = NotifyOutbox(
            org_id=ch.org_id,
            channel_id=ch.id,
            project_id=proj.id,
            event=event,
            dedup_key=dedup[:200],
            payload=facts,
            status=ST_PENDING,
            next_attempt_at=_utcnow(),
        )
        s.add(row)
        s.flush()
        queued.append(row.id)
    if own:
        s.commit()
        if queued:
            _spawn(queued)
    return len(queued)


def _spawn(row_ids: list[int]) -> None:
    if _SYNC:
        deliver_ids(row_ids)
        return
    global _POOL
    with _LAST_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify-send")
    _POOL.submit(deliver_ids, row_ids)


def deliver_ids(row_ids: list[int], *, deadline: float | None = None) -> int:
    sent = 0
    broken: set[int] = set()  # channels that just timed out or refused: skip their other rows
    for rid in row_ids:
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            if deliver_one(rid, broken):
                sent += 1
        except Exception:
            log.exception("notify delivery %s crashed", rid)
    return sent


def _claim(s: Session, rid: int, now: datetime) -> bool:
    res = s.execute(
        text(
            "UPDATE notifyoutbox SET status = :sending, next_attempt_at = :now WHERE id = :id "
            "AND status IN (:p, :r)"
        ),
        {"sending": ST_SENDING, "now": now, "id": rid, "p": ST_PENDING, "r": ST_RETRY},
    )
    s.commit()
    return res.rowcount == 1


def deliver_one(rid: int, broken: set | None = None) -> bool:
    """Deliver one outbox row. True when it was sent. The kill switch leaves the row pending."""
    if not outbound_enabled():
        return False
    now = _utcnow()
    with Session(engine) as s:
        pre = s.get(NotifyOutbox, rid)
        if pre is None or (broken is not None and pre.channel_id in broken):
            return False
        if not _claim(s, rid, now):
            return False
        row = s.get(NotifyOutbox, rid)
        ch = s.get(NotifyChannel, row.channel_id) if row else None
        if row is None:
            return False
        if row.created_at and now - row.created_at > STALE_AFTER:
            row.status = ST_SKIPPED
            row.last_error = "stale: not sent late"
            s.add(row)
            s.commit()
            return False
        if ch is None or not ch.enabled:
            row.status = ST_SKIPPED
            row.last_error = "channel missing or disabled"
            s.add(row)
            s.commit()
            return False
        try:
            code = send_facts(ch, dict(row.payload or {}), f"d{row.id}")
        except NotifyError as exc:
            if exc.sent_to:
                row.payload = {**(row.payload or {}), "_sent_to": exc.sent_to}
            if broken is not None and exc.http_status is None:
                broken.add(ch.id)
            _after_failure(s, row, ch, exc)
            return False
        except Exception as exc:  # a crash in an adapter is a failed delivery, never a crash
            _after_failure(s, row, ch, NotifyError(f"unexpected {type(exc).__name__}"))
            return False
        row.status = ST_SENT
        row.http_status = code
        row.sent_at = _utcnow()
        row.attempts += 1
        row.last_error = ""
        s.add(row)
        s.commit()
        return True


def _after_failure(s: Session, row: NotifyOutbox, ch: NotifyChannel, exc: NotifyError) -> None:
    row.attempts += 1
    row.http_status = exc.http_status
    row.last_error = _clean(str(exc), 240)
    if exc.retryable and row.attempts <= len(RETRY_DELAYS):
        row.status = ST_RETRY
        row.next_attempt_at = _utcnow() + timedelta(seconds=RETRY_DELAYS[row.attempts - 1])
    else:
        row.status = ST_FAILED
        row.next_attempt_at = None
    s.add(row)
    s.commit()
    if row.status == ST_FAILED and time.time() - _FAIL_AUDIT_AT.get(ch.id, 0.0) > 3600:
        _FAIL_AUDIT_AT[ch.id] = time.time()
        audit.log(
            actor="system",
            action="notify.delivery_failed",
            target=f"notify-channel {ch.id}",
            detail=f"event={row.event} attempts={row.attempts} reason={row.last_error}",
            customer_id=ch.customer_id,
            org_id=ch.org_id,
        )


def sweep(now: datetime | None = None, limit: int = SWEEP_LIMIT) -> int:
    """Retry due rows and reclaim rows a crashed sender left in `sending`. Returns the number
    sent. Bounded: a wall-clock budget, and a channel that times out is skipped for the rest of
    the pass. Also prunes old delivered rows. Call `sweep_async` from the scheduler tick."""
    if not outbound_enabled():
        return 0
    now = now or _utcnow()
    with Session(engine) as s:
        stale = now - timedelta(seconds=STALE_SENDING_S)
        for r in s.exec(
            select(NotifyOutbox).where(
                NotifyOutbox.status == ST_SENDING, NotifyOutbox.next_attempt_at < stale
            )
        ).all():
            r.status = ST_RETRY
            s.add(r)
        s.commit()
        due = [
            r.id
            for r in s.exec(
                select(NotifyOutbox)
                .where(
                    NotifyOutbox.status.in_([ST_PENDING, ST_RETRY]),
                    (NotifyOutbox.next_attempt_at == None) | (NotifyOutbox.next_attempt_at <= now),  # noqa: E711
                )
                .order_by(NotifyOutbox.id)
                .limit(limit)
            ).all()
        ]
    sent = deliver_ids(due, deadline=time.monotonic() + SWEEP_BUDGET_S)
    prune(now)
    return sent


def sweep_async(now: datetime | None = None) -> bool:
    """Run `sweep` on a daemon thread so the scheduler tick never waits on a network send.
    One sweep at a time; returns False when the previous one is still running."""
    if _SYNC:
        sweep(now)
        return True
    if not _SWEEP_LOCK.acquire(blocking=False):
        return False

    def run():
        try:
            sweep(now)
        except Exception:
            log.exception("notification retry sweep crashed")
        finally:
            _SWEEP_LOCK.release()

    threading.Thread(target=run, daemon=True, name="notify-sweep").start()
    return True


def prune(now: datetime | None = None) -> int:
    """Delete delivered/skipped rows older than 30 days and failed rows older than 90 (hourly)."""
    if time.time() - _LAST_PRUNE[0] < 3600:
        return 0
    _LAST_PRUNE[0] = time.time()
    now = now or _utcnow()
    n = 0
    with Session(engine) as s:
        for status, days in (
            (ST_SENT, PRUNE_SENT_DAYS),
            (ST_SKIPPED, PRUNE_SENT_DAYS),
            (ST_FAILED, PRUNE_FAILED_DAYS),
        ):
            for r in s.exec(
                select(NotifyOutbox)
                .where(
                    NotifyOutbox.status == status,
                    NotifyOutbox.created_at < now - timedelta(days=days),
                )
                .limit(500)
            ).all():
                s.delete(r)
                n += 1
        s.commit()
    return n


def deliveries(
    session: Session,
    *,
    channel_id: int | None = None,
    channel_ids=None,
    project_id: int | None = None,
    org_id=None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """The delivery log, newest first. Org-scoped in SQL; `channel_ids` confines it to the
    channels the caller may see. Carries no destination and no payload text."""
    if access.org_scope_blocked(org_id):
        return []
    org_id = access.org_scope_value(org_id)
    q = select(NotifyOutbox).order_by(NotifyOutbox.id.desc())
    if channel_ids is not None:
        q = q.where(NotifyOutbox.channel_id.in_(list(channel_ids)))
    if channel_id is not None:
        q = q.where(NotifyOutbox.channel_id == channel_id)
    if project_id is not None:
        q = q.where(NotifyOutbox.project_id == project_id)
    if org_id is not None:
        clause = access.org_id_clause(NotifyOutbox.org_id, org_id)
        if clause is not None:
            q = q.where(clause)
    out = []
    for r in session.exec(q.offset(offset).limit(limit)):
        out.append(
            {
                "id": r.id,
                "channel_id": r.channel_id,
                "project_id": r.project_id,
                "event": r.event,
                "status": r.status,
                "attempts": r.attempts,
                "http_status": r.http_status,
                "error": r.last_error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
            }
        )
    return out


def retry_failed(session: Session, ch: NotifyChannel, *, actor: str = "") -> int:
    """Put a channel's failed rows back in the queue (one more round of retries)."""
    n = 0
    for r in session.exec(
        select(NotifyOutbox).where(
            NotifyOutbox.channel_id == ch.id, NotifyOutbox.status == ST_FAILED
        )
    ).all():
        r.status, r.attempts, r.next_attempt_at = ST_RETRY, 0, _utcnow()
        session.add(r)
        n += 1
    session.commit()
    audit.log(
        actor=actor or "system",
        action="notify.channel.retry",
        target=f"notify-channel {ch.id}",
        detail=f"requeued={n}",
        customer_id=ch.customer_id,
        org_id=ch.org_id,
    )
    return n


def test_send(session: Session, ch: NotifyChannel, *, actor: str = "", ip: str = "") -> dict:
    """Send a harmless test message to the channel now and record the outcome on it."""
    stamps = [x for x in _TEST_TIMES.get(ch.id, []) if time.time() - x < TEST_WINDOW_S]
    if not outbound_enabled():
        result = {
            "ok": False,
            "message": "Outbound notifications are switched off on this install.",
        }
    elif len(stamps) >= TEST_LIMIT:
        result = {"ok": False, "message": "Too many test messages; try again in an hour."}
    else:
        stamps.append(time.time())
        _TEST_TIMES[ch.id] = stamps
        facts = {
            "event": "test",
            "project_id": None,
            "project": branding.brand_name(),
            "customer": "",
            "counts": {},
            "limit": "",
            "band": None,
            "at": _utcnow().replace(microsecond=0).isoformat(),
            "url": settings.external_base_url(),
        }
        try:
            send_facts(ch, facts, "test")
            result = {"ok": True, "message": "Test message delivered."}
        except NotifyError as exc:
            result = {"ok": False, "message": _clean(str(exc), 240)}
        except Exception as exc:
            result = {"ok": False, "message": f"unexpected {type(exc).__name__}"}
    ch.last_test_at = _utcnow()
    ch.last_test_ok = bool(result["ok"])
    ch.last_test_message = result["message"]
    session.add(ch)
    session.commit()
    audit.log(
        actor=actor or "system",
        action="notify.channel.test",
        target=f"notify-channel {ch.id}",
        detail=f"ok={result['ok']}",
        ip=ip,
        customer_id=ch.customer_id,
        org_id=ch.org_id,
    )
    return result


# --- hooks the existing code calls -------------------------------------------------------------
def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def job_counts(result: dict | None) -> dict:
    """Neutral integers from a connector job result (kinds differ in what they report)."""
    r = result or {}
    failures = r.get("failures")
    return {
        "created": _int(r.get("created")),
        "updated": _int(r.get("updated")),
        "held": _int(r.get("held_back")) + _int(r.get("partial_held")),
        "failed": len(failures) if isinstance(failures, (list, tuple)) else 0,
    }


def on_connector_job(jid: int, phase: str) -> None:
    """`started` (the job just went RUNNING) or `settled` (terminal) for a connector_translate job:
    emits run.started / run.finished / run.failed / run.needs_review / run.limit_hit on the
    connection's project. An empty run (nothing written, held or failed) is silent. Never raises."""
    try:
        if not outbound_enabled():
            return
        from app.models import Job, JobStatus
        from app.services import project_budgets

        with Session(engine) as s:
            job = s.get(Job, jid)
            if job is None or job.kind != "connector_translate":
                return
            pid = project_budgets.job_project_id_safe(s, job)
            status, result = job.status, dict(job.result or {})
        if pid is None:
            return
        key = f"job{jid}"
        if phase == "started":
            notify("run.started", pid, {"run_id": key})
            return
        counts = job_counts(result)
        if status == JobStatus.FAILED:
            notify("run.failed", pid, {"run_id": key, "counts": counts})
            return
        if status != JobStatus.COMPLETED:
            return
        if result.get("ceiling_hit"):
            notify(
                "run.limit_hit", pid, {"run_id": key, "limit": "write_ceiling", "counts": counts}
            )
        if counts["held"]:
            notify("run.needs_review", pid, {"run_id": key, "counts": counts})
        if any(counts.values()):
            notify("run.finished", pid, {"run_id": key, "counts": counts})
    except Exception:
        log.exception("connector job notification failed for job %s", jid)
