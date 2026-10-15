"""/api routes: smaller areas

Documented in: docs/architecture/surfaces-and-parity.md
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.db import engine
from app.kernel import (
    audit,
    worklist,
)
from app.kernel import query_threads as qthreads
from app.web import deps
from app.web.api_cli_common import (
    _NOT_FOUND,
    _admin_block,
    _creator,
    _cust_of,
    _gate,
    _int_id,
    _json,
    _list_window,
    _lqa_finding_api_error,
    _lqa_finding_dict,
    _lqa_finding_review_block,
    _project_in_org,
    _r3_gate,
    _visible_document,
)

router = APIRouter()


# --- LQA rulebooks (install-global per language pair) ----------------------
@router.get("/api/lqa/rulebook")
def api_lqa_rulebook_get(request: Request, source: str = "", target: str = ""):
    """Read a language-pair LQA rulebook (mirrors `admin lqa rulebook get`). is_admin.
    Query: source, target. Install-global (no per-org rulebooks exist), so under multi-org
    it is host-operator only (#425: 403, the text is never read)."""
    from app.kernel import lqa

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    if lqa.rulebook_host_only():
        return JSONResponse({"ok": False, "error": lqa.RULEBOOK_HOST_ONLY}, status_code=403)
    text = lqa.load_rulebook(source, target)
    return JSONResponse({"ok": True, "source": source, "target": target, "rulebook": text})


@router.post("/api/lqa/rulebook")
async def api_lqa_rulebook_set(request: Request):
    """Install a language-pair LQA rulebook (mirrors `admin lqa rulebook set`).
    is_admin. Body: source, target, text. Install-global, so under multi-org it is
    host-operator only (#425: 403 before the body is read; nothing written)."""
    from app.kernel import audit, lqa

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    if lqa.rulebook_host_only():
        return JSONResponse({"ok": False, "error": lqa.RULEBOOK_HOST_ONLY}, status_code=403)
    body = await _json(request)
    source = (body.get("source") or "").strip()
    target = (body.get("target") or "").strip()
    if not source or not target:
        return JSONResponse(
            {"ok": False, "error": "source and target are required."}, status_code=400
        )
    text = body.get("text") or ""
    try:
        lqa.save_rulebook(source, target, text)
    except ValueError as exc:
        # rulebook_path refuses a locale that is not a plain locale tag (#174). A WRITE
        # must say so rather than 500: the caller chose the value.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    audit.log(
        actor=ident.get("actor", ""),
        action="lqa.rulebook",
        target=f"{source}->{target}",
        detail="%d chars" % len(text),  # noqa: UP031
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "source": source, "target": target, "chars": len(text)})


# --- audit (read-only) -----------------------------------------------------
@router.get("/api/audit/export")
def api_audit_export(
    request: Request, since: str = "", actor: str = "", action: str = "", customer_id: int = 0
):
    """Stream the audit chain as NDJSON (mirrors `admin audit export`). is_admin, read,
    org-confined. Query: since (ISO-8601), actor, action, customer_id. Each row ships
    prev_hash/row_hash so a downstream store can re-verify the chain."""
    from app.kernel import audit
    from app.web.streaming import StreamingResponse

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    since_dt = None
    if since:
        from datetime import datetime, timezone

        parsed = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(since.strip(), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(since.strip())
            except ValueError:
                return JSONResponse(
                    {"ok": False, "error": "could not parse since (use ISO-8601)."}, status_code=400
                )
        # #546: audit.iter_export expects a naive-UTC bound and SQLite bind params
        # silently DROP a tz offset, so 10:00+05:00 used to filter at 10:00Z and an
        # incremental cursor skipped or duplicated rows. Normalize like the memory
        # rollback endpoint (api_cli_memories.py) does.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        since_dt = parsed
    cust = customer_id or None
    # #586: a customer-bound caller exports only its OWN customer's trail (no customer_id
    # means its own, never the whole install), and a customer id that does not exist, is
    # outside the caller's org, or is another customer's answers the same 404 (it used to
    # stream the foreign trail, or 500 on the audit self-log's customer FK for a missing id).
    bound = _cust_of(ident)
    if bound is not None and cust is None:
        cust = bound
    if cust is not None:
        from app.models import Customer

        with Session(engine) as s:
            row = s.get(Customer, cust)
            known = row is not None and (org_id is None or row.org_id == org_id)
        if not known or (bound is not None and cust != bound):
            return JSONResponse({"ok": False, "error": "no such customer."}, status_code=404)
    # AP-7: self-log the export BEFORE streaming, so a failed/aborted stream still
    # leaves a trace and the event ships in its own export when it matches — parity
    # with the web /audit export twin (app/web/admin.py) and the CLI local arm, which
    # were the only surfaces logging it before.
    audit.log(
        actor=ident["actor"],
        action="audit.export",
        detail="actor={} action={} since={} customer={}".format(
            actor or "*", action or "*", since or "*", cust or "*"
        ),
        ip=deps._ip(request),
        customer_id=cust,
    )

    def _stream():
        import json as _json

        for rec in audit.iter_export(
            actor=actor or None,
            action=action or None,
            since=since_dt,
            org_id=org_id,
            customer_id=cust,
        ):
            yield _json.dumps(rec, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@router.get("/api/audit/verify")
def api_audit_verify(request: Request):
    """Verify the keyed audit hash chain end to end (mirrors `admin audit verify`).
    is_admin, read."""
    from app.kernel import audit

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    ok, checked, first_bad = audit.verify_chain()
    body = {"ok": True, "valid": bool(ok), "checked": checked, "first_bad_id": first_bad}
    if not ok:
        body["hint"] = audit.legacy_restore_hint(first_bad)
    return JSONResponse(body)


# --- languages (catalog curation) ------------------------------------------
@router.post("/api/languages/{code}/enable")
def api_language_enable(request: Request, code: str):
    """Enable a catalog language code (mirrors `lang enable`). is_admin. Additive to
    the enabled set. Install-global catalog curation."""
    return _language_toggle(request, code, enable=True)


@router.post("/api/languages/{code}/disable")
def api_language_disable(request: Request, code: str):
    """Disable a catalog language code (mirrors `lang disable`). is_admin."""
    return _language_toggle(request, code, enable=False)


def _language_toggle(request: Request, code: str, enable: bool):
    from app.kernel import audit, languages_catalog

    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    b = _admin_block(ident)
    if b:
        return b
    valid, invalid = languages_catalog.validate_codes([code])
    if invalid:
        return JSONResponse(
            {"ok": False, "error": "unknown catalog code(s): {}".format(", ".join(invalid))},
            status_code=400,
        )
    current = list(languages_catalog.enabled_locales())
    if enable:
        for c in valid:
            if c not in current:
                current.append(c)
    else:
        current = [c for c in current if c not in valid]
    stored = languages_catalog.set_enabled(current, actor=ident.get("actor", ""))
    audit.log(
        actor=ident.get("actor", ""),
        action="language.%s" % ("enable" if enable else "disable"),
        detail=", ".join(valid),
    )
    return JSONResponse({"ok": True, "changed": valid, "enabled": stored})


# ============================================================================
# Wave R3 - configuration & credentials (SPEC-full-remote-parity).
#
# THE MOST DANGEROUS WAVE: configuration mutation + credential writes on the remote
# surface. EVERY route below is gated by ALL THREE independent locks, in this order:
#   1. `admin` scope     (_gate_admin -> deps._require_admin_scope; a docs/keys/api
#                          key is 403, a non-user token is 403);
#   2. is_admin ROLE     (_admin_block; a customer-bound key is never admin);
#   3. remote_admin_enabled SWITCH (_remote_admin_block; 404 - non-enumerable - when
#                          the install has not opted into remote administration).
# The switch is REMOTE_IMMUTABLE (settable only on the host), so a leaked admin key
# can never turn on the surface that would let it reconfigure the install.
#
# SECRETS ARE WRITE-ONLY over the wire, everywhere: a value in a request body is
# accepted and stored (Fernet at rest) but NEVER returned - reads show (set)/(unset)/
# source+last4 only, and the audit rows carry "(changed)"/last4, never cleartext. The
# two deliberate one-time reveals (connector token-regen, apikey issue in R2) are the
# only exceptions and they NEVER touch the audit log.
#
# INSTALL-GLOBAL SCOPE (flagged for the maintainer). settings (config), the cloud PROVIDER keys
# and the ENGINE registry have no per-tenant owner in this codebase - they are install
# infrastructure. An `admin`-scoped key administers them INSTALL-WIDE, exactly as the
# in-process `config`/`providers`/`engine` CLIs (local-root, no --org) and the R2
# semantic/srx/retention toggles already do. In a multi_org_enabled install this means
# an admin in ANY org changes shared install config for everyone. This mirrors the
# siblings deliberately; it is called out in llm.md and the report as an open posture
# question. CONNECTORS and VENDORS, by contrast, ARE tenant objects and are ringed by
# org (+ customer for connectors) via the helpers below - a foreign id is 404.
# ============================================================================
def _connection_in_org(s: Session, cid, org_id, ident=None):
    """Fetch a Connection, or None (404-worthy) if it is missing, in another ORG, or
    (R2-3) belongs to a DIFFERENT customer than a customer-bound caller. Mirrors
    api._conn_in_org / _project_in_org: org-scoped only when multi-org is on (org_id
    given), customer-scoped only for a bound caller. A foreign id must be
    indistinguishable from a missing one."""
    from app.kernel import connectors as connectors_registry
    from app.kernel.connectors import store as conn_store

    conn = conn_store.get(cid) if cid is not None else None
    if conn is None:
        return None
    if org_id is not None and getattr(conn, "org_id", None) != org_id:
        return None
    cust = _cust_of(ident)
    conn_cust = getattr(conn, "customer_id", None)
    if cust is not None and conn_cust is not None and conn_cust != cust:
        return None
    # ffadmin Wave 5: a connection whose KIND is disabled is 404-worthy (None) on the
    # /api/connectors/{cid}/secret + /oauth-secret credential-write routes too -- a
    # disabled kind's connection is unreachable on EVERY surface.
    if not connectors_registry.kind_enabled(conn.kind):
        return None
    return conn


# --- E. connector secrets ---------------------------------------------------
@router.post("/api/connectors/{cid}/secret")
async def api_connector_secret_set(request: Request, cid: int):
    """Rotate a connection's stored credentials (mirrors `connector rotate-secret`).
    admin scope + is_admin + remote_admin_enabled, connection RINGED by org (+ customer
    for a bound caller) - a foreign id is 404. Body: {"secret": {api_key|email|
    api_token|...}} - the FULL new secret (wholesale replace). WRITE-ONLY: the value is
    never echoed. rotate_secret verifies the new creds (auth probe, no content run)
    before storing, so a bad rotation leaves the old secret intact; the response relays
    only (ok, message)."""
    from app.kernel.connectors import store as conn_store

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        if _connection_in_org(s, _int_id(cid), org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
    body = await _json(request)
    secret = body.get("secret")
    if not isinstance(secret, dict) or not secret:
        return JSONResponse(
            {"ok": False, "error": "body must carry a non-empty `secret` object."}, status_code=400
        )
    try:
        ok, msg = await deps.offload_blocking(
            conn_store.rotate_secret,
            _int_id(cid),
            {str(k): v for k, v in secret.items()},
            actor=ident.get("actor", ""),
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@router.post("/api/connectors/{cid}/oauth-secret")
async def api_connector_oauth_secret_set(request: Request, cid: int):
    """Inject a provider-minted OAuth secret for a connection (mirrors `connector
    set-oauth-secret`). admin scope + is_admin + remote_admin_enabled, connection RINGED
    by org (+ customer). Body: {"oauth": {access_token, refresh_token, expires_at, ...}}
    OR the oauth sub-dict directly. WRITE-ONLY: tokens are Fernet-encrypted at rest and
    never echoed."""
    from app.kernel.connectors import store as conn_store

    ident, org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        if _connection_in_org(s, _int_id(cid), org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
    body = await _json(request)
    oauth = body.get("oauth") if isinstance(body.get("oauth"), dict) else body
    if not isinstance(oauth, dict) or not oauth:
        return JSONResponse(
            {
                "ok": False,
                "error": "body must carry an `oauth` object (access_token, "
                "refresh_token, expires_at, ...).",
            },
            status_code=400,
        )
    try:
        await deps.offload_blocking(
            conn_store.set_oauth_secret, _int_id(cid), {str(k): v for k, v in oauth.items()}
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))
    audit.log(
        actor=ident.get("actor", ""),
        action="connector.set_oauth_secret",
        target=f"connection:{_int_id(cid)}",
        detail="(secret set)",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "id": _int_id(cid), "auth": "oauth"})


@router.post("/api/connectors/token-regen")
async def api_connector_token_regen(request: Request):
    """Regenerate the shared connector API token (mirrors `connector token-regen`). admin
    scope + is_admin + remote_admin_enabled. INSTALL-GLOBAL - there is exactly one shared
    companion-app token (`lsk_...`) per install, not one per connection, so this route
    carries NO cid (a deliberate deviation from the spec's /{cid}/ shape, flagged in the
    report). The NEW token is revealed ONCE in the response (the one legitimate reveal,
    like an apikey issue) and remains recoverable by admins; it is NEVER audit-logged. The
    old token stops working immediately."""
    import secrets as _secrets

    from app.kernel import settings as _settings

    ident, _org_id, blocked = _r3_gate(request)
    if blocked:
        return blocked
    token = "lsk_" + _secrets.token_urlsafe(24)
    _settings.set_many({"connector_api_token": token})
    audit.log(
        actor=ident.get("actor", ""),
        action="connector.token_regenerate",
        target="connector_api_token",
        detail="(secret rotated)",
        ip=deps._ip(request),
    )
    return JSONResponse({"ok": True, "connector_api_token": token})


# ============================================================================
# Queries — the /api twin of the web /queries surface (SPEC-wave3 §4.3). RBAC is the
# SERVICE's job, derived from the key's User (the tenant ring + §4.5 role rules): a
# viewer key may open/post/answer; author/assignee/reviewer+ close/assign — so there is
# NO _write_block here (a can_write gate would exclude the customer-bound client RV-1
# names). A thread outside the caller's scope is 404, never 403 (non-enumerable). Every
# route: feature_api_block("queries") FIRST (layer 2), then _gate. Shared serializers so
# /api, the remote CLI and MCP never drift on row shape.
# ============================================================================
def _qthread_row_dict(r) -> dict:
    return {
        "id": r.id,
        "project_id": r.project_id,
        "document_id": r.document_id,
        "segment_id": r.segment_id,
        "key_id": r.key_id,
        "target_locale": r.target_locale,
        "subject": r.subject,
        "status": r.status,
        "author": r.author,
        "author_kind": r.author_kind,
        "assignee_email": r.assignee_email,
        "vendor_id": r.vendor_id,
        "dispatch_id": r.dispatch_id,
        "post_count": r.post_count,
        "created_at": str(r.created_at),
        "updated_at": str(r.updated_at),
        "closed_at": str(r.closed_at) if r.closed_at else None,
        "closed_by": r.closed_by,
    }


def _qthread_dict(t) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "document_id": t.document_id,
        "segment_id": t.segment_id,
        "key_id": t.key_id,
        "target_locale": t.target_locale,
        "subject": t.subject,
        "status": t.status,
        "author": t.author,
        "author_kind": t.author_kind,
        "assignee_email": t.assignee_email,
        "vendor_id": t.vendor_id,
        "dispatch_id": t.dispatch_id,
        "post_count": t.post_count,
        "created_at": str(t.created_at),
        "updated_at": str(t.updated_at),
        "closed_at": str(t.closed_at) if t.closed_at else None,
        "closed_by": t.closed_by,
    }


def _qpost_dict(p) -> dict:
    return {
        "id": p.id,
        "thread_id": p.thread_id,
        "body": p.body,
        "author": p.author,
        "author_kind": p.author_kind,
        "author_label": p.author_label,
        "vendor_id": p.vendor_id,
        "dispatch_id": p.dispatch_id,
        "external_id": p.external_id,
        "created_at": str(p.created_at),
    }


def _query_api_error(exc):
    """Map a service QueryError to the canonical /api envelope at its own status."""
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)


async def _query_notify(fn_name, *args):
    # #746 gate: the notifier sends SMTP inline (15 s connect timeout per recipient) when mail
    # is configured, so it runs in the bounded worker pool, never on the event loop. A busy
    # pool skips the mail like any other notifier failure: it never fails the call.
    try:
        from app.kernel import notifications

        await deps.offload_blocking(getattr(notifications, fn_name), *args)
    except Exception:  # noqa: S110 -- notification mail is a side effect; never fail the call
        pass


@router.get("/api/queries")
def api_queries_list(request: Request):
    """List query threads over the caller's in-scope projects, filtered + paginated.
    Same filters as the web list: status, project, document, segment, key, assignee=me,
    mine. Scope is applied BEFORE pagination + the count (the service), so a page or a
    `total` never leaks a foreign row."""
    blocked = deps.feature_api_block("queries")
    if blocked:
        return blocked
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    qp = request.query_params
    status = (qp.get("status") or "").strip() or None
    from app.models import QueryStatus

    if status is not None and status not in QueryStatus.ALL:
        return JSONResponse(
            {"ok": False, "error": "status must be one of open / answered / closed."},
            status_code=400,
        )
    me = _creator(ident)
    email = getattr(me, "email", "") or ""
    _asg = (qp.get("assignee") or "").strip()
    if _asg.lower() in ("me", "1", "true"):
        assignee = email or None  # the token's own user
    elif _asg:
        assignee = _asg  # an explicit email (service filters by it)
    else:
        assignee = None
    mine = (qp.get("mine") or "").strip().lower() in ("1", "true", "on", "yes")
    limit, offset, err = _list_window(qp.get("limit"), qp.get("offset"), cap=qthreads.LIST_PAGE_MAX)
    if err:
        return err
    with Session(engine) as s:
        rows, total = qthreads.list_threads(
            s,
            me,
            project_id=_int_id(qp.get("project")),
            document_id=_int_id(qp.get("document")),
            segment_id=_int_id(qp.get("segment")),
            key_id=_int_id(qp.get("key")),
            status=status,
            assignee=assignee,
            mine=mine,
            limit=limit,
            offset=offset,
            org_id=org_id,
        )
    return JSONResponse(
        {
            "ok": True,
            "threads": [_qthread_row_dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/api/queries/{qid}")
def api_query_show(qid: int, request: Request):
    """One thread + its posts (oldest first). Out-of-scope -> 404 (non-enumerable)."""
    blocked = deps.feature_api_block("queries")
    if blocked:
        return blocked
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        try:
            thread, posts = qthreads.get_thread(s, user=_creator(ident), thread_id=qid)
        except qthreads.QueryError as exc:
            return _query_api_error(exc)
        return JSONResponse(
            {"ok": True, "thread": _qthread_dict(thread), "posts": [_qpost_dict(p) for p in posts]}
        )


@router.post("/api/queries")
async def api_query_open(request: Request):
    """Open a thread. Body: a scope anchor (segment/document/key/project) + subject,
    body, optional assignee_email. Viewer-allowed (RV-1) — the service enforces the ring
    + caps + the assignee-is-an-in-scope-active-user rule (the mail-relay guard)."""
    blocked = deps.feature_api_block("queries")
    if blocked:
        return blocked
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        try:
            scope = qthreads.resolve_scope_in_ring(
                s,
                user=_creator(ident),
                org_id=_org_id,
                segment_id=_int_id(body.get("segment_id")),
                document_id=_int_id(body.get("document_id")),
                key_id=_int_id(body.get("key_id")),
                project_id=_int_id(body.get("project_id")),
            )
            thread = qthreads.open_thread(
                s,
                user=_creator(ident),
                scope=scope,
                subject=str(body.get("subject") or ""),
                body=str(body.get("body") or ""),
                assignee_email=str(body.get("assignee_email") or ""),
            )
        except qthreads.QueryError as exc:
            return _query_api_error(exc)
        audit.log(
            actor=ident.get("actor", ""),
            action="query.open",
            target=f"thread:{thread.id}",
            detail=f"project={thread.project_id} status={thread.status}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        out = _qthread_dict(thread)
        tid = thread.id
    await _query_notify("on_query_opened", tid, ident.get("actor", ""))  # L-5: skip self-assign
    return JSONResponse({"ok": True, "thread": out})


@router.post("/api/queries/{qid}/posts")
async def api_query_post(qid: int, request: Request):
    """Reply to a thread. Body: {"body": str, "answer": bool}. `answer` marks it
    answered; a plain reply never flips open->answered (§3.3). 409 on a closed thread."""
    blocked = deps.feature_api_block("queries")
    if blocked:
        return blocked
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    is_answer = bool(body.get("answer"))
    with Session(engine) as s:
        try:
            post = qthreads.add_post(
                s,
                user=_creator(ident),
                thread_id=qid,
                body=str(body.get("body") or ""),
                answer=is_answer,
            )
        except qthreads.QueryError as exc:
            return _query_api_error(exc)
        from app.models import QueryThread

        thread = s.get(QueryThread, qid)
        audit.log(
            actor=ident.get("actor", ""),
            action="query.post",
            target=f"thread:{qid}",
            detail=f"post={post.id} answer={int(is_answer)} status={thread.status}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        out = _qpost_dict(post)
    await _query_notify("on_query_post", qid, out["id"], ident.get("actor", ""))
    return JSONResponse({"ok": True, "post": out})


@router.post("/api/queries/{qid}/status")
async def api_query_status(qid: int, request: Request):
    """Set the lifecycle status. Body: {"status": "open"|"answered"|"closed"}. close
    needs author/assignee/reviewer+; reopen needs author/reviewer+ (the service, on the
    key's unforgeable identity)."""
    blocked = deps.feature_api_block("queries")
    if blocked:
        return blocked
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    from app.models import QueryStatus, QueryThread

    with Session(engine) as s:
        # M-2: notify on the TRANSITION, not the target state (no re-mail on a no-op close).
        prior = s.get(QueryThread, qid)
        prior_status = prior.status if prior else None
        try:
            thread = qthreads.set_status(
                s, user=_creator(ident), thread_id=qid, status=str(body.get("status") or "").strip()
            )
        except qthreads.QueryError as exc:
            return _query_api_error(exc)
        audit.log(
            actor=ident.get("actor", ""),
            action="query.status",
            target=f"thread:{qid}",
            detail=f"status={thread.status}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        out = _qthread_dict(thread)
        newly_closed = thread.status == QueryStatus.CLOSED and prior_status != QueryStatus.CLOSED
    if newly_closed:
        await _query_notify("on_query_closed", qid, ident.get("actor", ""))
    return JSONResponse({"ok": True, "thread": out})


@router.post("/api/queries/{qid}/assign")
async def api_query_assign(qid: int, request: Request):
    """Set or clear the assignee. Body: {"assignee_email": str} (empty clears). Needs
    author or reviewer+; the target must be an in-scope active user (the mail-relay
    guard) — the service enforces both."""
    blocked = deps.feature_api_block("queries")
    if blocked:
        return blocked
    ident, _org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    from app.models import QueryThread

    with Session(engine) as s:
        # M-3: notify only when the assignee ACTUALLY CHANGED (no re-mail on re-assign/clear).
        prior = s.get(QueryThread, qid)
        prior_assignee = (prior.assignee_email or "") if prior else ""
        try:
            thread = qthreads.assign(
                s,
                user=_creator(ident),
                thread_id=qid,
                assignee_email=str(body.get("assignee_email") or ""),
            )
        except qthreads.QueryError as exc:
            return _query_api_error(exc)
        audit.log(
            actor=ident.get("actor", ""),
            action="query.assign",
            target=f"thread:{qid}",
            detail="assignee=%s" % (thread.assignee_email or "(cleared)"),
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(thread.project_id),
        )
        out = _qthread_dict(thread)
        changed = bool(thread.assignee_email) and thread.assignee_email != prior_assignee
    if changed:
        await _query_notify("on_query_opened", qid, ident.get("actor", ""))
    return JSONResponse({"ok": True, "thread": out})


# ============================================================================
# Worklist (SPEC-wave3 §8.4) — GET /api/worklist. Identity is the API KEY'S USER
# (ident["user"]); a shared connector token has no user and is refused with a NAMED 400,
# never an empty list that reads as "you have no work" (§8.4, default #12). The service
# owns the scoping ring + the section rules; queries_enabled gates the query section.
# ============================================================================
def _worklist_item_dict(it) -> dict:
    """BYTE-IDENTICAL in key set to cmd_work._item_dict, so /api, the remote CLI and MCP
    never drift on row shape."""
    return {
        "section": it.section,
        "ref_id": it.ref_id,
        "title": it.title,
        "project_id": it.project_id,
        "project_name": it.project_name,
        "document_id": it.document_id,
        "target_locale": it.target_locale,
        "status": it.status,
        "reason": it.reason,
        "due": it.due,
        "url": it.url,
        "updated_at": str(it.updated_at) if it.updated_at else None,
    }


@router.get("/api/worklist")
def api_worklist(request: Request):
    """The caller's cross-project reviewer worklist (paused review steps that name them,
    queries assigned to / answered for them, documents needing quality attention). The
    identity is the token's own user; a shared connector token carries no user, so the
    worklist REFUSES it with a named 400 (§8.4) rather than answering for nobody."""
    blocked = deps.feature_api_block("worklist")
    if blocked:
        return blocked
    # A shared connector token authenticates but has no user; the worklist is per-person,
    # so refuse it with a NAMED 400 before the doc-scope gate turns it into a generic 403.
    raw = deps.api_identity(request)
    if raw is not None and raw.get("kind") == "connector":
        return JSONResponse(
            {
                "ok": False,
                "error": "the worklist needs a per-user API key (the shared connector "
                "token has no user).",
            },
            status_code=400,
        )
    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    me = _creator(ident)
    qp = request.query_params
    days = _int_id(qp.get("days")) or worklist.QUALITY_WINDOW_DAYS
    days = max(1, min(days, 3650))
    quality = (qp.get("quality") or "").strip().lower()
    quality = "needs_review" if quality == "needs_review" else ""
    limit, offset, err = _list_window(qp.get("limit"), qp.get("offset"), cap=worklist.LIST_PAGE_MAX)
    if err:
        return err
    with Session(engine) as s:
        rows, total = worklist.my_work(
            s, me, days=days, quality=quality, limit=limit, offset=offset, org_id=org_id
        )
    return JSONResponse(
        {
            "ok": True,
            "items": [_worklist_item_dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("/api/lqa/findings/{fid}")
async def api_lqa_finding_edit(fid: int, request: Request):
    """Edit a reviewer finding's dimension/severity/note. REVIEWER+ AND own-row OR
    can_manage+. A machine row is refused; a foreign-scope finding 404s."""
    from app.kernel import lqa as _lqa
    from app.models import LQAResult

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    body = await _json(request)
    with Session(engine) as s:
        # VISIBILITY BEFORE ROLE (#70): resolve the finding + its document ring FIRST,
        # so a foreign/invisible finding 404s (matching the web twin + the GET twin)
        # before the reviewer gate can emit a role 403.
        finding = s.get(LQAResult, fid)
        if finding is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, finding.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _lqa_finding_review_block(ident, "edit")
        if blocked:
            return blocked
        try:
            row = _lqa.edit_human_finding(
                s,
                finding=finding,
                dimension=str(body.get("dimension") or ""),
                severity=str(body.get("severity") or ""),
                note=str(body.get("note") or ""),
                user=_creator(ident),
                actor=ident["actor"],
            )
        except (_lqa.HumanFindingError, _lqa.HumanFindingForbidden) as exc:
            return _lqa_finding_api_error(exc)
        audit.log(
            actor=ident["actor"],
            action="lqa.finding.edit",
            target=f"finding:{fid}",
            detail=f"doc={finding.document_id}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(doc.project_id),
        )
        out = _lqa_finding_dict(row)
    return JSONResponse({"ok": True, "finding": out})


@router.post("/api/lqa/findings/{fid}/withdraw")
async def api_lqa_finding_withdraw(fid: int, request: Request):
    """Soft-withdraw a reviewer finding (§6.7). REVIEWER+ AND own-row OR can_manage+."""
    from app.kernel import lqa as _lqa
    from app.models import LQAResult

    ident, org_id, blocked = _gate(request)
    if blocked:
        return blocked
    with Session(engine) as s:
        # VISIBILITY BEFORE ROLE (#70): finding + document ring FIRST (foreign/invisible
        # 404s, matching the web twin + GET twin), then the reviewer gate.
        finding = s.get(LQAResult, fid)
        if finding is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        doc = _visible_document(s, finding.document_id)
        if doc is None or _project_in_org(s, doc.project_id, org_id, ident) is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
        blocked = _lqa_finding_review_block(ident, "withdraw")
        if blocked:
            return blocked
        try:
            row = _lqa.withdraw_human_finding(
                s, finding=finding, user=_creator(ident), actor=ident["actor"]
            )
        except (_lqa.HumanFindingError, _lqa.HumanFindingForbidden) as exc:
            return _lqa_finding_api_error(exc)
        audit.log(
            actor=ident["actor"],
            action="lqa.finding.withdraw",
            target=f"finding:{fid}",
            detail=f"doc={finding.document_id}",
            ip=deps._ip(request),
            customer_id=audit.customer_for_project(doc.project_id),
        )
        out = _lqa_finding_dict(row)
    return JSONResponse({"ok": True, "finding": out})
