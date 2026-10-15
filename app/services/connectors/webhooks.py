"""Generalized connector webhook intake (CONNECTOR_FRAMEWORK_DESIGN §3.4).

ONE endpoint — POST /webhooks/connectors/{connection_id} (wired in web/api.py) —
that any connector plugs into, with:

  * per-connector PLUGGABLE signature verification (a verifier keyed by connector
    kind; Zendesk plugs in first),
  * idempotency-key / event-id DEDUP (a WebhookEvent ledger row per accepted
    event; a provider RETRY ACKs 200 but enqueues nothing),
  * FAST-ACK-then-enqueue: the handler verifies + dedups + enqueues a Job and
    returns immediately. The actual re-fetch/translate/write-back runs async on
    the job worker. The design constraint is Intercom's "<5s ACK, single retry"
    rule (§3.4) — we must not do network I/O to the source system inline.

SCOPE / ISOLATION (brief requirement): the {connection_id} in the URL resolves the
connection, and the connection carries the org. So intake is org-scoped by
construction — a caller cannot reach another org's connection by guessing an id
that isn't theirs, because the per-connection webhook SECRET (vaulted) must verify
first. Safe when the login wall is OFF: this route is unauthenticated by design
(providers can't carry our session/api creds) but the signature IS the auth.

WHY THIN PAYLOADS ARE NEVER TRUSTED: Zendesk/Intercom article events carry id +
metadata only (no body) — ChangeSignals.thin_payload. The enqueued job re-fetches
the article via the connector before translating; the payload's content, if any,
is ignored. This module only records "record X on connection Y changed"; it never
carries content into the translate path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime

from sqlmodel import Session

from app.db import engine
from app.models import Connection, WebhookEvent
from app.services import access

log = logging.getLogger("loc_tms.connectors.webhooks")

# Hard cap on the number of changed record ids one delivery may carry into the job
# payload/logs (D5 audit). Enforced DURING extraction (early break + set-based O(1)
# dedup) so a signed-but-hostile 256KB body can't make us parse+regex+log tens of
# thousands of candidates before truncating. The connector's own fetch caps again;
# this bound is intake-side and never depends on that alone. Raw-body size is bounded
# separately, BEFORE signature verification, by config.WEBHOOK_MAX_BODY_BYTES (the
# route's streaming 413 guard) — so this only backstops the id COUNT, not body bytes.
_MAX_CHANGED_IDS = 100


# --- pluggable signature verification -------------------------------------------
# A verifier: (secret: str, body: bytes, headers: dict) -> bool. Registered per
# connector kind. Absent kind -> no webhook support -> intake rejects (404-ish).
_VERIFIERS: dict[str, callable] = {}
# Per-kind extractor of the provider event id from (body_json, headers) -> str|None.
_EVENT_ID_EXTRACTORS: dict[str, callable] = {}


def register_verifier(kind: str, verify, event_id=None) -> None:
    """Register a connector kind's signature verifier (+ optional event-id
    extractor). Called at connector-module import time."""
    _VERIFIERS[kind] = verify
    if event_id is not None:
        _EVENT_ID_EXTRACTORS[kind] = event_id


def supports_webhook(kind: str) -> bool:
    return kind in _VERIFIERS


def verify_signature(kind: str, secret: str, body: bytes, headers: dict) -> bool:
    """True when the connector kind's verifier accepts the signed body. No
    verifier registered -> False (fail closed). A blank secret -> False (an
    unconfigured connection can't accept webhooks; you must set one first)."""
    verifier = _VERIFIERS.get(kind)
    if verifier is None or not (secret or "").strip():
        return False
    try:
        return bool(verifier(secret, body, headers))
    except Exception:
        log.exception("webhook verifier for %s crashed; rejecting", kind)
        return False


def hmac_sha256_verifier(header_name: str, *, prefix: str = "", hex_digest: bool = True):
    """Factory for HMAC-SHA256 over the supplied bytes (body, or timestamp + body).
    `header_name` is compared case-insensitively; `prefix` is a
    leading string to strip (e.g. 'sha256='). Constant-time compared."""
    import base64

    hn = header_name.lower()

    def _verify(secret: str, body: bytes, headers: dict) -> bool:
        provided = ""
        for k, v in (headers or {}).items():
            if k.lower() == hn:
                provided = (v or "").strip()
                break
        if not provided:
            return False
        if prefix and provided.startswith(prefix):
            provided = provided[len(prefix) :]
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
        expected = digest.hexdigest() if hex_digest else base64.b64encode(digest.digest()).decode()
        return hmac.compare_digest(provided, expected)

    return _verify


# --- event id / dedup key -------------------------------------------------------
def dedup_key(kind: str, body: bytes, body_json, headers: dict) -> str:
    """A stable event id for the (connection, event) dedup. Prefers the provider's
    own event id via the registered extractor; falls back to a SHA-256 of the raw
    body so a provider that sends no id still dedups an identical retry. Truncated
    to keep the index tight."""
    extractor = _EVENT_ID_EXTRACTORS.get(kind)
    if extractor is not None:
        try:
            # Delivery headers are not covered by our providers' body MACs.
            # Only signed payload fields may supply a provider event identity.
            eid = extractor(body_json, {})
            if eid:
                return str(eid)[:200]
        except Exception:
            log.exception("event-id extractor for %s crashed; hashing body", kind)
    return "sha256:" + hashlib.sha256(body or b"").hexdigest()[:56]


# --- STRONG vs WEAK dedup keys (regression fix) ---------------------------------
# A STRONG key is a signed payload identity: an (issue, changelog-id),
# or an Airtable event identity from the body, anything the
# registered extractor returned. Those carry their OWN uniqueness, so two genuinely
# distinct edits get distinct keys and a true retry reproduces the same one.
#
# A WEAK key is the "sha256:<body>" FALLBACK dedup_key returns when NO provider
# identity was available (the maintainer's primary path: Jira Automation "Send web request"
# with a thin body like {"issue_key":"LOC-1"} + X-Loc-Webhook-Secret, no changelog,
# no Atlassian identifier). A weak key is a pure function of the body with NO time
# component, so EVERY later genuine re-edit of the same issue produces a byte-
# identical body -> the same weak key. On the no-job button-trigger path that would
# make a settled prior suppress every future real edit forever (silent data loss);
# record_event therefore refuses to let a settled WEAK-keyed no-job event dedup.
def is_weak_key(event_id: str) -> bool:
    """True when event_id is the body-hash FALLBACK (see dedup_key), i.e. no
    provider-supplied identity was available. Strong (provider-identity) keys do
    not carry this prefix."""
    return (event_id or "").startswith("sha256:")


def record_event(connection_id: int, kind: str, event_id: str) -> bool:
    """Insert a WebhookEvent row for (connection, event_id). Returns True when the
    caller should PROCESS this event, False when it's a settled DUPLICATE (ACK but
    do nothing). The caller MUST call settle_event() once it has finished accepting
    the event (whether or not a job was enqueued) so a later replay dedups.

    The (connection_id, event_id) UNIQUE index makes this race-safe: two
    concurrent deliveries of the same event both try to INSERT; exactly one wins,
    the other hits IntegrityError.

    CRASH-WINDOW RECOVERY (review finding): the ledger row commits BEFORE the
    event is settled, so a crash between record_event and settle_event would
    otherwise mark the event accepted while nothing happened — the provider's
    retry would then be deduped and the event lost forever. So a duplicate whose
    prior row is NOT yet settled (processed_at is NULL) is treated as retryable:
    return True so the retry re-processes.

    D4 (audit): for a STRONG (provider-identity) key, dedup keys on processed_at,
    NOT on job_id. The default Jira button-trigger posture records PENDING and
    enqueues NO job, so job_id stays NULL for a fully-accepted event; keying on
    job_id let every replay of that captured, validly-signed POST re-accept as fresh
    (unbounded replay). A settled STRONG-keyed event (processed_at set) is always a
    duplicate regardless of job_id.

    WEAK-key regression (adversarial review, live repro): the "sha256:<body>"
    fallback key (is_weak_key) is a pure function of the body with no time
    component, so a genuine LATER re-edit of the same record reproduces the exact
    same key. On the NO-JOB button-trigger path (prior.job_id IS NULL) a settled
    prior must therefore NOT suppress a new delivery — else the FIRST edit settles
    the row and every real re-edit is silently dropped forever. So a settled WEAK
    prior with job_id NULL is RE-ACCEPTED (return True): the downstream side effect
    is jira_record_pending, an idempotent set-merge, and jira_translate's content-
    hash suppression makes a human re-release of unchanged content a no-op, so
    replay harm is bounded while a real re-edit is never lost. A WEAK key that DID
    produce a job (auto-translate path, prior.job_id set) KEEPS dedup — that matches
    the pre-fix job_id-based semantics, so it is not a regression. KNOWN LIMITATION:
    an identical-thin-body re-edit in AUTO mode relies on jira_translate's
    downstream content-hash suppression; thin-body senders that want precise
    per-edit identity in auto mode should include a changing field (e.g.
    {{issue.fields.updated}}) in the signed body. Unsigned headers are ignored.

    Residual: two near-simultaneous deliveries of the same event can both see the
    prior unsettled and both process — provider retries arrive seconds+ apart in
    practice, and the job is guarded by content-hash suppression, so we accept that
    tiny window over losing events."""
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import select as _select

    with Session(engine) as s:
        row = WebhookEvent(connection_id=connection_id, event_id=event_id, kind=kind)
        s.add(row)
        try:
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            prior = s.exec(
                _select(WebhookEvent).where(
                    WebhookEvent.connection_id == connection_id, WebhookEvent.event_id == event_id
                )
            ).first()
            if prior is None:
                return True
            if prior.processed_at is None:
                # Crash-window recovery: an unsettled prior is retryable.
                return True
            # prior is SETTLED. A STRONG key is a true duplicate. A WEAK (body-hash)
            # key on the NO-JOB button-trigger path (job_id NULL) reproduces on every
            # genuine re-edit, so re-accept it rather than lose the edit forever.
            if is_weak_key(event_id) and prior.job_id is None:  # noqa: SIM103
                return True
            return False


def settle_event(connection_id: int, event_id: str, job_id: int | None = None) -> None:
    """Mark an accepted event SETTLED so a later replay of the same signed delivery
    is deduped (D4 audit). Called at EVERY terminal accept in handle_intake, whether
    or not a job was enqueued — the default Jira button-trigger posture records
    PENDING and enqueues NO job, so settlement (not job_id) is what makes a replay a
    duplicate. Records job_id too when a job resulted (diagnostics). A crash BEFORE
    this runs leaves processed_at NULL, so the provider's retry stays retryable."""
    from sqlmodel import select

    with Session(engine) as s:
        row = s.exec(
            select(WebhookEvent).where(
                WebhookEvent.connection_id == connection_id, WebhookEvent.event_id == event_id
            )
        ).first()
        if row:
            row.processed_at = datetime.utcnow()
            if job_id is not None:
                row.job_id = job_id
            s.add(row)
            s.commit()


# --- the intake --------------------------------------------------------------
class IntakeResult:
    """Outcome of handle_intake, mapped to an HTTP status by the route."""

    def __init__(
        self,
        status: int,
        ok: bool,
        message: str,
        enqueued: bool = False,
        job_id: int | None = None,
        record_ids=None,
    ):
        self.status = status
        self.ok = ok
        self.message = message
        self.enqueued = enqueued
        self.job_id = job_id
        self.record_ids = record_ids or []

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message": self.message,
            "enqueued": self.enqueued,
            "job_id": self.job_id,
            "record_ids": self.record_ids,
        }


def _webhook_secret(conn: Connection) -> str:
    """The per-connection webhook signing secret, from the vaulted credentials.
    Stored in secret_enc under `webhook_secret` (never in plaintext config)."""
    from app.services.connectors import store as conn_store  # lazy: avoid import cycle

    return (conn_store.dec_secret(conn.secret_enc) or {}).get("webhook_secret", "")


# --- TIMING equalization dummy (review finding) ----------------------------------
# The real (known-connection) path does a Fernet DECRYPT of Connection.secret_enc
# (_webhook_secret -> conn_store.dec_secret) BEFORE the HMAC verification. The
# unknown/unsupported-connection branch previously did only a dummy HMAC, so it was
# cheaper than the real path by exactly one Fernet decrypt — a latency probe could
# use that gap to distinguish "connection exists" from "does not exist" even though
# both branches return an identical 401. This is a throwaway, obviously-dead
# ciphertext (never a real secret) encrypted lazily on first use with the app's own
# ENCRYPTION_KEY (module-level so it costs one Fernet encrypt total, not one per
# request) purely so the unknown-connection branch can pay for a decrypt of the same
# shape as the real one.
_DUMMY_ENC_TOKEN: str | None = None


def _dummy_encrypted_token() -> str:
    global _DUMMY_ENC_TOKEN
    if _DUMMY_ENC_TOKEN is None:
        from app.services.connectors import store as conn_store  # lazy: avoid import cycle

        _DUMMY_ENC_TOKEN = conn_store.enc_secret({"webhook_secret": "dummy-equalize-value"})
    return _DUMMY_ENC_TOKEN


def changed_record_ids(kind: str, body_json) -> list[str]:
    """The record ids a (verified) webhook payload says changed. Delegates to the
    connector's own extractor when registered, else best-effort common shapes.
    Thin-payload contract: we only take IDS here — the job re-fetches content.

    D5 (audit): the id list is CAPPED at _MAX_CHANGED_IDS DURING this iteration —
    set-based O(1) dedup + an early break at the cap — so a signed-but-hostile body
    can't stuff tens of thousands of ids into the job payload/logs. This backstops
    EVERY connector even if its own extractor isn't cap-aware; cap-aware extractors
    (e.g. Jira) additionally stop before building the full list."""
    extractor = _CHANGED_IDS_EXTRACTORS.get(kind)
    if extractor is None:
        return []
    try:
        raw = extractor(body_json) or []
    except Exception:
        log.exception("changed-ids extractor for %s crashed", kind)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        s = str(x).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= _MAX_CHANGED_IDS:
            log.warning(
                "webhook intake: %s changed-id list hit cap %d; extra ids ignored",
                kind,
                _MAX_CHANGED_IDS,
            )
            break
    return out


_CHANGED_IDS_EXTRACTORS: dict[str, callable] = {}


def register_changed_ids(kind: str, extractor) -> None:
    _CHANGED_IDS_EXTRACTORS[kind] = extractor


# Connector kinds whose webhook is PING-THEN-PULL: the notification body carries NO
# record ids (Airtable posts only base+webhook ids), so the intake must still
# ENQUEUE a job on an empty-id ping — the job re-fetches payloads from the stored
# cursor. Contrast the default (Zendesk/Confluence) shape where an empty id list is
# a genuine no-op (nothing changed) and must NOT escalate into a full sweep.
_PING_PULL_KINDS: set[str] = set()


def register_ping_pull(kind: str) -> None:
    """Mark a connector kind as ping-then-pull (empty-id ping still enqueues a
    payload-pull job). Called at connector-module import."""
    _PING_PULL_KINDS.add(kind)


def is_ping_pull(kind: str) -> bool:
    return kind in _PING_PULL_KINDS


def handle_intake(connection_id: int, body: bytes, headers: dict, *, enqueue) -> IntakeResult:
    """The full intake pipeline for one delivery. `enqueue` is a callable
    (connection_id, kind, record_ids) -> job_id that submits the async job (the
    route injects the real jobs.submit_unique; tests inject a fake).

    Ordering: resolve connection -> verify signature -> parse -> dedup -> enqueue.
    Every failure returns a distinct status so a misconfigured provider gets a
    diagnosable response, but a BAD SIGNATURE returns 401 without revealing whether
    the connection exists (do not leak connection existence to an unsigned caller).
    """
    from app.services.connectors import store as conn_store  # lazy: avoid import cycle

    conn = conn_store.get(connection_id)
    # Fail closed & indistinguishable: unknown connection and bad signature both
    # look like "not authorized to post here". TIMING (review finding): the real
    # (known-connection) path does a Fernet DECRYPT (_webhook_secret) THEN an HMAC
    # (verify_signature), so the unknown-connection branch does a dummy decrypt of a
    # throwaway pre-encrypted token AND a dummy HMAC against a fixed throwaway
    # secret — equalizing BOTH operations a latency probe could otherwise use to
    # distinguish "connection exists" from "does not exist" (both branches still
    # return an identical 401).
    if conn is None or not access.org_active(conn.org_id) or not supports_webhook(conn.kind):
        conn_store.dec_secret(_dummy_encrypted_token())
        hmac.new(b"loc-studio-dummy-equalize-secret", body or b"", hashlib.sha256).digest()
        return IntakeResult(401, False, "unauthorized")
    secret = _webhook_secret(conn)
    if not verify_signature(conn.kind, secret, body, headers):
        return IntakeResult(401, False, "unauthorized")

    try:
        body_json = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        body_json = {}

    event_id = dedup_key(conn.kind, body, body_json, headers)
    if not record_event(conn.id, conn.kind, event_id):
        # A settled provider retry / duplicate delivery — ACK so the provider
        # stops retrying, but do NOT enqueue a second job.
        return IntakeResult(200, True, "duplicate event ignored (idempotent)")

    # The changed-id list is capped DURING extraction inside changed_record_ids
    # (D5 audit): a signed-but-misconfigured sender must not stuff an unbounded list
    # into the job payload/logs; the connector caps its own fetch too, but the bound
    # shouldn't depend on that alone.
    record_ids = changed_record_ids(conn.kind, body_json)
    if not record_ids:
        # PING-THEN-PULL connectors (Airtable): the notification legitimately carries
        # NO record ids — the ping only says "something changed on this webhook". We
        # MUST enqueue a payload-pull job (the job re-fetches payloads from the stored
        # cursor and filters to source-field changes). For every OTHER connector an
        # empty id list is a genuine no-op (nothing changed) and must NOT escalate
        # into a full paid-MT sweep (review finding) — the watermark backstop
        # reconciles anything missed.
        if is_ping_pull(conn.kind):
            job_id = enqueue(conn.id, conn.kind, [])
            # SETTLE regardless of whether a job resulted (D4): otherwise a replay
            # of a job-less accept would re-run.
            settle_event(conn.id, event_id, job_id)
            return IntakeResult(
                200,
                True,
                "accepted (ping - payload pull enqueued)",
                enqueued=job_id is not None,
                job_id=job_id,
            )
        # A genuine no-op is still an ACCEPTED event: settle it so a replay of the
        # same signed no-op body dedups instead of re-extracting.
        settle_event(conn.id, event_id, None)
        return IntakeResult(200, True, "accepted (no changed records identified)")
    job_id = enqueue(conn.id, conn.kind, record_ids)
    # SETTLE on every accept — including the default button-trigger posture where
    # enqueue records PENDING and returns None (job_id stays NULL). Keying dedup on
    # settlement (not job_id) is what stops a captured signed POST replaying
    # unboundedly (D4 audit).
    settle_event(conn.id, event_id, job_id)
    return IntakeResult(
        200, True, "accepted", enqueued=job_id is not None, job_id=job_id, record_ids=record_ids
    )
