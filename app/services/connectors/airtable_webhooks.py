"""Airtable official-Webhooks-API subsystem (Phase 2 LIVE) — relocated out of
store.py (Wave 3 store.py decomposition).

Keep-alive refresh, register/unregister, read-only status, and the ping-then-pull
payload consumption for an Airtable connection's OFFICIAL webhook, plus the cursor/
ping/pending-record + public-URL helpers they use. Behavior is byte-identical to the
former store.py implementation; store.py keeps thin delegators so store.<fn> stays
importable for the web/CLI/api entry points, the scheduler + job worker, and the
verify suites. All the MAC-verified inbound intake still lives in webhooks.py; this
module owns only the outbound registration + operator-triggered pull.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session

from app.db import engine
from app.models import Connection, ConnStatus
from app.services import audit
from app.services.connectors.store import (
    CONFIG_LOCK,
    _airtable_scope_target,
    _log,
    _set_status,
    build_fresh,
    dec_secret,
    dynamic_translate,
    enc_secret,
    get,
    patch_config,
)

log = logging.getLogger(__name__)

# Airtable PAT/OAuth webhooks expire 7 days after creation; refreshing (or listing
# payloads) while active extends +7 days. [CONFIRMED — research §2]. We refresh a
# comfortable margin BEFORE expiry.
_AIRTABLE_WEBHOOK_REFRESH_DAYS = 6


def refresh_webhooks(cid: int, now: datetime | None = None, transport=None) -> tuple[int, str]:
    """Keep-alive for an Airtable connection's OFFICIAL webhook (Phase 2 transport).

    DORMANT IN PHASE 1: no official webhook is registered yet, so a connection with
    no stored `airtable_webhook` config is a NO-OP (returns (0, "no webhook")). When
    Phase 2 registers a webhook and persists {id, base_id, refreshed_at}, this
    refreshes it once its age crosses the 6-day margin (before the 7-day expiry),
    updating `refreshed_at`. Wired into the scheduler tick so no new daemon is
    needed. Never raises — a keep-alive failure must not abort a scheduled pass."""

    now = now or datetime.utcnow()
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return 0, "not an airtable connection"
    cfg = conn.config if isinstance(conn.config, dict) else {}
    wh = cfg.get("airtable_webhook")
    wh = wh if isinstance(wh, dict) else {}
    wh_id = str(wh.get("id") or "").strip()
    wh_base = str(wh.get("base_id") or cfg.get("base_id") or "").strip()
    if not wh_id or not wh_base:
        return 0, "no webhook"  # Phase-1 dormant path — nothing registered
    # Only refresh once we're within the margin of expiry (or never refreshed).
    # (LOW-2, review: this short-circuit runs FIRST so a not-due tick adds no live
    # call — the health probe below is gated to the same ~6-day refresh cadence,
    # not fired unconditionally on every scheduler tick.)
    last = None
    try:
        raw = wh.get("refreshed_at")
        if raw:
            last = datetime.fromisoformat(str(raw).replace("Z", ""))
    except Exception:
        last = None
    if last is not None and (now - last) < timedelta(days=_AIRTABLE_WEBHOOK_REFRESH_DAYS):
        return 0, "not due"
    # Item #6: on the (now due) keep-alive cadence, also probe the webhook's HEALTH
    # (list-webhooks) so a watched-field-deleted / paused state is surfaced
    # proactively — the ping-then-pull path can't detect it (a disabled hook just
    # goes silent, never 404s). Best-effort + never raises; a `gone` outcome clears
    # registration and stops here (nothing left to refresh). The on-demand health
    # endpoint / CLI verb cover an immediate check between cadences.
    try:
        _h_ok, _h_msg, h_info = check_airtable_webhook_health(cid, transport=transport)
        if h_info.get("gone"):
            return 0, "webhook gone remotely; registration cleared"
    except Exception:  # noqa: S110 -- proactive health probe; the refresh below still runs
        pass
    try:
        at = build_fresh(cid, transport)
        ok, gone, exp, msg = at.refresh_webhook(wh_base, wh_id)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"
    if gone:
        # 404 — the webhook was deleted/expired remotely. CLEAR the stored
        # registration cleanly (config + vaulted mac secret) and surface a WARNING,
        # not an ERROR: the connection is simply back to "no live webhook". A
        # human re-registers from the connection page when they want push again.
        _clear_airtable_webhook_state(cid)
        _set_status(
            cid,
            ConnStatus.CONNECTED,
            "Airtable webhook expired/removed remotely; registration cleared. "
            "Re-register from the connection page to resume push.",
            synced=False,
        )
        return 0, msg
    if ok:
        new_wh = dict(wh)
        new_wh["refreshed_at"] = now.isoformat()
        if exp:
            new_wh["expiration_time"] = exp
        patch_config(cid, {"airtable_webhook": new_wh})
        return 1, msg
    # A non-404 refresh failure (transient 5xx, network): surface as a WARNING on
    # the connection, keep the registration (a later tick retries). Never ERROR —
    # the webhook may still be alive; a flaky refresh shouldn't red-flag the card.
    _set_status(cid, ConnStatus.CONNECTED, f"Airtable webhook keep-alive: {msg}", synced=False)
    return 0, msg


def _clear_airtable_webhook_state(cid: int) -> None:
    """Remove the stored `airtable_webhook` config AND the vaulted `webhook_secret`
    (the Airtable-minted MAC secret) for a connection — used on unregister and on a
    404-cleanup. The mac secret is cleared so a stale secret can't verify a spoofed
    ping after the webhook is gone; config is cleared so status shows 'no webhook'
    and the keep-alive goes dormant again."""
    patch_config(cid, remove=("airtable_webhook",))
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        sec = dec_secret(conn.secret_enc)
        if "webhook_secret" in sec:
            sec.pop("webhook_secret", None)
            conn.secret_enc = enc_secret(sec)
            s.add(conn)
            s.commit()


# --- official Webhooks API registration + ping-consumption (Phase 2 LIVE) --------
# THE PUBLIC-URL WALL [CONFIRMED — research §6, plan §3]: an Airtable-hosted webhook
# notification originates from Airtable's cloud, so the notificationUrl MUST be a
# publicly-reachable HTTPS host. localhost / 127.0.0.1 is unreachable from their
# servers → registration is REFUSED with an actionable error naming the setting +
# the tunnel requirement. Point `app_base_url` at your tunnel URL (for example cloudflared).
def _truthy(v) -> bool:
    """Parse a config flag value ('1'/'true'/'yes'/'on', case-insensitive) to bool.
    A plain non-empty string like '0' is FALSE here (unlike Python's bool('0')),
    which is what a user typing 0 into a text field expects."""
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _public_base_url() -> tuple[str, str]:
    """Return (base_url, error). base_url is the app_base_url setting, trimmed of a
    trailing slash, ONLY when it is a usable public HTTPS host; error is a clear,
    actionable message otherwise (and base_url is "").

    Refuses: unset, http:// (not https), any IP-literal host that is private/
    link-local/loopback/reserved per `ipaddress` (F4 fix, review — the old
    string-prefix allowlist missed 172.17-31.x [only 172.16.x was checked],
    169.254.x/16 link-local [incl. the AWS/GCP/Azure metadata endpoint
    169.254.169.254], and the shared CGNAT range 100.64.0.0/10), and any
    hostname ending .internal or .local (both are documented "never route to
    the public internet" TLD/suffix conventions, so no tunnel of Airtable's
    cloud can ever reach them). This is the ONE gate every registration surface
    (web/CLI/API) funnels through so the message is identical everywhere."""
    import ipaddress
    from urllib.parse import urlparse

    from app.services import settings as _settings

    raw = (_settings.get("app_base_url") or "").strip().rstrip("/")
    if not raw:
        return "", (
            "Set the 'app_base_url' setting (Settings -> Security) to your "
            "public HTTPS URL first -- Airtable's servers cannot reach a "
            "localhost address. Run a tunnel (cloudflared/ngrok) or deploy "
            "to a public host and paste its https URL there."
        )
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return "", (
            f"app_base_url must be HTTPS for Airtable webhooks (got {raw!r}). "
            "Airtable requires a globally-trusted https endpoint."
        )
    is_private_host = False
    try:
        # bracket-stripped by urlparse already for IPv6; ipaddress rejects a
        # non-IP-literal hostname (e.g. "tms.example.com") with a ValueError,
        # which we treat as "not an IP literal" rather than a private-range hit.
        ip = ipaddress.ip_address(host)
        # NOTE: ip.is_private does NOT flag the 100.64.0.0/10 shared/CGNAT range
        # (verified on 3.13 — is_private=False, is_global=False for it), so it is
        # checked explicitly rather than trusted to fall out of is_private.
        is_private_host = (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip in ipaddress.ip_network("100.64.0.0/10")
        )
    except ValueError:
        is_private_host = (
            host in ("localhost", "0.0.0.0")  # noqa: S104 -- host comparison, not a bind
            or host.endswith(".local")
            or host.endswith(".internal")
        )
    if is_private_host:
        return "", (
            f"app_base_url {raw!r} is a local/private address Airtable's "
            "cloud cannot reach. Set it to a public tunnel or https URL "
            "(cloudflared/ngrok) before registering the webhook."
        )
    return raw, ""


def _notification_url(base_url: str, cid: int) -> str:
    """The generalized-intake URL Airtable POSTs pings to for this connection —
    the SAME endpoint every connector's webhook plugs into, so the vaulted
    webhook_secret + MAC verifier already handle it."""
    return f"{base_url.rstrip('/')}/webhooks/connectors/{cid}"


def register_airtable_webhook(
    cid: int,
    base_id: str | None = None,
    table: str | None = None,
    source_field: str | None = None,
    actor: str = "",
    transport=None,
) -> tuple[bool, str]:
    """Register an OFFICIAL Airtable webhook for a connection (Phase 2 live path).

    Steps: guard a public URL → resolve table_id + source_field_id via the meta API
    → build the spec (webhook_spec: fromSources excluding publicApi + watch the
    source field) → POST create → VAULT the returned macSecretBase64 as the
    connection's `webhook_secret` (so the existing intake MAC verifier picks it up,
    exactly like sibling connectors) → persist {id, base_id, table, source_field_id,
    notification_url, expiration_time, refreshed_at, cursor:0} in the `airtable_webhook`
    config via patch_config. Idempotent-ish: an existing registration is DELETED
    first (best-effort) so re-register never orphans a webhook.

    Returns (ok, message). Never raises — the surface shows the message."""
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "No such Airtable connection."
    base_url, url_err = _public_base_url()
    if url_err:
        return False, url_err
    cfg = conn.config or {}
    # Security review 2026-10 (connectors-02): an override must match the
    # connection's configured base/table, so the stored credential can never set
    # a watch on (and vault a MAC secret for) a base outside the connection's scope.
    base, tbl, scope_err = _airtable_scope_target(cfg, base_id, table)
    if scope_err:
        _log(cid, "webhook", False, 0, f"register refused: {scope_err}", actor)
        return False, scope_err
    src_field = (source_field or cfg.get("source_field") or "").strip()
    if not base or not tbl or not src_field:
        return False, (
            "base_id, table, and source_field must be set (on the "
            "connection or this call) to register a webhook."
        )
    try:
        at = build_fresh(cid, transport)
        tbl_id, src_field_id, _tbl_name = at.resolve_ids(base, tbl, src_field)
    except Exception as e:
        return False, f"Could not resolve table/field ids: {type(e).__name__}: {e}"

    # If a webhook is already stored, tear it down first so we never orphan one.
    existing = cfg.get("airtable_webhook")
    if isinstance(existing, dict) and existing.get("id"):
        try:
            at.delete_webhook(existing.get("base_id") or base, existing["id"])
        except Exception:
            # A failed teardown can orphan a live remote webhook still pushing pings.
            log.warning(
                "airtable connection %s: could not delete prior webhook %s before re-register",
                cid,
                existing.get("id"),
                exc_info=True,
            )

    from app.services.connectors.airtable import webhook_spec

    spec = webhook_spec(tbl_id, [src_field_id])
    notify = _notification_url(base_url, cid)
    try:
        ok, data, msg = at.create_webhook(base, notify, spec)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if not ok:
        _log(cid, "webhook", False, 0, f"register failed: {msg}", actor)
        return False, msg

    # VAULT the mac secret exactly as sibling connectors store webhook_secret, so
    # the generalized-intake verifier authenticates pings with zero extra wiring.
    with CONFIG_LOCK, Session(engine) as s:
        row = s.get(Connection, cid)
        if row:
            sec = dec_secret(row.secret_enc)
            sec["webhook_secret"] = data.get("macSecretBase64") or ""
            row.secret_enc = enc_secret(sec)
            s.add(row)
            s.commit()
    now = datetime.utcnow()
    patch_config(
        cid,
        {
            "airtable_webhook": {
                "id": data.get("id"),
                "base_id": base,
                "table": tbl,
                "table_id": tbl_id,
                "source_field_id": src_field_id,
                "notification_url": notify,
                "expiration_time": data.get("expirationTime") or "",
                "refreshed_at": now.isoformat(),
                "cursor": 0,
                "registered_at": now.isoformat(),
                "last_error": "",
            }
        },
    )
    _set_status(
        cid, ConnStatus.CONNECTED, f"Airtable webhook registered ({data.get('id')}).", synced=False
    )
    _log(cid, "webhook", True, 0, f"registered webhook {data.get('id')}", actor)
    audit.log(
        actor=actor or "system",
        action="connector.airtable_webhook.register",
        target=f"connection:{cid}",
        detail=f"webhook:{data.get('id')} url:{notify}",
        customer_id=getattr(conn, "customer_id", None),
    )
    return True, f"Registered Airtable webhook {data.get('id')} (notifies {notify})."


def unregister_airtable_webhook(cid: int, actor: str = "", transport=None) -> tuple[bool, str]:
    """Delete a connection's official Airtable webhook + clear its stored state
    (config + vaulted mac secret). A remote 404 is treated as success (goal state
    is "no webhook"). Never raises."""
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "No such Airtable connection."
    cfg = conn.config or {}
    wh = cfg.get("airtable_webhook")
    wh = wh if isinstance(wh, dict) else {}
    wh_id = str(wh.get("id") or "").strip()
    wh_base = str(wh.get("base_id") or cfg.get("base_id") or "").strip()
    if not wh_id:
        # Nothing registered locally — still clear any residual state defensively.
        _clear_airtable_webhook_state(cid)
        return True, "No webhook was registered."
    ok, msg = True, "Deleted."
    try:
        at = build_fresh(cid, transport)
        ok, msg = at.delete_webhook(wh_base, wh_id)
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"
    if ok:
        _clear_airtable_webhook_state(cid)
        _log(cid, "webhook", True, 0, f"unregistered webhook {wh_id}", actor)
        audit.log(
            actor=actor or "system",
            action="connector.airtable_webhook.unregister",
            target=f"connection:{cid}",
            detail=f"webhook:{wh_id}",
            customer_id=getattr(conn, "customer_id", None),
        )
        return True, f"Unregistered Airtable webhook {wh_id}."
    return False, msg


def airtable_webhook_status(cid: int) -> dict:
    """Read-only view of a connection's official-webhook state for the connection
    page / CLI status verb. Pure config read — no network. Returns a dict with
    registered/id/expiration/last_ping/pending/auto flags."""
    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return {"registered": False}
    cfg = conn.config or {}
    wh = cfg.get("airtable_webhook")
    wh = wh if isinstance(wh, dict) else {}
    return {
        "registered": bool(wh.get("id")),
        "id": wh.get("id") or "",
        "notification_url": wh.get("notification_url") or "",
        "expiration_time": wh.get("expiration_time") or "",
        "refreshed_at": wh.get("refreshed_at") or "",
        "cursor": int(wh.get("cursor") or 0),
        "last_ping_at": wh.get("last_ping_at") or "",
        "last_error": wh.get("last_error") or "",
        # Item #6 (watched-field-deleted): a persisted disabled/error state from the
        # last health check so the connection page can show "STOPPED" instead of
        # silent death. `disabled` is the hard "Airtable turned the hook off" flag;
        # `error_state` is the human reason (also mirrored into last_error).
        "disabled": bool(wh.get("disabled")),
        "error_state": wh.get("error_state") or "",
        "auto_translate_on_change": _truthy(cfg.get("auto_translate_on_change")),
        "pending_record_ids": list(cfg.get("airtable_pending_records") or []),
        "app_base_url_ok": bool(_public_base_url()[0]),
    }


def check_airtable_webhook_health(cid: int, transport=None) -> tuple[bool, str, dict]:
    """Detect + surface a webhook error state (round-2 #6). A DELETED watched field
    makes Airtable DISABLE the webhook: it stops generating payloads but is NOT
    404'd, so the ping-then-pull path only sees silence. The list-webhooks endpoint
    surfaces the state (`isHookEnabled` goes false), which webhook_error_state()
    turns into a human reason.

    Persists the outcome onto the stored `airtable_webhook` config
    (disabled/error_state/last_error) so the connection page + status show it, and
    sets a WARNING status (never a hard ERROR — the connection itself is fine; only
    its push transport stopped). Returns (ok, message, info):
      * ok=False + info['gone']=True  -> the webhook is gone remotely (list no longer
        contains it); registration is cleared cleanly (same as the 404 path).
      * ok=True  + info['error_state'] -> a reason string (disabled/paused).
      * ok=True  + info['error_state']=='' -> healthy; any prior error is cleared.
    Never raises — a health probe must not crash a scheduled pass."""
    from app.services.connectors.airtable import webhook_error_state

    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "not an airtable connection", {}
    cfg = conn.config if isinstance(conn.config, dict) else {}
    wh = cfg.get("airtable_webhook")
    wh = wh if isinstance(wh, dict) else {}
    wh_id = str(wh.get("id") or "").strip()
    wh_base = str(wh.get("base_id") or cfg.get("base_id") or "").strip()
    if not wh_id or not wh_base:
        return True, "no webhook", {"registered": False}
    try:
        at = build_fresh(cid, transport)
        ok, hooks, msg = at.list_webhooks(wh_base)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", {}
    if not ok:
        # transient (network/5xx): don't touch stored state, just report
        return False, msg, {"transient": True}
    mine = next((h for h in hooks if h.get("id") == wh_id), None)
    if mine is None:
        # The webhook no longer exists remotely (deleted/expired) — clear cleanly,
        # exactly like the 404 path elsewhere, so a human re-registers when ready.
        _clear_airtable_webhook_state(cid)
        _set_status(
            cid,
            ConnStatus.CONNECTED,
            "Airtable webhook no longer exists remotely; registration cleared. "
            "Re-register from the connection page to resume push.",
            synced=False,
        )
        return False, "Webhook not found in Airtable; registration cleared.", {"gone": True}
    state = webhook_error_state(mine)
    _persist_webhook_health(cid, disabled=bool(state), error_state=state or "")
    if state:
        _set_status(cid, ConnStatus.CONNECTED, f"Airtable webhook: {state}"[:300], synced=False)
        return True, state, {"error_state": state, "disabled": True}
    return True, "ok", {"error_state": "", "disabled": False}


def _persist_webhook_health(cid: int, disabled: bool, error_state: str) -> None:
    """Write disabled/error_state onto the stored webhook (and mirror error_state into
    last_error so the existing 'Webhook warning' surfaces it too). When healthy,
    clears disabled/error_state and clears last_error ONLY when it still equals the
    error-state message we ourselves set (so a transient last_error from another code
    path is never stomped). Under the config lock; a missing webhook is a no-op."""
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        cur = dict(cfg.get("airtable_webhook") or {})
        if not cur:
            return
        prev_state = cur.get("error_state") or ""
        cur["disabled"] = bool(disabled)
        cur["error_state"] = (error_state or "")[:300]
        if error_state:
            cur["last_error"] = error_state[:300]
        elif cur.get("last_error") and cur.get("last_error") == prev_state:
            cur["last_error"] = ""  # heal: clear only OUR own prior error-state text
        cfg["airtable_webhook"] = cur
        conn.config = cfg
        s.add(conn)
        s.commit()


def consume_airtable_payloads(
    cid: int, actor: str = "", transport=None, force: bool = False
) -> tuple[bool, str, dict]:
    """The ping-consumption job body (Phase 2 ping-then-pull).

    An Airtable ping is THIN (base+webhook ids only). This function does the PULL
    side: it fetches payloads via `list payloads` from the STORED CURSOR, extracts
    the record ids whose SOURCE field changed (via the V2 parser +
    webhook_source_changed_record_ids, filtering out target-column echoes), advances
    the cursor ONLY after a successful page, and then EITHER:
      * auto_translate_on_change ON (or force=True, the "Translate pending now"
        action) → runs the existing targeted dynamic_translate for exactly those
        records, OR
      * auto_translate_on_change OFF (default) → records the changed ids in
        `airtable_pending_records` config + a "changes pending" status, translating
        NOTHING. The connection page / CLI shows the pending set and a
        "Translate pending now" button that calls this with force=True.

    Cursor discipline: the cursor advances after EACH successfully-fetched page and
    is merge-persisted, so a crash mid-run re-reads only the unprocessed tail (at
    least once). A 404 (webhook gone) clears the registration cleanly. Respects the
    5 rps/base limit via the connector's existing request backoff; loops while
    `mightHaveMore`. Returns (ok, message, summary).

    F2 fix (review): the AUTO/forced branch used to clear ALL of `changed` from
    `airtable_pending_records` whenever dynamic_translate returned ok=True — but
    ok only means "at least one record wrote"; a partial failure (a record in
    summary["failures"]) was silently dropped from the queue and would only
    recover on a LATER, unrelated source edit re-adding it. Now only records that
    neither failed write-back NOR were capped out by F1's per-run write ceiling
    (summary["capped_record_ids"]) are cleared — a failed or capped-out record
    stays pending so the next ping / "Translate pending now" retries it."""
    from app.services.connectors.airtable import (
        payloads_might_have_more,
        payloads_next_cursor,
        webhook_source_changed_record_ids,
    )

    conn = get(cid)
    if not conn or conn.kind != "airtable":
        return False, "No such Airtable connection.", {}
    cfg = conn.config or {}
    wh = cfg.get("airtable_webhook")
    wh = wh if isinstance(wh, dict) else {}
    wh_id = str(wh.get("id") or "").strip()
    wh_base = str(wh.get("base_id") or cfg.get("base_id") or "").strip()
    if not wh_id or not wh_base:
        return False, "No official Airtable webhook is registered for this connection.", {}
    src_field_id = str(wh.get("source_field_id") or "").strip()
    src_field_ids = {src_field_id} if src_field_id else set()

    at = build_fresh(cid, transport)
    cursor = int(wh.get("cursor") or 0) or None  # None reads from the start
    changed: list[str] = []
    pages = 0
    _MAX_PAGES = 50  # bound the poll so a runaway/huge backlog can't loop forever
    while pages < _MAX_PAGES:
        ok, data, msg = at.list_payloads(wh_base, wh_id, cursor=cursor)
        if not ok:
            if data.get("gone"):
                _clear_airtable_webhook_state(cid)
                _set_status(
                    cid,
                    ConnStatus.CONNECTED,
                    "Airtable webhook not found (404); registration cleared.",
                    synced=False,
                )
                return False, "Webhook not found (404); registration cleared.", {"gone": True}
            # transient failure: surface as warning, keep cursor, retry next ping
            _record_webhook_error(cid, wh, msg)
            return False, msg, {"pages": pages}
        pages += 1
        for rid in webhook_source_changed_record_ids(data, src_field_ids):
            if rid not in changed:
                changed.append(rid)
        nxt = payloads_next_cursor(data)
        if nxt is not None:
            cursor = nxt
            # advance the stored cursor after EACH successful page (at-least-once)
            _advance_airtable_cursor(cid, cursor)
        if not payloads_might_have_more(data):
            break

    now_iso = datetime.utcnow().isoformat()
    auto = _truthy(cfg.get("auto_translate_on_change")) or force
    # FORCE (the "Translate pending now" action) also drains the ALREADY-pending
    # queue — records recorded by earlier auto-off pings — not just whatever this
    # pull turned up. Union them in (order-preserving) so a manual drain translates
    # the full backlog even when this pull found nothing new. Re-read pending from
    # the freshest config (a burst of pings may have accumulated more since load).
    if force:
        fresh = (get(cid).config or {}) if get(cid) else cfg
        for rid in fresh.get("airtable_pending_records") or []:
            if rid not in changed:
                changed.append(rid)
    if not changed:
        _touch_webhook_ping(cid, wh, now_iso, error="")
        return (
            True,
            "No source-field changes to translate.",
            {"pages": pages, "changed": 0, "translated": 0, "auto": auto},
        )

    if not auto:
        # OFF (default): record pending changes, translate NOTHING. Merge with any
        # already-pending ids so a burst of pings between manual runs accumulates.
        _add_pending_records(cid, changed)
        _touch_webhook_ping(cid, wh, now_iso, error="")
        _set_status(
            cid,
            ConnStatus.CONNECTED,
            f"{len(changed)} record(s) changed — pending translation "
            "(auto-translate is off; use 'Translate pending now').",
            synced=False,
        )
        _log(cid, "webhook", True, 0, f"{len(changed)} pending (auto off)", actor)
        return (
            True,
            (f"{len(changed)} changed record(s) recorded as pending (auto-translate off)."),
            {
                "pages": pages,
                "changed": len(changed),
                "translated": 0,
                "pending": True,
                "auto": False,
                "record_ids": changed,
            },
        )

    # AUTO / forced: translate exactly the changed records via the existing targeted
    # path. The written-hash loop-guard inside dynamic_translate backstops any echo
    # that slipped past the fromSources + field-id filters.
    ok, tmsg, summary = dynamic_translate(
        cid,
        base_id=wh_base,
        table=wh.get("table") or None,
        record_ids=changed,
        actor=actor or "webhook",
        transport=transport,
    )
    # F2 fix (review): previously this cleared ALL of `changed` from pending on
    # ok=True (ok just means "at least one record wrote") — a partial failure
    # (summary["failures"]) was silently dropped from the queue and only ever
    # recovered on a LATER, unrelated source edit. Now only remove ids that
    # neither (a) failed write-back / produced no translation (summary
    # ["failures"]), NOR (b) were capped out by F1's per-run ceiling before a
    # unit was even built (summary["capped_record_ids"]) — both stay pending so
    # a subsequent "Translate pending now" / ping picks them straight back up.
    summary = dict(summary or {})
    failed_ids = {f.get("record_id") for f in (summary.get("failures") or [])}
    capped_ids = set(summary.get("capped_record_ids") or [])
    retained = failed_ids | capped_ids
    # Issue #445: a run that failed OUTRIGHT (scope mismatch, disabled kind, mock-egress
    # block, schema/fetch error, dead engine) returns no per-record outcome, so both sets
    # are empty; an archived project is skipped with ok=True and nothing translated. In
    # either case NO record was handled, so every changed id must stay queued, never be
    # treated as done.
    whole_run_unhandled = (not ok and not retained) or bool(summary.get("skipped_archived"))
    if whole_run_unhandled:
        retained = set(changed)
    # In auto mode the ids came straight off the cursor (already advanced) and were never
    # recorded as pending, so a retained id must be (re)queued, not merely "not removed".
    if retained:
        _add_pending_records(cid, [rid for rid in changed if rid in retained])
    to_clear = [rid for rid in changed if rid not in retained]
    if to_clear:
        _remove_pending_records(cid, to_clear)
    _touch_webhook_ping(cid, wh, now_iso, error="" if ok else tmsg)
    summary.update({"pages": pages, "changed": len(changed), "auto": True})
    return ok, tmsg, summary


def _advance_airtable_cursor(cid: int, cursor: int) -> None:
    """Merge-persist the webhook cursor (never regress): re-read airtable_webhook
    under the config lock and set cursor = max(stored, cursor). Commutative with a
    concurrent update, mirroring patch_config_merge's watermark discipline."""
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        wh = dict(cfg.get("airtable_webhook") or {})
        if not wh:
            return
        wh["cursor"] = max(int(wh.get("cursor") or 0), int(cursor))
        cfg["airtable_webhook"] = wh
        conn.config = cfg
        s.add(conn)
        s.commit()


def _touch_webhook_ping(cid: int, wh: dict, at_iso: str, error: str = "") -> None:
    """Record last_ping_at + last_error on the stored webhook (status display)."""
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        cur = dict(cfg.get("airtable_webhook") or {})
        if not cur:
            return
        cur["last_ping_at"] = at_iso
        cur["last_error"] = (error or "")[:300]
        cfg["airtable_webhook"] = cur
        conn.config = cfg
        s.add(conn)
        s.commit()


def _record_webhook_error(cid: int, wh: dict, error: str) -> None:
    """Surface a webhook error on the connection (warning, not ERROR) + store it for
    the status panel. A webhook in error state (e.g. a watched field deleted) must
    be VISIBLE, per research §2, not silently swallowed."""
    _touch_webhook_ping(cid, wh, datetime.utcnow().isoformat(), error=error)
    _set_status(cid, ConnStatus.CONNECTED, f"Airtable webhook: {error}"[:300], synced=False)


def _add_pending_records(cid: int, record_ids: list[str]) -> None:
    """Union record_ids into `airtable_pending_records` (bounded) under the lock."""
    _PENDING_CAP = 1000
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        cur = list(cfg.get("airtable_pending_records") or [])
        seen = set(cur)
        for rid in record_ids:
            if rid not in seen:
                cur.append(rid)
                seen.add(rid)
        cfg["airtable_pending_records"] = cur[-_PENDING_CAP:]
        conn.config = cfg
        s.add(conn)
        s.commit()


def _remove_pending_records(cid: int, record_ids: list[str]) -> None:
    """Drop record_ids from `airtable_pending_records` under the lock."""
    drop = set(record_ids)
    with CONFIG_LOCK, Session(engine) as s:
        conn = s.get(Connection, cid)
        if not conn:
            return
        cfg = dict(conn.config or {})
        cur = [r for r in (cfg.get("airtable_pending_records") or []) if r not in drop]
        cfg["airtable_pending_records"] = cur
        conn.config = cfg
        s.add(conn)
        s.commit()
