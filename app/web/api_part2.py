"""api: part2

Documented in: docs/architecture/surfaces-and-parity.md
"""

import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from app import config
from app.db import engine
from app.filters.base import EmptyDocumentError, InputLimitError, UnsupportedEncodingError
from app.filters.csv_filter_common import ColumnMappingError
from app.filters.xml_its import XmlFilterError
from app.filters.xml_resource_common import XmlResourceError
from app.kernel import (
    access,
    audit,
    connected_projects,
    ids,
    jobs,
    languages,
    languages_catalog,
    providers,
    routing,
    security,
    tm_delivery,
)
from app.kernel import connectors as connectors_registry
from app.kernel.connectors import store as conn_store
from app.kernel.connectors import webhooks
from app.kernel.ingest import NotADocumentProject, UnsupportedFormat, ingest_document
from app.models import Document, Project, Segment, TargetLocale
from app.web import deps
from app.web.api_cli_common import _project_in_org
from app.web.api_discovery import (
    _APP_VERSION_COARSE,
    _FORBIDDEN,
    _NOT_FOUND,
    _UNAUTHORIZED,
    _ai_role_block,
    _doc_authz,
    _ensure_inbox_project,
    _find_secret_fields,
    _ident_org_id,
    _job_readable,
)

router = APIRouter()
_LOG = logging.getLogger("loc_tms")


def _write_role_block(ident: dict):
    """Return a ready-to-return 403 JSONResponse when a doc-scoped caller's role
    fails can_write (>= translator), else None. Mirrors api_cli._write_block: the
    FULL_TEXT paragraph WRITE routes (save / revert / revert-all-damaged) are
    content-edit actions, gated like their approve / apply-tm /api siblings and like
    their WEB twins — which the login-wall middleware already gates at can_write
    (app/main.py `_security_audit`, proven by probe: a viewer web POST is 403, a
    translator reaches the route). /api bypasses that middleware, so the parity gate
    must be applied here. A no-op when auth is off. The shared connector token can
    NEVER reach these routes (`deps._require_doc_scope` rejects `kind != user`
    outright), so `ident['user']` is always a real per-user identity here -- no
    connector carve-out is needed (unlike _ai_role_block, whose routes the token can
    reach)."""
    if not security.auth_required():
        return None
    if security.can_write(ident.get("user")):
        return None
    return JSONResponse(_FORBIDDEN, status_code=403)


def _conn_in_org(ident: dict, cid: int):
    """Object-authz for an inbound-API connector call (leak vectors 6/10 + R2-3
    task_bcae962f). Returns a ready-to-return JSON 404 when the connection is missing,
    belongs to another ORG, or — for a customer-BOUND caller — belongs to a DIFFERENT
    CUSTOMER (a foreign cid must be indistinguishable from a missing one — 404, never
    a signal that it exists), else None to proceed. A stored project must also be
    visible to the caller, even when the connection itself is global (#370).

    Org resolution goes through the per-user identity's org_id (HOME org).
    Shared connector tokens are refused before any connection lookup, even with
    auth off or a configured org binding.

    R2-3 customer ring (task_bcae962f — the write-capable sibling of the /api list
    gap): a customer-bound per-user key may act only on its OWN customer's connection
    or a GLOBAL/unassigned one (customer_id NULL) — a different customer's connection
    is a 404, mirroring the web _conn_or_404 predicate and the remote-CLI job wall
    (api_cli._payload_org_block) EXACTLY. deps._ident_customer_id is None for UNBOUND
    identities — internal-staff keys (no customer binding), the shared connector token
    (no user), and (F1: mirroring access.current_customer_id) under auth OFF — so on
    the default install and single-operator mode this ring is inert. This is the ONE
    predicate every /api/connectors/{cid}/* action route funnels through; keep the
    check here, never per-route."""
    if ident.get("kind") != "user":
        return JSONResponse(_FORBIDDEN, status_code=403)
    conn = conn_store.get(cid)
    if conn is None:
        return JSONResponse(_NOT_FOUND, status_code=404)
    if access.multi_org_active() and _ident_org_id(ident) != getattr(conn, "org_id", None):
        return JSONResponse(_NOT_FOUND, status_code=404)
    bound_cust = deps._ident_customer_id(ident)
    conn_cust = getattr(conn, "customer_id", None)
    if bound_cust is not None and conn_cust is not None and conn_cust != bound_cust:
        return JSONResponse(_NOT_FOUND, status_code=404)
    # A global connection does not grant access to its stored project (#370).
    if conn.project_id is not None:
        project_ident = {**ident, "_cust_id": bound_cust}
        with Session(engine) as s:
            if _project_in_org(s, conn.project_id, _ident_org_id(ident), project_ident) is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
    # ffadmin Wave 5: a connection whose KIND is disabled on this install is 404 on
    # every /api/connectors/{cid}/* route (the generic connection routes AND the per-kind
    # translate/webhook routes all funnel through this one predicate via _conn_gate/
    # _conn_cid_gate) -- non-enumerable, the kill-switch law per kind. Preserved but
    # hidden: the row is untouched and returns when the kind is re-enabled.
    if not connectors_registry.kind_enabled(conn.kind):
        return JSONResponse(_NOT_FOUND, status_code=404)
    return None


def _conn_gate(request: Request, cid: int, *, role: bool = True):
    """The shared inbound-API connector auth preamble, factored out of the ~22
    /api/connectors/{cid}/* handlers whose first five lines were byte-identical:
    resolve identity (401 when absent) -> _ai_role_block (write-role RBAC, skipped
    for read-only routes via role=False) -> deps._api_org_block (unbound shared
    token wall) -> _conn_in_org (object authz). Returns (ident, blocked) where
    `blocked` is a ready-to-return JSONResponse or None; on a 401 ident is None.
    The per-connector DECORATOR/path/docstring stay untouched — only the identical
    body preamble is shared, so the wire surface companion apps hit is unchanged."""
    ident = deps.api_identity(request)
    if ident is None:
        return None, JSONResponse(_UNAUTHORIZED, status_code=401)
    blocked = (
        (_ai_role_block(ident) if role else None)
        or deps._api_org_block(request, ident)
        or _conn_in_org(ident, cid)
    )
    return ident, blocked


_FMT_LABELS = {
    "catkeys": "Haiku catalog (catkeys)",
    "desktop": "Freedesktop desktop entry",
    "docx": "Word document",
    "eml": "Email message (EML)",
    "epub": "EPUB e-book",
    "dxf": "AutoCAD DXF drawing text",
    "pdf": "PDF",
    "html": "HTML",
    "htm": "HTML",
    "hhc": "HTML Help table of contents",
    "hhk": "HTML Help index",
    "hhp": "HTML Help project",
    "json": "JSON (i18n strings)",
    "json5": "JSON5 (comments kept)",
    "jsonc": "JSON with Comments (JSONC)",
    "md": "Markdown",
    "mif": "FrameMaker MIF",
    "adoc": "AsciiDoc",
    "mdx": "MDX (Markdown + JSX)",
    "txt": "Plain text",
    "po": "gettext PO",
    "srt": "SubRip subtitles",
    "sbv": "YouTube SBV subtitles",
    "sub": "SubViewer or MicroDVD subtitles",
    "toml": "TOML (i18n strings)",
    "stf": "Salesforce Translation Workbench",
    "vtt": "WebVTT subtitles",
    "ttml": "TTML subtitles",
    "dfxp": "DFXP (TTML) subtitles",
    "scc": "Scenarist SCC captions (CEA-608)",
    "stl": "EBU STL subtitles",
    "xaml": "XAML resources",
    "xliff": "XLIFF",
    "xmb": "Angular XMB (exports XTB)",
    "xtb": "Angular XTB",
    "xtg": "QuarkXPress Tags",
    "tag": "QuarkXPress Tags",
    "xlf": "XLIFF",
    "odt": "OpenDocument text",
    "ott": "OpenDocument text template",
    "ods": "OpenDocument spreadsheet",
    "ots": "OpenDocument spreadsheet template",
    "odp": "OpenDocument presentation",
    "otp": "OpenDocument presentation template",
    "odg": "OpenDocument drawing",
    "otg": "OpenDocument drawing template",
    "php": "PHP (template or language array)",
    "phtml": "PHP template",
    "jsp": "JSP page",
    "jspf": "JSP fragment",
    "aspx": "ASP.NET page",
    "ascx": "ASP.NET user control",
    "erb": "ERB template",
    "shtml": "SSI page",
    "shtm": "SSI page",
    "stm": "SSI page",
    "ttx": "Trados TagEditor TTX",
    "txlf": "Wordfast TXLF",
    "txml": "Wordfast TXML",
    "sdlppx": "Trados project package",
    "sdlrpx": "Trados return package",
    "mqxlz": "memoQ MQXLZ (zipped MQXLIFF)",
    "xlz": "WorldServer XLZ package",
    "wsxz": "WorldServer WSXZ package",
    "flprjzip": "MadCap Flare project (zip)",
    "wiki": "MediaWiki wikitext",
    "mediawiki": "MediaWiki wikitext",
    "wikitext": "MediaWiki wikitext",
    "dokuwiki": "DokuWiki markup",
    "xlsx": "Excel workbook",
    "xlsm": "Excel macro-enabled workbook",
    "xltx": "Excel template",
    "xltm": "Excel macro-enabled template",
    "docm": "Word macro-enabled document",
    "dotx": "Word template",
    "dotm": "Word macro-enabled template",
    "pptx": "PowerPoint presentation",
    "pptm": "PowerPoint macro-enabled presentation",
    "potx": "PowerPoint template",
    "potm": "PowerPoint macro-enabled template",
    "ppsx": "PowerPoint show",
    "ppsm": "PowerPoint macro-enabled show",
    "xml": "XML (Android strings.xml, Java XML properties, or ITS 2.0 rules)",
    "dita": "DITA topic or map",
    "ditamap": "DITA map",
    "bookmap": "DITA bookmap",
    "docbook": "DocBook",
    "dbk": "DocBook",
    "sdlxliff": "Trados SDLXLIFF",
    "sdlxlf": "Trados SDLXLIFF",
    "mqxliff": "memoQ MQXLIFF",
    "mxliff": "Phrase MXLIFF",
    "dtd": "Mozilla DTD entities",
    "ftl": "Fluent (FTL)",
    "properties": "Java properties",
    "strings": "Apple strings",
    "ini": "INI (generic and Joomla)",
    "resjson": "Windows RESJSON",
    "xsl": "XSLT stylesheet",
    "xslt": "XSLT stylesheet",
    "yaml": "YAML (i18n strings, Rails, Symfony)",
    "yml": "YAML (i18n strings, Rails, Symfony)",
    "resx": ".NET RESX resources",
    "resw": "Windows RESW resources",
    "wxl": "WiX localization",
    "ts": "Qt Linguist TS or TypeScript locale module",
    "rc": "Windows resource script",
    "idml": "Adobe InDesign (IDML)",
    "vsdx": "Visio drawing",
    "vsdm": "Visio drawing (macro-enabled)",
    "plist": "Apple property list",
    "stringsdict": "Apple plurals (stringsdict)",
    "xcstrings": "Apple String Catalog",
    "js": "JavaScript locale module",
    "mjs": "JavaScript locale module",
    "cjs": "JavaScript locale module",
    "mts": "TypeScript locale module",
    "cts": "TypeScript locale module",
    "icml": "Adobe InCopy (ICML)",
    "xcloc": "Xcode localization catalog (zipped)",
    "rst": "reStructuredText",
    "tex": "LaTeX",
}


# Per-format download MIME types live in app/web/deps.py so the UI export route
# and this API download route share ONE map (BUG-2 fix — they must not drift).
# Friendly names for the discovery route.
@router.get("/api/connectors/health")
def api_health(request: Request):
    """Unauthenticated liveness probe. Returns ONLY ``{ok, version}`` to an anonymous caller
    (audit 2026-09-05 F2): the install's enabled-connector-kind enumeration used to ride here with
    no key, disclosing the connector topology to anyone who could reach the port. That enumeration
    now sits behind the SAME docs-scoped key the rest of the MCP front door uses -- a docs-scoped
    caller additionally gets the ``connectors`` list, everyone else (no key, wrong scope) gets the
    bare liveness answer. Sibling of ``/health`` / ``/ready``; the anonymous shape is stable, so a
    proxy/k8s liveness check never needs a token.

    ``version`` is coarsened to MAJOR.MINOR (audit 2026-09-05 L4): the exact patch release on the
    one route that answers without a key is a free fingerprint for known-CVE targeting, and a
    liveness check does not need it.

    A tokenless request never touches the DB (deps.api_identity short-circuits on an absent
    header), so this stays a cheap liveness probe."""
    payload = {"ok": True, "version": _APP_VERSION_COARSE}
    _ident, blocked = deps._require_doc_scope(request)
    if not blocked:
        # ffadmin Wave 5: enumerate only the ENABLED kinds -- a disabled kind is non-enumerable,
        # so it is absent from the advertisement too.
        payload["connectors"] = [s.kind for s in connectors_registry.enabled_specs()]
    return JSONResponse(payload)


@router.get("/api/connectors")
def api_list_connections(request: Request):
    ident = deps.api_identity(request)
    if ident is None:
        return JSONResponse(_UNAUTHORIZED, status_code=401)
    if ident.get("kind") != "user":
        return JSONResponse(_FORBIDDEN, status_code=403)
    # Per-user keys always carry their HOME org when the multi-org gate is on.
    blocked = deps._api_org_block(request, ident)
    if blocked:
        return blocked
    # A per-user key sees only its org's connections. No org filtering with the gate off.
    org_id = _ident_org_id(ident)
    # R2-3 (task_bcae962f): a customer-BOUND per-user key is further confined to its
    # OWN customer's connections PLUS globals (customer_id IS NULL) — never a foreign
    # customer's, exactly as the web connector list (_my_customer_id) and the connector
    # job authz (api_cli) already do. None for UNBOUND identities — internal-staff keys,
    # the shared connector token (no user), and (F1) under auth off — where it is an
    # unfiltered no-op matching the web session (SYSTEM operator, current_customer_id None).
    customer_id = deps._ident_customer_id(ident)

    # Issue #17: --remote rows were missing auth/customer_id/content_type_id vs the
    # in-process `connector list` (which builds via cmd_connector._conn_row) --
    # docs/CLI.md promises identical output shape between the two transports.
    # 'auth' mirrors cmd_connector._auth_summary exactly (never the secret itself,
    # only its mode: oauth/token/none).
    def _auth(c):
        secret = conn_store.dec_secret(c.secret_enc)
        return conn_store.auth_mode(secret) if secret else "none"

    # #474: a connection bound to a project the caller cannot see is left out of the
    # list, the same project rule _conn_in_org applies to every per-connection route
    # (#370). None = unrestricted (staff, auth off).
    with Session(engine) as s:
        visible = access.visible_project_ids(s, ident.get("user"))
    visible = None if visible is None else set(visible)

    def _project_visible(c):
        return c.project_id is None or visible is None or c.project_id in visible

    # ffadmin Wave 5: EXCLUDE connections of a disabled kind -- a disabled kind is
    # non-enumerable, so its connections drop out of the list (mirrors the web
    # /connectors page, which renders only enabled kinds). The rows are untouched and
    # reappear when the kind is re-enabled.
    return JSONResponse(
        {
            "ok": True,
            "connections": [
                {
                    "id": c.id,
                    "kind": c.kind,
                    "name": c.name,
                    "status": c.status,
                    "auth": _auth(c),
                    "project_id": c.project_id,
                    "customer_id": getattr(c, "customer_id", None),
                    "content_type_id": getattr(c, "content_type_id", None),
                    "languages_project_id": conn_store.languages_project_id(c.id),
                }
                for c in conn_store.list_connections(org_id=org_id, customer_id=customer_id)
                if connectors_registry.kind_enabled(c.kind) and _project_visible(c)
            ],
        }
    )


@router.get("/api/jobs/{jid}")
def api_job_status(jid: int, request: Request):
    ident = deps.api_identity(request)
    if ident is None:
        return JSONResponse(_UNAUTHORIZED, status_code=401)
    # Unbound shared connector token is rejected in multi-org before it can enumerate
    # job ids (leak vectors 5/6); no-op with the gate off.
    blocked = deps._api_org_block(request, ident)
    if blocked:
        return blocked
    job = jobs.get(jid)
    if not job or not _job_readable(ident, job):
        # 404 for both missing and out-of-scope: ids must not be enumerable
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, **jobs.status_dict(job)})


@router.post("/webhooks/connectors/{connection_id}")
async def connector_webhook_intake(connection_id: int, request: Request):
    """Generalized connector webhook intake (CONNECTOR_FRAMEWORK_DESIGN §3.4).

    ONE endpoint any connector plugs into. It is UNAUTHENTICATED at the session/API
    layer BY DESIGN — an external provider (Zendesk) can't carry our session or API
    key — and is instead authed by the connector's per-connection webhook SIGNATURE
    (the vaulted `webhook_secret`). So it is safe when the login wall is off, and
    the {connection_id} lookup scopes the org (a caller who can't sign for a
    connection is 401, indistinguishable from a missing connection — no cross-org
    or existence leak).

    Fast-ACK-then-enqueue (Intercom's <5s rule): verify -> dedup -> enqueue a
    connector_translate job carrying the changed article ids -> return. All the
    re-fetch/translate/write-back happens async on the job worker. A provider RETRY
    of the same event id ACKs 200 but enqueues nothing (idempotent).

    BODY SIZE CAP (review finding): this route is unauthenticated at the session/API
    layer BY DESIGN (see above) — reading an unbounded body BEFORE the signature can
    even be checked would be a memory-exhaustion vector reachable with NO
    credentials. Webhook payloads are thin by contract (id + metadata only), so both
    a declared Content-Length over the cap AND an actual body over the cap (the
    no/lying-Content-Length case) are rejected 413 — BEFORE signature verification,
    so an oversized delivery never reaches the HMAC computation at all."""
    cap = config.WEBHOOK_MAX_BODY_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return JSONResponse(
                    {"ok": False, "error": f"Webhook payload too large (limit {cap} bytes)."},
                    status_code=413,
                )
        except ValueError:
            pass  # unparseable Content-Length -> fall through to the bounded stream read
    chunks, total = [], 0
    async for piece in request.stream():
        total += len(piece)
        if total > cap:
            return JSONResponse(
                {"ok": False, "error": f"Webhook payload too large (limit {cap} bytes)."},
                status_code=413,
            )
        chunks.append(piece)
    # Disabled kinds have the same unauthorized status and body as missing connections.
    conn0 = conn_store.get(connection_id)
    if conn0 is not None and not connectors_registry.kind_enabled(conn0.kind):
        result = webhooks.IntakeResult(401, False, "unauthorized")
        return JSONResponse(result.as_dict(), status_code=result.status)
    body = b"".join(chunks)
    headers = {k: v for k, v in request.headers.items()}  # noqa: C416

    def _enqueue(cid: int, kind: str, record_ids):
        # JIRA PHASE 2 POSTURE GATE (the maintainer's confirmed default: a push should NOT
        # auto-translate unless opted in). For a Jira connection that has NOT set
        # auto_translate_on_change, a verified push does NOT enqueue paid MT — it
        # records the changed issue key(s) as PENDING on the connection (surfaced on
        # the connection page + released with "Translate pending now" / the CLI verb)
        # and returns None so handle_intake ACKs "accepted" (enqueued=False). A
        # connection that opts IN falls through to the sibling auto-enqueue path.
        if kind == "jira":
            conn = conn_store.get(cid)
            if conn is not None and not conn_store.jira_auto_translate_on(conn):
                conn_store.jira_record_pending(cid, record_ids)
                return None  # pending recorded; no job, no billing
        # PLAIN submit, NOT submit_unique (review finding): submit_unique dedups on
        # connection_id alone, so a webhook naming specific article ids that landed
        # while a full sync (or another webhook's job) was in-flight would COALESCE
        # onto that job and its article ids would be silently dropped — the event
        # is already marked accepted in the ledger, so those articles would never
        # be translated. Targeted webhook jobs are small (intake caps at 100 ids,
        # the connector caps its fetch again) and event-id-deduped upstream, so
        # stacking is bounded and correct; content-hash suppression makes an
        # overlapping run cheap. handle_intake never calls this with empty ids.
        # `webhook_record_ids` is the kind-generic changed-id list; the legacy
        # `webhook_article_ids` alias is kept for Zendesk so an in-flight/queued job
        # enqueued before this change still dispatches correctly. The job handler
        # routes by connection kind (Zendesk -> articles, Confluence -> pages,
        # Jira -> issues).
        #
        # PING-THEN-PULL (Airtable): the intake calls this with an EMPTY id list for
        # a bare ping — the job must re-fetch payloads from the stored cursor, not
        # fall through to a full sweep. A `webhook_ping_pull` marker flags that so
        # the handler routes to consume_airtable_payloads even with no ids.
        payload = {
            "connection_id": cid,
            "actor": "webhook",
            "webhook_record_ids": record_ids,
            "webhook_article_ids": record_ids,
        }
        if webhooks.is_ping_pull(kind):
            payload["webhook_ping_pull"] = True
            # F3 fix (review): a ping-pull payload NEVER carries per-event data —
            # record_ids is always [] here; the real content is fetched from the
            # STORED CURSOR at run time. So unlike the targeted-webhook path above
            # (where submit_unique could silently drop a specific event's ids),
            # coalescing ping-pull jobs loses nothing: N distinct-timestamp pings
            # before the worker gets to it would otherwise enqueue N cheap-but-
            # queue-pressuring pull jobs (each a real API call once drained, on
            # top of the 5rps/base budget) when ONE in-flight pull already drains
            # the whole cursor for every ping that arrived before it ran.
            # submit_unique's default dedupe_key ("connection_id") is exactly
            # right here: a pull already QUEUED/RUNNING for this connection
            # absorbs any further ping for the SAME connection.
            jid, _created = jobs.submit_unique("connector_translate", payload, actor="webhook")
            return jid
        return jobs.submit("connector_translate", payload, actor="webhook")

    result = webhooks.handle_intake(connection_id, body, headers, enqueue=_enqueue)
    return JSONResponse(result.as_dict(), status_code=result.status)


async def _api_sdl_new_project(request, ident, filename, data, raw_pid, raw_customer, name):
    """POST /api/documents with sdl_new_project=1: a new project from a Trados project
    package (the API twin of the web new-project form's package field and the CLI's
    `project create --from-sdl-package`; one service, create_project_from_package).
    The same customer rules as POST /api/projects: a customer is required, a bound key is
    confined to its own customer, an unknown or other-org customer is the shared 404."""
    from app.kernel import content
    from app.kernel import ingest as _ingest
    from app.models import Customer

    if str(raw_pid or "").strip():
        return JSONResponse(
            {"ok": False, "error": "Send project_id or sdl_new_project=1, not both."},
            status_code=400,
        )
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("sdlppx", "sdlrpx"):
        return JSONResponse(
            {"ok": False, "error": "sdl_new_project=1 needs a Trados project package (.sdlppx)."},
            status_code=400,
        )
    bound = deps._ident_customer_id(ident)
    org_id = _ident_org_id(ident)
    cid = None
    if str(raw_customer or "").strip():
        cid = ids.coerce_id(str(raw_customer).strip())
        if cid is None:
            return JSONResponse(_NOT_FOUND, status_code=404)
    with Session(engine) as s:
        if cid is not None and bound is None:
            row = s.get(Customer, cid)
            if row is None or (org_id is not None and getattr(row, "org_id", None) != org_id):
                return JSONResponse(_NOT_FOUND, status_code=404)
        try:
            project, doc = await deps.offload_parse(
                _ingest.create_sdl_package_project,
                s,
                data,
                filename,
                name=name,
                created_by=ident["actor"],
                customer_id=cid,
                creator=ident.get("user"),
                bound_customer_id=bound,
                org_id=org_id,
                default_provider=config.DEFAULT_PROVIDER,
                require_customer=True,
                reject_duplicate=True,
            )
        except deps.WorkerBusy:
            return deps.busy_response()
        except ids.InvalidNameError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except content.DuplicateNameError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "existing_id": exc.existing_id}, status_code=409
            )
        except content.UnknownCustomerError:
            return JSONResponse(_NOT_FOUND, status_code=404)
        except content.ForeignCustomerError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except (
            content.MissingCustomerError,
            content.InvalidLocaleError,
            content.ArchivedCustomerError,
        ) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except (UnsupportedEncodingError, EmptyDocumentError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except InputLimitError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=413)
        except Exception:
            _LOG.warning("Trados package project create failed for %r", filename, exc_info=True)
            return JSONResponse(
                {"ok": False, "error": "The file couldn't be read - it may be corrupted."},
                status_code=422,
            )
        result = {
            "ok": True,
            "project_id": project.id,
            "name": project.name,
            "document_id": doc.id,
            "format": doc.fmt,
            "source_locale": project.source_locale,
            "target_locales": [
                tl.locale
                for tl in s.exec(
                    select(TargetLocale).where(
                        TargetLocale.project_id == project.id, TargetLocale.stopped_at.is_(None)
                    )
                ).all()
            ],
            "sdl_package": _ingest.sdl_package_summary(s, doc),
        }
    audit.log(
        actor=ident["actor"],
        action="document.upload",
        target=f"doc {result['document_id']}",
        detail=f"sdl_new_project project {result['project_id']}",
        ip=deps._ip(request),
        customer_id=audit.customer_for_project(result["project_id"]),
    )
    return JSONResponse(result, status_code=200)


async def _api_upload_zip_batch(
    request, s, ident, project, filename, data, mode, ctx_pkg, routing_note
):
    """Batch 13: a `.zip` upload to /api/documents creates one document per supported
    member. 200 `{ok, batch_id, archive, project_id, documents: [{id, filename, path, fmt,
    segments}], skipped: [{path, reason}], failed: [{path, error}]}`; 422 when no member
    became a document (the same lists say why); 413 for an archive over a limit or with an
    unsafe path; 415 when the bytes are not a ZIP."""
    from app.kernel.ingest import ingest_zip_batch, zip_batch_summary

    try:
        result = await deps.offload_parse(
            ingest_zip_batch,
            s,
            project,
            filename,
            data,
            mode,
            created_by=ident["actor"],
            context=ctx_pkg,
        )
    except InputLimitError as exc:
        s.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=413)
    except deps.WorkerBusy:
        return deps.busy_response()
    except NotADocumentProject as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except UnsupportedFormat as e:
        return JSONResponse(
            {"ok": False, "error": f"Unsupported file format: {e}"}, status_code=415
        )
    body = {"ok": bool(result["documents"]), "project_id": project.id, **result}
    if routing_note:
        body["routing"] = routing_note
    if not result["documents"]:
        body["error"] = "No document was created. " + zip_batch_summary(result)
        return JSONResponse(body, status_code=422)
    audit.log(
        actor=ident["actor"],
        action="document.upload",
        target=f"zip batch {result['batch_id']} ({len(result['documents'])} docs)",
        ip=deps._ip(request),
        customer_id=audit.customer_for_project(project.id),
    )
    return JSONResponse(body, status_code=200)


@router.post("/api/documents")
async def api_upload_document(
    request: Request,
    file: UploadFile | None = File(None),
    project_id: str = Form(""),
    mode: str = Form("segmented"),
    context: str = Form(""),
    sdl_new_project: str = Form(""),
    customer_id: str = Form(""),
    name: str = Form(""),
    columns: str = Form(""),
):
    """Ingest an uploaded file (multipart). Requires the docs scope. The body is
    size-bounded (config.MAX_UPLOAD_BYTES) via deps.read_capped_upload — identically
    to the web UI's upload (app/web/documents.py), which caps its read the same way.
    Customer-bound callers must supply a project_id in their customer (422 if absent).
    For unbound callers, no project_id means routing rules or a per-identity inbox. Returns
    {ok, document_id, project_id, format, segments, translation_rows, target_locales},
    where `segments` counts distinct source segments, `translation_rows` is the segment
    rows written (segments x target locales) and `target_locales` is the project's target
    locale count.

    A file that would exceed `MAX_DOCUMENT_SEGMENTS` (default 50000 segments) or
    `MAX_DOCUMENT_ROWS` (default 500000 rows, segments x target languages), or holds a line
    or paragraph over `MAX_SEGMENT_CHARS` (default 100000 characters), is refused with 413
    and a plain message, whatever its format; nothing is stored. Ingest runs in a
    bounded worker thread (503 with Retry-After when workers are busy).

    `context` (SPEC-context-metadata phase 1) is an OPTIONAL JSON string form field
    carrying a TranslationContext package: validated + sanitized via context_pkg and
    stored on Document.context. Invalid context is a clean 422 with the validation
    message and NO document is created.

    `file` is declared optional so the docs-scope gate runs BEFORE body validation
    (a missing file must be an authenticated 400, never an anonymous 422).

    A Trados project package (.sdlppx) becomes one document per target SDLXLIFF the
    project translates into; the response lists them under `sdl_package`. With
    `sdl_new_project=1` (and no project_id) a NEW project is created from the package
    instead: its source and target languages come from the package's .sdlproj,
    `customer_id` is required (a customer-bound key's own customer is used), `name` is
    optional (default: the package's project name). A refused import leaves no project.
    The response then also carries `source_locale` and `target_locales`."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    blocked = _write_role_block(ident)
    if blocked:
        return blocked
    if file is None:
        return JSONResponse({"ok": False, "error": "No file uploaded."}, status_code=400)

    # Validate the optional context BEFORE reading the (potentially large) body so an
    # invalid package fails fast (422) and never ingests. An empty/absent field is
    # None (byte-identical to today).
    ctx_pkg = None
    if (context or "").strip():
        import json as _json

        from app.kernel import context_pkg

        # Adversarial review 2026-07-22, Findings A+B: the `context` Form field was
        # otherwise unbounded, and a deeply-nested payload ({"a":{"a":... or
        # [[[...]]]) blows json.loads's internal recursion handling BEFORE the
        # narrow except below could ever catch it -- an uncaught RecursionError
        # propagating as a raw 500, not the clean 422 this route promises. Reject
        # past the cap BEFORE parsing so the recursion never has a chance to
        # happen; a real TranslationContext renders to <= context_pkg.BLOCK_CAP
        # (600) chars even before sanitization, so this is generous headroom.
        if len(context) > config.CONTEXT_JSON_MAX_CHARS:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "context package too large (limit "
                    f"{config.CONTEXT_JSON_MAX_CHARS} chars).",
                },
                status_code=422,
            )
        try:
            raw = _json.loads(context)
        # RecursionError is widened in as defense-in-depth: the length cap above
        # already keeps json.loads from ever seeing a payload deep enough to
        # recurse that far for the common (object-nesting) shape, but a
        # char-cheaper shape (array nesting packs far more depth per char) can
        # still exceed the parser's internal recursion handling while staying
        # under the cap -- this except makes the clean-422 promise unconditional.
        except (ValueError, TypeError, RecursionError):
            return JSONResponse(
                {"ok": False, "error": "context must be valid JSON."}, status_code=422
            )
        try:
            ctx_pkg = context_pkg.validate_and_sanitize(raw)
        except context_pkg.ContextError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    # Bounded read: abort past the cap so a hostile/huge upload can never be
    # fully buffered into memory.
    cap = config.MAX_UPLOAD_BYTES
    data = await deps.read_capped_upload(file, cap)
    if data is None:
        return JSONResponse(
            {"ok": False, "error": f"File too large (limit {cap} bytes)."}, status_code=413
        )

    if (sdl_new_project or "").strip().lower() in ("1", "true", "yes", "on"):
        return await _api_sdl_new_project(
            request, ident, file.filename or "", data, project_id, customer_id, name
        )

    with Session(engine) as s:
        # The "did the caller NAME a project?" decision stays on the RAW string, NOT on
        # coerce_id -- otherwise the domain shifts: coerce_id rejects "0" / >2**31 (values
        # the old isdigit gate accepted and that took the not-found 404 arm), so they would
        # silently fall to the no-project routing arm and the document would be INGESTED
        # into a different project. coerce_id is used ONLY to make the value bind-safe: a
        # named-but-malformed id (U+00B2 / >2**63 / 0 / out-of-range) is a clean 404, never
        # a 500 and never a silent auto-route.
        raw_pid = str(project_id or "").strip()
        routing_note = None
        bound_customer = deps._ident_customer_id(ident)
        if bound_customer is not None and not raw_pid:
            return JSONResponse(
                {"ok": False, "error": "Choose a project in your customer before uploading."},
                status_code=422,
            )
        if raw_pid:
            pid = ids.coerce_id(raw_pid)
            if pid is None:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            project = s.get(Project, pid)
            if project is None:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            if bound_customer is not None and project.customer_id != bound_customer:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            # naming a project you can't see must not leak its existence
            if security.auth_required() and not access.can_view_project(
                s, ident.get("user"), project
            ):
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        else:
            # No caller project_id: consult content-routing rules by the file's ext
            # + the submitter's department BEFORE falling back to the department
            # inbox (Finding 1). A matching rule's target project now places the
            # document; with no matching rule, resolve() find-or-creates the
            # department inbox (ensure_inbox=True). A caller-passed project_id above
            # still wins outright.
            ext = (file.filename or "").rsplit(".", 1)[-1] if "." in (file.filename or "") else ""
            decision = routing.resolve(
                s,
                ext=ext,
                department=routing.department_for(ident),
                actor=ident["actor"],
                explicit_project_id=None,
                ensure_inbox=True,
                org_id=_ident_org_id(ident),
            )
            project = s.get(Project, decision.project_id) if decision.project_id else None
            routing_note = decision.reason or "department inbox"
            if project is None:  # defensive: resolve always yields an inbox here
                project = _ensure_inbox_project(s, ident)
                routing_note = "department inbox"

        # Connected projects take their content from the source, never from uploads (also when
        # a routing rule or the inbox picked the project).
        if connected_projects.is_connected(project):
            return JSONResponse(
                {"ok": False, "error": connected_projects.UPLOAD_REFUSED}, status_code=400
            )

        from app.filters.zip_batch import is_batch_upload

        if is_batch_upload(file.filename or ""):
            return await _api_upload_zip_batch(
                request,
                s,
                ident,
                project,
                file.filename or "upload.zip",
                data,
                mode,
                ctx_pkg,
                routing_note,
            )
        try:
            # #224/#271: parse in a worker thread, never on the event loop.
            doc = await deps.offload_parse(
                ingest_document,
                s,
                project,
                file.filename or "upload",
                data,
                mode,
                created_by=ident["actor"],
                context=ctx_pkg,
                columns=(columns or "").strip() or None,
            )
        except (UnsupportedEncodingError, XmlResourceError) as exc:
            # #509: not UTF-8. Say so (422), never the generic "corrupted" line. The same for
            # an XML resource file that is unsafe or of an unsupported kind (Batch 2).
            s.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except XmlFilterError as exc:
            # Batch 7: XML/DITA/DocBook that is not well-formed or not safe (DTD internal
            # subset, bad ITS rule). Say why (422), never the generic "corrupted" line.
            s.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except ColumnMappingError as exc:
            # Batch 6: the CSV/TSV/XLSX column mapping does not fit the file (422).
            s.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except EmptyDocumentError as exc:
            # #573: it parsed but has no text to translate (empty or whitespace-only).
            # Not an over-limit refusal, so answer 422 (not the InputLimitError 413) with
            # the seam's own message; nothing was created (the raise is pre-Document).
            s.rollback()
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except InputLimitError as exc:
            # #224/#272: over a safety limit (zip bomb, too many PDF pages): 413. A refusal
            # that is not about size names its own status (#687 FileTargetMismatch: 422).
            s.rollback()
            return JSONResponse(
                {"ok": False, "error": str(exc)}, status_code=getattr(exc, "http_status", 413)
            )
        except deps.WorkerBusy:
            return deps.busy_response()
        except NotADocumentProject as exc:
            # A-F4: a key project takes no documents on ANY surface.
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except UnsupportedFormat as e:
            return JSONResponse(
                {"ok": False, "error": f"Unsupported file format: {e}"}, status_code=415
            )
        except Exception:
            s.rollback()
            return JSONResponse(
                {"ok": False, "error": "The file couldn't be read — it may be corrupted."},
                status_code=422,
            )

        order_indexes = s.exec(
            select(Segment.order_index).where(Segment.document_id == doc.id)
        ).all()
        translation_rows = len(order_indexes)
        seg_count = len(set(order_indexes))
        result = {
            "ok": True,
            "document_id": doc.id,
            "project_id": project.id,
            "format": doc.fmt,
            # #581: `segments` is the number of DISTINCT source segments (distinct
            # order_index). One Segment row is written per (segment x target locale), so
            # the previous count (segments x locales) is now reported separately as
            # `translation_rows`, with `target_locales` = rows / segments.
            "segments": seg_count,
            "translation_rows": translation_rows,
            "target_locales": (translation_rows // seg_count) if seg_count else 0,
        }
        if (doc.skeleton or {}).get("file_warnings"):
            result["file_warnings"] = doc.skeleton["file_warnings"]
        if doc.fmt == "sdlrpx":
            # A Trados package: one document per target SDLXLIFF, plus the skipped files.
            from app.kernel.ingest import sdl_package_summary

            result["sdl_package"] = sdl_package_summary(s, doc)
        if routing_note:
            # #580: no project_id was sent, so routing rules or the department inbox chose
            # one. That fallback is by design (docs: routing); saying how keeps it from being
            # silent. Absent when the caller named the project.
            result["routing"] = routing_note

    # R2-3: stamp the owning customer (project-scoped) so a customer-bound API
    # caller's /audit view surfaces only its own uploads.
    audit.log(
        actor=ident["actor"],
        action="document.upload",
        target=f"doc {result['document_id']}",
        ip=deps._ip(request),
        customer_id=audit.customer_for_project(result["project_id"]),
    )
    return JSONResponse(result, status_code=200)


@router.post("/api/translate/text")
async def api_translate_text(request: Request):
    """The external-system FRONT DOOR (SPEC-context-metadata phase 1): translate one
    text string WITH a context package, DeepL-style.

    Body: {text, source_locale?, target_locale (required), provider?, context?, workflow_id?}.
    `workflow_id` (#356 phase 2b, only while the system-prompt overrides feature is on) names a
    workflow whose own machine-translation head override applies; an unknown or foreign
    workflow is a 404.
    Validates + RESOLVES the effective context (the request `context` layered over the
    caller's customer / optional content_type profiles via the API token's scoping),
    renders it via context_pkg.render_block, and passes the block through the EXISTING
    `context_brief` seam (translate.translate_text_once -> the plain-translate prompt
    builder) — no prompts.py / orchestrator.py change (phase 2).

    Auth is the same X-Loc-Token machinery + RBAC as the sibling connector-translate
    endpoints (a per-user key role-gated by _ai_role_block; the legacy shared
    companion-app token is refused with a 403 by that same block; org binding via
    _api_org_block). The
    SPEND CAP + refusal guard + mock-awareness ALL apply automatically via the
    existing translate/run_completion seams (a keyless install returns a MockProvider
    result exactly like every other translate path).

    Response: {ok, translation, provider_used, model_used, context_applied}. An
    invalid context is a clean 422 (nothing translated); a spend-block / refusal /
    provider error returns ok:false with the message."""
    ident = deps.api_identity(request)
    if ident is None:
        return JSONResponse(_UNAUTHORIZED, status_code=401)
    blocked = _ai_role_block(ident) or deps._api_org_block(request, ident)
    if blocked:
        return blocked
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # F6: reject a non-string `text` (a list/int/dict/bool) with a clean 400
    # instead of str()-coercing it (["a","b"] -> "['a', 'b']" and translating the
    # repr). None/absent stays "" and hits the "text is required" 400 below.
    raw_text = payload.get("text")
    if raw_text is not None and not isinstance(raw_text, str):
        return JSONResponse({"ok": False, "error": "text must be a string."}, status_code=400)
    text = raw_text or ""
    # Reject a non-string target_locale (a list / int / dict) with a 422 rather than
    # str()-coercing it into a junk locale that is then fired at a paid provider
    # (BUG-A11). None/absent stays "" and hits the "required" 400 below.
    raw_target = payload.get("target_locale")
    if raw_target is not None and not isinstance(raw_target, str):
        return JSONResponse(
            {"ok": False, "error": "target_locale must be a string."}, status_code=422
        )
    target_locale = str(raw_target or "").strip()
    from app.kernel import settings as _settings

    source_locale = (
        str(payload.get("source_locale") or "").strip()
        or _settings.get("default_source_locale")
        or "en-US"
    )
    provider = str(payload.get("provider") or "").strip()
    if not target_locale:
        return JSONResponse({"ok": False, "error": "target_locale is required."}, status_code=400)
    # Catalog-validate the target BEFORE any provider call (BUG-A11): a code not in the
    # language catalog is refused with a clean 400 (the same gate add-locale / the
    # document-translate seam apply), so a typo'd/hostile locale never reaches a paid
    # provider. is_valid accepts any catalog code (disabled ones included); only
    # genuinely-unknown codes reject.
    if not languages_catalog.is_valid(target_locale):
        return JSONResponse(
            {
                "ok": False,
                "error": "unknown locale code: "
                + target_locale
                + " (not in the language catalog).",
            },
            status_code=400,
        )
    if not text.strip():
        return JSONResponse({"ok": False, "error": "text is required."}, status_code=400)
    # Size cap (adversarial review MED-3): this is the only endpoint that takes
    # unbounded free text and fires it as ONE run_completion call (no chunking), so an
    # oversized `text` is a single-call token bomb the spend cap can't pre-gate (spend
    # only blocks on ALREADY-spent total). Reject over the cap BEFORE any provider call;
    # large documents go through /api/documents, which chunks. (Body buffering itself
    # happens in request.json() above and isn't pre-gated here — the token bomb is the
    # concern this guards.)
    if len(text) > config.TRANSLATE_TEXT_MAX_CHARS:
        return JSONResponse(
            {
                "ok": False,
                "error": "text exceeds the %d-character limit for this "  # noqa: UP031
                "endpoint; upload it as a document instead." % config.TRANSLATE_TEXT_MAX_CHARS,
            },
            status_code=413,
        )

    from app.kernel import context_pkg

    # Validate the request context package (422 on anything malformed — nothing runs).
    try:
        request_ctx = context_pkg.validate_and_sanitize(payload.get("context"))
    except context_pkg.ContextError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    # #617 item 7 (ADR 0062): an optional `formality` register for this call, validated for the
    # target language BEFORE any provider call (an unknown register is a 400, like every door).
    raw_fm = payload.get("formality")
    if raw_fm is not None and raw_fm != "":
        from app.kernel import formality as _formality

        if not isinstance(raw_fm, str):
            return JSONResponse(
                {"ok": False, "error": "formality must be a string."}, status_code=400
            )
        try:
            _formality.normalize_register(target_locale, raw_fm)
        except _formality.FormalityError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # #599: everything below blocks (DB reads, then the engine call with its network stream,
    # retry backoff sleeps and truncation escalation), so it runs on a bounded worker, never
    # on the event loop. Inline, one slow call froze /health and every other request.
    try:
        return await deps.offload_blocking(
            _translate_text_blocking,
            ident,
            payload,
            text,
            source_locale,
            target_locale,
            provider,
            request_ctx,
        )
    except deps.WorkerBusy as exc:
        return deps.busy_response(str(exc))


def _translate_text_blocking(
    ident, payload, text, source_locale, target_locale, provider, request_ctx
) -> JSONResponse:
    """The blocking half of POST /api/translate/text (#599), run by deps.offload_blocking."""
    from app.kernel import context_pkg, system_prompts, translate

    # #599: one user-perceived character carrying thousands of combining marks / joiners is
    # refused before any engine call (it reads as short text but tokenizes to a huge output
    # that walks the truncation-escalation ladder for minutes). Checked here, off the loop.
    stacked = translate.stacked_cluster_error(text)
    if stacked:
        return JSONResponse({"ok": False, "error": stacked}, status_code=422)

    # Resolve the effective package from the request + the caller's scoped profiles
    # (org + customer via the token, optional content_type from the body). A foreign
    # org's / customer's card is never applied (org_id + customer_id both scope the
    # profile reads).
    content_type_id = context_pkg._coerce_id(payload.get("content_type_id"))
    _cust_id = deps._ident_customer_id(ident)
    if _cust_id is not None:
        # AI opt-out (DPAI-08): a key bound to an opted-out CUSTOMER is refused (409), the
        # same answer every other AI surface gives; its context card never reaches a prompt.
        from app.kernel import ai_optout
        from app.models import Customer

        with Session(engine) as s:
            _cust = s.get(Customer, _cust_id)
            if _cust is not None and bool(getattr(_cust, "ai_opt_out", False)):
                raise ai_optout.AIOptOutRefused(
                    "machine translation", project_name=_cust.name, via="customer"
                )
    with Session(engine) as s:
        effective, _prov = context_pkg.resolve(
            s,
            customer_id=deps._ident_customer_id(ident),
            content_type_id=content_type_id,
            request_ctx=request_ctx,
            org_id=_ident_org_id(ident),
        )
    block = context_pkg.render_block(effective)
    # #617 item 7: the register for this call: the request's own `formality`, else the key's
    # customer default for the target language. It rides the same fenced style-guide block the
    # document paths use; nothing resolved or `auto` adds not a byte.
    import dataclasses

    from app.kernel import formality as _formality
    from app.kernel import style_guides as _style_guides

    _req_fm = payload.get("formality")
    _fm = None
    if isinstance(_req_fm, str) and _req_fm.strip():
        _fm = _formality.resolve_maps(document={target_locale: _req_fm}, locale=target_locale)
        if _fm is not None:
            _fm = dataclasses.replace(_fm, source="request")
    elif _cust_id is not None:
        with Session(engine) as s:
            _fm = _formality.resolve_for(s, customer_id=_cust_id, target_locale=target_locale)
    _fm_block = _style_guides.render_style_guide(None, _fm)
    _context_given = bool(block)  # `context_applied` reports the context card, not formality
    block = "\n\n".join(x for x in (block, _fm_block) if x)
    context_brief = block or None  # never pass an empty block (golden byte-safety)

    spec = translate.spec_for_provider(provider) if provider else None
    # GitHub #356 phases 2a/2b: the MT head override in force for this call: the named
    # workflow's own (when the body carries `workflow_id` and the caller may see that
    # workflow), else the caller's own org's, else None (the built-in text). With the feature
    # flag off the field is ignored and nothing is read.
    workflow_id = None
    if system_prompts.overrides_enabled() and payload.get("workflow_id") is not None:
        raw_wf = payload.get("workflow_id")
        if (
            isinstance(raw_wf, bool)
            or not isinstance(raw_wf, int)
            or not 0 < raw_wf <= 2**63 - 1  # gate fix: an out-of-range id was a 500
        ):
            return JSONResponse(
                {"ok": False, "error": "workflow_id must be a whole number."}, status_code=400
            )
        from app.web.api_cli_common import _wf_in_org

        # Gate fix: this door resolves its identity with deps.api_identity, not _gate, so the
        # customer ring field `_wf_in_org` reads (`_cust_id`) was never set and a
        # customer-bound key could name ANOTHER customer's workflow and have its text applied.
        # Set it the way _gate does. An archived workflow is not usable either (a plain-MT pass
        # ignores an archived default the same way).
        ring_ident = {**ident, "_cust_id": deps._ident_customer_id(ident)}
        with Session(engine) as s:
            wf_row = _wf_in_org(s, raw_wf, _ident_org_id(ident), ring_ident)
            if wf_row is None or wf_row.archived_at is not None:
                return JSONResponse(_NOT_FOUND, status_code=404)
        workflow_id = raw_wf
    with Session(engine) as s:
        head_override = system_prompts.mt_head_body(s, _ident_org_id(ident), workflow_id)
    try:
        res = translate.translate_text_once(
            text,
            source_locale,
            target_locale,
            spec=spec,
            context_brief=context_brief,
            head_override=head_override,
        )
    except providers.DeadEngineError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if res.get("error"):
        return JSONResponse(
            {"ok": False, "error": res["error"], "context_applied": _context_given},
            status_code=200,
        )
    out = {
        "ok": True,
        "translation": res["text"],
        "provider_used": res.get("provider_used"),
        "model_used": res.get("model_used"),
        "context_applied": _context_given,
    }
    if _fm is not None:
        out["formality_applied"] = _fm.record()  # #617 item 7: only present when one resolved
    return JSONResponse(out)


@router.post("/api/documents/{doc_id}/paragraphs/{index}/revert")
def api_document_paragraph_revert(doc_id: int, index: int, request: Request, locale: str = ""):
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    blocked = _write_role_block(ident)  # can_write parity (web middleware twin)
    if blocked:
        return blocked
    from app.kernel import fulltext_paragraphs as ftp

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        res = ftp.revert_paragraph(
            s, doc, (locale or "").strip(), index, actor=ident["actor"], ip=deps._ip(request)
        )
    return JSONResponse(res, status_code=deps.paragraph_status(res))


@router.post("/api/documents/{doc_id}/paragraphs/revert-all-damaged")
def api_document_paragraph_revert_all(doc_id: int, request: Request, locale: str = ""):
    """Atomic batch revert (GitHub #42) — the /api twin of `doc revert-paragraph
    --all-damaged`. Serves the remote CLI arm so `--all-damaged` is atomic on a ceiling
    refusal over HTTP too (the branch's F-3 bug was a per-index loop that half-wrote)."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    blocked = _write_role_block(ident)  # can_write parity (web middleware twin)
    if blocked:
        return blocked
    from app.kernel import fulltext_paragraphs as ftp

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        res = ftp.revert_all_damaged(
            s, doc, (locale or "").strip(), actor=ident["actor"], ip=deps._ip(request)
        )
    return JSONResponse(res, status_code=deps.paragraph_status(res))


@router.post("/api/documents/{doc_id}/paragraphs/{index}/save")
def api_document_paragraph_save(
    doc_id: int, index: int, request: Request, locale: str = "", payload: dict | None = None
):
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    blocked = _write_role_block(ident)  # can_write parity (web middleware twin)
    if blocked:
        return blocked
    body = payload or {}
    from app.kernel import fulltext_paragraphs as ftp

    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        res = ftp.save_paragraph(
            s,
            doc,
            (locale or "").strip(),
            index,
            body.get("text") or "",
            actor=ident["actor"],
            ip=deps._ip(request),
            confirm=bool(body.get("confirm")),
        )
    return JSONResponse(res, status_code=deps.paragraph_status(res))


# --- TM-only delivery empty gate: document mode (Batch 15 T2, §9.3/§9.6) ----
# These three are CORE /api routes (under /api/documents, F.CORE_API). MCP-EXEMPT: the
# approve/revoke are human delivery authorizations (an agent relays, never approves --
# category human-decision); the empties GET is the human's pre-approval review surface
# (category human-repair-surface). No new MCP tool ships (S4).
@router.get("/api/documents/{doc_id}/empties")
def api_document_empties(doc_id: int, request: Request, locale: str = ""):
    """List the untranslated (empty) targets for a held (doc, locale) -- the ACTUAL empty
    strings a reviewer approves before delivery (owner's flag). Read: docs scope + authz."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    locale = (locale or "").strip()
    if not locale:
        # A-LOW-4: `locale` is required (parity with the download route's contract).
        return JSONResponse({"ok": False, "error": "locale is required"}, status_code=400)
    with Session(engine) as s:
        doc = s.get(Document, doc_id)
        _project, denied = _doc_authz(s, ident, doc)
        if denied:
            return denied
        if locale not in tm_delivery._doc_target_locales(s, doc_id):
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        decision = tm_delivery.empty_gate_decision(s, doc=doc, locale=locale)
        empties = tm_delivery.doc_empties(s, doc, locale)
        row = tm_delivery._doc_approval_row(s, doc_id, locale)
        return JSONResponse(
            {
                "ok": True,
                "locale": locale,
                "decision": decision,
                "empty_count": len(empties),
                "empties": empties,
                "approved": bool(row is not None and row.status == "approved"),
            }
        )


@router.get("/api/languages")
def api_languages(request: Request):
    """Saved Language Sets + a flat de-duped list of every locale they mention.
    Requires a valid user identity (docs scope)."""
    ident, blocked = deps._require_doc_scope(request)
    if blocked:
        return blocked
    # Org-scoped language sets (leak vector 2): a docs-scoped key sees only its org.
    sets = languages.list_sets(org_id=_ident_org_id(ident))
    flat: list[str] = []
    seen = set()
    payload_sets = []
    for ls in sets:
        locs = ls.target_locales or []
        payload_sets.append(
            {
                "id": ls.id,
                "name": ls.name,
                "source_locale": ls.source_locale,
                "target_locales": locs,
            }
        )
        for loc in [ls.source_locale, *locs]:
            if loc and loc not in seen:
                seen.add(loc)
                flat.append(loc)
    # a11y T5 (additive): each listed locale's writing direction, "rtl" / "ltr", or "" for
    # a code that is not a well-formed language tag, so a client can set `dir` on
    # translated text it displays (the same helper the web pages use).
    directions = {loc: languages_catalog.text_direction(loc) for loc in flat}
    return JSONResponse(
        {"ok": True, "language_sets": payload_sets, "locales": flat, "directions": directions}
    )


@router.get("/api/capabilities/{slug}")
def api_capability_card(slug: str, request: Request):
    """One capability card — the install's OWN self-description, as JSON.

    docs/specs/mcp-server.md section 10 / decision 7, and the ONLY app/ touch of the MCP
    v1 build. `/help/{module}` renders the same card as HTML for a human; an agent
    (the MCP server's `locstudio://guide/llm` resource, or any other HTTP caller)
    needs it as data, from THE INSTALL IT IS DRIVING. Bundling a copy of the .md
    next to a remote MCP process would describe the developer's checkout while the
    tools call prod — the exact drift class the MCP guard exists to prevent.

    BASE `api` SCOPE, deliberately: a card contains no tenant data, only repo-tracked
    documentation, so it needs identity (never anonymous) but not the `docs` grant the
    document routes require — an agent must be able to read the driver's manual before
    it holds the scopes to act. Thin over `assist.load_card`, which is itself
    traversal-safe (it accepts `[a-z0-9_-]+` only); unknown and disabled slugs share
    one 404 body. The enabled card index remains at `/help`."""
    from app import features
    from app.kernel import assist

    ident = deps.api_identity(request)
    if ident is None:
        return JSONResponse(_UNAUTHORIZED, status_code=401)
    if ident.get("kind") != "user":
        return JSONResponse(_FORBIDDEN, status_code=403)
    blocked = deps._api_org_block(request, ident)
    if blocked:
        return blocked
    # Disabled cards use the middleware refusal, without enumeration hints (#113).
    if features.card_is_off(slug):
        return JSONResponse({"ok": False, "error": features.REMOVED_REFUSAL}, status_code=404)
    card = assist.load_card(slug)
    if card is None:
        return JSONResponse({"ok": False, "error": features.REMOVED_REFUSAL}, status_code=404)
    meta = {k: v for k, v in card.items() if k != "body"}
    # Drop `connects` edges to a neighbour whose card does not load (disabled OR
    # nonexistent), so a disabled feature is not DETECTABLE through another card's edge
    # list -- the same non-enumerable posture the /help neighbour list keeps.
    if isinstance(meta.get("connects"), list):
        meta = {
            **meta,
            "connects": [
                e
                for e in meta["connects"]
                if isinstance(e, dict) and assist.load_card(e.get("module")) is not None
            ],
        }
    return JSONResponse(
        {
            "ok": True,
            "slug": meta.get("module") or slug,
            "title": card.get("title") or "",
            "card": meta,
            "body": card.get("body") or "",
            "text": assist.card_text(card),
        }
    )


def _reject_connector_secret(body: dict):
    """A ready-to-return 400 when a non-secret connector body carries any credential
    field ANYWHERE (top level or nested under `config`), else None. R2 refuses secrets
    over the plain connector routes: they ride the R3 admin surface with write-only
    credential handling -- and a refusal that only scanned the top level would silently
    STORE a config-nested credential in cleartext, breaking the refusal contract."""
    present = _find_secret_fields(body or {})
    if present:
        return JSONResponse(
            {
                "ok": False,
                "error": "secret/credential fields ({}) are not accepted "
                "here (including nested under config); connector secrets ride the admin "
                "surface (Wave R3). Create the connection non-secret, then set "
                "credentials there.".format(", ".join(present)),
            },
            status_code=400,
        )
    return None


def _conn_role_block(ident: dict, predicate) -> "JSONResponse | None":
    """Require a user identity; check the requested role when the login wall is on.

    Shared companion credentials never inherit administrative or AI authority.
    """
    if ident.get("kind") != "user":
        return JSONResponse(_FORBIDDEN, status_code=403)
    if not security.auth_required():
        return None
    if predicate(ident.get("user")):
        return None
    return JSONResponse(_FORBIDDEN, status_code=403)
